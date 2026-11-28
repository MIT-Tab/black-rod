use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};

use super::{Error, Summary, erase, integer, invalid, text};
use crate::{ApiError, agent::journal::TABLES, tournament_import::identity};

pub(in crate::admin) const SLOTS: [(&str, &str); 4] = [
    ("gov_1", "gov_1_alias_id"),
    ("gov_2", "gov_2_alias_id"),
    ("opp_1", "opp_1_alias_id"),
    ("opp_2", "opp_2_alias_id"),
];

type Fields = Map<String, Value>;

fn optional_id(value: &Value, field: &str) -> Result<Option<i64>, Error> {
    if value.is_null() {
        return Ok(None);
    }
    crate::django_int(value)
        .map(Some)
        .ok_or_else(|| invalid(format!("'{field}' must be an integer.")))
}

pub(super) async fn resolve(conn: &mut AnyConnection, action: &Value) -> Result<i64, Error> {
    let supplied_id = action
        .get("round_id")
        .or_else(|| action.get("id"))
        .unwrap_or(&Value::Null);
    if let Some(id) = optional_id(supplied_id, "round_id")? {
        let exists =
            sqlx::query_scalar::<_, i64>("SELECT CAST(id AS BIGINT) FROM core_round WHERE id=$1")
                .bind(id)
                .fetch_optional(conn)
                .await?;
        return exists.ok_or_else(|| invalid("Round matching query does not exist."));
    }
    let key = text(&action["import_key"]);
    let key = key.trim();
    if key.is_empty() {
        return Err(invalid(
            "Round actions require either 'id'/'round_id' or 'import_key'.",
        ));
    }
    let rows = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_round WHERE import_key=$1
        AND (CAST($2 AS BIGINT) IS NULL OR tournament_id=$2) LIMIT 2",
    )
    .bind(key)
    .bind(optional_id(&action["tournament_id"], "tournament_id")?)
    .fetch_all(conn)
    .await?;
    match rows.as_slice() {
        [id] => Ok(*id),
        [] => Err(invalid(format!("No round found for import_key '{key}'."))),
        _ => Err(invalid(format!(
            "Multiple rounds matched import_key '{key}'; include tournament_id."
        ))),
    }
}

