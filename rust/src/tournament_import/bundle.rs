use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};

use super::{
    Error, as_id,
    identity::{self, Person},
    invalid, text,
};
use crate::{ApiError, AppState, agent::journal::TABLES, delete_relations};

const KIND: &str = "mittab_bundle";

#[derive(Default)]
pub(super) struct Summary {
    pub(super) created: usize,
    pub(super) updated: usize,
    pub(super) deleted: usize,
    pub(super) skipped: Vec<String>,
}

impl Summary {
    pub(super) fn flash(&self) -> (i32, String) {
        if self.skipped.is_empty() {
            return (
                25,
                format!(
                    "Mit-Tab bundle imported successfully. Created {} rounds, updated {} rounds, deleted {} stale rounds.",
                    self.created, self.updated, self.deleted
                ),
            );
        }
        let count = self.skipped.len();
        let noun = if count == 1 { "round" } else { "rounds" };
        let mut details = self
            .skipped
            .iter()
            .take(5)
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join(" ");
        if count > 5 {
            details.push_str(&format!(" {} more skipped.", count - 5));
        }
        (
            30,
            format!(
                "Skipped {count} {noun} with incomplete rosters. {details} Check the team rosters in MIT-Tab and import again."
            ),
        )
    }
}

pub(super) fn validate(document: &Value) -> Result<(), Error> {
    if !document.is_object() {
        return Err(invalid(
            "Mit-Tab bundle JSON must be an object at the top level.",
        ));
    }
    if as_id(&document["schema_version"]) != Some(1) {
        return Err(invalid("Unsupported Mit-Tab bundle schema version."));
    }
    if text(&document["source"]).trim() != "mit_tab_black_rod_bundle" {
        return Err(invalid("Uploaded JSON is not a supported Mit-Tab bundle."));
    }
    for field in ["schools", "debaters"] {
        if !document[field].is_array() {
            return Err(invalid(format!("Mit-Tab bundle '{field}' must be a list.")));
        }
    }
    if document["rounds"].as_array().is_none_or(Vec::is_empty) {
        return Err(invalid("Mit-Tab bundle must include at least one round."));
    }
    Ok(())
}

fn index<'a>(document: &'a Value, key: &str) -> Result<BTreeMap<i64, &'a Value>, Error> {
    let mut rows = BTreeMap::new();
    for row in document[key].as_array().into_iter().flatten() {
        if !row.is_object() {
            return Err(invalid(format!(
                "Mit-Tab bundle '{key}' rows must be objects."
            )));
        }
        let id = as_id(&row["id"])
            .ok_or_else(|| invalid(format!("Each Mit-Tab bundle '{key}' row requires an id.")))?;
        if rows.insert(id, row).is_some() {
            return Err(invalid(format!("Duplicate id '{id}' found in '{key}'.")));
        }
    }
    Ok(rows)
}

fn roster<'a>(
    round: &'a Value,
    side: &str,
    key: &str,
) -> Result<Vec<(&'a Value, &'a Value)>, String> {
    let payload = &round[side];
    if !payload.is_object() {
        return Err(format!("Round '{key}' is missing side '{side}'."));
    }
    let ids = payload["debater_ids"]
        .as_array()
        .ok_or_else(|| format!("Round '{key}' side '{side}' has no debater list."))?;
    if !matches!(ids.len(), 1 | 2) {
        return Err(format!(
            "Round '{key}' side '{side}' needs 1 or 2 debater IDs, but the export lists {}.",
            ids.len()
        ));
    }
    let names = payload["source_names"]
        .as_array()
        .filter(|names| names.len() == ids.len())
        .ok_or_else(|| {
            format!("Round '{key}' side '{side}' needs a source name for each debater ID.")
        })?;
    Ok(ids.iter().zip(names).collect())
}

struct Importer<'a> {
    document: &'a Value,
    tournament: i64,
    season: i32,
    source: i64,
    schools: BTreeMap<i64, &'a Value>,
    competitors: BTreeMap<i64, &'a Value>,
    people: BTreeMap<i64, Person>,
    judges: BTreeMap<String, i64>,
}

