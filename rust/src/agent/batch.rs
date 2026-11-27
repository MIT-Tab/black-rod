use std::collections::BTreeMap;

use axum::{
    body::to_bytes,
    extract::Request,
    http::{StatusCode, header},
    response::Response,
};
use chrono::{SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};

use super::{
    BATCH, Endpoint, catalog,
    journal::{self, Journal, TABLES, Table},
    mutation::{self, Failure},
};
use crate::{ApiError, AppState};

const LIMIT: usize = 200;
const MAX_CHANGES: usize = 20_000;

fn conflict(endpoint: Endpoint<'_>, message: &str) -> Response {
    endpoint.render(StatusCode::CONFLICT, json!({"detail":message}))
}

fn invalid(endpoint: Endpoint<'_>, message: &str) -> Response {
    endpoint.invalid(json!([message]))
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn zero_fraction(timestamp: &str) -> &str {
    timestamp
        .strip_suffix(".000Z")
        .or_else(|| timestamp.strip_suffix('Z'))
        .unwrap_or(timestamp)
}

fn matches_journal_row(table: &Table, current: &Value, expected: &Value) -> bool {
    if current == expected {
        return true;
    }
    let (Some(current), Some(expected)) = (current.as_object(), expected.as_object()) else {
        return false;
    };
    current.len() == expected.len()
        && expected.iter().all(|(name, value)| {
            let Some(actual) = current.get(name) else {
                return false;
            };
            if actual == value {
                return true;
            }
            match table.fields.get(name).map(|field| field.kind()) {
                Some("JSONField") => catalog::hash(actual) == catalog::hash(value),
                Some("DateTimeField") => {
                    actual
                        .as_str()
                        .zip(value.as_str())
                        .is_some_and(|(actual, expected)| {
                            actual.ends_with('Z')
                                && expected.ends_with('Z')
                                && zero_fraction(actual) == zero_fraction(expected)
                        })
                }
                _ => false,
            }
        })
}

async fn restore(conn: &mut AnyConnection, changes: &[Value]) -> Result<Vec<Value>, Failure> {
    let mut reversed = Vec::with_capacity(changes.len());
    let mut restoration = Vec::with_capacity(changes.len());
    for row in changes {
        let label = row["model"].as_str().ok_or(ApiError::Serialization)?;
        let table = TABLES.get(label).ok_or(ApiError::Serialization)?;
        let id = row["id"].as_i64().ok_or(ApiError::Serialization)?;
        let current = table.get(conn, id).await?;
        if !matches_journal_row(table, &current, &row["after"]) {
            return Err(Failure::Stale(
                json!({"model":label,"id":id,"detail":"Later edits prevent undo."}),
            ));
        }
        let mut restored = row["before"].clone();
        let mut stored = restored.clone();
        if let Some(fields) = restored.as_object_mut() {
            let timestamps = table
                .fields
                .iter()
                .filter(|(_, field)| field.kind() == "DateTimeField")
                .map(|(name, _)| name)
                .collect::<Vec<_>>();
            let preserved = timestamps.iter().any(|name| {
                current.get(*name).is_some_and(Value::is_string)
                    && current.get(*name) == fields.get(*name)
            });
            let precise = if preserved {
                table.get_precise(conn, id).await?
            } else {
                Value::Null
            };
            for name in timestamps {
                // Django's journal exposes milliseconds. Preserve the database
                // value when the timestamp is outside this operation's changes.
                if current.get(name) == fields.get(name)
                    && let Some(value) = precise.get(name).filter(|value| value.is_string())
                {
                    stored[name] = value.clone();
                } else if let Some(timestamp) = fields
                    .get(name)
                    .and_then(Value::as_str)
                    .and_then(|value| value.strip_suffix(".000Z"))
                {
                    fields.insert(name.clone(), json!(format!("{timestamp}Z")));
                }
            }
        }
        restoration.push((table, stored));
        reversed.push(json!({"model":label,"id":id,"before":current,"after":restored}));
    }
    for row in changes {
        let table = &TABLES[row["model"].as_str().ok_or(ApiError::Serialization)?];
        table
            .delete(conn, row["id"].as_i64().ok_or(ApiError::Serialization)?)
            .await?;
    }
    for (table, stored) in restoration {
        if let Some(before) = stored.as_object() {
            table.write(conn, before, None).await?;
        }
    }
    let constraints = if conn.backend_name() == "SQLite" {
        sqlx::query("PRAGMA foreign_key_check")
            .fetch_optional(conn)
            .await
            .map(|row| row.is_none())
    } else {
        sqlx::query("SET CONSTRAINTS ALL IMMEDIATE")
            .execute(conn)
            .await
            .map(|_| true)
    };
    if !matches!(constraints, Ok(true)) {
        return Err(Failure::Stale(
            json!({"detail":"Later references or constraints prevent undo; reverse dependent changes first."}),
        ));
    }
    Ok(reversed)
}

async fn complete(
    state: &AppState,
    endpoint: Endpoint<'_>,
    credential: i64,
    undo: Option<i64>,
    payload: Value,
) -> Result<Response, ApiError> {
    let Some(payload) = payload.as_object() else {
        return Ok(invalid(endpoint, "Expected a JSON object."));
    };
    let unknown = payload
        .keys()
        .filter(|key| {
            !(matches!(
                key.as_str(),
                "request_id" | "reason" | "dry_run" | "expected_change_hash"
            ) || (key.as_str() == "operations" && undo.is_none()))
        })
        .map(String::as_str)
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return Ok(invalid(
            endpoint,
            &format!("Unknown request fields: {}", unknown.join(", ")),
        ));
    }
    let Some(request_id) = payload
        .get("request_id")
        .and_then(Value::as_str)
        .and_then(|id| uuid::Uuid::parse_str(id).ok())
    else {
        return Ok(invalid(
            endpoint,
            "request_id must be a UUID (the idempotency key).",
        ));
    };
    let Some(reason) = payload
        .get("reason")
        .and_then(Value::as_str)
        .filter(|reason| !reason.trim().is_empty() && reason.chars().count() <= 4000)
    else {
        return Ok(invalid(
            endpoint,
            "A reason of 1–4000 characters is required.",
        ));
    };
    let dry_run = match payload.get("dry_run").map(Value::as_bool) {
        Some(Some(flag)) => flag,
        None => false,
        _ => return Ok(invalid(endpoint, "dry_run must be a boolean.")),
    };
    let mut tx = state.pool.begin().await?;
    if !journal::lock(&mut tx, credential).await? {
        return Ok(endpoint.render(
            StatusCode::FORBIDDEN,
            json!({"detail":"Credential is no longer authorized to write."}),
        ));
    }
    let hashed = payload
        .iter()
        .filter(|(key, _)| key.as_str() != "dry_run")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<Map<_, _>>();
    let request_hash = catalog::hash(&json!({"payload":hashed,"undo_id":undo}));
    let sqlite = tx.backend_name() == "SQLite";
    let key = if sqlite {
        request_id.simple().to_string()
    } else {
        request_id.to_string()
    };
    let lookup = if sqlite {
        "SELECT CAST(id AS BIGINT) AS id,CAST(credential_id AS BIGINT) AS credential_id,request_hash FROM core_agentchangeset WHERE request_id=$1"
    } else {
        "SELECT CAST(id AS BIGINT) AS id,CAST(credential_id AS BIGINT) AS credential_id,request_hash FROM core_agentchangeset WHERE request_id=CAST(CAST($1 AS TEXT) AS UUID)"
    };
    if let Some(row) = sqlx::query(lookup)
        .bind(key)
        .fetch_optional(&mut *tx)
        .await?
    {
        if row.try_get::<i64, _>("credential_id")? != credential
            || row.try_get::<&str, _>("request_hash")? != request_hash
        {
            return Ok(conflict(
                endpoint,
                "request_id has already been used for another request.",
            ));
        }
        return super::batch(&mut tx, endpoint, row.try_get("id")?).await;
    }
    let (operations, results, changes) = if let Some(id) = undo {
        let statement = format!("{BATCH} WHERE b.id=$1");
        let Some(original) = sqlx::query(sqlx::AssertSqlSafe(statement))
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?
        else {
            return Ok(endpoint.not_found());
        };
        if original.try_get::<Option<i64>, _>("reversed_by")?.is_some() {
            return Ok(conflict(
                endpoint,
                "This change set has already been reversed.",
            ));
        }
        let changes = super::changes(&original)?;
        let list = changes.as_array().ok_or(ApiError::Serialization)?;
        let changes = match restore(&mut tx, list).await {
            Ok(reversed) => reversed,
            Err(Failure::Stale(detail)) => return Ok(endpoint.render(StatusCode::CONFLICT, detail)),
            Err(Failure::Database(_)) => {
                return Ok(conflict(
                    endpoint,
                    "Later references or constraints prevent undo; reverse dependent changes first.",
                ));
            }
            Err(Failure::Invalid(detail)) => return Ok(invalid(endpoint, &detail)),
        };
        (
            json!([{"op":"undo","changeset_id":id}]),
            Vec::new(),
            changes,
        )
    } else {
        let Some(operations) = payload
            .get("operations")
            .and_then(Value::as_array)
            .filter(|operations| (1..=LIMIT).contains(&operations.len()))
        else {
            return Ok(invalid(
                endpoint,
                &format!("operations must contain 1–{LIMIT} operations."),
            ));
        };
        let mut journal = Journal::default();
        let mut refs = BTreeMap::new();
        let mut results = Vec::new();
        for (index, operation) in operations.iter().enumerate() {
            let invalid = |detail: String| {
                endpoint.invalid(json!({"operation_index":index.to_string(),"detail":detail}))
            };
            let result = match mutation::apply(
                &mut tx,
                &mut journal,
                operation,
                &refs,
                state.current_season,
                super::merge::Actor { credential, reason },
            )
            .await
            {
                Ok(result) => result,
                Err(Failure::Stale(detail)) => {
                    return Ok(endpoint.render(StatusCode::CONFLICT, detail));
                }
                Err(Failure::Database(error)) => return Err(error),
                Err(Failure::Invalid(detail)) => return Ok(invalid(detail)),
            };
            if operation.get("ref").is_some_and(truthy) {
                let Some(reference) = operation["ref"]
                    .as_str()
                    .filter(|name| !refs.contains_key(*name))
                    .zip(result["id"].as_i64())
                else {
                    return Ok(invalid(
                        "ref must be a unique name for an operation that returns an id.".to_owned(),
                    ));
                };
                refs.insert(reference.0.to_owned(), reference.1);
            }
            results.push(result);
        }
        let changes = journal.changes(&mut tx).await?;
        if changes.len() > MAX_CHANGES {
            return Ok(invalid(
                endpoint,
                &format!("Batch changes more than {MAX_CHANGES} rows; use smaller batches."),
            ));
        }
        (json!(operations), results, changes)
    };
    let change_hash = catalog::hash(&json!(changes));
    if let Some(expected) = payload
        .get("expected_change_hash")
        .filter(|value| truthy(value))
        && expected.as_str() != Some(change_hash.as_str())
    {
        return Ok(conflict(
            endpoint,
            "The preview has changed; run a new dry run.",
        ));
    }
    if dry_run {
        tx.rollback().await?;
        return Ok(endpoint.render(
            StatusCode::OK,
            json!({"dry_run":true,"change_hash":change_hash,"results":results,
            "changes":changes.iter().map(catalog::change_summary).collect::<Vec<_>>()}),
        ));
    }
    let id = TABLES["core.AgentChangeSet"]
        .write(
            &mut tx,
            &Map::from_iter([
                ("credential_id".to_owned(), json!(credential)),
                ("request_id".to_owned(), json!(request_id.to_string())),
                ("request_hash".to_owned(), json!(request_hash)),
                ("reason".to_owned(), json!(reason)),
                ("operations".to_owned(), operations),
                ("changes".to_owned(), json!(changes)),
                ("results".to_owned(), json!(results)),
                (
                    "created_at".to_owned(),
                    json!(Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)),
                ),
                ("undoes_id".to_owned(), json!(undo)),
            ]),
            None,
        )
        .await?;
    let response = super::batch(&mut tx, endpoint, id).await?;
    tx.commit().await.map_err(journal::storage_error)?;
    state.search.invalidate_all();
    let mut seasons = std::collections::BTreeSet::new();
    for row in &changes {
        if matches!(
            row["model"].as_str(),
            Some(
                "core.TOTY"
                    | "core.SOTY"
                    | "core.COTY"
                    | "core.NOTY"
                    | "core.OnlineQUAL"
                    | "core.QUAL"
                    | "core.QualPoints"
                    | "core.Reaff"
                    | "core.TOTYReaff"
                    | "core.Tournament"
            )
        ) {
            seasons.extend(
                [&row["before"], &row["after"]]
                    .into_iter()
                    .filter_map(|record| record["season"].as_str()),
            );
        }
    }
    for season in seasons {
        crate::standings::invalidate(state, season, crate::standings::STORED_BOARDS).await;
    }
    if changes.iter().any(|row| {
        matches!(
            row["model"].as_str(),
            Some(
                "core.Tournament"
                    | "core.Debater"
                    | "core.School"
                    | "core.Team"
                    | "core.TeamResult"
                    | "core.SpeakerResult"
            )
        )
    }) {
        crate::recent_results::invalidate(state).await;
    }
    crate::elo::invalidate(state).await;
    Ok(response)
}

