mod debater;
mod rankings;

use chrono::{SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe};

use super::{
    catalog::MODELS,
    journal::{Journal, TABLES},
    mutation::{Failure, erase, invalid, positive_id, read},
};
use crate::ApiError;

async fn rewrite(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    field: &str,
    source: i64,
    target: i64,
) -> Result<(), Failure> {
    if journal
        .related(conn, label, field, source)
        .await?
        .is_empty()
    {
        return Ok(());
    }
    let table = &TABLES[label];
    let column = &table.fields[field].column;
    let statement = format!(
        "UPDATE \"{}\" SET \"{column}\"=$1 WHERE \"{column}\"=$2",
        table.table
    );
    sqlx::query(AssertSqlSafe(statement))
        .bind(target)
        .bind(source)
        .execute(conn)
        .await?;
    Ok(())
}

async fn standings(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    field: &str,
    source: i64,
    target: i64,
) -> Result<(), Failure> {
    let table = &TABLES[label];
    let lookup = format!(
        "SELECT CAST(id AS BIGINT) FROM \"{}\" WHERE season=$1 AND \"{field}\"=$2 ORDER BY place,id LIMIT 1",
        table.table
    );
    for id in journal.related(conn, label, field, source).await? {
        let data = table.get(conn, id).await?;
        let season = data["season"].as_str().ok_or(ApiError::Serialization)?;
        let existing = sqlx::query_scalar::<_, i64>(AssertSqlSafe(lookup.clone()))
            .bind(season)
            .bind(target)
            .fetch_optional(&mut *conn)
            .await?;
        if let Some(existing) = existing {
            journal.watch(conn, label, existing).await?;
            let standing = table.get(conn, existing).await?;
            let points = data["points"]
                .as_f64()
                .unwrap_or_default()
                .max(standing["points"].as_f64().unwrap_or_default());
            table
                .write(
                    conn,
                    &Map::from_iter([("points".into(), json!(points))]),
                    Some(existing),
                )
                .await?;
            table.delete(conn, id).await?;
        } else {
            table
                .write(
                    conn,
                    &Map::from_iter([(field.into(), json!(target))]),
                    Some(id),
                )
                .await?;
        }
    }
    Ok(())
}

async fn team_snapshot(conn: &mut AnyConnection, source: &Value) -> Result<Value, ApiError> {
    let members=sqlx::query_scalar::<_,i64>("SELECT CAST(d.id AS BIGINT) FROM core_debater d
        JOIN core_team_debaters m ON m.debater_id=d.id WHERE m.team_id=$1 AND NOT d.temporary AND NOT d.synthetic ORDER BY d.id")
        .bind(source["id"].as_i64()).fetch_all(&mut *conn).await?;
    Ok(
        json!({"id":source["id"],"name":source["name"],"short_name":source["short_name"],"synthetic":source["synthetic"],
        "member_ids":members,"temporary_members":[]}),
    )
}

pub(super) struct Actor<'a> {
    pub(super) credential: i64,
    pub(super) reason: &'a str,
}

async fn log(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    entity: &str,
    source: &Value,
    target: &Value,
    snapshot: Value,
    actor: Actor<'_>,
) -> Result<(), Failure> {
    let owner = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(created_by_id AS BIGINT) FROM core_agentcredential WHERE id=$1",
    )
    .bind(actor.credential)
    .fetch_one(&mut *conn)
    .await?;
    let name = |record: &Value| {
        record["name"]
            .as_str()
            .unwrap_or_default()
            .chars()
            .take(255)
            .collect::<String>()
    };
    let label = "core.SyntheticResolutionLog";
    let fields = Map::from_iter([
        ("action".to_owned(), json!("resolved")),
        ("entity_type".to_owned(), json!(entity)),
        ("synthetic_id".to_owned(), source["id"].clone()),
        ("synthetic_name".to_owned(), json!(name(source))),
        ("resolved_to_id".to_owned(), target["id"].clone()),
        ("resolved_to_name".to_owned(), json!(name(target))),
        ("actor_id".to_owned(), json!(owner)),
        ("reason".to_owned(), json!(actor.reason)),
        (
            "source_context".to_owned(),
            json!({"agent_credential_id":actor.credential}),
        ),
        ("synthetic_snapshot".to_owned(), snapshot),
        (
            "created_at".to_owned(),
            json!(Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)),
        ),
    ]);
    let id = TABLES[label].write(conn, &fields, None).await?;
    journal.created(label, id);
    Ok(())
}

