use std::{collections::BTreeMap, sync::LazyLock};

use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use super::catalog::Field;
use crate::ApiError;

const SCHEMA: &str = include_str!("../../schema/agent_storage.json");

#[derive(Deserialize)]
pub(crate) struct Table {
    pub(super) table: String,
    pub(super) fields: BTreeMap<String, Field>,
}

pub(crate) static TABLES: LazyLock<BTreeMap<String, Table>> =
    LazyLock::new(|| serde_json::from_str(SCHEMA).expect("agent journal storage schema"));

pub(super) static LABELS: LazyLock<BTreeMap<&'static str, &'static str>> = LazyLock::new(|| {
    TABLES
        .iter()
        .map(|(label, model)| (model.table.as_str(), label.as_str()))
        .collect()
});

pub(super) async fn lock(conn: &mut AnyConnection, credential: i64) -> Result<bool, ApiError> {
    if conn.backend_name() == "SQLite" {
        sqlx::query("UPDATE core_agentcredential SET last_used_at=last_used_at WHERE id=$1")
            .bind(credential)
            .execute(&mut *conn)
            .await?;
    } else {
        sqlx::query("SET LOCAL lock_timeout='10s'")
            .execute(&mut *conn)
            .await?;
        let tables = LABELS.keys().copied().collect::<Vec<_>>().join(", ");
        let statement = format!("LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE");
        sqlx::query(AssertSqlSafe(statement))
            .execute(&mut *conn)
            .await?;
    }
    Ok(sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_agentcredential
        WHERE id=$1 AND revoked_at IS NULL AND expires_at>CURRENT_TIMESTAMP AND can_write
        AND EXISTS (SELECT 1 FROM core_user WHERE id=created_by_id AND is_active AND is_superuser)",
    )
    .bind(credential)
    .fetch_optional(conn)
    .await?
    .is_some())
}

pub(crate) fn storage_error(error: sqlx::Error) -> ApiError {
    if error.as_database_error().is_some_and(|detail| {
        detail.is_unique_violation()
            || detail.is_foreign_key_violation()
            || detail.is_check_violation()
            || matches!(detail.code().as_deref(), Some("23502" | "1299"))
    }) {
        ApiError::Constraint
    } else {
        ApiError::Database
    }
}

impl Table {
    pub(crate) async fn get(&self, conn: &mut AnyConnection, id: i64) -> Result<Value, ApiError> {
        let fields = self
            .fields
            .values()
            .map(|field| format!("CAST(\"{0}\" AS TEXT) AS \"{0}\"", field.column))
            .collect::<Vec<_>>()
            .join(",");
        let statement = format!("SELECT {fields} FROM \"{}\" WHERE id=$1", self.table);
        let Some(row) = sqlx::query(AssertSqlSafe(statement))
            .bind(id)
            .fetch_optional(conn)
            .await?
        else {
            return Ok(Value::Null);
        };
        let fields = self
            .fields
            .iter()
            .map(|(name, field)| {
                let value = row
                    .try_get::<Option<&str>, _>(field.column.as_str())?
                    .map_or(Value::Null, |value| field.convert(value));
                Ok((name.clone(), value))
            })
            .collect::<Result<Map<_, _>, sqlx::Error>>()?;
        Ok(Value::Object(fields))
    }

    fn expression(field: &Field, index: usize, sqlite: bool) -> String {
        let marker = format!("${index}");
        if sqlite {
            return marker;
        }
        let ty = match field.kind() {
            "BigAutoField"
            | "BigIntegerField"
            | "PositiveBigIntegerField"
            | "ForeignKey"
            | "OneToOneField" => "BIGINT",
            "AutoField" | "IntegerField" | "PositiveIntegerField" => "INTEGER",
            "SmallIntegerField" | "PositiveSmallIntegerField" => "SMALLINT",
            "BooleanField" => "BOOLEAN",
            "FloatField" => "DOUBLE PRECISION",
            "DecimalField" => "NUMERIC",
            "DateField" => "DATE",
            "DateTimeField" => "TIMESTAMPTZ",
            "JSONField" => "JSONB",
            "UUIDField" => "UUID",
            _ => "TEXT",
        };
        format!("CAST(CAST({marker} AS TEXT) AS {ty})")
    }

