use std::{collections::BTreeMap, sync::LazyLock};

use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use ring::digest;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row, any::AnyRow};

use crate::{ApiError, django_json};

const SCHEMA: &str = include_str!("../../schema/agent_resources.json");

#[derive(Deserialize)]
pub(super) struct Field {
    pub(super) column: String,
    pub(super) exposed: bool,
    scale: Option<usize>,
    pub(super) max_length: Option<usize>,
    #[serde(default)]
    pub(super) default: Value,
    #[serde(default)]
    pub(super) auto_now: bool,
    #[serde(default)]
    pub(super) auto_now_add: bool,
    pub(super) schema: Value,
}

impl Field {
    pub(super) fn kind(&self) -> &str {
        self.schema["type"].as_str().unwrap_or_default()
    }

    pub(super) fn convert(&self, input: &str) -> Value {
        match self.kind() {
            "AutoField"
            | "BigAutoField"
            | "ForeignKey"
            | "OneToOneField"
            | "IntegerField"
            | "BigIntegerField"
            | "SmallIntegerField"
            | "PositiveIntegerField"
            | "PositiveSmallIntegerField"
            | "PositiveBigIntegerField" => json!(input.parse::<i64>().ok()),
            "BooleanField" => json!(matches!(input, "1" | "true" | "t")),
            "FloatField" => json!(input.parse::<f64>().ok()),
            "DecimalField" => input.parse::<f64>().ok().map_or_else(
                || json!(input),
                |number| {
                    json!(format!(
                        "{number:.places$}",
                        places = self.scale.unwrap_or_default()
                    ))
                },
            ),
            "DateTimeField" => json!(datetime(input, true)),
            "JSONField" => serde_json::from_str(input).unwrap_or(Value::Null),
            "UUIDField" => json!(uuid::Uuid::parse_str(input).ok().map(|id| id.to_string())),
            _ => json!(input),
        }
    }
}

pub(super) fn datetime(input: &str, abbreviate: bool) -> String {
    let parsed = DateTime::parse_from_rfc3339(input)
        .map(|time| time.with_timezone(&Utc))
        .ok()
        .or_else(|| {
            let utc = input.trim_end_matches("+00").trim_end_matches("+00:00");
            NaiveDateTime::parse_from_str(utc, "%Y-%m-%d %H:%M:%S%.f")
                .ok()
                .map(|dt| dt.and_utc())
        });
    parsed.map_or_else(
        || input.to_owned(),
        |instant| {
            let whole_second = instant.timestamp_subsec_nanos() == 0;
            let seconds = if whole_second {
                SecondsFormat::Secs
            } else if abbreviate {
                SecondsFormat::Millis
            } else {
                SecondsFormat::Micros
            };
            instant.to_rfc3339_opts(seconds, true)
        },
    )
}

#[derive(Deserialize)]
pub(super) struct Membership {
    pub(super) table: String,
    pub(super) label: String,
    pub(super) source: String,
    pub(super) target: String,
    pub(super) exposed: bool,
    pub(super) related_table: String,
}

#[derive(Deserialize)]
pub(super) struct Model {
    pub(super) table: String,
    pub(super) label: String,
    pub(super) writable: bool,
    pub(super) fields: BTreeMap<String, Field>,
    pub(super) memberships: BTreeMap<String, Membership>,
}

pub(super) static MODELS: LazyLock<BTreeMap<String, Model>> =
    LazyLock::new(|| serde_json::from_str(SCHEMA).expect("agent model field metadata"));

fn ordered(value: &Value) -> Value {
    match value {
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), ordered(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(rows) => Value::Array(rows.iter().map(ordered).collect()),
        value => value.clone(),
    }
}

pub(super) fn hash(value: &Value) -> String {
    hex::encode(digest::digest(
        &digest::SHA256,
        django_json::ascii(&ordered(value).to_string()).as_bytes(),
    ))
}