pub(super) async fn post(
    state: &AppState,
    endpoint: Endpoint<'_>,
    credential: i64,
    undo: Option<i64>,
    request: Request,
) -> Result<Response, ApiError> {
    if let Some(kind) = request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        && kind.split(';').next().map(str::trim) != Some("application/json")
    {
        return Ok(endpoint.render(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            json!({"detail":format!("Unsupported media type \"{kind}\" in request.")}),
        ));
    }
    let Ok(body) = to_bytes(request.into_body(), 2_621_440).await else {
        return Ok(endpoint.render(
            StatusCode::PAYLOAD_TOO_LARGE,
            json!({"detail":"Request body too large."}),
        ));
    };
    let payload = if body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice(&body) {
            Ok(payload) => payload,
            Err(error) => {
                return Ok(
                    endpoint.invalid(json!({"detail":format!("JSON parse error - {error}")}))
                );
            }
        }
    };
    complete(state, endpoint, credential, undo, payload).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn journal_undo_matches_legacy_zero_millisecond_timestamps() {
        let table = &TABLES["core.SyntheticResolutionLog"];
        let timestamp = "2026-11-27T15:25:41.000001Z";
        assert_eq!(
            catalog::datetime(timestamp, true),
            "2026-11-27T15:25:41.000Z"
        );
        assert_eq!(
            catalog::datetime(timestamp, false),
            "2026-11-27T15:25:41.000001Z"
        );

        let restored = json!({"created_at": "2026-11-27T15:25:41Z", "reason": "same"});
        let mut legacy = json!({"created_at": "2026-11-27T15:25:41.000Z", "reason": "same"});
        assert!(matches_journal_row(table, &restored, &legacy));
        assert!(matches_journal_row(table, &legacy, &restored));
        legacy["reason"] = json!("changed");
        assert!(!matches_journal_row(table, &restored, &legacy));
        legacy["reason"] = json!("same");
        legacy["created_at"] = json!("2026-11-27T15:25:41.001Z");
        assert!(!matches_journal_row(table, &restored, &legacy));
    }
}
