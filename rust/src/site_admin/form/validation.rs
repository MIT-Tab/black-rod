use std::{
    borrow::Cow,
    collections::{BTreeMap, BTreeSet},
    net::Ipv4Addr,
    sync::LazyLock,
};

use chrono::{DateTime, NaiveDateTime, NaiveTime, SecondsFormat};
use serde_json::{Value as Json, json};
use sqlx::{AnyConnection, AssertSqlSafe};

use super::{
    AdminRequest, ApiError, Errors, Field, Model, Values, Widget, checkbox_posted, error,
    primitive, raw, related_model,
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

pub(in crate::site_admin) async fn related_exists(
    conn: &mut AnyConnection,
    model: &Model,
    field: &Field,
    id: i64,
) -> Result<bool, ApiError> {
    let table = related_table(field).ok_or(ApiError::Serialization)?;
    let scope = related_scope(model, field);
    let sql = format!("SELECT CAST(id AS BIGINT) FROM {table} WHERE id=$1{scope}");
    Ok(sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(conn)
        .await?
        .is_some())
}

fn datetime(values: &Values, name: &str) -> Option<Json> {
    let date = raw(values, &format!("{name}_0"));
    let time = raw(values, &format!("{name}_1"));
    if !date.is_empty() || !time.is_empty() {
        let date = crate::django_date::form(date)?;
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

fn integer_range(kind: &str, sqlite: bool) -> (i64, i64) {
    let (minimum, maximum) = match (kind, sqlite) {
        ("SmallIntegerField" | "PositiveSmallIntegerField", false) => {
            (i64::from(i16::MIN), i64::from(i16::MAX))
        }
        ("IntegerField" | "PositiveIntegerField", false) => {
            (i64::from(i32::MIN), i64::from(i32::MAX))
        }
        _ => (i64::MIN, i64::MAX),
    };
    (
        if kind.starts_with("Positive") {
            0
        } else {
            minimum
        },
        maximum,
    )
}

fn integer_value(raw: &str) -> Option<i64> {
    let raw = raw.trim();
    let value = raw
        .rsplit_once('.')
        .filter(|(_, fraction)| fraction.bytes().all(|digit| digit == b'0'))
        .map_or(raw, |(whole, _)| whole);
    value.parse().ok()
}

fn normalize_url<'a>(value: &'a str, scheme: &str) -> Cow<'a, str> {
    let has_scheme = value.split_once(':').is_some_and(|(scheme, _)| {
        !scheme.contains('/') && scheme.starts_with(|first: char| first.is_ascii_alphabetic())
    });
    if value.is_empty() || has_scheme {
        Cow::Borrowed(value)
    } else if value.starts_with("//") {
        Cow::Owned(format!("{scheme}:{value}"))
    } else {
        Cow::Owned(format!("{scheme}://{value}"))
    }
}

fn valid_url(value: &str) -> bool {
    static AUTHORITY: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(
            r"(?i)^(?:[^\s:@/]+(?::[^\s:@/]*)?@)?(?:\[[0-9a-f:.]+\]|[a-z0-9.\-\u{a1}-\u{ffff}]+)(?::[0-9]{1,5})?$",
        )
        .expect("valid URL authority pattern")
    });
    if value.chars().count() > 2048 || value.chars().any(char::is_whitespace) {
        return false;
    }
    let Some((_, address)) = value.split_once("://") else {
        return false;
    };
    let authority = address.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.contains('\\') || !AUTHORITY.is_match(authority) {
        return false;
    }
    let Ok(parsed) = url::Url::parse(value) else {
        return false;
    };
    if !matches!(parsed.scheme(), "http" | "https" | "ftp" | "ftps") {
        return false;
    }
    match parsed.host() {
        Some(url::Host::Ipv4(_)) => {
            let host = authority.rsplit('@').next().unwrap_or_default();
            host.split(':')
                .next()
                .unwrap_or_default()
                .parse::<Ipv4Addr>()
                .is_ok()
        }
        Some(url::Host::Ipv6(_)) => true,
        Some(url::Host::Domain(name)) => {
            let name = name.strip_suffix('.').unwrap_or(name);
            if name == "localhost" {
                return true;
            }
            let Some((_, suffix)) = name.rsplit_once('.') else {
                return false;
            };
            (2..=253).contains(&name.len())
                && suffix.len() >= 2
                && (suffix.starts_with("xn--")
                    || suffix
                        .bytes()
                        .all(|ch| ch.is_ascii_alphabetic() || ch == b'-'))
                && name.split('.').all(|label| {
                    !label.is_empty()
                        && label.len() <= 63
                        && !label.starts_with('-')
                        && !label.ends_with('-')
                        && label
                            .bytes()
                            .all(|ch| ch.is_ascii_alphanumeric() || ch == b'-')
                })
        }
        None => false,
    }
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
    let submitted = raw(values, name);
    let kind = widget.cleaning.as_deref().unwrap_or(&field.kind);
    let normalized = match kind {
        "URLField" => normalize_url(
            submitted.trim(),
            widget.assumed_url_scheme.as_deref().unwrap_or("http"),
        ),
        "EmailField" => Cow::Borrowed(submitted.trim()),
        _ => Cow::Borrowed(submitted),
    };
    let raw = normalized.as_ref();
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
        if let Ok(id) = raw.trim().parse::<i64>()
            && related_exists(&mut *admin.state.pool.acquire().await?, model, field, id).await?
        {
            return Ok(Some(json!(id)));
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
    let choices = widget.choices(admin.state.current_season);
    if !raw.is_empty()
        && ((!choices.is_empty() && !choices.iter().any(|(key, _)| key == raw))
            || (!field.choices.is_empty()
                && !field.choices.iter().any(|(key, _)| primitive(key) == raw)))
    {
        error(
            errors,
            name,
            &format!("Select a valid choice. {raw} is not one of the available choices."),
        );
    }
    let value = match kind {
        "BooleanField" => Some(json!(checkbox_posted(values, name))),
        "IntegerField"
        | "SmallIntegerField"
        | "BigIntegerField"
        | "PositiveIntegerField"
        | "PositiveSmallIntegerField"
        | "PositiveBigIntegerField"
            if !raw.is_empty() =>
        {
            if let Some(value) = integer_value(raw) {
                let sqlite = admin.state.pool.connect_options().database_url.scheme() == "sqlite";
                let (minimum, maximum) = integer_range(kind, sqlite);
                let bounds = if value < minimum {
                    Some(("greater", minimum))
                } else if value > maximum {
                    Some(("less", maximum))
                } else {
                    None
                };
                if let Some((direction, bound)) = bounds {
                    error(
                        errors,
                        name,
                        &format!("Ensure this value is {direction} than or equal to {bound}."),
                    );
                    return Ok(None);
                }
                Some(json!(value))
            } else {
                None
            }
        }
        "DecimalField" if !raw.trim().is_empty() => {
            match crate::decimal::validate(raw, field.precision, field.scale) {
                Ok(decimal) => Some(json!(decimal.to_string())),
                Err(message) => {
                    error(errors, name, &message);
                    return Ok(None);
                }
            }
        }
        "FloatField" if !raw.is_empty() => raw
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .map(|_| json!(raw)),
        "DateTimeField" => date_value,
        "DateField" if !raw.is_empty() => {
            crate::django_date::form(raw).map(|date| json!(date.to_string()))
        }
        "JSONField" if !raw.is_empty() => serde_json::from_str(raw).ok(),
        "EmailField" if !raw.is_empty() => {
            crate::account::contact_email_valid(raw).then(|| json!(raw))
        }
        "URLField" if !raw.is_empty() => valid_url(raw).then(|| json!(raw)),
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
            match kind {
                "DateTimeField" | "DateField" => "Enter a valid date.",
                "JSONField" => "Enter a valid JSON.",
                "EmailField" => "Enter a valid email address.",
                "URLField" => "Enter a valid URL.",
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
