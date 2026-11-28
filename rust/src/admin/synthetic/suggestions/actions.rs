use axum::{
    Json,
    extract::Request,
    response::{IntoResponse, Response},
};
use chrono::{SecondsFormat, Utc};
use serde_json::{Value, json};
use sqlx::AssertSqlSafe;

use super::{Cache, PEOPLE, Person, schools};
use crate::{
    ApiError, AppState,
    admin::amendment,
    agent::{
        journal::TABLES,
        merge::{self, AuditActor},
        mutation::Failure,
    },
};

struct Action<'a> {
    action: &'a str,
    kind: &'a str,
    source: i64,
    target: i64,
    reason: &'a str,
    actor: Option<i64>,
}

struct Resolved {
    source_name: String,
    target_name: String,
    snapshot: Value,
}

impl Action<'_> {
    async fn labels(&self, state: &AppState) -> Result<Option<Resolved>, ApiError> {
        if self.kind == "debater" {
            let sql = format!(
                "{PEOPLE} WHERE (d.id=$1 AND d.synthetic) OR (d.id=$2 AND NOT d.temporary AND NOT d.synthetic)"
            );
            let rows = sqlx::query(AssertSqlSafe(sql))
                .bind(self.source)
                .bind(self.target)
                .fetch_all(&state.pool)
                .await?;
            let people = rows
                .iter()
                .map(Person::read)
                .collect::<Result<Vec<_>, _>>()?;
            let (Some(source), Some(target)) = (
                people.iter().find(|row| row.id == self.source),
                people.iter().find(|row| row.id == self.target),
            ) else {
                return Ok(None);
            };
            Ok(Some(Resolved {
                source_name: source.name.clone(),
                target_name: target.name.clone(),
                snapshot: json!({
                    "id":source.id,"name":source.name,"school_id":source.school_id,
                    "school_name":source.school.as_ref().map_or("",|school|school.name.as_str()),
                    "temporary":source.temporary,"synthetic":true
                }),
            }))
        } else {
            let source_ids = [self.source];
            let target_ids = [self.target];
            let (sources, targets) = tokio::try_join!(
                schools(&state.pool, true, &source_ids, false),
                schools(&state.pool, false, &target_ids, false)
            )?;
            let (Some(source), Some(target)) = (sources.first(), targets.first()) else {
                return Ok(None);
            };
            Ok(Some(Resolved {
                source_name: source.name.clone(),
                target_name: target.name.clone(),
                snapshot: json!({
                    "id":source.id,"name":source.name,"short_name":source.short_name,"temporary":source.temporary,"synthetic":true,
                }),
            }))
        }
    }
}

pub(super) async fn apply(
    state: &AppState,
    actor: Option<i64>,
    request: Request,
) -> Result<Response, ApiError> {
    let (_, _, body) = match super::super::form(state, request).await {
        Ok(values) => values,
        Err(error) => return Ok(*error),
    };
    let fields = body
        .into_iter()
        .collect::<std::collections::HashMap<_, _>>();
    let field = |name| fields.get(name).map_or("", |value| value.trim());
    let fallback = |name, default| {
        let value = field(name);
        if value.is_empty() { default } else { value }
    };
    let default = |name, legacy| {
        let value = field(name);
        if value.is_empty() {
            field(legacy)
        } else {
            value
        }
    };
    let failure = |error: &str| Json(json!({"success":false,"error":error})).into_response();
    let action = fallback("action", "resolve").to_lowercase();
    if !matches!(action.as_str(), "resolve" | "reject") {
        return Ok(failure("Unsupported synthetic suggestion action."));
    }
    let kind = fallback("entity_type", "debater").to_lowercase();
    if !matches!(kind.as_str(), "school" | "debater") {
        return Ok(failure("Unsupported synthetic entity type."));
    }
    let source = default("synthetic_id", "synthetic_debater");
    let target = default("target_id", "canonical_debater");
    if source.is_empty() || target.is_empty() {
        return Ok(failure("Missing resolution selection."));
    }
    let (Ok(source), Ok(target)) = (source.parse::<i64>(), target.parse::<i64>()) else {
        return Ok(failure("Invalid resolution selection."));
    };
    if source == target {
        return Ok(failure(
            "Synthetic and canonical records must be different.",
        ));
    }
    let fallback_reason = if action == "resolve" {
        format!("Suggested synthetic {kind} resolution")
    } else {
        format!("Rejected synthetic {kind} suggestion")
    };
    let input = Action {
        action: &action,
        kind: &kind,
        source,
        target,
        reason: fallback("reason", &fallback_reason),
        actor,
    };
    let Some(labels) = input.labels(state).await? else {
        return Ok(failure("Unable to find one of the selected records."));
    };
    let mut tx = state.pool.begin().await?;
    let context = json!({"source":"synthetic_resolution_suggestions"});
    let mut warning = None;
    if input.action == "resolve" {
        if let Err(error) = merge::resolve(
            &mut tx,
            input.kind,
            input.source,
            input.target,
            AuditActor {
                user: input.actor,
                reason: input.reason,
                context,
            },
        )
        .await
        {
            let error = match error {
                Failure::Invalid(message) => message,
                Failure::Stale(detail) => amendment::text(&detail["detail"]),
                Failure::Database(error) => amendment::Error::from(error).message(),
            };
            return Ok(failure(&error));
        }
        tx.commit().await?;
        amendment::invalidate(state).await;
        let event = json!({"type":"resolve_synthetic","entity_type":input.kind,"synthetic_id":input.source,
            "target_id":input.target,"reason":input.reason});
        if let Err(error) = amendment::record::append(&event).await {
            warning = Some(format!(
                "Synthetic entity resolved, but amendment recording failed: {error}"
            ));
        }
    } else {
        let update = if input.kind == "school" {
            "UPDATE core_school SET id=id WHERE id=$1"
        } else {
            "UPDATE core_debater SET id=id WHERE id=$1"
        };
        sqlx::query(update)
            .bind(input.source)
            .execute(&mut *tx)
            .await?;
        let exists=sqlx::query_scalar::<_,i64>("SELECT CAST(id AS BIGINT) FROM core_syntheticresolutionlog
            WHERE action='rejected' AND entity_type=$1 AND synthetic_id=$2 AND resolved_to_id=$3 LIMIT 1")
            .bind(input.kind).bind(input.source).bind(input.target).fetch_optional(&mut *tx).await?;
        if exists.is_none() {
            let fields = json!({
                "action":"rejected","entity_type":input.kind,"synthetic_id":input.source,"resolved_to_id":input.target,
                "synthetic_name":labels.source_name,"resolved_to_name":labels.target_name,"actor_id":actor,
                "reason":input.reason,"source_context":context,"synthetic_snapshot":labels.snapshot,
                "created_at":Utc::now().to_rfc3339_opts(SecondsFormat::Micros,true)
            });
            TABLES["core.SyntheticResolutionLog"]
                .write(&mut tx, fields.as_object().expect("resolution log"), None)
                .await?;
        }
        tx.commit().await?;
    }
    Cache::invalidate(state).await;
    let message = if input.action == "resolve" {
        format!(
            "Resolved synthetic {} {} into {}.",
            input.kind, labels.source_name, labels.target_name
        )
    } else {
        format!(
            "Rejected synthetic {} suggestion {} -> {}.",
            input.kind, labels.source_name, labels.target_name
        )
    };
    let mut response = json!({"success":true,"message":message,"action":input.action});
    if let Some(warning) = warning {
        response["warning"] = json!(warning);
    }
    Ok(Json(response).into_response())
}
