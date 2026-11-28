pub(crate) mod qualifications;

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use crate::{ApiError, agent::journal::TABLES, delete_relations, points};

const LABELS: [&str; 6] = ["one", "two", "three", "four", "five", "six"];

struct ScoredResult {
    event: i64,
    place: i64,
    teams: i64,
    ghost: bool,
    qual: bool,
    online: bool,
    kind: i64,
    bar: i64,
}

struct Participant {
    school: Option<i64>,
    latest: Option<String>,
    included: bool,
    manual: BTreeMap<i64, Option<i64>>,
    results: Vec<ScoredResult>,
}

impl Participant {
    fn from_row(row: &sqlx::any::AnyRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            school: row.try_get("school_id")?,
            latest: row.try_get("latest_season")?,
            included: row.try_get::<i64, _>("included")? == 1,
            manual: BTreeMap::new(),
            results: Vec::new(),
        })
    }
}

pub(super) fn bonus(kind: i64, season: &str) -> bool {
    year(season).unwrap_or_default() < 2026 || !matches!(kind, 1 | 2 | 3 | 5 | 6 | 16)
}

fn year(season: &str) -> Option<i64> {
    season.split('-').next()?.parse().ok()
}

pub(crate) async fn lock_season(conn: &mut AnyConnection, season: &str) -> Result<(), ApiError> {
    if conn.backend_name() == "PostgreSQL" {
        // Match Django's standings_write_lock so a staged cutover can share the database.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtext('black-rod standings'), hashtext($1))")
            .bind(season)
            .execute(conn)
            .await?;
    }
    Ok(())
}

pub(super) async fn rank(
    conn: &mut AnyConnection,
    table: &str,
    season: &str,
) -> Result<(), ApiError> {
    let statement = format!("DELETE FROM {table} WHERE season=$1 AND points=0");
    sqlx::query(AssertSqlSafe(statement))
        .bind(season)
        .execute(&mut *conn)
        .await?;
    let statement=format!("WITH ranking AS (
        SELECT id,RANK() OVER (ORDER BY points DESC) AS new_place,COUNT(*) OVER(PARTITION BY points)>1 AS new_tied
        FROM {table} WHERE season=$1
    ) UPDATE {table} SET place=(SELECT new_place FROM ranking WHERE ranking.id={table}.id),
    tied=(SELECT new_tied FROM ranking WHERE ranking.id={table}.id) WHERE id IN (SELECT id FROM ranking)");
    sqlx::query(AssertSqlSafe(statement))
        .bind(season)
        .execute(conn)
        .await?;
    Ok(())
}

type Fields = Map<String, Value>;

fn ranked(season: &str, entity: &str, id: i64, points: f64) -> Fields {
    Map::from_iter([
        ("season".to_owned(), json!(season)),
        (entity.to_owned(), json!(id)),
        ("points".to_owned(), json!(points)),
        ("place".to_owned(), json!(-1)),
        ("tied".to_owned(), json!(false)),
    ])
}