pub(super) fn change_summary(change: &Value) -> Value {
    let label = change["model"].as_str().unwrap_or_default();
    let fields = MODELS
        .values()
        .find(|model| model.label.eq_ignore_ascii_case(label))
        .map(|model| {
            model
                .fields
                .iter()
                .filter_map(|(name, field)| field.exposed.then_some(name.as_str()))
                .collect::<Vec<_>>()
        })
        .or_else(|| {
            MODELS
                .values()
                .flat_map(|model| model.memberships.values())
                .find(|member| member.exposed && member.label == label)
                .map(|member| vec!["id", member.source.as_str(), member.target.as_str()])
        })
        .unwrap_or_default();
    let mask = |data: &Value| {
        data.as_object()
            .into_iter()
            .flat_map(|fields| fields.iter())
            .filter(|(name, _)| fields.contains(&name.as_str()))
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect::<serde_json::Map<_, _>>()
    };
    json!({"model":change["model"],"id":change["id"],"action":match (change["before"].is_null(),change["after"].is_null()){
        (true,_)=>"create",(_,true)=>"delete",_=>"update"
    }, "before":mask(&change["before"]), "after":mask(&change["after"])})
}

pub(super) fn discovery(can_write: bool) -> Value {
    let resources = MODELS
        .iter()
        .map(|(name, model)| {
            let fields = model
                .fields
                .iter()
                .filter(|(_, field)| field.exposed)
                .map(|(key, value)| (key.clone(), value.schema.clone()))
                .collect::<serde_json::Map<_, _>>();
            let membership = model
                .memberships
                .iter()
                .filter_map(|(name, member)| member.exposed.then_some(name))
                .collect::<Vec<_>>();
            (
                name.clone(),
                json!({"writable":model.writable,"fields":fields,"many_to_many":membership}),
            )
        })
        .collect::<serde_json::Map<_, _>>();
    json!({"version":1,"transport":"REST","can_write":can_write,"resources":resources,
        "operations":["create","update","delete","merge_synthetic","merge_debaters"],
        "max_operations":200,"max_changed_rows":20000,"bulk_url":"/api/agent/v1/batches/",
        "docs":"/api/agent/v1/guide/","undo_url_template":"/api/agent/v1/batches/{id}/undo/",
        "concurrency":"update/delete require revision; merge requires source_revision and target_revision"})
}

impl Model {
    pub(super) fn projection(&self) -> String {
        self.fields
            .values()
            .map(|field| format!("CAST(\"{}\" AS TEXT) AS \"{}\"", field.column, field.column))
            .collect::<Vec<_>>()
            .join(",")
    }

    pub(super) async fn row(
        &self,
        conn: &mut AnyConnection,
        index: &AnyRow,
    ) -> Result<Value, ApiError> {
        let mut actual = serde_json::Map::new();
        let mut visible = serde_json::Map::new();
        for (name, field) in &self.fields {
            let value = index
                .try_get::<Option<String>, _>(field.column.as_str())?
                .map_or(Value::Null, |value| field.convert(&value));
            if field.exposed {
                visible.insert(name.clone(), value.clone());
            }
            actual.insert(name.clone(), value);
        }
        let id = actual["id"].as_i64().unwrap_or_default();
        for (name, member) in &self.memberships {
            let statement = format!(
                "SELECT CAST(\"{target}\" AS BIGINT) FROM \"{table}\"
                WHERE \"{source}\"=$1 ORDER BY \"{target}\"",
                target = member.target,
                table = member.table,
                source = member.source
            );
            let records = sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
                .bind(id)
                .fetch_all(&mut *conn)
                .await?;
            let value = json!(records);
            if member.exposed {
                visible.insert(name.clone(), value.clone());
            }
            actual.insert(name.clone(), value);
        }
        visible.insert("revision".to_owned(), json!(hash(&Value::Object(actual))));
        Ok(Value::Object(visible))
    }
}
