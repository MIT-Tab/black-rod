use chrono::{DateTime, NaiveDate, NaiveDateTime};

use super::{Field, Model, Parameters, Source, add_values, search_condition};

pub(in crate::site_admin) struct Filter<'a> {
    pub(in crate::site_admin) source: Source<'a>,
    pub(in crate::site_admin) condition: String,
    pub(in crate::site_admin) values: Vec<String>,
}

fn boolean(value: &str) -> Option<bool> {
    match value {
        "1" | "t" | "True" | "true" => Some(true),
        "0" | "f" | "False" | "false" => Some(false),
        _ => None,
    }
}

fn lookup(parameter: &str) -> (&str, &str) {
    parameter
        .rsplit_once("__")
        .filter(|(_, suffix)| {
            matches!(
                *suffix,
                "exact"
                    | "iexact"
                    | "isnull"
                    | "in"
                    | "range"
                    | "contains"
                    | "icontains"
                    | "startswith"
                    | "istartswith"
                    | "endswith"
                    | "iendswith"
                    | "gt"
                    | "gte"
                    | "lt"
                    | "lte"
                    | "year"
                    | "month"
                    | "day"
            )
        })
        .unwrap_or((parameter, "exact"))
}

fn allowed_path<'a>(model: &Model, path: &'a str) -> Option<&'a str> {
    let local = |name: &str| {
        model.fields.get(name).or_else(|| {
            name.strip_suffix("_id").and_then(|related| {
                model
                    .fields
                    .get(related)
                    .filter(|field| field.related.is_some())
            })
        })
    };
    if let Some((relation, "id" | "pk")) = path.split_once("__")
        && local(relation).is_some_and(|field| field.related.is_some())
    {
        return Some(relation);
    }
    if path == "pk" || local(path).is_some() || model.filter.iter().any(|filter| path == filter) {
        return Some(path);
    }
    None
}

