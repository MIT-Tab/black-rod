use std::collections::{BTreeMap, HashSet};

use chrono::{NaiveDate, SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe};

use super::{
    catalog::{self, Field, MODELS},
    journal::{Journal, LABELS, TABLES},
};
use crate::{ApiError, claims, delete_relations, tournament_profile::edit};

pub(super) enum Failure {
    Invalid(String),
    Stale(Value),
    Database(ApiError),
}

impl From<ApiError> for Failure {
    fn from(error: ApiError) -> Self {
        Self::Database(error)
    }
}

impl From<sqlx::Error> for Failure {
    fn from(error: sqlx::Error) -> Self {
        Self::Database(error.into())
    }
}

pub(super) fn invalid(text: impl Into<String>) -> Failure {
    Failure::Invalid(text.into())
}

fn resolve(input: &Value, refs: &BTreeMap<String, i64>) -> Result<Value, Failure> {
    match input {
        Value::String(text) if text.starts_with('$') => refs
            .get(&text[1..])
            .map(|id| json!(id))
            .ok_or_else(|| invalid(format!("Unknown operation reference: {text}"))),
        Value::Array(rows) => rows
            .iter()
            .map(|row| resolve(row, refs))
            .collect::<Result<Vec<_>, _>>()
            .map(Value::Array),
        _ => Ok(input.clone()),
    }
}

pub(super) fn positive_id(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|id| *id > 0)
}

async fn exists(conn: &mut AnyConnection, table: &str, id: i64) -> Result<bool, ApiError> {
    let statement = format!("SELECT CAST(id AS BIGINT) FROM \"{table}\" WHERE id=$1");
    Ok(sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
        .bind(id)
        .fetch_optional(conn)
        .await?
        .is_some())
}

fn validate_text(name: &str, field: &Field, converted: &Value) -> Result<(), Failure> {
    if let (Some(text), Some(limit)) = (converted.as_str(), field.max_length)
        && text.chars().count() > limit
    {
        return Err(invalid(format!(
            "{{'{name}': ['Ensure this value has at most {limit} characters (it has {}).']}}",
            text.chars().count()
        )));
    }
    if let Some(choices) = field.schema["choices"].as_array()
        && converted.as_str() != Some("")
        && !choices.iter().any(|choice| choice[0] == *converted)
    {
        return Err(invalid(format!(
            "{{'{name}': ['Value {} is not a valid choice.']}}",
            converted
        )));
    }
    if converted.as_str() == Some("") && field.schema["blank"] == false {
        return Err(invalid(format!(
            "{{'{name}': ['This field cannot be blank.']}}"
        )));
    }
    Ok(())
}

async fn validate(
    conn: &mut AnyConnection,
    name: &str,
    field: &Field,
    data: Value,
) -> Result<Value, Failure> {
    if data.is_null() {
        return if field.schema["nullable"] == true {
            Ok(data)
        } else {
            Err(invalid(format!(
                "{{'{name}': ['This field cannot be null.']}}"
            )))
        };
    }
    let kind = field.kind();
    let raw = data
        .as_str()
        .map_or_else(|| data.to_string(), str::to_owned);
    let converted = match kind {
        "ForeignKey" | "OneToOneField" => {
            let id = positive_id(&data)
                .ok_or_else(|| invalid(format!("{{'{name}': ['Enter a whole number.']}}")))?;
            let label = field.schema["related_model"]
                .as_str()
                .ok_or(ApiError::Serialization)?;
            let table = TABLES.get(label).ok_or(ApiError::Serialization)?;
            if !exists(conn, &table.table, id).await? {
                return Err(invalid(format!(
                    "{{'{name}': ['Related record does not exist.']}}"
                )));
            }
            json!(id)
        }
        "IntegerField" | "PositiveIntegerField" | "PositiveSmallIntegerField" => {
            let value = raw
                .trim()
                .parse::<i64>()
                .ok()
                .filter(|value| match kind {
                    "PositiveSmallIntegerField" => (0..=32767).contains(value),
                    "PositiveIntegerField" => (0..=i64::from(i32::MAX)).contains(value),
                    _ => i32::try_from(*value).is_ok(),
                })
                .ok_or_else(|| invalid(format!("{{'{name}': ['Enter a whole number.']}}")))?;
            json!(value)
        }
        "BooleanField" => match raw.as_str() {
            "true" | "True" | "1" | "t" => json!(true),
            "false" | "False" | "0" | "f" => json!(false),
            _ => {
                return Err(invalid(format!(
                    "{{'{name}': ['Value must be either True or False.']}}"
                )));
            }
        },
        "DecimalField" => {
            let parsed = raw
                .parse::<f64>()
                .ok()
                .filter(|value| value.is_finite() && value.abs() < 100.0);
            let precision = raw.split_once('.').map_or(0, |(_, decimal)| decimal.len());
            if parsed.is_none() || precision > 4 {
                return Err(invalid(format!(
                    "{{'{name}': ['Ensure that there are no more than 6 digits in total and 4 decimal places.']}}"
                )));
            }
            field.convert(&raw)
        }
        "FloatField" => raw
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .map(|value| json!(value))
            .ok_or_else(|| invalid(format!("{{'{name}': ['Enter a number.']}}")))?,
        "DateField" => json!(
            NaiveDate::parse_from_str(&raw, "%Y-%m-%d")
                .map_err(|_| invalid(format!("{{'{name}': ['Enter a valid date.']}}")))?
                .to_string()
        ),
        "JSONField" => data,
        _ => json!(raw),
    };
    validate_text(name, field, &converted)?;
    Ok(converted)
}

