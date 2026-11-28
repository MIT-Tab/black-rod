use std::collections::{BTreeMap, HashSet};

use chrono::{SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use super::{
    super::{
        journal::{Journal, TABLES},
        mutation::{Failure, erase},
    },
    rankings, rewrite,
};
use crate::{ApiError, claims, tournament_import::results::standings::lock_season};

type Fields = Map<String, Value>;

async fn relatives(
    conn: &mut AnyConnection,
    primary: i64,
    secondary: i64,
) -> Result<Vec<String>, ApiError> {
    Ok(sqlx::query_scalar::<_,String>("SELECT season FROM (
        SELECT t.season FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
            JOIN core_team_debaters m ON m.team_id=r.team_id WHERE m.debater_id IN ($1,$2)
        UNION SELECT t.season FROM core_speakerresult r JOIN core_tournament t ON t.id=r.tournament_id WHERE r.debater_id IN ($1,$2)
        UNION SELECT t.season FROM core_tournamentautoqual r JOIN core_tournament t ON t.id=r.tournament_id WHERE r.debater_id IN ($1,$2)
        UNION SELECT season FROM core_qualpoints WHERE debater_id IN ($1,$2)
        UNION SELECT season FROM core_qual WHERE debater_id IN ($1,$2)
        UNION SELECT season FROM core_soty WHERE debater_id IN ($1,$2)
        UNION SELECT season FROM core_noty WHERE debater_id IN ($1,$2)
        UNION SELECT season FROM core_onlinequal WHERE debater_id IN ($1,$2)
        ) AS affected WHERE season<>'' ORDER BY season")
        .bind(primary).bind(secondary).fetch_all(conn).await?)
}

async fn round_stats(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<(), ApiError> {
    let label = "core.RoundStats";
    let table = &TABLES[label];
    let mut occupied = HashSet::new();
    let mut maximum = BTreeMap::<i64, i64>::new();
    let rows = sqlx::query(
        "SELECT CAST(round_id AS BIGINT) AS round_id,CAST(score_index AS BIGINT) AS score_index
        FROM core_roundstats WHERE debater_id=$1",
    )
    .bind(primary)
    .fetch_all(&mut *conn)
    .await?;
    for row in rows {
        let round = row.try_get("round_id")?;
        let score = row.try_get::<i64, _>("score_index")?;
        occupied.insert((round, score.max(1)));
        let current = maximum.entry(round).or_default();
        *current = (*current).max(score);
    }
    let rows=sqlx::query("SELECT CAST(id AS BIGINT) AS id,CAST(round_id AS BIGINT) AS round_id,
        CAST(score_index AS BIGINT) AS score_index FROM core_roundstats WHERE debater_id=$1 ORDER BY round_id,score_index,id")
        .bind(secondary).fetch_all(&mut *conn).await?;
    let mut next = BTreeMap::new();
    for row in rows {
        let id = row.try_get("id")?;
        let round = row.try_get("round_id")?;
        let original = row.try_get::<i64, _>("score_index")?;
        let desired = original.max(1);
        let score = if occupied.contains(&(round, desired)) {
            let slot = next
                .entry(round)
                .or_insert_with(|| maximum.get(&round).copied().unwrap_or_default() + 1);
            let result = *slot;
            *slot += 1;
            result
        } else {
            desired
        };
        occupied.insert((round, score));
        let current = maximum.entry(round).or_default();
        *current = (*current).max(score);
        journal.watch(conn, label, id).await?;
        table
            .write(
                conn,
                &Fields::from_iter([
                    ("debater_id".into(), json!(primary)),
                    ("score_index".into(), json!(score)),
                ]),
                Some(id),
            )
            .await?;
    }
    Ok(())
}

async fn aliases(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<(), Failure> {
    let label = "core.DebaterAlias";
    let table = &TABLES[label];
    for id in journal
        .related(conn, label, "debater_id", secondary)
        .await?
    {
        let data = table.get(conn, id).await?;
        let conflict = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT)
            FROM core_debateralias WHERE debater_id=$1 AND source_name=$2 ORDER BY id LIMIT 1",
        )
        .bind(primary)
        .bind(data["source_name"].as_str())
        .fetch_optional(&mut *conn)
        .await?;
        if let Some(other) = conflict {
            for field in [
                "gov_1_alias_id",
                "gov_2_alias_id",
                "opp_1_alias_id",
                "opp_2_alias_id",
            ] {
                rewrite(
                    conn,
                    journal,
                    "core.ImportedRoundMetadata",
                    field,
                    id,
                    other,
                )
                .await?;
            }
            rewrite(
                conn,
                journal,
                "core.ImportedRoundJudge",
                "debater_alias_id",
                id,
                other,
            )
            .await?;
            table.delete(conn, id).await?;
        } else {
            table
                .write(
                    conn,
                    &Fields::from_iter([("debater_id".into(), json!(primary))]),
                    Some(id),
                )
                .await?;
        }
    }
    Ok(())
}

async fn some_related(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    field: &str,
    parent: i64,
    debater: i64,
) -> Result<(), ApiError> {
    let table = &TABLES[label];
    let sql = format!(
        "SELECT CAST(id AS BIGINT) FROM \"{}\" WHERE \"{}\"=$1 AND debater_id=$2",
        table.table, table.fields[field].column
    );
    if sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
        .bind(parent)
        .bind(debater)
        .fetch_optional(&mut *conn)
        .await?
        .is_none()
    {
        let id = table
            .write(
                conn,
                &Fields::from_iter([
                    (field.into(), json!(parent)),
                    ("debater_id".into(), json!(debater)),
                ]),
                None,
            )
            .await?;
        journal.created(label, id);
    }
    Ok(())
}

async fn teams(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<(), ApiError> {
    let label = "core.Team_debaters";
    let table = &TABLES[label];
    for id in journal
        .related(conn, label, "debater_id", secondary)
        .await?
    {
        let row = table.get(conn, id).await?;
        let team = row["team_id"].as_i64().ok_or(ApiError::Serialization)?;
        journal.watch(conn, "core.Team", team).await?;
        some_related(conn, journal, label, "team_id", team, primary).await?;
        let hidden=sqlx::query_scalar::<_,i64>("SELECT CAST(h.teamresult_id AS BIGINT)
            FROM core_teamresult_hidden_from_debaters h JOIN core_teamresult r ON r.id=h.teamresult_id
            WHERE h.debater_id=$1 AND r.team_id=$2 ORDER BY r.id")
            .bind(secondary).bind(team).fetch_all(&mut *conn).await?;
        for result in hidden {
            some_related(
                conn,
                journal,
                "core.TeamResult_hidden_from_debaters",
                "teamresult_id",
                result,
                primary,
            )
            .await?;
        }
        table.delete(conn, id).await?;
        claims::edit::team_name(conn, team).await?;
    }
    Ok(())
}

async fn merged_points(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<(), ApiError> {
    let label = "core.QualPoints";
    let table = &TABLES[label];
    let seasons = sqlx::query_scalar::<_, String>(
        "SELECT DISTINCT season FROM core_qualpoints
        WHERE debater_id IN ($1,$2) AND season<>'' ORDER BY season",
    )
    .bind(primary)
    .bind(secondary)
    .fetch_all(&mut *conn)
    .await?;
    for season in seasons {
        let rows=sqlx::query("SELECT CAST(id AS BIGINT) AS id,CAST(debater_id AS BIGINT) AS owner,
            CAST(points AS DOUBLE PRECISION) AS points FROM core_qualpoints WHERE season=$1 AND debater_id IN ($2,$3)
            ORDER BY id").bind(&season).bind(primary).bind(secondary).fetch_all(&mut *conn).await?;
        let mut champion = None;
        let mut keep = None;
        let mut total = 0.0;
        let mut ids = Vec::new();
        for row in rows {
            let id = row.try_get("id")?;
            ids.push(id);
            keep.get_or_insert(id);
            if row.try_get::<i64, _>("owner")? == primary {
                champion.get_or_insert(id);
            }
            total += row.try_get::<f64, _>("points")?;
        }
        if let Some(winner) = champion.or(keep) {
            journal.watch(conn, label, winner).await?;
            table
                .write(
                    conn,
                    &Fields::from_iter([
                        ("points".into(), json!(total)),
                        ("debater_id".into(), json!(primary)),
                    ]),
                    Some(winner),
                )
                .await?;
            if !rankings::online_season(&season) {
                rankings::points_qualification(
                    conn,
                    journal,
                    primary,
                    &season,
                    total >= rankings::qual_bar(&season),
                )
                .await?;
            }
            for id in ids.into_iter().filter(|id| *id != winner) {
                journal.watch(conn, label, id).await?;
                table.delete(conn, id).await?;
            }
        }
    }
    Ok(())
}

async fn derived(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    primary: i64,
    secondary: i64,
) -> Result<(), ApiError> {
    let table = &TABLES[label];
    let qual = label == "core.QUAL";
    let qualifier = if qual { " AND qual_type=$3" } else { "" };
    let ranking = if qual { "id" } else { "place,id" };
    let sql = format!(
        "SELECT CAST(id AS BIGINT) FROM \"{}\" WHERE season=$1 AND debater_id=$2{qualifier} ORDER BY {ranking} LIMIT 1",
        table.table
    );
    for id in journal
        .related(conn, label, "debater_id", secondary)
        .await?
    {
        let row = table.get(conn, id).await?;
        let mut query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql.clone()))
            .bind(row["season"].as_str())
            .bind(primary);
        if qual {
            query = query.bind(row["qual_type"].as_i64());
        }
        if let Some(old) = query.fetch_optional(&mut *conn).await? {
            journal.watch(conn, label, old).await?;
            let other = table.get(conn, old).await?;
            let mut data = Fields::new();
            if qual && other["tournament_id"].is_null() && !row["tournament_id"].is_null() {
                data.insert("tournament_id".into(), row["tournament_id"].clone());
            }
            let place = row["place"].as_i64().unwrap_or(-1);
            let current = other["place"].as_i64().unwrap_or(-1);
            if place != -1 && (current == -1 || place < current) {
                data.insert("place".into(), json!(place));
            }
            let points = row["points"].as_f64().unwrap_or(-1.0);
            if (!qual || points != -1.0) && points > other["points"].as_f64().unwrap_or(-1.0) {
                data.insert("points".into(), json!(points));
            }
            if row["tied"] == true && other["tied"] != true {
                data.insert("tied".into(), json!(true));
            }
            if !data.is_empty() {
                table.write(conn, &data, Some(old)).await?;
            }
            table.delete(conn, id).await?;
        } else {
            table
                .write(
                    conn,
                    &Fields::from_iter([("debater_id".into(), json!(primary))]),
                    Some(id),
                )
                .await?;
        }
    }
    Ok(())
}

async fn profile(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<(), ApiError> {
    let label = "core.Debater";
    let table = &TABLES[label];
    let major = table.get(conn, primary).await?;
    let minor = table.get(conn, secondary).await?;
    let mut data = Fields::new();
    let mut merge_season = |key: &str, latest: bool| {
        let left = major[key].as_str().filter(|s| !s.is_empty());
        let right = minor[key].as_str().filter(|s| !s.is_empty());
        let merged = match (left, right) {
            (Some(a), Some(b)) if latest => Some(a.max(b)),
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        };
        if let Some(merged) = merged {
            data.insert(key.into(), json!(merged));
        }
    };
    merge_season("first_season", false);
    merge_season("latest_season", true);
    if major["school_id"].is_null() && !minor["school_id"].is_null() {
        data.insert("school_id".into(), minor["school_id"].clone());
    }
    let major_elo = major["elo_manual_opt"].as_str().unwrap_or_default().trim();
    let minor_elo = minor["elo_manual_opt"]
        .as_str()
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    if major_elo.is_empty() && !minor_elo.is_empty() {
        data.insert("elo_manual_opt".into(), json!(minor_elo));
    }
    for (key, fallback) in [("first_name", "Removed"), ("last_name", "")] {
        data.insert(
            key.into(),
            json!(claims::edit::sanitize_name(
                major[key].as_str().unwrap_or_default(),
                fallback
            )),
        );
    }
    let eligible = major["status"] == 2;
    if !eligible {
        data.insert("dino_judge_contact_opt_in".into(), json!(false));
    }
    if !(eligible && major["dino_judge_contact_opt_in"] == true
        || major["dino_to_contact_opt_in"] == true)
    {
        data.insert("region".into(), json!([]));
    }
    journal.watch(conn, label, primary).await?;
    table.write(conn, &data, Some(primary)).await?;
    let teams = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(team_id AS BIGINT) FROM core_team_debaters WHERE debater_id=$1",
    )
    .bind(primary)
    .fetch_all(&mut *conn)
    .await?;
    for team in teams {
        journal.watch(conn, "core.Team", team).await?;
    }
    claims::edit::team_names(conn, primary).await?;
    Ok(())
}

async fn finish(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<(), ApiError> {
    let label = "core.MergeDebaterRequest";
    let table = &TABLES[label];
    let pending=sqlx::query_scalar::<_,i64>("SELECT CAST(id AS BIGINT) FROM core_mergedebaterrequest
        WHERE status='pending' AND (primary_debater_id IN ($1,$2) OR secondary_debater_id IN ($1,$2))")
        .bind(primary).bind(secondary).fetch_all(&mut *conn).await?;
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true);
    for id in pending {
        journal.watch(conn, label, id).await?;
        table
            .write(
                conn,
                &Fields::from_iter([
                    ("status".into(), json!("denied")),
                    (
                        "denial_reason".into(),
                        json!(
                            "Auto-cancelled: One of these debaters was merged in another request."
                        ),
                    ),
                    ("processed_at".into(), json!(now)),
                    ("processed_by_id".into(), Value::Null),
                ]),
                Some(id),
            )
            .await?;
    }
    let label = "core.ImmaculateGridHiddenDebater";
    let existing = journal
        .related(conn, label, "debater_id", secondary)
        .await?;
    if !existing.is_empty()
        && journal
            .related(conn, label, "debater_id", primary)
            .await?
            .is_empty()
    {
        let id = TABLES[label]
            .write(
                conn,
                &Fields::from_iter([("debater_id".into(), json!(primary))]),
                None,
            )
            .await?;
        journal.created(label, id);
    }
    Ok(())
}

pub(super) async fn merge(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    primary: i64,
    secondary: i64,
) -> Result<Vec<String>, Failure> {
    let team_ids = sqlx::query_scalar::<_, i64>(
        "SELECT DISTINCT CAST(team_id AS BIGINT) FROM core_team_debaters
        WHERE debater_id IN ($1,$2) ORDER BY 1",
    )
    .bind(primary)
    .bind(secondary)
    .fetch_all(&mut *conn)
    .await?;
    let schools = sqlx::query_scalar::<_, i64>(
        "SELECT DISTINCT CAST(school_id AS BIGINT) FROM core_debater
        WHERE id IN ($1,$2) AND school_id IS NOT NULL ORDER BY 1",
    )
    .bind(primary)
    .bind(secondary)
    .fetch_all(&mut *conn)
    .await?;
    let seasons = relatives(conn, primary, secondary).await?;
    for season in &seasons {
        lock_season(conn, season).await?;
    }
    rewrite(
        conn,
        journal,
        "core.SpeakerResult",
        "debater_id",
        secondary,
        primary,
    )
    .await?;
    round_stats(conn, journal, primary, secondary).await?;
    aliases(conn, journal, primary, secondary).await?;
    merged_points(conn, journal, primary, secondary).await?;
    derived(conn, journal, "core.QUAL", primary, secondary).await?;
    for id in journal
        .related(conn, "core.TournamentAutoQual", "debater_id", secondary)
        .await?
    {
        let item = TABLES["core.TournamentAutoQual"].get(conn, id).await?;
        let tournament = item["tournament_id"]
            .as_i64()
            .ok_or(ApiError::Serialization)?;
        some_related(
            conn,
            journal,
            "core.TournamentAutoQual",
            "tournament_id",
            tournament,
            primary,
        )
        .await?;
    }
    for label in ["core.SOTY", "core.NOTY", "core.OnlineQUAL"] {
        derived(conn, journal, label, primary, secondary).await?;
    }
    for (label, field) in [
        ("core.Video", "pm_id"),
        ("core.Video", "mg_id"),
        ("core.Video", "lo_id"),
        ("core.Video", "mo_id"),
        ("core.Reaff", "old_debater_id"),
        ("core.Reaff", "new_debater_id"),
    ] {
        rewrite(conn, journal, label, field, secondary, primary).await?;
    }
    teams(conn, journal, primary, secondary).await?;
    profile(conn, journal, primary, secondary).await?;
    finish(conn, journal, primary, secondary).await?;
    erase(conn, journal, "core_debater", secondary).await?;
    rankings::recompute(conn, journal, primary, &team_ids, &schools, &seasons).await?;
    Ok(seasons)
}

pub(super) async fn snapshot(conn: &mut AnyConnection, row: &Value) -> Result<Value, ApiError> {
    let school = sqlx::query_scalar::<_, String>("SELECT name FROM core_school WHERE id=$1")
        .bind(row["school_id"].as_i64())
        .fetch_optional(conn)
        .await?
        .unwrap_or_default();
    Ok(
        json!({"id":row["id"],"name":name(row),"school_id":row["school_id"],"school_name":school,
        "temporary":row["temporary"],"synthetic":row["synthetic"]}),
    )
}

pub(super) fn name(row: &Value) -> String {
    [
        row["first_name"].as_str().unwrap_or_default(),
        row["last_name"].as_str().unwrap_or_default(),
    ]
    .join(" ")
    .trim()
    .to_owned()
}
