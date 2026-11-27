use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    response::{Html, IntoResponse, Response},
};
use chrono::{Days, Utc};
use minijinja::context;
use serde::Serialize;
use sqlx::{AnyConnection, Row};

use crate::{ApiError, AppState, web};

const WIDGET_KEY: &str = ":1:recent_results_widget_html";
const WIDGET_TTL: u32 = 24 * 60 * 60;

pub(crate) async fn invalidate(state: &AppState) {
    if let Some(cache) = &state.memcached {
        cache.invalidate_raw(WIDGET_KEY).await;
    } else {
        state.recent_results.invalidate(&()).await;
    }
}

const TOURNAMENTS: &str = "
    WITH posted AS (
        SELECT t.id, t.name, t.date, t.num_teams,
            CASE WHEN t.qual_type IN (0, 4, 7, 8, 9, 10, 15) AND LOWER(t.name) NOT LIKE '%bp%'
                THEN 1 ELSE 0 END AS eligible,
            (SELECT r.team_id FROM core_teamresult r WHERE r.tournament_id = t.id
                AND r.type_of_place = 1 AND r.place = 1 LIMIT 1) AS winner,
            (SELECT r.team_id FROM core_teamresult r WHERE r.tournament_id = t.id
                AND r.type_of_place = 1 AND r.place = 2 LIMIT 1) AS finalist
        FROM core_tournament t WHERE t.num_teams > 0
    ), finals AS (
        SELECT *, CASE WHEN eligible = 1 AND CAST(date AS TEXT) BETWEEN $1 AND $2
            THEN 2 ELSE eligible END AS priority
        FROM posted WHERE winner IS NOT NULL AND finalist IS NOT NULL
    ), selection AS (
        SELECT *, DENSE_RANK() OVER (ORDER BY priority DESC, date DESC) AS day_rank,
            ROW_NUMBER() OVER (ORDER BY priority DESC, date DESC) AS position,
            MAX(priority) OVER () AS selected_priority
        FROM finals
    )
    SELECT CAST(id AS BIGINT) AS id, name, CAST(date AS TEXT) AS date,
        CAST(num_teams AS BIGINT) AS num_teams, CAST(winner AS BIGINT) AS winner,
        CAST(finalist AS BIGINT) AS finalist
    FROM selection WHERE (selected_priority = 2 AND position <= 3)
        OR (selected_priority <> 2 AND day_rank = 1)
    ORDER BY date DESC, CASE WHEN selected_priority <> 2 THEN name END
";
const TEAM: &str = "
    SELECT t.name AS team_name, CAST(d.id AS BIGINT) AS debater_id, d.first_name, d.last_name,
        CAST(s.id AS BIGINT) AS school_id, s.name AS school
    FROM core_team t LEFT JOIN core_team_debaters m ON m.team_id = t.id
    LEFT JOIN core_debater d ON d.id = m.debater_id LEFT JOIN core_school s ON s.id = d.school_id
    WHERE t.id = $1 ORDER BY d.id
";
const SPEAKERS: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, d.first_name, d.last_name, s.name AS school,
        CAST(r.place AS BIGINT) AS place, CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS tie
    FROM core_speakerresult r JOIN core_debater d ON d.id = r.debater_id
    LEFT JOIN core_school s ON s.id = d.school_id
    WHERE r.tournament_id = $1 AND r.type_of_place = 1 AND r.place > 0 ORDER BY r.place LIMIT 3
";

#[derive(Serialize)]
struct Tournament {
    name: String,
    date: String,
    num_teams: i64,
    url: String,
    winner: Team,
    finalist: Team,
    speakers: Vec<Speaker>,
}

#[derive(Serialize)]
struct Team {
    debaters: String,
    school: String,
    url: String,
}

#[derive(Serialize)]
struct Speaker {
    name: String,
    school: String,
    place: i64,
    tie: bool,
    url: String,
}

fn full_name(first: &str, last: &str) -> String {
    format!("{first} {last}").trim().to_owned()
}