async fn person(conn: &mut AnyConnection, id: i64) -> Result<String, Error> {
    sqlx::query_scalar::<_, String>(
        "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(conn)
    .await?
    .ok_or_else(|| invalid("Debater matching query does not exist."))
}

async fn team(conn: &mut AnyConnection, action: &Value, side: &str) -> Result<Option<i64>, Error> {
    let nested = &action[side];
    if !nested.is_null() && !nested.is_object() {
        return Err(invalid(format!(
            "'{side}' must be an object when provided."
        )));
    }
    let field = format!("{side}_team_id");
    let supplied = action
        .get(&field)
        .filter(|value| !value.is_null())
        .unwrap_or(&nested["team_id"]);
    if let Some(id) = optional_id(supplied, &field)? {
        let row =
            sqlx::query_scalar::<_, i64>("SELECT CAST(id AS BIGINT) FROM core_team WHERE id=$1")
                .bind(id)
                .fetch_optional(conn)
                .await?;
        return row
            .map(Some)
            .ok_or_else(|| invalid("Team matching query does not exist."));
    }
    let people = action
        .get(format!("{side}_debater_ids"))
        .filter(|raw| !raw.is_null())
        .unwrap_or(&nested["debater_ids"]);
    if people.is_null() {
        return Ok(None);
    }
    let list = people
        .as_array()
        .filter(|values| matches!(values.len(), 1 | 2))
        .ok_or_else(|| {
            invalid(format!(
                "'{side}_debater_ids' must contain one or two debater IDs."
            ))
        })?;
    let mut roster = Vec::new();
    for row in list {
        let id = integer(row).ok_or_else(|| invalid("Invalid debater ID."))?;
        person(conn, id).await?;
        roster.push(id);
    }
    Ok(Some(
        identity::team(conn, &roster).await.map_err(Error::import)?,
    ))
}

pub(super) fn object(value: &Value, name: &str) -> Result<Value, Error> {
    if value.is_null() {
        return Ok(json!({}));
    }
    if !value.is_object() {
        return Err(invalid(format!("{name} must be an object.")));
    }
    Ok(value.clone())
}

async fn alias(conn: &mut AnyConnection, payload: &Value, season: i32) -> Result<i64, Error> {
    if let Some(id) = integer(&payload["alias_id"]) {
        let value = TABLES["core.DebaterAlias"].get(conn, id).await?;
        return (!value.is_null())
            .then_some(id)
            .ok_or_else(|| invalid("DebaterAlias matching query does not exist."));
    }
    let source = text(&payload["source_name"]).trim().to_owned();
    let debater = if let Some(id) = integer(&payload["debater_id"]) {
        id
    } else {
        if !super::boolean(&payload["create_synthetic"]) {
            return Err(invalid(
                "Alias payloads require either alias_id, debater_id, or create_synthetic=true.",
            ));
        }
        if source.is_empty() {
            return Err(invalid(
                "Synthetic alias payloads require a source_name when debater_id is omitted.",
            ));
        }
        let mut first = text(&payload["first_name"]).trim().to_owned();
        let mut last = text(&payload["last_name"]).trim().to_owned();
        if first.is_empty() {
            let mut words = source.split_whitespace();
            first = words.next().unwrap_or_default().to_owned();
            let remainder = words.collect::<Vec<_>>().join(" ");
            if !remainder.is_empty() {
                last = remainder;
            }
        }
        let rows = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(d.id AS BIGINT) FROM core_debateralias a
            JOIN core_debater d ON d.id=a.debater_id WHERE a.source_name=$1 AND d.synthetic
            AND d.first_name=$2 AND d.last_name=$3 LIMIT 2",
        )
        .bind(&source)
        .bind(&first)
        .bind(&last)
        .fetch_all(&mut *conn)
        .await?;
        match rows.as_slice() {
            [id] => *id,
            [] => {
                identity::synthetic(
                    conn,
                    &format!("{first} {last}"),
                    integer(&payload["school_id"]),
                    season,
                )
                .await
                .map_err(Error::import)?
                .id
            }
            _ => {
                return Err(invalid(format!(
                    "Multiple synthetic aliases already exist for source_name '{source}'. Specify debater_id explicitly."
                )));
            }
        }
    };
    let name = person(conn, debater).await?;
    identity::alias(
        conn,
        debater,
        if source.is_empty() { &name } else { &source },
    )
    .await
    .map_err(Error::import)
}