    pub(crate) async fn write(
        &self,
        conn: &mut AnyConnection,
        fields: &Map<String, Value>,
        id: Option<i64>,
    ) -> Result<i64, ApiError> {
        let sqlite = conn.backend_name() == "SQLite";
        let columns = fields
            .iter()
            .map(|(name, value)| {
                let field = self.fields.get(name).ok_or(ApiError::Serialization)?;
                Ok((field, value))
            })
            .collect::<Result<Vec<_>, ApiError>>()?;
        let markers = columns
            .iter()
            .enumerate()
            .map(|(index, (field, _))| Self::expression(field, index + 1, sqlite))
            .collect::<Vec<_>>();
        let table = &self.table;
        let statement = if id.is_some() {
            let assignments = columns
                .iter()
                .zip(&markers)
                .map(|((field, _), marker)| format!("\"{}\"={marker}", field.column))
                .collect::<Vec<_>>()
                .join(",");
            format!(
                "UPDATE \"{table}\" SET {assignments} WHERE id=${} RETURNING CAST(id AS BIGINT)",
                markers.len() + 1
            )
        } else {
            let inserted = columns
                .iter()
                .map(|(field, _)| format!("\"{}\"", field.column))
                .collect::<Vec<_>>()
                .join(",");
            format!(
                "INSERT INTO \"{table}\" ({inserted}) VALUES ({}) RETURNING CAST(id AS BIGINT)",
                markers.join(",")
            )
        };
        let mut query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement));
        for (field, value) in columns {
            let stored = match value {
                Value::Null => None,
                Value::String(value) if field.kind() == "UUIDField" && sqlite => {
                    Some(value.replace('-', ""))
                }
                Value::String(value) if field.kind() == "DateTimeField" && sqlite => {
                    Some(value.trim_end_matches('Z').replace('T', " "))
                }
                value if field.kind() == "JSONField" => Some(value.to_string()),
                Value::String(value) => Some(value.clone()),
                Value::Bool(value) if sqlite => Some(if *value { "1" } else { "0" }.to_owned()),
                value => Some(value.to_string()),
            };
            query = query.bind(stored);
        }
        if let Some(id) = id {
            query = query.bind(id);
        }
        query.fetch_one(conn).await.map_err(storage_error)
    }

    pub(super) async fn delete(&self, conn: &mut AnyConnection, id: i64) -> Result<(), ApiError> {
        let statement = format!("DELETE FROM \"{}\" WHERE id=$1", self.table);
        sqlx::query(AssertSqlSafe(statement))
            .bind(id)
            .execute(conn)
            .await?;
        Ok(())
    }
}

#[derive(Default)]
pub(super) struct Journal(BTreeMap<(&'static str, i64), Value>);

impl Journal {
    pub(super) async fn watch(
        &mut self,
        conn: &mut AnyConnection,
        label: &'static str,
        id: i64,
    ) -> Result<(), ApiError> {
        if let std::collections::btree_map::Entry::Vacant(entry) = self.0.entry((label, id)) {
            entry.insert(TABLES[label].get(conn, id).await?);
        }
        Ok(())
    }

    pub(super) fn created(&mut self, label: &'static str, id: i64) {
        self.0.entry((label, id)).or_insert(Value::Null);
    }

    pub(super) async fn related(
        &mut self,
        conn: &mut AnyConnection,
        label: &'static str,
        field: &str,
        id: i64,
    ) -> Result<Vec<i64>, ApiError> {
        let schema = &TABLES[label];
        let column = schema.fields.get(field).ok_or(ApiError::Serialization)?;
        let statement = format!(
            "SELECT CAST(id AS BIGINT) FROM \"{}\" WHERE \"{}\"=$1 ORDER BY id",
            schema.table, column.column
        );
        let ids = sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
            .bind(id)
            .fetch_all(&mut *conn)
            .await?;
        for &id in &ids {
            self.watch(conn, label, id).await?;
        }
        Ok(ids)
    }

    pub(super) async fn changes(self, conn: &mut AnyConnection) -> Result<Vec<Value>, ApiError> {
        let mut changes = Vec::new();
        for ((label, id), before) in self.0 {
            let after = TABLES[label].get(conn, id).await?;
            if before != after {
                changes.push(json!({"model":label,"id":id,"before":before,"after":after}));
            }
        }
        Ok(changes)
    }
}