pub(super) async fn apply(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    operation: &Map<String, Value>,
    actor: Actor<'_>,
) -> Result<Value, Failure> {
    if !operation.keys().all(|key| {
        matches!(
            key.as_str(),
            "op" | "entity_type"
                | "source_id"
                | "target_id"
                | "source_revision"
                | "target_revision"
        )
    }) {
        return Err(invalid("Unknown operation fields."));
    }
    let entity = if operation.get("op").and_then(Value::as_str) == Some("merge_debaters") {
        "debater"
    } else {
        operation
            .get("entity_type")
            .and_then(Value::as_str)
            .unwrap_or_default()
    };
    let (resource, table) = match entity {
        "school" => ("schools", "core_school"),
        "team" => ("teams", "core_team"),
        "debater" => ("debaters", "core_debater"),
        _ => return Err(invalid("entity_type must be debater, school, or team.")),
    };
    let model = &MODELS[resource];
    let source_id = operation
        .get("source_id")
        .and_then(positive_id)
        .ok_or_else(|| invalid("Record not found."))?;
    let target_id = operation
        .get("target_id")
        .and_then(positive_id)
        .ok_or_else(|| invalid("Record not found."))?;
    let source = read(conn, model, source_id).await?;
    let target = read(conn, model, target_id).await?;
    if source_id == target_id {
        return Err(invalid("Cannot merge an identity into itself."));
    }
    for (key, row) in [("source_revision", &source), ("target_revision", &target)] {
        if operation.get(key) != row.get("revision") {
            return Err(Failure::Stale(
                json!({"model":model.label,"id":row["id"],"detail":"Missing or stale revision."}),
            ));
        }
    }
    let synthetic = operation.get("op").and_then(Value::as_str) == Some("merge_synthetic");
    if !synthetic {
        debater::merge(conn, journal, target_id, source_id).await?;
        return Ok(json!({"entity_type":entity,"source_id":source_id,"target_id":target_id}));
    }
    if source["synthetic"] != true {
        return Err(invalid(format!(
            "Synthetic source {entity} must have synthetic=True"
        )));
    }
    if target["synthetic"] == true {
        return Err(invalid(format!(
            "Target {entity} must have synthetic=False"
        )));
    }
    if entity == "debater" {
        let snapshot = debater::snapshot(conn, &source).await?;
        let mut source = source;
        let mut target = target;
        source["name"] = json!(debater::name(&source));
        target["name"] = json!(debater::name(&target));
        log(conn, journal, entity, &source, &target, snapshot, actor).await?;
        debater::merge(conn, journal, target_id, source_id).await?;
        return Ok(json!({"entity_type":entity,"source_id":source_id,"target_id":target_id}));
    }
    let snapshot = if entity == "school" {
        for (label, field) in [
            ("core.Debater", "school_id"),
            ("core.SchoolLookup", "school_id"),
            ("core.Tournament", "host_id"),
        ] {
            rewrite(conn, journal, label, field, source_id, target_id).await?;
        }
        standings(
            conn,
            journal,
            "core.COTY",
            "school_id",
            source_id,
            target_id,
        )
        .await?;
        json!({"id":source["id"],"name":source["name"],"short_name":source["short_name"],
            "temporary":source["temporary"],"synthetic":source["synthetic"]})
    } else {
        let snapshot = team_snapshot(conn, &source).await?;
        for (label, field) in [
            ("core.Round", "gov_id"),
            ("core.Round", "opp_id"),
            ("core.TeamResult", "team_id"),
            ("core.TOTYReaff", "old_team_id"),
            ("core.TOTYReaff", "new_team_id"),
        ] {
            rewrite(conn, journal, label, field, source_id, target_id).await?;
        }
        standings(conn, journal, "core.TOTY", "team_id", source_id, target_id).await?;
        snapshot
    };
    log(conn, journal, entity, &source, &target, snapshot, actor).await?;
    erase(conn, journal, table, source_id).await?;
    Ok(json!({"entity_type":entity,"source_id":source_id,"target_id":target_id}))
}