fn scalar(field: &Field, value: &str, sqlite: bool, values: &mut Vec<String>) -> Option<String> {
    let marker = |values: &mut Vec<String>| add_values(values, value);
    if field.kind == "BooleanField" {
        return Some(if boolean(value)? { "TRUE" } else { "FALSE" }.to_owned());
    }
    if field.primary
        || field.through.is_some()
        || field.related.is_some()
        || field.kind.ends_with("IntegerField")
    {
        value.trim().parse::<i64>().ok()?;
        return Some(format!("CAST({} AS BIGINT)", marker(values)));
    }
    match field.kind.as_str() {
        "DecimalField" | "FloatField" => {
            value
                .trim()
                .parse::<f64>()
                .ok()
                .filter(|number| number.is_finite())?;
            Some(format!("CAST({} AS NUMERIC)", marker(values)))
        }
        "DateField" => {
            let date = NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()?;
            Some(add_values(values, &date.to_string()))
        }
        "DateTimeField" => {
            let time = DateTime::parse_from_rfc3339(value)
                .ok()
                .map(|time| time.naive_utc())
                .or_else(|| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f").ok())
                .or_else(|| {
                    NaiveDate::parse_from_str(value, "%Y-%m-%d")
                        .ok()?
                        .and_hms_opt(0, 0, 0)
                })?;
            if sqlite {
                Some(add_values(
                    values,
                    &time.format("%Y-%m-%d %H:%M:%S%.6f").to_string(),
                ))
            } else {
                let marker = add_values(values, &time.and_utc().to_rfc3339());
                Some(format!("CAST({marker} AS TIMESTAMPTZ)"))
            }
        }
        "UUIDField" => {
            let id = uuid::Uuid::parse_str(value).ok()?;
            if sqlite {
                Some(add_values(values, &id.simple().to_string()))
            } else {
                let marker = add_values(values, &id.to_string());
                Some(format!("CAST({marker} AS UUID)"))
            }
        }
        "JSONField" => None,
        _ => Some(marker(values)),
    }
}

fn predicate(
    column: &str,
    field: &Field,
    lookup: &str,
    value: &str,
    sqlite: bool,
    values: &mut Vec<String>,
) -> Option<String> {
    let text = format!("CAST({column} AS TEXT)");
    if lookup == "isnull" {
        let negated = if boolean(value)? { "" } else { "NOT " };
        return Some(format!("{column} IS {negated}NULL"));
    }
    if matches!(lookup, "year" | "month" | "day") {
        if !matches!(field.kind.as_str(), "DateField" | "DateTimeField") {
            return None;
        }
        let (offset, width, maximum) = match lookup {
            "year" => (1, 4, 9999),
            "month" => (6, 2, 12),
            _ => (9, 2, 31),
        };
        let value = value
            .parse::<u32>()
            .ok()
            .filter(|value| (1..=maximum).contains(value))?;
        let marker = add_values(values, &format!("{value:0width$}"));
        return Some(format!("SUBSTR({text},{offset},{width})={marker}"));
    }
    if lookup == "iexact" {
        let marker = add_values(values, value);
        return Some(format!("LOWER({text})=LOWER({marker})"));
    }
    let like = match lookup {
        "contains" | "icontains" => Some(("%", "%")),
        "startswith" | "istartswith" => Some(("", "%")),
        "endswith" | "iendswith" => Some(("%", "")),
        _ => None,
    };
    if let Some((left, right)) = like {
        let value = value
            .replace('!', "!!")
            .replace('%', "!%")
            .replace('_', "!_");
        let marker = add_values(values, &format!("{left}{value}{right}"));
        return Some(if lookup.starts_with('i') {
            format!("LOWER({text}) LIKE LOWER({marker}) ESCAPE '!'")
        } else {
            format!("{text} LIKE {marker} ESCAPE '!'")
        });
    }
    let comparable = match field.kind.as_str() {
        "DateField" => text,
        // SQLite stores UTC timestamps with optional fractional seconds and a UTC suffix.
        "DateTimeField" if sqlite => format!(
            "(REPLACE(SUBSTR({text},1,19),'T',' ') || '.' || CASE WHEN SUBSTR({text},20,1)='.'              THEN SUBSTR(REPLACE(REPLACE(REPLACE(SUBSTR({text},21),'+00:00',''),'+00',''),'Z','') || '000000',1,6)              ELSE '000000' END)"
        ),
        _ => column.to_owned(),
    };
    if matches!(lookup, "in" | "range") {
        let markers = value
            .split(',')
            .map(|item| scalar(field, item, sqlite, values))
            .collect::<Option<Vec<_>>>()?;
        if lookup == "in" {
            return Some(format!("{comparable} IN ({})", markers.join(",")));
        }
        let [start, end] = markers.as_slice() else {
            return None;
        };
        return Some(format!("{comparable} BETWEEN {start} AND {end}"));
    }
    let operation = match lookup {
        "gt" => ">",
        "gte" => ">=",
        "lt" => "<",
        "lte" => "<=",
        "exact" => "=",
        _ => return None,
    };
    let marker = scalar(field, value, sqlite, values)?;
    Some(format!("{comparable}{operation}{marker}"))
}

impl<'a> Filter<'a> {
    pub(in crate::site_admin) fn new(
        model: &'a Model,
        params: &Parameters,
        sqlite: bool,
    ) -> Option<Self> {
        let mut source = Source::new(model);
        let mut values = Vec::new();
        let mut conditions = Vec::new();
        if model.slug() == "school" {
            conditions.push("NOT t0.temporary".to_owned());
        }
        if let Some(search) = params
            .get("q")
            .and_then(|query| search_condition(model, query, &mut values))
        {
            conditions.push(search);
        }
        for (parameter, value) in params {
            if matches!(
                parameter.as_str(),
                "q" | "p" | "o" | "all" | "_popup" | "_to_field" | "_facets" | "e"
            ) {
                continue;
            }
            if model.slug() == "teamresult" && parameter == "profile_visibility" {
                let present = "EXISTS (SELECT 1 FROM core_teamresult_hidden_from_debaters m WHERE m.teamresult_id=t0.id)";
                conditions.push(match value.as_str() {
                    "hidden" => present.to_owned(),
                    "visible" => format!("NOT {present}"),
                    _ => return None,
                });
                continue;
            }
            let (path, kind) = lookup(parameter);
            if model.key == "core.user" && path.starts_with("password") {
                return None;
            }
            let path = allowed_path(model, path)?;
            if let Some(field) = model.fields.get(path)
                && let Some([table, from, to]) = field.through.as_ref()
            {
                let mut owner = format!(r#"member."{from}"=t0.id"#);
                if field.generic_through.unwrap_or_default() {
                    let app = add_values(&mut values, model.app());
                    let name = add_values(&mut values, model.slug());
                    owner.push_str(&format!(" AND member.content_type_id=(SELECT id FROM django_content_type WHERE app_label={app} AND model={name})"));
                }
                let exists = format!("SELECT 1 FROM {table} member WHERE {owner}");
                let expression = if kind == "isnull" {
                    let negated = if boolean(value)? { "NOT " } else { "" };
                    format!("{negated}EXISTS ({exists})")
                } else {
                    let test = predicate(
                        &format!(r#"member."{to}""#),
                        field,
                        kind,
                        value,
                        sqlite,
                        &mut values,
                    )?;
                    format!("EXISTS ({exists} AND {test})")
                };
                conditions.push(expression);
            } else {
                let (column, field) = source.resolve(path)?;
                conditions.push(predicate(&column, field, kind, value, sqlite, &mut values)?);
            }
        }
        let condition = if conditions.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", conditions.join(" AND "))
        };
        Some(Self {
            source,
            condition,
            values,
        })
    }
}
