use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, SecondsFormat};
use rust_decimal::Decimal;
use serde_json::{Value as Json, json};
use sqlx::AssertSqlSafe;

use super::{
    AdminRequest, ApiError, Errors, Field, Model, Values, Widget, error, primitive, raw,
    related_model,
};

fn related_table(field: &Field) -> Option<&'static str> {
    if let Some(target) = related_model(field) {
        return Some(target.table.as_str());
    }
    match field.related.as_deref()? {
        "auth.permission" => Some("auth_permission"),
        "contenttypes.contenttype" => Some("django_content_type"),
        _ => None,
    }
}

fn related_scope(model: &Model, field: &Field) -> &'static str {
    match (
        field.related.as_deref(),
        model.slug(),
        field.through.is_some(),
    ) {
        (Some("core.debater"), "teamresult", true) => "",
        (Some("core.debater"), _, _) => " AND NOT (temporary OR synthetic)",
        (Some("core.school"), _, _) => " AND NOT temporary",
        _ => "",
    }
}

fn datetime(values: &Values, name: &str) -> Option<Json> {
    let date = raw(values, &format!("{name}_0"));
    let time = raw(values, &format!("{name}_1"));
    if !date.is_empty() || !time.is_empty() {
        let date = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
        let time = NaiveTime::parse_from_str(time, "%H:%M:%S")
            .or_else(|_| NaiveTime::parse_from_str(time, "%H:%M"))
            .ok()?;
        return Some(json!(
            date.and_time(time)
                .and_utc()
                .to_rfc3339_opts(SecondsFormat::Secs, true)
        ));
    }
    let value = raw(values, name);
    if value.is_empty() {
        return Some(Json::Null);
    }
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|datetime| json!(datetime.to_rfc3339()))
        .or_else(|| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S")
                .ok()
                .map(|datetime| json!(datetime.and_utc().to_rfc3339()))
        })
}

fn decimal_value(raw: &str, field: &Field) -> Result<Decimal, String> {
    let raw = raw.trim();
    let number = Decimal::from_str_exact(raw)
        .or_else(|_| Decimal::from_scientific(raw))
        .map_err(|_| "Enter a number.".to_owned())?;
    let scale = number.scale() as usize;
    let digits = scale.max(
        number
            .mantissa()
            .unsigned_abs()
            .checked_ilog10()
            .map_or(1, |leading| leading as usize + 1),
    );
    if let Some(max) = field.precision.filter(|max| digits > *max) {
        return Err(format!(
            "Ensure that there are no more than {max} digits in total."
        ));
    }
    if let Some(places) = field.scale {
        if scale > places {
            return Err(format!(
                "Ensure that there are no more than {places} decimal places."
            ));
        }
        if let Some(max) = field.precision.and_then(|max| max.checked_sub(places))
            && digits - scale > max
        {
            return Err(format!(
                "Ensure that there are no more than {max} digits before the decimal point."
            ));
        }
    }
    Ok(number)
}