impl Importer<'_> {
    async fn side(
        &mut self,
        conn: &mut AnyConnection,
        roster: Vec<(&Value, &Value)>,
    ) -> Result<(i64, [Option<i64>; 2]), Error> {
        let mut members = Vec::new();
        let mut aliases = [None; 2];
        for (index, (id, name)) in roster.into_iter().enumerate() {
            let bundle_id = as_id(id)
                .ok_or_else(|| invalid(format!("Unknown bundle debater id: {}.", text(id))))?;
            if !self.people.contains_key(&bundle_id) {
                let payload = self
                    .competitors
                    .get(&bundle_id)
                    .ok_or_else(|| invalid(format!("Unknown bundle debater id: {bundle_id}.")))?;
                let school = as_id(&payload["school_id"])
                    .map(|id| {
                        self.schools
                            .get(&id)
                            .copied()
                            .ok_or_else(|| invalid(format!("Unknown bundle school id: {id}.")))
                    })
                    .transpose();
                let person =
                    identity::competitor(conn, self.tournament, payload, school, self.season)
                        .await?;
                self.people.insert(bundle_id, person);
            }
            let person = &self.people[&bundle_id];
            let name = text(name);
            let source_name = name.trim();
            let alias = identity::alias(
                conn,
                person.id,
                if source_name.is_empty() {
                    &person.name
                } else {
                    source_name
                },
            )
            .await?;
            members.push(person.id);
            aliases[index] = Some(alias);
        }
        Ok((identity::team(conn, &members).await?, aliases))
    }

    async fn judge_rows(
        &mut self,
        conn: &mut AnyConnection,
        round: &Value,
        key: &str,
    ) -> Result<Vec<(String, i64, bool)>, Error> {
        let payload = &round["judges"];
        if payload.is_null() || payload.as_str() == Some("") {
            return Ok(Vec::new());
        }
        let list = payload
            .as_array()
            .ok_or_else(|| invalid(format!("Round '{key}' judges must be a list.")))?;
        let mut judges = Vec::new();
        let mut chairs = 0;
        for judge in list {
            if !judge.is_object() {
                return Err(invalid(format!(
                    "Round '{key}' judge entries must be objects."
                )));
            }
            let name = text(&judge["original_name"]).trim().to_owned();
            if name.is_empty() {
                return Err(invalid(format!(
                    "Round '{key}' includes a judge without a name."
                )));
            }
            let chair = super::truthy(&judge["is_chair"]);
            chairs += usize::from(chair);
            if chairs > 1 {
                return Err(invalid(format!(
                    "Round '{key}' includes more than one chair judge."
                )));
            }
            if !self.judges.contains_key(&name) {
                let judge = identity::judge(conn, self.tournament, &name, self.season).await?;
                self.judges.insert(name.clone(), judge);
            }
            let alias = identity::alias(conn, self.judges[&name], &name).await?;
            judges.push((name, alias, chair));
        }
        Ok(judges)
    }

    async fn write(
        &mut self,
        conn: &mut AnyConnection,
        round: &Value,
        key: &str,
        gov: Vec<(&Value, &Value)>,
        opp: Vec<(&Value, &Value)>,
    ) -> Result<bool, Error> {
        let (gov, gov_aliases) = self.side(conn, gov).await?;
        let (opp, opp_aliases) = self.side(conn, opp).await?;
        let judges = self.judge_rows(conn, round, key).await?;
        let current = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_round WHERE tournament_id=$1 AND import_key=$2",
        )
        .bind(self.tournament)
        .bind(key)
        .fetch_optional(&mut *conn)
        .await?;
        let metadata = json!({"bundle_source":"mit_tab_black_rod_bundle","schema_version":1,
            "source_tournament_name":text(&self.document["tournament_name"]),
            "bundle_exported_at":text(&self.document["exported_at"])});
        let mut fields = Map::new();
        for (name, value) in [
            ("tournament_id", json!(self.tournament)),
            ("import_key", json!(key)),
            ("import_origin", json!(KIND)),
            ("gov_id", json!(gov)),
            ("opp_id", json!(opp)),
            (
                "round_number",
                json!(as_id(&round["round_number"]).unwrap_or_default()),
            ),
            ("stage", json!(text(&round["stage"]).trim())),
            (
                "division",
                json!(round["division"].as_str().filter(|s| !s.is_empty())),
            ),
            ("elim_size", json!(as_id(&round["elim_size"]))),
            ("round_label", json!(text(&round["label"]))),
            ("victor", json!(as_id(&round["victor"]).unwrap_or_default())),
            ("metadata", metadata),
        ] {
            fields.insert(name.to_owned(), value);
        }
        let id = TABLES["core.Round"].write(conn, &fields, current).await?;
        // A legacy bundle supplies outcomes and provenance and replaces previously attached score rows.
        sqlx::query("DELETE FROM core_roundstats WHERE round_id=$1")
            .bind(id)
            .execute(&mut *conn)
            .await?;
        let existing = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_importedroundmetadata WHERE round_id=$1",
        )
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?;
        let mut fields = Map::new();
        for (name, value) in [
            ("round_id", json!(id)),
            ("gov_1_alias_id", json!(gov_aliases[0])),
            ("gov_2_alias_id", json!(gov_aliases[1])),
            ("opp_1_alias_id", json!(opp_aliases[0])),
            ("opp_2_alias_id", json!(opp_aliases[1])),
            ("raw_result_code", json!(text(&round["victor"]))),
            ("raw_outcome_text", json!(text(&round["label"]))),
        ] {
            fields.insert(name.to_owned(), value);
        }
        let metadata = TABLES["core.ImportedRoundMetadata"]
            .write(conn, &fields, existing)
            .await?;
        sqlx::query(
            "DELETE FROM core_importedroundmetadata_sources WHERE importedroundmetadata_id=$1",
        )
        .bind(metadata)
        .execute(&mut *conn)
        .await?;
        sqlx::query("INSERT INTO core_importedroundmetadata_sources (importedroundmetadata_id,tournamentimport_id) VALUES ($1,$2)")
            .bind(metadata).bind(self.source).execute(&mut *conn).await?;
        sqlx::query("DELETE FROM core_importedroundjudge WHERE round_metadata_id=$1")
            .bind(metadata)
            .execute(&mut *conn)
            .await?;
        for (name, alias, chair) in judges {
            sqlx::query("INSERT INTO core_importedroundjudge (round_metadata_id,original_name,debater_alias_id,is_chair) VALUES ($1,$2,$3,$4)")
                .bind(metadata).bind(name).bind(alias).bind(chair).execute(&mut *conn).await?;
        }
        Ok(current.is_some())
    }
}

