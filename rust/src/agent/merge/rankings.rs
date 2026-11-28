use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use super::super::journal::{Journal, TABLES};
use crate::{ApiError, points};

const MARKERS: [&str; 6] = ["one", "two", "three", "four", "five", "six"];

pub(super) fn online_season(season: &str) -> bool {
    matches!(season.split('-').next(), Some("2020" | "2021"))
}

pub(super) fn qual_bar(season: &str) -> f64 {
    points::historical_qualification_bar(season.split('-').next().unwrap_or_default())
        .unwrap_or(12.0)
}

#[derive(Clone, Copy)]
enum Benefit {
    Toty,
    Soty,
    Noty,
    Online,
}

impl Benefit {
    fn binding(self) -> (&'static str, &'static str) {
        match self {
            Self::Toty => ("core.TOTY", "team_id"),
            Self::Soty => ("core.SOTY", "debater_id"),
            Self::Noty => ("core.NOTY", "debater_id"),
            Self::Online => ("core.OnlineQUAL", "debater_id"),
        }
    }
}

type Fields = Map<String, Value>;

async fn placement(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    season: &str,
) -> Result<(), ApiError> {
    let table = &TABLES[label];
    let statement = format!("SELECT CAST(id AS BIGINT) AS id,CAST(place AS BIGINT) AS place,
        CAST(CASE WHEN tied THEN 1 ELSE 0 END AS BIGINT) AS tied,CAST(points AS DOUBLE PRECISION) AS points,
        CAST(RANK() OVER (ORDER BY points DESC) AS BIGINT) AS expected_place,
        CAST(COUNT(*) OVER (PARTITION BY points) AS BIGINT) AS group_size
        FROM \"{}\" WHERE season=$1 AND points<>0", table.table);
    let zeros = format!(
        "SELECT CAST(id AS BIGINT) FROM \"{}\" WHERE season=$1 AND points=0",
        table.table
    );
    for id in sqlx::query_scalar::<_, i64>(AssertSqlSafe(zeros))
        .bind(season)
        .fetch_all(&mut *conn)
        .await?
    {
        journal.watch(conn, label, id).await?;
        table.delete(conn, id).await?;
    }
    for row in sqlx::query(AssertSqlSafe(statement))
        .bind(season)
        .fetch_all(&mut *conn)
        .await?
    {
        let tied = row.try_get::<i64, _>("group_size")? > 1;
        let place = row.try_get::<i64, _>("expected_place")?;
        if (row.try_get::<i64, _>("tied")? != 0) != tied || row.try_get::<i64, _>("place")? != place
        {
            let id = row.try_get("id")?;
            journal.watch(conn, label, id).await?;
            table
                .write(
                    conn,
                    &Fields::from_iter([
                        ("place".into(), json!(place)),
                        ("tied".into(), json!(tied)),
                    ]),
                    Some(id),
                )
                .await?;
        }
    }
    Ok(())
}

async fn find(
    conn: &mut AnyConnection,
    label: &str,
    field: &str,
    season: &str,
    owner: i64,
) -> Result<Option<i64>, ApiError> {
    let table = &TABLES[label];
    let order = if table.fields.contains_key("place") {
        "place,id"
    } else {
        "id"
    };
    let statement = format!(
        "SELECT CAST(id AS BIGINT) FROM \"{}\" WHERE season=$1 AND \"{field}\"=$2 ORDER BY {order} LIMIT 1",
        table.table
    );
    Ok(sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
        .bind(season)
        .bind(owner)
        .fetch_optional(conn)
        .await?)
}

async fn upsert(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    mut fields: Fields,
    id: Option<i64>,
) -> Result<i64, ApiError> {
    if let Some(id) = id {
        journal.watch(conn, label, id).await?;
    } else if TABLES[label].fields.contains_key("place") {
        fields.entry("place").or_insert(json!(-1));
        fields.entry("tied").or_insert(json!(false));
    }
    let saved = TABLES[label].write(conn, &fields, id).await?;
    if id.is_none() {
        journal.created(label, saved);
    }
    Ok(saved)
}

async fn discard(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    label: &'static str,
    id: Option<i64>,
) -> Result<(), ApiError> {
    if let Some(id) = id {
        journal.watch(conn, label, id).await?;
        TABLES[label].delete(conn, id).await?;
    }
    Ok(())
}

pub(super) async fn points_qualification(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    debater: i64,
    season: &str,
    qualified: bool,
) -> Result<(), ApiError> {
    let label = "core.QUAL";
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_qual
        WHERE season=$1 AND debater_id=$2 AND qual_type=0",
    )
    .bind(season)
    .bind(debater)
    .fetch_optional(&mut *conn)
    .await?;
    if qualified {
        if existing.is_none() {
            qualification(conn, journal, debater, season, 0, None, false).await?;
        }
    } else {
        discard(conn, journal, label, existing).await?;
    }
    Ok(())
}