async fn team(conn: &mut AnyConnection, id: i64) -> Result<Team, ApiError> {
    let rows = sqlx::query(TEAM).bind(id).fetch_all(conn).await?;
    let mut people = Vec::new();
    for row in &rows {
        if row.try_get::<Option<i64>, _>("debater_id")?.is_some() {
            people.push((
                full_name(row.try_get("first_name")?, row.try_get("last_name")?),
                row.try_get::<Option<i64>, _>("school_id")?,
                row.try_get::<Option<String>, _>("school")?
                    .unwrap_or_else(|| "Unknown School".to_owned()),
            ));
        }
    }
    let (debaters, school) = match people.as_slice() {
        [] => (
            rows.first()
                .ok_or(ApiError::Database)?
                .try_get("team_name")?,
            String::new(),
        ),
        [
            (left, left_id, left_school),
            (right, right_id, right_school),
        ] => (
            format!("{left} and {right}"),
            if left_id == right_id {
                left_school.to_owned()
            } else {
                format!("{left_school} / {right_school}")
            },
        ),
        [(name, _, school), ..] => (name.to_owned(), school.to_owned()),
    };
    Ok(Team {
        debaters,
        school,
        url: format!("/core/teams/{id}"),
    })
}

async fn render(state: &AppState) -> Result<String, ApiError> {
    let mut conn = state.pool.acquire().await?;
    let today = Utc::now().date_naive();
    let start = today - Days::new(6);
    let selected = sqlx::query(TOURNAMENTS)
        .bind(start.to_string())
        .bind(today.to_string())
        .fetch_all(&mut *conn)
        .await?;
    let mut tournaments = Vec::with_capacity(selected.len());
    for row in selected {
        let id: i64 = row.try_get("id")?;
        let speaker_rows = sqlx::query(SPEAKERS).bind(id).fetch_all(&mut *conn).await?;
        let mut speakers = Vec::with_capacity(speaker_rows.len());
        for row in speaker_rows {
            let debater: i64 = row.try_get("id")?;
            speakers.push(Speaker {
                name: full_name(row.try_get("first_name")?, row.try_get("last_name")?),
                school: row
                    .try_get::<Option<String>, _>("school")?
                    .unwrap_or_default(),
                place: row.try_get("place")?,
                tie: row.try_get::<i64, _>("tie")? != 0,
                url: format!("/core/debaters/{debater}"),
            });
        }
        let name: &str = row.try_get("name")?;
        tournaments.push(Tournament {
            name: name.split(" (").next().unwrap_or(name).to_owned(),
            date: row.try_get("date")?,
            num_teams: row.try_get("num_teams")?,
            url: format!("/core/tournaments/{id}"),
            winner: team(&mut conn, row.try_get("winner")?).await?,
            finalist: team(&mut conn, row.try_get("finalist")?).await?,
            speakers,
        });
    }
    web::render(
        "tournaments/recent_results_widget.html",
        context! {
            has_results => !tournaments.is_empty(), tournaments,
        },
    )
    .map_err(|error| {
        eprintln!("Recent results template failed: {error}");
        ApiError::Serialization
    })
}

pub(crate) async fn page(State(state): State<AppState>) -> Response {
    if let Some(cache) = &state.memcached {
        // Django stores the response body as bytes using pymemcache's zero flag.
        if let Some((0, bytes)) = cache.read_raw(WIDGET_KEY).await
            && let Ok(html) = String::from_utf8(bytes)
            && !html.is_empty()
        {
            return Html(html).into_response();
        }
        return match render(&state).await {
            Ok(html) => {
                cache
                    .write_raw(WIDGET_KEY, 0, html.as_bytes(), WIDGET_TTL, false)
                    .await;
                Html(html).into_response()
            }
            Err(error) => {
                eprintln!("Recent results were unavailable: {error:?}");
                StatusCode::INTERNAL_SERVER_ERROR.into_response()
            }
        };
    }
    match state
        .recent_results
        .try_get_with((), async { render(&state).await.map(Arc::new) })
        .await
    {
        Ok(html) => Html((*html).clone()).into_response(),
        Err(error) => {
            eprintln!("Recent results were unavailable: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