pub(super) async fn apply(
    state: &AppState,
    tournament: i64,
    document: &Value,
    file_name: &str,
    source_hash: &str,
) -> Result<Summary, Error> {
    validate(document)?;
    let mut tx = state.pool.begin().await?;
    let lock = sqlx::query("UPDATE core_tournament SET id=id WHERE id=$1")
        .bind(tournament)
        .execute(&mut *tx)
        .await?;
    if lock.rows_affected() != 1 {
        return Err(invalid("Tournament not found."));
    }
    if !super::ready(&mut tx, tournament).await? {
        return Err(invalid(
            "Results must be imported for this tournament before uploading a Mit-Tab bundle.",
        ));
    }
    let previous = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_tournamentimport
        WHERE tournament_id=$1 AND import_type='mittab_bundle' AND source_hash=$2",
    )
    .bind(tournament)
    .bind(source_hash)
    .fetch_optional(&mut *tx)
    .await?;
    if previous.is_some() {
        return Err(invalid(
            "This exact Mit-Tab bundle was already uploaded for the selected tournament.",
        ));
    }
    let source = TABLES["core.TournamentImport"]
        .write(
            &mut tx,
            &Map::from_iter([
                ("tournament_id".to_owned(), json!(tournament)),
                ("batch_id".to_owned(), Value::Null),
                ("import_type".to_owned(), json!(KIND)),
                ("original_file_name".to_owned(), json!(file_name)),
                ("source_hash".to_owned(), json!(source_hash)),
                ("imported_at".to_owned(), json!(identity::now())),
            ]),
            None,
        )
        .await?;
    let mut importer = Importer {
        document,
        tournament,
        season: state.current_season,
        source,
        schools: index(document, "schools")?,
        competitors: index(document, "debaters")?,
        people: BTreeMap::new(),
        judges: BTreeMap::new(),
    };
    let mut keys = BTreeSet::new();
    let mut summary = Summary::default();
    for round in document["rounds"].as_array().into_iter().flatten() {
        if !round.is_object() {
            return Err(invalid("Each round entry must be an object."));
        }
        let key = text(&round["import_key"]).trim().to_owned();
        if key.is_empty() {
            return Err(invalid("Each round entry requires an import_key."));
        }
        if !keys.insert(key.clone()) {
            return Err(invalid(format!("Duplicate round import key '{key}'.")));
        }
        if !matches!(text(&round["stage"]).trim(), "prelim" | "outround") {
            return Err(invalid(format!("Round '{key}' has an unsupported stage.")));
        }
        if !round["division"].is_null()
            && !matches!(round["division"].as_str(), Some("" | "varsity" | "novice"))
        {
            return Err(invalid(format!(
                "Round '{key}' has an unsupported division."
            )));
        }
        let sides = roster(round, "gov", &key)
            .and_then(|gov| roster(round, "opp", &key).map(|opp| (gov, opp)));
        match sides {
            Ok((gov, opp)) => {
                if importer.write(&mut tx, round, &key, gov, opp).await? {
                    summary.updated += 1;
                } else {
                    summary.created += 1;
                }
            }
            Err(message) => summary.skipped.push(message),
        }
    }
    if summary.created + summary.updated > 0 {
        let stale = sqlx::query(
            "SELECT DISTINCT CAST(r.id AS BIGINT) AS id,r.import_key FROM core_round r
            JOIN core_importedroundmetadata m ON m.round_id=r.id
            JOIN core_importedroundmetadata_sources s ON s.importedroundmetadata_id=m.id
            JOIN core_tournamentimport i ON i.id=s.tournamentimport_id
            WHERE r.tournament_id=$1 AND i.import_type='mittab_bundle' ORDER BY id",
        )
        .bind(tournament)
        .fetch_all(&mut *tx)
        .await?;
        for row in stale {
            if !keys.contains(row.try_get::<&str, _>("import_key")?) {
                delete_relations::delete(&mut tx, "core_round", row.try_get("id")?)
                    .await
                    .map_err(|error| match error {
                        delete_relations::DeleteError::Database(error) => Error::from(error),
                        _ => invalid("Could not delete the stale round."),
                    })?;
                summary.deleted += 1;
            }
        }
    }
    tx.commit().await.map_err(|_| ApiError::Database)?;
    Ok(summary)
}
