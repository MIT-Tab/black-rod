use std::{collections::BTreeMap, sync::Arc};

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row};

use crate::{ApiError, AppState, schedule::season_display, session, team_profile, web};

const COUNTS: &str = "
    SELECT
        CAST((SELECT COUNT(*) FROM core_tournament) AS BIGINT) AS tournaments,
        CAST((SELECT COUNT(*) FROM core_debater WHERE NOT synthetic AND NOT temporary) AS BIGINT) AS debaters,
        CAST((SELECT COUNT(*) FROM core_school WHERE NOT temporary) AS BIGINT) AS schools,
        CAST((SELECT COUNT(*) FROM core_team) AS BIGINT) AS teams,
        CAST((SELECT COUNT(*) FROM core_teamresult) AS BIGINT) AS team_results,
        CAST((SELECT COUNT(*) FROM core_speakerresult) AS BIGINT) AS speaker_results,
        CAST((SELECT AVG(num_teams) FROM core_tournament WHERE num_teams > 0) AS DOUBLE PRECISION) AS average,
        CAST((SELECT COUNT(*) FROM core_debater
              WHERE NOT synthetic AND NOT temporary
                AND (first_season IS NULL OR latest_season IS NULL OR first_season <> latest_season)) AS BIGINT) AS multi,
        CAST((SELECT COUNT(*) FROM (
            SELECT team_id FROM core_teamresult GROUP BY team_id HAVING COUNT(DISTINCT tournament_id) >= 3
        ) counted) AS BIGINT) AS recurring
";

const SEASONS: &str = "
    WITH seasons AS (SELECT DISTINCT season FROM core_tournament),
    team_counts AS (
        SELECT t.season, COUNT(DISTINCT r.team_id) AS count
        FROM core_teamresult r JOIN core_tournament t ON t.id = r.tournament_id
        GROUP BY t.season
    )
    SELECT s.season,
           CAST((SELECT COUNT(*) FROM core_debater d WHERE NOT synthetic AND NOT temporary
               AND d.first_season <= s.season AND d.latest_season >= s.season) AS BIGINT) AS debaters,
           CAST(COALESCE(tc.count, 0) AS BIGINT) AS teams
    FROM seasons s LEFT JOIN team_counts tc ON tc.season = s.season
    ORDER BY s.season
";

const PEOPLE_COLUMNS: &str = "CAST(d.id AS BIGINT) AS id, d.first_name, d.last_name,
    CAST(s.id AS BIGINT) AS school_id, s.name AS school_name, CAST(c.count AS BIGINT) AS count";

const ATTENDANCE: &str = "
    WITH attendance AS (
        SELECT rs.debater_id, r.tournament_id FROM core_roundstats rs JOIN core_round r ON r.id = rs.round_id
        UNION
        SELECT debater_id, tournament_id FROM core_speakerresult
        UNION
        SELECT td.debater_id, r.tournament_id FROM core_teamresult r JOIN core_team_debaters td ON td.team_id = r.team_id
        UNION
        SELECT td.debater_id, r.tournament_id FROM core_round r JOIN core_team_debaters td ON td.team_id = r.gov_id
        UNION
        SELECT td.debater_id, r.tournament_id FROM core_round r JOIN core_team_debaters td ON td.team_id = r.opp_id
    ), counts AS (
        SELECT debater_id, COUNT(*) AS count FROM attendance GROUP BY debater_id
    )
    SELECT {people} FROM counts c
    JOIN core_debater d ON d.id = c.debater_id LEFT JOIN core_school s ON s.id = d.school_id
    WHERE NOT d.synthetic AND NOT d.temporary
    ORDER BY c.count DESC, d.last_name, d.first_name LIMIT 10
";

const VIDEOS: &str = "
    WITH participations AS (
        SELECT pm_id AS debater_id FROM core_video
        UNION ALL SELECT mg_id FROM core_video
        UNION ALL SELECT lo_id FROM core_video
        UNION ALL SELECT mo_id FROM core_video
    ), counts AS (
        SELECT debater_id, COUNT(*) AS count FROM participations GROUP BY debater_id
    )
    SELECT {people}
    FROM counts c JOIN core_debater d ON d.id = c.debater_id LEFT JOIN core_school s ON s.id = d.school_id
    WHERE NOT d.synthetic AND NOT d.temporary ORDER BY c.count DESC, d.id LIMIT 10
";