async fn provenance(
    conn: &mut AnyConnection,
    round: i64,
    event: i64,
    stage: &str,
    payload: &Value,
    season: i32,
) -> Result<(BTreeMap<&'static str, i64>, [Option<i64>; 2]), Error> {
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_importedroundmetadata WHERE round_id=$1",
    )
    .bind(round)
    .fetch_optional(&mut *conn)
    .await?;
    if payload.is_null() {
        if let Some(id) = existing {
            erase(conn, "core_importedroundmetadata", id).await?;
        }
        return Ok((BTreeMap::new(), [None; 2]));
    }
    if !payload.is_object() {
        return Err(invalid(
            "Imported round metadata must be an object or null.",
        ));
    }
    let mut fields = Fields::new();
    if existing.is_none() {
        fields.extend([
            ("round_id".to_owned(), json!(round)),
            ("raw_result_code".to_owned(), json!("")),
            ("raw_outcome_text".to_owned(), json!("")),
        ]);
    }
    let mut refs = BTreeMap::new();
    for (slot, field) in SLOTS {
        if let Some(row) = payload.get(slot) {
            let id = if row.is_null() {
                None
            } else if row.is_object() {
                Some(alias(conn, row, season).await?)
            } else {
                return Err(invalid(format!(
                    "'{slot}' metadata must be an object or null."
                )));
            };
            fields.insert(field.to_owned(), json!(id));
        }
    }
    for key in ["raw_result_code", "raw_outcome_text"] {
        if let Some(value) = payload.get(key) {
            fields.insert(key.to_owned(), json!(text(value)));
        }
    }
    let metadata = if fields.is_empty() {
        existing.ok_or(ApiError::Serialization)?
    } else {
        TABLES["core.ImportedRoundMetadata"]
            .write(conn, &fields, existing)
            .await?
    };
    let resolved = TABLES["core.ImportedRoundMetadata"]
        .get(conn, metadata)
        .await?;
    let mut roles = BTreeMap::new();
    for (slot, field) in SLOTS {
        if let Some(alias) = integer(&resolved[field]) {
            let row = TABLES["core.DebaterAlias"].get(conn, alias).await?;
            if let Some(id) = integer(&row["debater_id"]) {
                refs.insert(slot, id);
                let role = text(&payload[slot]["role"]);
                if !role.trim().is_empty() {
                    roles.insert(id, role.trim().to_owned());
                }
            }
        }
    }
    if stage != "outround" {
        for (id, role) in roles {
            sqlx::query("UPDATE core_roundstats SET debater_role=$1 WHERE round_id=$2 AND debater_id=$3
                AND (debater_role IS NULL OR debater_role='') AND NOT EXISTS(SELECT 1 FROM core_roundstats s
                    WHERE s.round_id=$2 AND s.debater_id=$3 AND s.debater_role IS NOT NULL AND s.debater_role<>'')")
                .bind(role).bind(round).bind(id).execute(&mut *conn).await?;
        }
    }
    if let Some(sources) = payload.get("source_import_ids") {
        let rows = super::array(sources, "'source_import_ids' must be a list.")?;
        let mut ids = BTreeSet::new();
        for row in rows {
            let id = integer(row).ok_or_else(|| {
                invalid("One or more source_import_ids did not match a tournament import.")
            })?;
            let target = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(tournament_id AS BIGINT) FROM core_tournamentimport WHERE id=$1",
            )
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?
            .ok_or_else(|| {
                invalid("One or more source_import_ids did not match a tournament import.")
            })?;
            if target != event {
                return Err(invalid(format!(
                    "Imported metadata sources must belong to the same tournament as the round. Mismatched import ids: [{id}]."
                )));
            }
            ids.insert(id);
        }
        let old = sqlx::query(
            "SELECT CAST(id AS BIGINT) AS id,CAST(tournamentimport_id AS BIGINT) AS provenance
            FROM core_importedroundmetadata_sources WHERE importedroundmetadata_id=$1",
        )
        .bind(metadata)
        .fetch_all(&mut *conn)
        .await?;
        for row in old {
            if !ids.remove(&row.try_get::<i64, _>("provenance")?) {
                sqlx::query("DELETE FROM core_importedroundmetadata_sources WHERE id=$1")
                    .bind(row.try_get::<i64, _>("id")?)
                    .execute(&mut *conn)
                    .await?;
            }
        }
        for id in ids {
            sqlx::query("INSERT INTO core_importedroundmetadata_sources (importedroundmetadata_id,tournamentimport_id) VALUES($1,$2)")
                .bind(metadata).bind(id).execute(&mut *conn).await?;
        }
    }
    if let Some(judges) = payload.get("judges") {
        let rows = super::array(judges, "'judges' must be a list.")?;
        if rows.iter().any(|row| !row.is_object()) {
            return Err(invalid("Each judge entry must be an object."));
        }
        if rows
            .iter()
            .filter(|row| super::boolean(&row["is_chair"]))
            .count()
            > 1
        {
            return Err(invalid(
                "Imported metadata cannot have more than one chair judge.",
            ));
        }
        sqlx::query("DELETE FROM core_importedroundjudge WHERE round_metadata_id=$1")
            .bind(metadata)
            .execute(&mut *conn)
            .await?;
        for row in rows {
            let name = text(if super::truthy(&row["original_name"]) {
                &row["original_name"]
            } else {
                &row["source_name"]
            })
            .trim()
            .to_owned();
            if name.is_empty() {
                return Err(invalid("Each judge entry requires an original_name."));
            }
            let id = if !row["alias_id"].is_null() || !row["debater_id"].is_null() {
                let mut payload = row.clone();
                if !super::truthy(&payload["source_name"]) {
                    payload["source_name"] = json!(name);
                }
                Some(alias(conn, &payload, season).await?)
            } else {
                None
            };
            sqlx::query("INSERT INTO core_importedroundjudge (round_metadata_id,original_name,debater_alias_id,is_chair) VALUES($1,$2,$3,$4)")
                .bind(metadata).bind(&name).bind(id).bind(super::boolean(&row["is_chair"])).execute(&mut *conn).await?;
        }
    }
    let mut teams = [None; 2];
    for (index, pair) in [["gov_1", "gov_2"], ["opp_1", "opp_2"]]
        .into_iter()
        .enumerate()
    {
        if pair.iter().any(|slot| payload.get(slot).is_some())
            && let Some((left, right)) = refs.get(pair[0]).zip(refs.get(pair[1]))
        {
            teams[index] = Some(
                identity::team(conn, &[*left, *right])
                    .await
                    .map_err(Error::import)?,
            );
        }
    }
    Ok((refs, teams))
}