pub(super) async fn read(
    conn: &mut AnyConnection,
    model: &catalog::Model,
    id: i64,
) -> Result<Value, Failure> {
    let sql = format!(
        "SELECT {} FROM \"{}\" WHERE id=$1",
        model.projection(),
        model.table
    );
    let row = sqlx::query(AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?
        .ok_or_else(|| {
            invalid(format!(
                "{} matching query does not exist.",
                model.label.rsplit('.').next().unwrap_or(&model.label)
            ))
        })?;
    Ok(model.row(conn, &row).await?)
}

async fn membership(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    member: &catalog::Membership,
    parent: i64,
    input: &Value,
) -> Result<(), Failure> {
    let error = || invalid("Expected a list of positive integer IDs.");
    let mut distinct = HashSet::new();
    let mut desired = Vec::new();
    for row in input.as_array().ok_or_else(error)? {
        let id = row.as_i64().filter(|id| *id > 0).ok_or_else(error)?;
        if distinct.insert(id) {
            if !exists(conn, &member.related_table, id).await? {
                return Err(invalid("Related record does not exist."));
            }
            desired.push(id);
        }
    }
    let label = LABELS[member.table.as_str()];
    let statement = format!(
        "SELECT CAST(\"{}\" AS BIGINT) FROM \"{}\" WHERE \"{}\"=$1 ORDER BY id",
        member.target, member.table, member.source
    );
    let current = sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
        .bind(parent)
        .fetch_all(&mut *conn)
        .await?;
    if current == desired {
        return Ok(());
    }
    for id in journal.related(conn, label, &member.source, parent).await? {
        TABLES[label].delete(conn, id).await?;
    }
    for id in desired {
        let fields = Map::from_iter([
            (member.source.clone(), json!(parent)),
            (member.target.clone(), json!(id)),
        ]);
        let id = TABLES[label].write(conn, &fields, None).await?;
        journal.created(label, id);
    }
    Ok(())
}

fn deletable(resource: &str) -> bool {
    matches!(
        resource,
        "rounds"
            | "round_stats"
            | "round_metadata"
            | "round_judges"
            | "imports"
            | "import_batches"
            | "team_results"
            | "speaker_results"
            | "school_lookups"
            | "qualifications"
            | "reaffiliations"
            | "team_reaffiliations"
    )
}

pub(super) async fn erase(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    table: &'static str,
    id: i64,
) -> Result<(), Failure> {
    use delete_relations::DeleteError;
    let process = |error| match error {
        DeleteError::Absent => invalid("Record not found."),
        DeleteError::Protected => invalid("Related records protect this record from deletion."),
        DeleteError::Database(error) => Failure::from(error),
    };
    let plan = delete_relations::plan(conn, table, id)
        .await
        .map_err(process)?;
    for (table, id) in plan.affected() {
        if let Some(label) = LABELS.get(table) {
            journal.watch(conn, label, id).await?;
        }
    }
    plan.apply(conn).await.map_err(process)?;
    Ok(())
}

async fn hooks(
    conn: &mut AnyConnection,
    resource: &str,
    fields: &mut Map<String, Value>,
    create: bool,
    season: i32,
    id: Option<i64>,
) -> Result<(), Failure> {
    match resource {
        "round_stats" => {
            let parent = fields
                .get("round_id")
                .and_then(Value::as_i64)
                .ok_or_else(|| invalid("Round is required."))?;
            let stage = sqlx::query_scalar::<_, String>("SELECT stage FROM core_round WHERE id=$1")
                .bind(parent)
                .fetch_optional(conn)
                .await?
                .ok_or_else(|| invalid("Related record does not exist."))?;
            if stage == "outround" {
                for key in ["speaks", "ranks", "debater_role"] {
                    fields.insert(key.to_owned(), Value::Null);
                }
            }
            fields.insert("stage".to_owned(), json!(stage));
        }
        "debaters" => {
            for (key, fallback) in [("first_name", "Removed"), ("last_name", "")] {
                let name = fields.get(key).and_then(Value::as_str).unwrap_or_default();
                fields.insert(
                    key.to_owned(),
                    json!(claims::edit::sanitize_name(name, fallback)),
                );
            }
            if create {
                for key in ["first_season", "latest_season"] {
                    if fields
                        .get(key)
                        .and_then(Value::as_str)
                        .is_none_or(str::is_empty)
                    {
                        fields.insert(key.to_owned(), json!(season.to_string()));
                    }
                }
            }
            if fields.get("status").and_then(Value::as_i64) != Some(2) {
                fields.insert("dino_judge_contact_opt_in".to_owned(), json!(false));
            }
            if !["dino_judge_contact_opt_in", "dino_to_contact_opt_in"]
                .iter()
                .any(|k| fields.get(*k) == Some(&json!(true)))
            {
                fields.insert("region".to_owned(), json!([]));
            }
        }
        "tournaments" => {
            let text = |key: &str| fields.get(key).and_then(Value::as_str).unwrap_or_default();
            let manual_name = text("manual_name");
            if let Some(host) = fields.get("host_id").and_then(Value::as_i64) {
                let event_season = text("season");
                let code = fields
                    .get("qual_type")
                    .and_then(Value::as_i64)
                    .unwrap_or_default();
                let suffix = fields
                    .get("name_suffix")
                    .and_then(Value::as_i64)
                    .unwrap_or_default();
                let (name, short_name) = edit::names(
                    conn,
                    host,
                    code,
                    id,
                    edit::Naming {
                        season: event_season,
                        date: text("date"),
                        name_suffix: &suffix.to_string(),
                        manual_name,
                        short_name: text("short_name"),
                    },
                )
                .await?;
                let scoring = edit::scoring(code, event_season);
                for (key, value) in [
                    ("name", json!(name)),
                    ("short_name", json!(short_name)),
                    ("toty", json!(scoring.toty)),
                    ("soty", json!(scoring.soty)),
                    ("noty", json!(false)),
                    ("qual", json!(scoring.qual)),
                ] {
                    fields.insert(key.to_owned(), value);
                }
                if let Some(online) = scoring.online {
                    fields.insert("online_qual_points".to_owned(), json!(online));
                }
                if let Some(bar) = scoring.bar {
                    fields.insert("autoqual_bar".to_owned(), json!(bar));
                }
            } else if !manual_name.is_empty() {
                fields.insert("name".to_owned(), json!(manual_name));
            }
        }
        _ => (),
    }
    Ok(())
}

pub(super) async fn apply(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    operation: &Value,
    refs: &BTreeMap<String, i64>,
    season: i32,
    actor: super::merge::Actor<'_>,
) -> Result<Value, Failure> {
    let operation = operation
        .as_object()
        .ok_or_else(|| invalid("Each operation must be an object."))?;
    let action = operation
        .get("op")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let resource = operation
        .get("resource")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if matches!(action, "merge_synthetic" | "merge_debaters") {
        return super::merge::apply(conn, journal, operation, actor).await;
    }
    if !operation.keys().all(|key| {
        matches!(
            key.as_str(),
            "op" | "resource" | "id" | "revision" | "data" | "ref"
        )
    }) {
        return Err(invalid("Unknown operation fields."));
    }
    let model = MODELS
        .get(resource)
        .filter(|model| model.writable && matches!(action, "create" | "update" | "delete"))
        .ok_or_else(|| invalid("Unknown operation or writable resource."))?;
    let label = LABELS[model.table.as_str()];
    let table = &TABLES[label];
    let create = action == "create";
    let mut previous = Value::Null;
    let id = if create {
        None
    } else {
        let id = positive_id(&resolve(operation.get("id").unwrap_or(&Value::Null), refs)?)
            .ok_or_else(|| invalid("Record not found."))?;
        let public = read(conn, model, id).await?;
        if operation.get("revision") != public.get("revision") {
            return Err(Failure::Stale(
                json!({"model":label,"id":id,"detail":"Missing or stale revision."}),
            ));
        }
        if action == "delete" {
            if !deletable(resource) {
                return Err(invalid(
                    "Delete is not supported for this resource; repair it or merge the synthetic identity.",
                ));
            }
            let table = LABELS
                .get_key_value(model.table.as_str())
                .ok_or(ApiError::Serialization)?
                .0;
            erase(conn, journal, table, id).await?;
            return Ok(json!({"resource":resource,"id":id,"deleted":true}));
        }
        journal.watch(conn, label, id).await?;
        previous = table.get(conn, id).await?;
        Some(id)
    };
    let data = operation
        .get("data")
        .and_then(Value::as_object)
        .filter(|data| create || !data.is_empty())
        .ok_or_else(|| invalid("data must be an object (nonempty for updates)."))?;
    let mut protected = data
        .keys()
        .filter(|name| {
            model
                .fields
                .get(*name)
                .is_none_or(|field| field.schema["writable"] != true)
                && model
                    .memberships
                    .get(*name)
                    .is_none_or(|member| !member.exposed)
        })
        .map(String::as_str)
        .collect::<Vec<_>>();
    if !protected.is_empty() {
        protected.sort_unstable();
        return Err(invalid(format!(
            "{{'data': ErrorDetail(string='Unknown or protected fields: {}', code='invalid')}}",
            protected.join(", ")
        )));
    }
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true);
    let mut fields = previous.as_object().cloned().unwrap_or_default();
    fields.remove("id");
    for (name, field) in model
        .fields
        .iter()
        .filter(|(name, _)| name.as_str() != "id")
    {
        let value = if let Some(value) = data.get(name) {
            let value = resolve(value, refs)?;
            if resource == "round_stats"
                && matches!(name.as_str(), "speaks" | "ranks" | "debater_role")
            {
                let round = data
                    .get("round_id")
                    .map(|value| resolve(value, refs))
                    .transpose()?
                    .or_else(|| previous.get("round_id").cloned());
                let outround =
                    sqlx::query_scalar::<_, String>("SELECT stage FROM core_round WHERE id=$1")
                        .bind(round.as_ref().and_then(positive_id))
                        .fetch_optional(&mut *conn)
                        .await?
                        .is_some_and(|stage| stage == "outround");
                if outround {
                    Value::Null
                } else {
                    validate(conn, name, field, value).await?
                }
            } else {
                validate(conn, name, field, value).await?
            }
        } else if field.auto_now || (create && field.auto_now_add) {
            json!(now)
        } else if create {
            if field.schema["required_on_create"] == true {
                return Err(invalid(format!(
                    "{{'{name}': ['This field cannot be null.']}}"
                )));
            }
            field.default.clone()
        } else {
            continue;
        };
        fields.insert(name.clone(), value);
    }
    hooks(conn, resource, &mut fields, create, season, id).await?;
    let id = table.write(conn, &fields, id).await?;
    if create {
        journal.created(label, id);
    }
    if resource == "rounds" && !create && previous["stage"] != fields["stage"] {
        journal
            .related(conn, "core.RoundStats", "round_id", id)
            .await?;
        let stage = fields["stage"].as_str().ok_or(ApiError::Serialization)?;
        sqlx::query(
            "UPDATE core_roundstats SET stage=$1,
            speaks=CASE WHEN $1='outround' THEN NULL ELSE speaks END,
            ranks=CASE WHEN $1='outround' THEN NULL ELSE ranks END,
            debater_role=CASE WHEN $1='outround' THEN NULL ELSE debater_role END WHERE round_id=$2",
        )
        .bind(stage)
        .bind(id)
        .execute(&mut *conn)
        .await?;
    }
    if resource == "debaters" && !create {
        let teams = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(team_id AS BIGINT) FROM core_team_debaters WHERE debater_id=$1",
        )
        .bind(id)
        .fetch_all(&mut *conn)
        .await?;
        for team in teams {
            journal.watch(conn, "core.Team", team).await?;
        }
        claims::edit::team_names(conn, id).await?;
    }
    for (name, member) in &model.memberships {
        if let Some(value) = data.get(name) {
            membership(conn, journal, member, id, &resolve(value, refs)?).await?;
        }
    }
    if resource == "teams" && data.contains_key("debaters") {
        claims::edit::team_name(conn, id).await?;
    }
    read(conn, model, id).await
}