fn script_json(rows: &[Value], columns: &[&str]) -> String {
    let objects = rows
        .iter()
        .map(|row| {
            let fields = columns
                .iter()
                .map(|column| format!(r#""{column}": {}"#, row[*column]))
                .collect::<Vec<_>>();
            format!("{{{}}}", fields.join(", "))
        })
        .collect::<Vec<_>>();
    format!("[{}]", objects.join(", "))
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
}

fn median(numbers: &mut [i64]) -> Value {
    let len = numbers.len();
    if len == 0 {
        return json!(0);
    }
    numbers.sort_unstable();
    let middle = len / 2;
    if len % 2 == 1 {
        json!(numbers[middle])
    } else {
        json!((numbers[middle - 1] + numbers[middle]) as f64 / 2.0)
    }
}

async fn school_table(pool: &AnyPool, hosted: bool) -> Result<Vec<Value>, ApiError> {
    let query = if hosted {
        "SELECT CAST(s.id AS BIGINT) AS id, s.name, CAST(COUNT(t.id) AS DOUBLE PRECISION) AS total
         FROM core_school s JOIN core_tournament t ON t.host_id = s.id
         WHERE NOT s.temporary GROUP BY s.id, s.name ORDER BY COUNT(t.id) DESC, s.id LIMIT 10"
    } else {
        "SELECT CAST(s.id AS BIGINT) AS id, s.name, CAST(SUM(c.points) AS DOUBLE PRECISION) AS total
         FROM core_school s JOIN core_coty c ON c.school_id = s.id
         WHERE NOT s.temporary GROUP BY s.id, s.name ORDER BY SUM(c.points) DESC, s.id LIMIT 10"
    };
    let rows = sqlx::query(query).fetch_all(pool).await?;
    let mut values = Vec::new();
    for row in rows {
        let id: i64 = row.try_get("id")?;
        let total: f64 = row.try_get("total")?;
        values.push(
            json!({"name":row.try_get::<&str,_>("name")?, "url":format!("/core/schools/{id}"),
              "tournament_count": total as i64, "total_coty_points": total}),
        );
    }
    Ok(values)
}

async fn people_table(pool: &AnyPool, rounds: bool) -> Result<Vec<Value>, ApiError> {
    let query = (if rounds { VIDEOS } else { ATTENDANCE }).replace("{people}", PEOPLE_COLUMNS);
    let rows = sqlx::query(AssertSqlSafe(query)).fetch_all(pool).await?;
    let mut values = Vec::new();
    for row in rows {
        let id: i64 = row.try_get("id")?;
        let first: &str = row.try_get("first_name")?;
        let last: &str = row.try_get("last_name")?;
        let school = match row.try_get::<Option<i64>, _>("school_id")? {
            Some(id) => {
                json!({"name":row.try_get::<&str,_>("school_name")?, "url":format!("/core/schools/{id}")})
            }
            None => Value::Null,
        };
        values.push(json!({"name":format!("{first} {last}").trim(), "url":format!("/core/debaters/{id}"),
            "school":school, "tournament_count":row.try_get::<i64,_>("count")?,
            "round_count": if rounds { row.try_get::<i64,_>("count")? } else { 0 }, "debater_list":[]}));
    }
    Ok(values)
}

async fn team_table(pool: &AnyPool) -> Result<Vec<Value>, ApiError> {
    let rows = sqlx::query("
        SELECT CAST(t.id AS BIGINT) AS id, t.name, CAST(COUNT(DISTINCT r.tournament_id) AS BIGINT) AS count
        FROM core_team t JOIN core_teamresult r ON r.team_id = t.id
        GROUP BY t.id, t.name ORDER BY COUNT(DISTINCT r.tournament_id) DESC, t.id LIMIT 10
    ").fetch_all(pool).await?;
    let mut conn = pool.acquire().await?;
    let mut values = Vec::new();
    for row in rows {
        let id: i64 = row.try_get("id")?;
        values.push(
            json!({"name":row.try_get::<&str,_>("name")?, "url":format!("/core/teams/{id}"),
            "tournament_count":row.try_get::<i64,_>("count")?, "round_count":0, "school":null,
            "debater_list":team_profile::members(&mut conn, id, "").await?}),
        );
    }
    Ok(values)
}

#[derive(Default)]
struct Sizes {
    tournaments: usize,
    teams: Vec<i64>,
    novice: Vec<i64>,
}

async fn season_sizes(pool: &AnyPool) -> Result<(Vec<Value>, Vec<Value>), ApiError> {
    let rows = sqlx::query("
        SELECT season, CAST(num_teams AS BIGINT) AS teams, CAST(num_novice_debaters AS BIGINT) AS novice
        FROM core_tournament
    ").fetch_all(pool).await?;
    let mut by_season = BTreeMap::<String, Sizes>::new();
    for row in rows {
        let season = row.try_get("season")?;
        let sizes = by_season.entry(season).or_default();
        sizes.tournaments += 1;
        let teams = row.try_get::<i64, _>("teams")?;
        let novice = row.try_get::<i64, _>("novice")?;
        if teams > 0 {
            sizes.teams.push(teams);
        }
        if novice > 0 {
            sizes.novice.push(novice);
        }
    }
    let mut averages = Vec::new();
    let mut medians = Vec::new();
    for (season, mut sizes) in by_season {
        let average = (!sizes.teams.is_empty())
            .then(|| sizes.teams.iter().sum::<i64>() as f64 / sizes.teams.len() as f64);
        averages.push(
            json!({"season":season,"tournament_count":sizes.tournaments,"avg_size":average,
            "median_size":sizes.tournaments}),
        );
        medians.push(json!({"season":season, "median_size":median(&mut sizes.teams), "median_novices":median(&mut sizes.novice)}));
    }
    Ok((averages, medians))
}

async fn data(pool: &AnyPool, season: i32) -> Result<Arc<Value>, ApiError> {
    let totals = sqlx::query(COUNTS).fetch_one(pool).await?;
    let total_debaters = totals.try_get::<i64, _>("debaters")?;
    let multi = totals.try_get::<i64, _>("multi")?;
    let percent = if total_debaters > 0 {
        format!("{:.1}", (multi as f64 / total_debaters as f64) * 100.0)
    } else {
        "0".into()
    };
    let average = totals
        .try_get::<Option<f64>, _>("average")?
        .map_or_else(|| "0".into(), |avg| format!("{avg:.1}"));
    let seasons = sqlx::query(SEASONS).fetch_all(pool).await?;
    let mut debaters_by_season = Vec::new();
    let mut teams_by_season = Vec::new();
    for row in seasons {
        let season: &str = row.try_get("season")?;
        debaters_by_season.push(json!({"season":season,"count":row.try_get::<i64,_>("debaters")?}));
        teams_by_season.push(json!({"season":season,"count":row.try_get::<i64,_>("teams")?}));
    }
    let toty = sqlx::query("SELECT season, CAST(SUM(points) AS DOUBLE PRECISION) AS total FROM core_toty GROUP BY season ORDER BY season")
        .fetch_all(pool).await?.iter().map(|row| {
            Ok(json!({"season":row.try_get::<&str,_>("season")?, "total_points":row.try_get::<f64,_>("total")?}))
        }).collect::<Result<Vec<_>, sqlx::Error>>()?;
    let (tournaments, medians) = season_sizes(pool).await?;
    Ok(Arc::new(json!({
        "total_tournaments":totals.try_get::<i64,_>("tournaments")?,
        "total_debaters":total_debaters,"total_schools":totals.try_get::<i64,_>("schools")?,
        "total_teams":totals.try_get::<i64,_>("teams")?,
        "total_team_results":totals.try_get::<i64,_>("team_results")?,
        "total_speaker_results":totals.try_get::<i64,_>("speaker_results")?,
        "current_season":season.to_string(), "current_season_display":season_display(season),
        "avg_tournament_size":average,"multi_season_percentage":percent,
        "teams_with_multiple_tournaments":totals.try_get::<i64,_>("recurring")?,
        "debaters_by_season":script_json(&debaters_by_season, &["season", "count"]),
        "teams_by_season":script_json(&teams_by_season, &["season", "count"]),
        "toty_points_by_season":script_json(&toty, &["season", "total_points"]),
        "tournaments_by_season":script_json(&tournaments, &["season", "tournament_count", "avg_size", "median_size"]),
        "median_stats_by_season":script_json(&medians, &["season", "median_size", "median_novices"]),
        "schools_by_tournament_count":school_table(pool,true).await?,
        "schools_by_coty_points":school_table(pool,false).await?,
        "debaters_by_tournament_count":people_table(pool,false).await?,
        "debaters_by_round_count":people_table(pool,true).await?,
        "teams_by_tournament_count":team_table(pool).await?,
    })))
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let data = state
        .stats
        .try_get_with((), data(&state.pool, state.current_season))
        .await;
    let Ok(data) = data else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let common = web::base_context(&state, &user, &uri);
    let page = minijinja::Value::from_serialize(data.as_ref());
    match web::render("core/stats.html", context! {..common, ..page}) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Statistics template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