async fn loaded_refs(
    conn: &mut AnyConnection,
    id: i64,
) -> Result<BTreeMap<&'static str, i64>, Error> {
    let aliases=sqlx::query("SELECT CAST(g1.debater_id AS BIGINT) AS gov_1,CAST(g2.debater_id AS BIGINT) AS gov_2,
        CAST(o1.debater_id AS BIGINT) AS opp_1,CAST(o2.debater_id AS BIGINT) AS opp_2
        FROM core_importedroundmetadata m LEFT JOIN core_debateralias g1 ON g1.id=m.gov_1_alias_id
        LEFT JOIN core_debateralias g2 ON g2.id=m.gov_2_alias_id LEFT JOIN core_debateralias o1 ON o1.id=m.opp_1_alias_id
        LEFT JOIN core_debateralias o2 ON o2.id=m.opp_2_alias_id WHERE m.round_id=$1")
        .bind(id).fetch_optional(conn).await?;
    let mut slots = BTreeMap::new();
    if let Some(row) = aliases {
        for (slot, _) in SLOTS {
            if let Some(id) = row.try_get::<Option<i64>, _>(slot)? {
                slots.insert(slot, id);
            }
        }
    }
    Ok(slots)
}

async fn stats(
    conn: &mut AnyConnection,
    id: i64,
    stage: &str,
    payload: &Value,
    slots: &BTreeMap<&str, i64>,
) -> Result<(), Error> {
    let list = super::array(payload, "Round stats must be provided as a list.")?;
    sqlx::query("DELETE FROM core_roundstats WHERE round_id=$1")
        .bind(id)
        .execute(&mut *conn)
        .await?;
    let mut seen = BTreeSet::new();
    for row in list {
        if !row.is_object() {
            return Err(invalid("Each round stat entry must be an object."));
        }
        let person_id = if let Some(id) = integer(&row["debater_id"]) {
            id
        } else {
            let slot = text(&row["slot_ref"]);
            let slot = slot.trim();
            if slot.is_empty() {
                return Err(invalid(
                    "Each round stat entry requires either debater_id or slot_ref.",
                ));
            }
            if !SLOTS.iter().any(|(key, _)| *key == slot) {
                return Err(invalid(format!("Unsupported round stat slot_ref: {slot}.")));
            }
            slots.get(slot).copied().ok_or_else(|| {
                invalid(format!(
                    "Round stat slot_ref '{slot}' could not be resolved to an imported alias."
                ))
            })?
        };
        person(conn, person_id).await?;
        let index = integer(&row["score_index"])
            .filter(|&i| i != 0)
            .unwrap_or(1);
        if !seen.insert((person_id, index)) {
            return Err(invalid(format!(
                "Duplicate round stat payload for debater_id={person_id}, score_index={index}."
            )));
        }
        let prelim = stage != "outround";
        let role = text(&row["debater_role"]);
        let role = role.trim();
        let fields = Fields::from_iter([
            ("round_id".into(), json!(id)),
            ("debater_id".into(), json!(person_id)),
            ("stage".into(), json!(stage)),
            ("score_index".into(), json!(index)),
            ("source_status".into(), json!(text(&row["source_status"]))),
            (
                "metadata".into(),
                object(&row["metadata"], "stat metadata")?,
            ),
            (
                "debater_role".into(),
                if prelim && !role.is_empty() {
                    json!(role)
                } else {
                    Value::Null
                },
            ),
            (
                "speaks".into(),
                if prelim {
                    row["speaks"].clone()
                } else {
                    Value::Null
                },
            ),
            (
                "ranks".into(),
                if prelim {
                    row["ranks"].clone()
                } else {
                    Value::Null
                },
            ),
        ]);
        TABLES["core.RoundStats"].write(conn, &fields, None).await?;
    }
    Ok(())
}