async fn qualification(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    debater: i64,
    season: &str,
    kind: i64,
    tournament: Option<i64>,
    fill_existing: bool,
) -> Result<(), ApiError> {
    let label = "core.QUAL";
    let fields = Fields::from_iter([
        ("debater_id".into(), json!(debater)),
        ("season".into(), json!(season)),
        ("qual_type".into(), json!(kind)),
        ("tournament_id".into(), json!(tournament)),
        ("points".into(), json!(-1.0)),
    ]);
    let existing = sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id,CAST(tournament_id AS BIGINT) AS tournament_id
        FROM core_qual WHERE season=$1 AND debater_id=$2 AND qual_type=$3",
    )
    .bind(season)
    .bind(debater)
    .bind(kind)
    .fetch_optional(&mut *conn)
    .await?;
    if let Some(old) = existing {
        if fill_existing
            && tournament.is_some()
            && old.try_get::<Option<i64>, _>("tournament_id")?.is_none()
        {
            upsert(
                conn,
                journal,
                label,
                Fields::from_iter([("tournament_id".into(), json!(tournament))]),
                Some(old.try_get("id")?),
            )
            .await?;
        }
    } else {
        upsert(conn, journal, label, fields, None).await?;
    }
    Ok(())
}

async fn qualifying_points(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    debater: i64,
    season: &str,
    included: bool,
) -> Result<(), ApiError> {
    let label = "core.QualPoints";
    let existing = find(conn, label, "debater_id", season, debater).await?;
    if !included {
        discard(conn, journal, label, existing).await?;
        if !online_season(season) {
            points_qualification(conn, journal, debater, season, false).await?;
        }
        return Ok(());
    }
    let manual = sqlx::query("SELECT CAST(t.id AS BIGINT) AS id,CAST(t.qual_type AS BIGINT) AS qual_type
        FROM core_tournamentautoqual a JOIN core_tournament t ON a.tournament_id=t.id
        WHERE t.season=$1 AND a.debater_id=$2 AND t.qual_type IN (1,2,3,4,5,6,7,8,9,10,15,16) ORDER BY a.id")
        .bind(season).bind(debater).fetch_all(&mut *conn).await?;
    for row in manual {
        qualification(
            conn,
            journal,
            debater,
            season,
            row.try_get("qual_type")?,
            Some(row.try_get("id")?),
            false,
        )
        .await?;
    }
    let results=sqlx::query("SELECT CAST(t.id AS BIGINT) AS id,CAST(t.qual_type AS BIGINT) AS qual_type,
        CAST(t.autoqual_bar AS BIGINT) AS bar,CAST(t.num_teams AS BIGINT) AS teams,CAST(r.place AS BIGINT) AS place,
        CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
        CAST(CASE WHEN r.counts_for_points THEN 1 ELSE 0 END AS BIGINT) AS score,
        CAST(CASE WHEN t.qual THEN 1 ELSE 0 END AS BIGINT) AS qual
        FROM core_teamresult r JOIN core_tournament t ON r.tournament_id=t.id
        JOIN core_team_debaters m ON m.team_id=r.team_id
        WHERE t.season=$1 AND m.debater_id=$2 AND r.type_of_place=1 ORDER BY r.id")
        .bind(season).bind(debater).fetch_all(&mut *conn).await?;
    if results.is_empty() {
        return Ok(());
    }
    let mut total = 0.0;
    for row in results {
        if row.try_get::<i64, _>("score")? == 0 {
            continue;
        }
        let place = row.try_get::<i64, _>("place")?;
        if place != -1 && place <= row.try_get::<i64, _>("bar")? {
            qualification(
                conn,
                journal,
                debater,
                season,
                row.try_get("qual_type")?,
                Some(row.try_get("id")?),
                true,
            )
            .await?;
        }
        if row.try_get::<i64, _>("qual")? == 1 {
            total += points::team_points_for_size(
                row.try_get("teams")?,
                place,
                row.try_get::<i64, _>("ghost")? == 1,
            );
        }
    }
    if total > 0.0 {
        upsert(
            conn,
            journal,
            label,
            Fields::from_iter([
                ("season".into(), json!(season)),
                ("debater_id".into(), json!(debater)),
                ("points".into(), json!(total)),
            ]),
            existing,
        )
        .await?;
    } else {
        discard(conn, journal, label, existing).await?;
    }
    if !online_season(season) {
        points_qualification(conn, journal, debater, season, total >= qual_bar(season)).await?;
    }
    Ok(())
}

async fn reaffiliated(
    conn: &mut AnyConnection,
    season: &str,
    owner: i64,
    team: bool,
) -> Result<Option<i64>, ApiError> {
    let statement = if team {
        "SELECT CAST(old_team_id AS BIGINT) FROM core_totyreaff WHERE season=$1 AND new_team_id=$2 ORDER BY id LIMIT 1"
    } else {
        "SELECT CAST(old_debater_id AS BIGINT) FROM core_reaff WHERE season=$1 AND new_debater_id=$2 ORDER BY id LIMIT 1"
    };
    Ok(sqlx::query_scalar::<_, i64>(statement)
        .bind(season)
        .bind(owner)
        .fetch_optional(conn)
        .await?)
}

async fn markers(
    conn: &mut AnyConnection,
    kind: Benefit,
    season: &str,
    id: i64,
) -> Result<Vec<(f64, i64)>, ApiError> {
    let (table, owner, filter) = match kind {
        Benefit::Toty => (
            "core_teamresult",
            "(r.team_id=$2 OR r.team_id=$3)",
            "t.toty AND r.type_of_place=1 AND r.counts_for_points",
        ),
        Benefit::Soty => (
            "core_speakerresult",
            "(r.debater_id=$2 OR r.debater_id=$3)",
            "t.soty AND r.type_of_place=1 AND r.counts_for_points",
        ),
        Benefit::Noty => (
            "core_speakerresult",
            "r.debater_id=$2 AND ($3 IS NULL OR r.debater_id=$3)",
            "t.noty AND r.type_of_place=0",
        ),
        Benefit::Online => (
            "core_teamresult",
            "(EXISTS (SELECT 1 FROM core_team_debaters m WHERE m.team_id=r.team_id AND m.debater_id=$2)) AND CAST($3 AS BIGINT) IS NULL",
            "r.type_of_place=1 AND r.counts_for_points",
        ),
    };
    let detail = match kind {
        Benefit::Toty => "r.ghost_points",
        Benefit::Soty => "r.tie",
        Benefit::Noty => "FALSE",
        Benefit::Online => "t.online_qual_points",
    };
    let old = match kind {
        Benefit::Toty | Benefit::Soty => {
            reaffiliated(conn, season, id, matches!(kind, Benefit::Toty)).await?
        }
        _ => None,
    };
    let sql = format!(
        "SELECT CAST(t.id AS BIGINT) AS tournament_id,CAST(t.num_teams AS BIGINT) AS teams,
        CAST(t.num_novice_debaters AS BIGINT) AS novices,CAST(r.place AS BIGINT) AS place,
        CAST(CASE WHEN {detail} THEN 1 ELSE 0 END AS BIGINT) AS detail
        FROM {table} r JOIN core_tournament t ON r.tournament_id=t.id
        WHERE t.season=$1 AND {owner} AND {filter} ORDER BY r.id"
    );
    let mut records = Vec::new();
    for row in sqlx::query(AssertSqlSafe(sql))
        .bind(season)
        .bind(id)
        .bind(old)
        .fetch_all(conn)
        .await?
    {
        let place = row.try_get::<i64, _>("place")?;
        let detail = row.try_get::<i64, _>("detail")?;
        let points = match kind {
            Benefit::Toty => {
                points::team_points_for_size(row.try_get("teams")?, place, detail == 1)
            }
            Benefit::Soty => points::speaker_points_for_size(row.try_get("teams")?, place - detail),
            Benefit::Noty => points::novice_points_for_size(row.try_get("novices")?, place),
            Benefit::Online if detail == 1 => points::online_points(place),
            Benefit::Online => 0.0,
        };
        records.push((points, row.try_get("tournament_id")?));
    }
    records.sort_by(|(left, _), (right, _)| right.total_cmp(left));
    Ok(records)
}

async fn scored(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    kind: Benefit,
    season: &str,
    id: i64,
    included: bool,
) -> Result<(), ApiError> {
    let (label, field) = kind.binding();
    let existing = find(conn, label, field, season, id).await?;
    let results = if included {
        markers(conn, kind, season, id).await?
    } else {
        Vec::new()
    };
    let mut total = 0.0;
    if results.is_empty() {
        discard(conn, journal, label, existing).await?;
        if existing.is_some() {
            placement(conn, journal, label, season).await?;
        }
    } else {
        let max = if matches!(kind, Benefit::Toty | Benefit::Noty) {
            5
        } else {
            6
        };
        let mut fields =
            Fields::from_iter([("season".into(), json!(season)), (field.into(), json!(id))]);
        for (index, marker) in MARKERS.iter().enumerate() {
            let (points, tournament) = if index < max {
                results
                    .get(index)
                    .map_or((0.0, None), |(points, tournament)| {
                        (*points, Some(*tournament))
                    })
            } else {
                (0.0, None)
            };
            fields.insert(format!("marker_{marker}"), json!(points));
            fields.insert(format!("tournament_{marker}_id"), json!(tournament));
            total += points;
        }
        fields.insert("points".into(), json!(total));
        upsert(conn, journal, label, fields, existing).await?;
        placement(conn, journal, label, season).await?;
    }
    if matches!(kind, Benefit::Online) && online_season(season) {
        points_qualification(conn, journal, id, season, total >= 10.0).await?;
    }
    Ok(())
}

async fn school(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    id: i64,
    season: &str,
) -> Result<(), ApiError> {
    let label = "core.COTY";
    let existing = find(conn, label, "school_id", season, id).await?;
    let included=sqlx::query_scalar::<_,i64>("SELECT CAST(CASE WHEN included_in_oty THEN 1 ELSE 0 END AS BIGINT) FROM core_school WHERE id=$1 AND NOT temporary")
        .bind(id).fetch_optional(&mut *conn).await?;
    if included.is_none() {
        return Ok(());
    }
    if included != Some(1) || online_season(season) {
        discard(conn, journal, label, existing).await?;
        if existing.is_some() {
            placement(conn, journal, label, season).await?;
        }
        return Ok(());
    }
    let rule = season
        .split('-')
        .next()
        .and_then(|value| value.parse::<i32>().ok())
        .unwrap_or_default()
        >= 2026;
    let bonus = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(DISTINCT q.debater_id) FROM core_qual q
        JOIN core_debater d ON q.debater_id=d.id WHERE d.school_id=$1 AND q.season=$2
        AND (NOT $3 OR q.qual_type NOT IN (1,2,3,5,6,16))",
    )
    .bind(id)
    .bind(season)
    .bind(rule)
    .fetch_one(&mut *conn)
    .await?;
    let base=sqlx::query_scalar::<_,f64>("SELECT CAST(COALESCE(SUM(CASE WHEN q.points>60 THEN 60.0 ELSE q.points END),0.0) AS DOUBLE PRECISION)
        FROM core_qualpoints q JOIN core_debater d ON q.debater_id=d.id WHERE d.school_id=$1 AND q.season=$2")
        .bind(id).bind(season).fetch_one(&mut *conn).await?;
    upsert(
        conn,
        journal,
        label,
        Fields::from_iter([
            ("school_id".into(), json!(id)),
            ("season".into(), json!(season)),
            ("points".into(), json!(base + 6.0 * bonus as f64)),
        ]),
        existing,
    )
    .await?;
    placement(conn, journal, label, season).await?;
    Ok(())
}

async fn team(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    id: i64,
    season: &str,
) -> Result<(), ApiError> {
    let schools = sqlx::query(
        "SELECT CAST(d.school_id AS BIGINT) AS school_id,
        CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included
        FROM core_team_debaters m JOIN core_debater d ON m.debater_id=d.id
        LEFT JOIN core_school s ON d.school_id=s.id
        WHERE m.team_id=$1 AND NOT d.temporary AND NOT d.synthetic ORDER BY d.id",
    )
    .bind(id)
    .fetch_all(&mut *conn)
    .await?;
    let old = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_totyreaff WHERE season=$1 AND old_team_id=$2 LIMIT 1",
    )
    .bind(season)
    .bind(id)
    .fetch_optional(&mut *conn)
    .await?
    .is_some();
    let mut included = false;
    if !old && let Some(first) = schools.first() {
        let home = first.try_get::<Option<i64>, _>("school_id")?;
        included = first.try_get::<i64, _>("included")? == 1
            && schools
                .iter()
                .all(|record| record.try_get::<Option<i64>, _>("school_id").ok().flatten() == home);
    }
    scored(conn, journal, Benefit::Toty, season, id, included).await
}

pub(super) async fn recompute(
    conn: &mut AnyConnection,
    journal: &mut Journal,
    debater: i64,
    teams: &[i64],
    schools: &[i64],
    seasons: &[String],
) -> Result<(), ApiError> {
    let included = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT)
        FROM core_debater d LEFT JOIN core_school s ON d.school_id=s.id WHERE d.id=$1",
    )
    .bind(debater)
    .fetch_one(&mut *conn)
    .await?
        == 1;
    for season in seasons {
        for &id in teams {
            team(conn, journal, id, season).await?;
        }
        qualifying_points(conn, journal, debater, season, included).await?;
        scored(conn, journal, Benefit::Online, season, debater, included).await?;
        scored(conn, journal, Benefit::Soty, season, debater, included).await?;
        if let Some(year) = season
            .split('-')
            .next()
            .and_then(|value| value.parse::<i32>().ok())
            .filter(|year| *year <= 2020)
        {
            scored(
                conn,
                journal,
                Benefit::Noty,
                &year.to_string(),
                debater,
                included,
            )
            .await?;
        }
        for &id in schools {
            school(conn, journal, id, season).await?;
        }
    }
    Ok(())
}