pub(super) async fn cleaned(
    admin: &AdminRequest<'_>,
    model: &Model,
    field: &Field,
    widget: &Widget,
    name: &str,
    values: &Values,
    errors: &mut Errors,
) -> Result<Option<Json>, ApiError> {
    let raw = raw(values, name);
    let date_value = if field.kind == "DateTimeField" {
        datetime(values, name)
    } else {
        None
    };
    if widget.required
        && raw.trim().is_empty()
        && date_value.as_ref().is_none_or(Json::is_null)
        && field.kind != "BooleanField"
    {
        error(errors, name, "This field is required.");
        return Ok(None);
    }
    if matches!(field.kind.as_str(), "ForeignKey" | "OneToOneField") && !raw.trim().is_empty() {
        if let Ok(id) = raw.trim().parse::<i64>() {
            let table = related_table(field).ok_or(ApiError::Serialization)?;
            let scope = related_scope(model, field);
            let sql = format!("SELECT CAST(id AS BIGINT) FROM {table} WHERE id=$1{scope}");
            if sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
                .bind(id)
                .fetch_optional(&admin.state.pool)
                .await?
                .is_some()
            {
                return Ok(Some(json!(id)));
            }
        }
        error(
            errors,
            name,
            "Select a valid choice. That choice is not one of the available choices.",
        );
        return Ok(None);
    }
    if field.size.is_some_and(|max| raw.chars().count() > max) {
        error(
            errors,
            name,
            &format!(
                "Ensure this value has at most {} characters (it has {}).",
                field.size.unwrap_or_default(),
                raw.chars().count()
            ),
        );
    }
    if !raw.is_empty()
        && ((!widget.choices.is_empty() && !widget.choices.iter().any(|(key, _)| key == raw))
            || (!field.choices.is_empty()
                && !field.choices.iter().any(|(key, _)| primitive(key) == raw)))
    {
        error(
            errors,
            name,
            &format!("Select a valid choice. {raw} is not one of the available choices."),
        );
    }
    let value = match field.kind.as_str() {
        "BooleanField" => Some(json!(values.contains_key(name))),
        "IntegerField"
        | "SmallIntegerField"
        | "BigIntegerField"
        | "PositiveIntegerField"
        | "PositiveSmallIntegerField"
        | "PositiveBigIntegerField"
            if !raw.is_empty() =>
        {
            raw.parse::<i64>()
                .ok()
                .filter(|value| !field.kind.starts_with("Positive") || *value >= 0)
                .map(|value| json!(value))
        }
        "DecimalField" if !raw.trim().is_empty() => match decimal_value(raw, field) {
            Ok(decimal) => Some(json!(decimal.to_string())),
            Err(message) => {
                error(errors, name, &message);
                return Ok(None);
            }
        },
        "FloatField" if !raw.is_empty() => raw
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .map(|_| json!(raw)),
        "DateTimeField" => date_value,
        "DateField" if !raw.is_empty() => NaiveDate::parse_from_str(raw, "%Y-%m-%d")
            .ok()
            .map(|date| json!(date.to_string())),
        "JSONField" if !raw.is_empty() => serde_json::from_str(raw).ok(),
        _ if raw.is_empty() && field.null => Some(Json::Null),
        _ => Some(json!(if field.kind == "TextField" {
            raw
        } else {
            raw.trim()
        })),
    };
    if value.is_none() {
        error(
            errors,
            name,
            match field.kind.as_str() {
                "DateTimeField" | "DateField" => "Enter a valid date.",
                "JSONField" => "Enter a valid JSON.",
                "FloatField" | "DecimalField" => "Enter a number.",
                _ => "Enter a whole number.",
            },
        );
    }
    Ok(value)
}

pub(super) type RelatedSelections = BTreeMap<String, BTreeSet<i64>>;

pub(super) async fn clean_memberships(
    admin: &AdminRequest<'_>,
    model: &Model,
    widgets: &BTreeMap<String, Widget>,
    values: &Values,
    errors: &mut Errors,
) -> Result<RelatedSelections, ApiError> {
    let mut selections = RelatedSelections::new();
    for (name, widget) in widgets {
        let Some(field) = model.fields.get(name).filter(|field| {
            field.through.is_some()
                && field.kind != "Tags"
                && !field.generic_through.unwrap_or_default()
        }) else {
            continue;
        };
        let selected = if widget.kind == "ManyToManyRawIdWidget" {
            raw(values, name)
                .split(',')
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>()
        } else {
            values
                .get(name)
                .into_iter()
                .flatten()
                .map(String::as_str)
                .collect()
        };
        if selected.is_empty() && widget.required {
            error(errors, name, "This field is required.");
            continue;
        }
        let mut ids = BTreeSet::new();
        for input in selected {
            match input.trim().parse::<i64>() {
                Ok(id) if input == id.to_string() => {
                    ids.insert(id);
                }
                Ok(_) => error(
                    errors,
                    name,
                    &format!("Select a valid choice. {input} is not one of the available choices."),
                ),
                Err(_) => error(errors, name, &format!("“{input}” is not a valid value.")),
            }
            if errors.contains_key(name) {
                break;
            }
        }
        if errors.contains_key(name) {
            continue;
        }
        if !ids.is_empty() {
            let table = related_table(field).ok_or(ApiError::Serialization)?;
            let filter = related_scope(model, field);
            let list = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
            let sql =
                format!("SELECT CAST(id AS BIGINT) FROM {table} WHERE id IN ({list}){filter}");
            let found = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
                .fetch_all(&admin.state.pool)
                .await?
                .into_iter()
                .collect::<BTreeSet<_>>();
            if let Some(missing) = ids.difference(&found).next() {
                error(
                    errors,
                    name,
                    &format!(
                        "Select a valid choice. {missing} is not one of the available choices."
                    ),
                );
                continue;
            }
        }
        selections.insert(name.clone(), ids);
    }
    Ok(selections)
}