pub(super) async fn save(
    conn: &mut AnyConnection,
    action: &Value,
    created: bool,
    season: i32,
    summary: &mut Summary,
) -> Result<i64, Error> {
    let id = if created {
        None
    } else {
        Some(resolve(conn, action).await?)
    };
    let mut fields = Fields::new();
    let previous = if let Some(id) = id {
        TABLES["core.Round"].get(conn, id).await?
    } else {
        Value::Null
    };
    if created {
        fields.extend([
            ("stage".into(), json!("prelim")),
            ("round_number".into(), json!(0)),
            ("victor".into(), json!(0)),
            ("division".into(), Value::Null),
            ("elim_size".into(), Value::Null),
            ("metadata".into(), json!({})),
            ("round_label".into(), json!("")),
            ("import_origin".into(), json!("")),
            ("import_key".into(), json!("")),
        ]);
    }
    if let Some(event) = optional_id(&action["tournament_id"], "tournament_id")? {
        let found = sqlx::query_scalar::<_, i64>(
            "UPDATE core_tournament SET id=id WHERE id=$1 RETURNING CAST(id AS BIGINT)",
        )
        .bind(event)
        .fetch_optional(&mut *conn)
        .await?
        .is_some();
        if !found {
            return Err(invalid("Tournament matching query does not exist."));
        }
        fields.insert("tournament_id".into(), json!(event));
    } else if created {
        return Err(invalid("'tournament_id' is required."));
    }
    for side in ["gov", "opp"] {
        if let Some(id) = team(conn, action, side).await? {
            fields.insert(format!("{side}_id"), json!(id));
        } else if created {
            return Err(invalid(
                "Round creation requires gov and opp team identifiers.",
            ));
        }
    }
    for key in ["stage", "division"] {
        if let Some(value) = action.get(key) {
            let value = text(value);
            if !(key == "stage" && matches!(value.as_str(), "prelim" | "outround")
                || key == "division" && matches!(value.as_str(), "" | "varsity" | "novice"))
            {
                return Err(invalid(format!("Unsupported {key}: {value}.")));
            }
            fields.insert(
                key.into(),
                if value.is_empty() {
                    Value::Null
                } else {
                    json!(value)
                },
            );
        }
    }
    for key in ["round_number", "victor", "elim_size"] {
        if let Some(value) = action.get(key) {
            let parsed = if key == "round_number" && !super::truthy(value) {
                Some(0)
            } else if key == "elim_size" && (value.is_null() || value.as_str() == Some("")) {
                None
            } else {
                Some(
                    crate::django_int(value)
                        .ok_or_else(|| invalid(format!("'{key}' must be an integer.")))?,
                )
            };
            fields.insert(key.into(), json!(parsed));
        }
    }
    for key in ["round_label", "import_origin", "import_key"] {
        if let Some(value) = action.get(key) {
            fields.insert(key.into(), json!(text(value)));
        }
    }
    if let Some(data) = action.get("metadata") {
        fields.insert("metadata".into(), object(data, "metadata")?);
    }
    let stage = fields
        .get("stage")
        .unwrap_or(&previous["stage"])
        .as_str()
        .ok_or(ApiError::Serialization)?
        .to_owned();
    let round = if fields.is_empty() {
        id.ok_or(ApiError::Serialization)?
    } else {
        TABLES["core.Round"].write(conn, &fields, id).await?
    };
    if !created && previous["stage"].as_str() != Some(&stage) {
        sqlx::query("UPDATE core_roundstats SET stage=$1,speaks=CASE WHEN $1='outround' THEN NULL ELSE speaks END,
            ranks=CASE WHEN $1='outround' THEN NULL ELSE ranks END,
            debater_role=CASE WHEN $1='outround' THEN NULL ELSE debater_role END WHERE round_id=$2")
            .bind(&stage).bind(round).execute(&mut *conn).await?;
    }
    let event = fields
        .get("tournament_id")
        .unwrap_or(&previous["tournament_id"])
        .as_i64()
        .ok_or(ApiError::Serialization)?;
    let slots = if let Some(data) = action.get("imported_metadata") {
        let (slots, teams) = provenance(conn, round, event, &stage, data, season).await?;
        for (key, value) in ["gov_id", "opp_id"].into_iter().zip(teams) {
            if let Some(id) = value {
                TABLES["core.Round"]
                    .write(
                        conn,
                        &Fields::from_iter([(key.into(), json!(id))]),
                        Some(round),
                    )
                    .await?;
            }
        }
        slots
    } else if action.get("stats").is_some() {
        loaded_refs(conn, round).await?
    } else {
        BTreeMap::new()
    };
    if created || action.get("stats").is_some() {
        stats(conn, round, &stage, &action["stats"], &slots).await?;
    }
    if created {
        summary.rounds_created += 1;
    } else {
        summary.rounds_updated += 1;
    }
    Ok(round)
}