pub(super) async fn rebuild(conn: &mut AnyConnection, season: &str) -> Result<(), ApiError> {
    let online = matches!(season, "2020" | "2021");
    let mut people = BTreeMap::<i64, Participant>::new();
    for row in sqlx::query("SELECT CAST(d.id AS BIGINT) AS debater,CAST(d.school_id AS BIGINT) AS school_id,d.latest_season,
        CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included,
        CAST(t.id AS BIGINT) AS event,CAST(t.qual_type AS BIGINT) AS kind
        FROM core_tournamentautoqual a JOIN core_tournament t ON t.id=a.tournament_id
        JOIN core_debater d ON d.id=a.debater_id LEFT JOIN core_school s ON s.id=d.school_id
        WHERE t.season=$1 AND t.qual_type IN (1,2,3,4,5,6,7,8,9,10,15,16) ORDER BY a.id")
        .bind(season).fetch_all(&mut *conn).await? {
        let id=row.try_get("debater")?;
        let person=people.entry(id).or_insert(Participant::from_row(&row)?);
        person.manual.entry(row.try_get("kind")?).or_insert(Some(row.try_get("event")?));
    }
    for row in sqlx::query("SELECT CAST(d.id AS BIGINT) AS debater,CAST(d.school_id AS BIGINT) AS school_id,d.latest_season,
        CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included,
        CAST(CASE WHEN r.counts_for_points THEN 1 ELSE 0 END AS BIGINT) AS scoring,
        CAST(t.id AS BIGINT) AS event,CAST(t.qual_type AS BIGINT) AS kind,CAST(t.autoqual_bar AS BIGINT) AS bar,
        CAST(t.num_teams AS BIGINT) AS teams,CAST(r.place AS BIGINT) AS place,
        CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
        CAST(CASE WHEN t.qual THEN 1 ELSE 0 END AS BIGINT) AS qual,
        CAST(CASE WHEN t.online_qual_points THEN 1 ELSE 0 END AS BIGINT) AS online
        FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
        JOIN core_team_debaters m ON m.team_id=r.team_id JOIN core_debater d ON d.id=m.debater_id
        LEFT JOIN core_school s ON s.id=d.school_id
        WHERE t.season=$1 AND r.type_of_place=1 AND NOT(d.temporary OR d.synthetic) ORDER BY r.id,m.id")
        .bind(season).fetch_all(&mut *conn).await? {
        let id=row.try_get("debater")?;
        let person=people.entry(id).or_insert(Participant::from_row(&row)?);
        if row.try_get::<i64,_>("scoring")?==1 {
            person.results.push(ScoredResult{
                event:row.try_get("event")?,place:row.try_get("place")?,teams:row.try_get("teams")?,
                ghost:row.try_get::<i64,_>("ghost")?==1,qual:row.try_get::<i64,_>("qual")?==1,
                online:row.try_get::<i64,_>("online")?==1,kind:row.try_get("kind")?,bar:row.try_get("bar")?,
            });
        }
    }
    let mut qualifications = Vec::new();
    let mut points_rows = Vec::new();
    let mut online_rows = Vec::new();
    let mut latest_updates = Vec::new();
    let mut schools = BTreeMap::<i64, f64>::new();
    let mut awarded = BTreeSet::new();
    let standalone=sqlx::query("SELECT CAST(q.debater_id AS BIGINT) AS debater,CAST(q.qual_type AS BIGINT) AS kind,
        CAST(q.tournament_id AS BIGINT) AS event,CAST(q.place AS BIGINT) AS place,q.points,
        CAST(CASE WHEN q.tied THEN 1 ELSE 0 END AS BIGINT) AS tied, CAST(d.school_id AS BIGINT) AS school_id,
        CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included
        FROM core_qual q JOIN core_debater d ON d.id=q.debater_id LEFT JOIN core_school s ON s.id=d.school_id
        WHERE q.season=$1 AND NOT EXISTS(SELECT 1 FROM core_qualpoints p WHERE p.season=q.season AND p.debater_id=q.debater_id)
        AND NOT EXISTS(SELECT 1 FROM core_onlinequal o WHERE o.season=q.season AND o.debater_id=q.debater_id) ORDER BY q.id")
        .bind(season).fetch_all(&mut *conn).await?;
    for row in standalone {
        let id = row.try_get("debater")?;
        if people.contains_key(&id) {
            continue;
        }
        if !online
            && row.try_get::<i64, _>("included")? == 1
            && bonus(row.try_get("kind")?, season)
            && let Some(school) = row.try_get::<Option<i64>, _>("school_id")?
            && awarded.insert(id)
        {
            *schools.entry(school).or_default() += 6.0;
        }
        let mut values = ranked(season, "debater_id", id, row.try_get("points")?);
        values.insert(
            "qual_type".to_owned(),
            json!(row.try_get::<i64, _>("kind")?),
        );
        values.insert(
            "tournament_id".to_owned(),
            json!(row.try_get::<Option<i64>, _>("event")?),
        );
        values.insert("place".to_owned(), json!(row.try_get::<i64, _>("place")?));
        values.insert(
            "tied".to_owned(),
            json!(row.try_get::<i64, _>("tied")? != 0),
        );
        qualifications.push(values);
    }
    for table in [
        "core_qual",
        "core_qualpoints",
        "core_onlinequal",
        "core_coty",
    ] {
        let statement = format!("DELETE FROM {table} WHERE season=$1");
        sqlx::query(AssertSqlSafe(statement))
            .bind(season)
            .execute(&mut *conn)
            .await?;
    }
    for (id, person) in people {
        if person.latest.as_deref().and_then(year) < year(season) {
            latest_updates.push(id);
        }
        if !person.included {
            continue;
        }
        let mut autoquals = person.manual;
        let mut total = 0.0;
        let mut online_markers = Vec::new();
        for result in person.results {
            if result.kind != 8 && result.place > 0 && result.place <= result.bar {
                autoquals.entry(result.kind).or_insert(Some(result.event));
            }
            if result.qual {
                total += points::team_points_for_size(result.teams, result.place, result.ghost);
            }
            online_markers.push((
                if result.online {
                    points::online_points(result.place)
                } else {
                    0.0
                },
                result.event,
            ));
        }
        if total > 0.0 {
            let fields = Fields::from_iter([
                ("season".to_owned(), json!(season)),
                ("debater_id".to_owned(), json!(id)),
                ("points".to_owned(), json!(total)),
            ]);
            points_rows.push(fields);
        }
        online_markers.sort_by(|left, right| right.0.total_cmp(&left.0));
        let online_total = online_markers
            .iter()
            .take(6)
            .map(|(value, _)| value)
            .sum::<f64>();
        if !online_markers.is_empty() {
            let mut fields = ranked(season, "debater_id", id, online_total);
            for (index, name) in LABELS.iter().enumerate() {
                let (value, event) = online_markers
                    .get(index)
                    .map_or((0.0, None), |(value, event)| (*value, Some(*event)));
                fields.insert(format!("marker_{name}"), json!(value));
                fields.insert(format!("tournament_{name}_id"), json!(event));
            }
            online_rows.push(fields);
        }
        if if online {
            online_total >= 10.0
        } else {
            total >= 12.0
        } {
            autoquals.entry(0).or_insert(None);
        }
        if !online && let Some(school) = person.school {
            let bonus = if autoquals.keys().any(|&kind| bonus(kind, season)) {
                6.0
            } else {
                0.0
            };
            *schools.entry(school).or_default() += total.min(60.0) + bonus;
        }
        for (kind, event) in autoquals {
            let mut values = ranked(season, "debater_id", id, -1.0);
            values.insert("qual_type".to_owned(), json!(kind));
            values.insert("tournament_id".to_owned(), json!(event));
            qualifications.push(values);
        }
    }
    let school_rows = schools
        .into_iter()
        .filter(|(_, total)| *total > 0.0)
        .map(|(school, total)| ranked(season, "school_id", school, total))
        .collect::<Vec<_>>();
    for (table, rows) in [
        ("core.QUAL", qualifications),
        ("core.QualPoints", points_rows),
        ("core.OnlineQUAL", online_rows),
        ("core.COTY", school_rows),
    ] {
        TABLES[table].insert_many(conn, &rows).await?;
    }
    for chunk in latest_updates.chunks(500) {
        let ids = chunk
            .iter()
            .map(i64::to_string)
            .collect::<Vec<_>>()
            .join(",");
        let statement = format!("UPDATE core_debater SET latest_season=$1 WHERE id IN ({ids})");
        sqlx::query(AssertSqlSafe(statement))
            .bind(season)
            .execute(&mut *conn)
            .await?;
    }
    rank(conn, "core_coty", season).await?;
    rank(conn, "core_onlinequal", season).await?;
    Ok(())
}

#[derive(Clone, Copy)]
pub(super) enum Award {
    Team,
    Speaker,
    Novice,
}

impl Award {
    fn binding(self) -> (&'static str, &'static str, &'static str, &'static str) {
        match self {
            Self::Team => ("core.TOTY", "core_toty", "team_id", "core_teamresult"),
            Self::Speaker => ("core.SOTY", "core_soty", "debater_id", "core_speakerresult"),
            Self::Novice => ("core.NOTY", "core_noty", "debater_id", "core_speakerresult"),
        }
    }
}

pub(crate) async fn recompute_rankings(
    conn: &mut AnyConnection,
    selected: &str,
    ranking: &str,
    current: i32,
) -> Result<(), ApiError> {
    lock_season(conn, selected).await?;
    if ranking == "coty" {
        return qualifications::recompute(conn, selected, selected == current.to_string()).await;
    }
    let (kind, orphan_query, all_query, entity) = if ranking == "toty" {
        (
            Award::Team,
            "SELECT CAST(id AS BIGINT) FROM core_team t WHERE NOT EXISTS (
                SELECT 1 FROM core_teamresult r WHERE r.team_id=t.id) ORDER BY id",
            "SELECT CAST(id AS BIGINT) FROM core_team ORDER BY id",
            "core_team",
        )
    } else {
        let kind = if ranking == "soty" {
            Award::Speaker
        } else {
            Award::Novice
        };
        (kind,
            "SELECT CAST(id AS BIGINT) FROM core_debater d WHERE NOT (d.temporary OR d.synthetic)
            AND NOT EXISTS (SELECT 1 FROM core_speakerresult r WHERE r.debater_id=d.id)
            AND NOT EXISTS (SELECT 1 FROM core_tournamentautoqual a WHERE a.debater_id=d.id)
            AND NOT EXISTS (
                SELECT 1 FROM core_team_debaters m WHERE m.debater_id=d.id AND (
                    EXISTS (SELECT 1 FROM core_teamresult r WHERE r.team_id=m.team_id)
                    OR EXISTS (SELECT 1 FROM core_round r WHERE r.gov_id=m.team_id OR r.opp_id=m.team_id)
                )
            ) ORDER BY id",
            "SELECT CAST(id AS BIGINT) FROM core_debater WHERE NOT (temporary OR synthetic) ORDER BY id",
            "core_debater")
    };
    if matches!(kind, Award::Novice) && year(selected).is_none_or(|value| value > 2020) {
        return Ok(());
    }
    for id in sqlx::query_scalar::<_, i64>(orphan_query)
        .fetch_all(&mut *conn)
        .await?
    {
        match delete_relations::delete(conn, entity, id).await {
            Ok(()) | Err(delete_relations::DeleteError::Absent) => (),
            Err(delete_relations::DeleteError::Protected) => return Err(ApiError::Constraint),
            Err(delete_relations::DeleteError::Database(error)) => return Err(error.into()),
        }
    }
    let ids = sqlx::query_scalar::<_, i64>(all_query)
        .fetch_all(&mut *conn)
        .await?
        .into_iter()
        .collect();
    let season = if matches!(kind, Award::Team) {
        selected
    } else {
        selected.split('-').next().unwrap_or(selected)
    };
    write_awards(conn, season, kind, &ids).await?;
    if !matches!(kind, Award::Novice) {
        rank(conn, kind.binding().1, selected).await?;
    }
    Ok(())
}

pub(super) async fn awards(
    conn: &mut AnyConnection,
    season: &str,
    kind: Award,
    ids: &BTreeSet<i64>,
) -> Result<(), ApiError> {
    let (_, table, owner, source) = kind.binding();
    if !ids.is_empty() {
        let affected = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
        let delete = format!(
            "DELETE FROM {table} WHERE season=$1 AND {owner} IN ({affected})
             AND NOT EXISTS (SELECT 1 FROM {source} result WHERE result.{owner}={table}.{owner})"
        );
        sqlx::query(AssertSqlSafe(delete))
            .bind(season)
            .execute(&mut *conn)
            .await?;
        if !matches!(kind, Award::Novice) || year(season).is_some_and(|value| value <= 2020) {
            let owners = format!(
                "SELECT DISTINCT CAST({owner} AS BIGINT) FROM {source} WHERE {owner} IN ({affected})"
            );
            let active = sqlx::query_scalar::<_, i64>(AssertSqlSafe(owners))
                .fetch_all(&mut *conn)
                .await?
                .into_iter()
                .collect();
            write_awards(conn, season, kind, &active).await?;
        }
    }
    rank(conn, table, season).await
}

async fn write_awards(
    conn: &mut AnyConnection,
    season: &str,
    kind: Award,
    ids: &BTreeSet<i64>,
) -> Result<(), ApiError> {
    let (label, table, owner, source) = kind.binding();
    let team = matches!(kind, Award::Team);
    let novice = matches!(kind, Award::Novice);
    let (reaff, old, new) = if team {
        ("core_totyreaff", "old_team_id", "new_team_id")
    } else {
        ("core_reaff", "old_debater_id", "new_debater_id")
    };
    let filters = match kind {
        Award::Team => "t.toty AND r.type_of_place=1 AND r.counts_for_points",
        Award::Speaker => "t.soty AND r.type_of_place=1 AND r.counts_for_points",
        Award::Novice => "t.noty AND r.type_of_place=0",
    };
    let extra = match kind {
        Award::Team => "r.ghost_points",
        Award::Speaker => "r.tie",
        Award::Novice => "FALSE",
    };
    let statement = format!(
        "SELECT CAST(t.id AS BIGINT) AS id,CAST(t.num_teams AS BIGINT) AS teams,
        CAST(t.num_novice_debaters AS BIGINT) AS novices,CAST(r.place AS BIGINT) AS place,
        CAST(CASE WHEN {extra} THEN 1 ELSE 0 END AS BIGINT) AS detail
        FROM {source} r JOIN core_tournament t ON t.id=r.tournament_id WHERE t.season=$1
        AND (r.{owner}=$2 OR r.{owner}=$3) AND {filters} ORDER BY r.id"
    );
    for &id in ids {
        let (included, school) = if team {
            let roster=sqlx::query("SELECT CAST(d.school_id AS BIGINT) AS school_id,
                CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included
                FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id LEFT JOIN core_school s ON s.id=d.school_id
                WHERE m.team_id=$1 AND NOT(d.synthetic OR d.temporary) ORDER BY d.id")
                .bind(id).fetch_all(&mut *conn).await?;
            let Some(first) = roster.first() else {
                continue;
            };
            let school = first.try_get::<Option<i64>, _>("school_id")?;
            let schools = roster
                .iter()
                .map(|row| row.try_get::<Option<i64>, _>("school_id"))
                .collect::<Result<BTreeSet<_>, _>>()?;
            if schools.len() == 2 {
                continue;
            }
            (first.try_get::<i64, _>("included")? != 0, school)
        } else {
            let row = sqlx::query(
                "SELECT CAST(d.school_id AS BIGINT) AS school_id,
                CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included
                FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id WHERE d.id=$1",
            )
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
            let Some(row) = row else { continue };
            (
                row.try_get::<i64, _>("included")? != 0,
                row.try_get("school_id")?,
            )
        };
        let outgoing =
            format!("SELECT CAST(id AS BIGINT) FROM {reaff} WHERE season=$1 AND {old}=$2 LIMIT 1");
        let reassigned = !novice
            && sqlx::query_scalar::<_, i64>(AssertSqlSafe(outgoing))
                .bind(season)
                .bind(id)
                .fetch_optional(&mut *conn)
                .await?
                .is_some();
        if team && reassigned {
            sqlx::query("DELETE FROM core_toty WHERE season=$1 AND team_id=$2")
                .bind(season)
                .bind(id)
                .execute(&mut *conn)
                .await?;
            continue;
        }
        if team && !included {
            sqlx::query(
                "DELETE FROM core_toty WHERE season=$1 AND team_id IN (
                SELECT m.team_id FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id
                WHERE d.school_id=$2 OR (d.school_id IS NULL AND CAST($2 AS BIGINT) IS NULL)
            )",
            )
            .bind(season)
            .bind(school)
            .execute(&mut *conn)
            .await?;
            continue;
        }
        let incoming = format!(
            "SELECT CAST({old} AS BIGINT) FROM {reaff} WHERE season=$1 AND {new}=$2 ORDER BY id LIMIT 1"
        );
        let alternate = if novice {
            None
        } else {
            sqlx::query_scalar::<_, i64>(AssertSqlSafe(incoming))
                .bind(season)
                .bind(id)
                .fetch_optional(&mut *conn)
                .await?
        };
        let mut markers = Vec::new();
        if included && !reassigned {
            for row in sqlx::query(AssertSqlSafe(statement.clone()))
                .bind(season)
                .bind(id)
                .bind(alternate)
                .fetch_all(&mut *conn)
                .await?
            {
                let detail = row.try_get::<i64, _>("detail")?;
                let place = row.try_get::<i64, _>("place")?;
                let value = match kind {
                    Award::Team => {
                        points::team_points_for_size(row.try_get("teams")?, place, detail != 0)
                    }
                    Award::Speaker => {
                        points::speaker_points_for_size(row.try_get("teams")?, place - detail)
                    }
                    Award::Novice => points::novice_points_for_size(row.try_get("novices")?, place),
                };
                markers.push((value, row.try_get::<i64, _>("id")?));
            }
        }
        let existing = format!(
            "SELECT CAST(id AS BIGINT) FROM {table} WHERE season=$1 AND {owner}=$2 ORDER BY place,id LIMIT 1"
        );
        let existing = sqlx::query_scalar::<_, i64>(AssertSqlSafe(existing))
            .bind(season)
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
        if markers.is_empty() {
            if let Some(id) = existing {
                let delete = format!("DELETE FROM {table} WHERE id=$1");
                sqlx::query(AssertSqlSafe(delete))
                    .bind(id)
                    .execute(&mut *conn)
                    .await?;
            }
            continue;
        }
        markers.sort_by(|left, right| right.0.total_cmp(&left.0));
        let slots = if team || novice { 5 } else { 6 };
        let points = markers
            .iter()
            .take(slots)
            .map(|(value, _)| value)
            .sum::<f64>();
        let mut values = if existing.is_none() {
            ranked(season, owner, id, points)
        } else {
            Fields::from_iter([("points".to_owned(), json!(points))])
        };
        for (index, name) in LABELS.iter().enumerate() {
            if let Some((score, event)) = markers.get(index).filter(|_| index < slots) {
                values.insert(format!("marker_{name}"), json!(score));
                values.insert(format!("tournament_{name}_id"), json!(event));
            } else if existing.is_none() {
                values.insert(format!("marker_{name}"), json!(0.0));
                values.insert(format!("tournament_{name}_id"), Value::Null);
            }
        }
        TABLES[label].write(conn, &values, existing).await?;
    }
    Ok(())
}

pub(crate) async fn refresh_current_awards(
    conn: &mut AnyConnection,
    current: i32,
) -> Result<(), ApiError> {
    let season = current.to_string();
    for name in sqlx::query_scalar::<_, String>("SELECT name FROM core_team ORDER BY id")
        .fetch_all(&mut *conn)
        .await?
    {
        println!("Updating {name}");
    }
    recompute_rankings(conn, &season, "toty", current).await?;
    for name in sqlx::query_scalar::<_, String>("SELECT name FROM core_team ORDER BY id")
        .fetch_all(&mut *conn)
        .await?
    {
        println!("Updating {name}");
    }
    let people = sqlx::query_scalar::<_, i64>(
        "SELECT DISTINCT CAST(m.debater_id AS BIGINT) FROM core_team_debaters m
         JOIN core_debater d ON d.id=m.debater_id
         WHERE NOT (d.temporary OR d.synthetic)
         AND EXISTS (SELECT 1 FROM core_teamresult r WHERE r.team_id=m.team_id)",
    )
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .collect();
    qualifications::recompute_team_members(conn, &season, &people).await?;
    let print_people = |names: Vec<String>| {
        for name in names {
            println!("Updating {}", name.trim());
        }
    };
    let members = "SELECT first_name || ' ' || last_name FROM core_debater
        WHERE NOT (temporary OR synthetic) ORDER BY id";
    print_people(sqlx::query_scalar(members).fetch_all(&mut *conn).await?);
    recompute_rankings(conn, &season, "soty", current).await?;
    print_people(sqlx::query_scalar(members).fetch_all(&mut *conn).await?);
    recompute_rankings(conn, &season, "noty", current).await?;
    for (kind, table) in [
        ("TOTY", "core_toty"),
        ("NOTY", "core_noty"),
        ("COTY", "core_coty"),
        ("SOTY", "core_soty"),
    ] {
        println!("Ranking {kind}");
        rank(conn, table, &season).await?;
    }
    Ok(())
}

pub(crate) async fn rerank_current_awards(
    conn: &mut AnyConnection,
    current: i32,
) -> Result<(), ApiError> {
    let season = current.to_string();
    lock_season(conn, &season).await?;
    for table in ["core_toty", "core_coty", "core_soty"] {
        rank(conn, table, &season).await?;
    }
    Ok(())
}
