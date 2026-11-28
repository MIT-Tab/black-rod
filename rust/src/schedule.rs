use std::collections::BTreeMap;

use axum::{
    extract::{OriginalUri, State},
    http::HeaderMap,
    response::Response,
};
use chrono::{Datelike, Days, NaiveDate};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AssertSqlSafe, Row, any::AnyRow};

use crate::{ApiError, AppState, Endpoint, cached_json, options, schools::School};

pub(crate) const TOURNAMENT_COLUMNS: &str =
    "CAST(t.id AS BIGINT) AS id, t.name, t.season, CAST(t.date AS TEXT) AS date,
           CAST(t.num_teams AS BIGINT) AS num_teams,
           CAST(t.num_novice_debaters AS BIGINT) AS num_novice_debaters,
           CAST(t.qual_type AS BIGINT) AS qual_type,
           CAST(t.autoqual_bar AS BIGINT) AS autoqual_bar,
           CAST(CASE WHEN t.toty THEN 1 ELSE 0 END AS BIGINT) AS toty,
           CAST(CASE WHEN t.soty THEN 1 ELSE 0 END AS BIGINT) AS soty,
           CAST(CASE WHEN t.qual THEN 1 ELSE 0 END AS BIGINT) AS qual,
           CAST(s.id AS BIGINT) AS host_id, s.name AS host_name,
           CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS host_included";

const QUERY: &str = "
    SELECT {columns}
    FROM core_tournament t
    LEFT JOIN core_school s ON s.id = t.host_id
    WHERE t.season = $1
    ORDER BY t.date
";

const NOTES: [&str; 2] = [
    "COTY points and qual points describe the same scoring buckets.",
    "Most schools abstain from competing at tournaments they host, but this rarely affects yearlong awards.",
];

pub(crate) fn season_display(season: i32) -> String {
    format!("{season}-{:02}", (season + 1) % 100)
}

pub(crate) fn requested_season(query: &str, current_season: i32) -> i32 {
    url::form_urlencoded::parse(query.as_bytes())
        .filter(|(name, _)| name == "season")
        .last()
        .and_then(|(_, value)| {
            let season = value.parse::<i32>().ok()?;
            ((2004..=current_season).contains(&season) && value == season.to_string())
                .then_some(season)
        })
        .unwrap_or(current_season)
}

pub(crate) fn proxy_url(origin: &str, target: &str) -> String {
    format!("{origin}/llm/?endpoint={target}")
}

#[derive(Serialize)]
pub(crate) struct Tournament {
    pub(crate) id: i64,
    name: String,
    pub(crate) season: String,
    season_display: String,
    pub(crate) date: String,
    display: String,
    num_teams: i64,
    num_novice_debaters: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    host: Option<School>,
    url: String,
    api_url: String,
}

#[derive(Serialize)]
struct Oty {
    toty_points: bool,
    soty_points: bool,
    coty_points: bool,
    qual_type: Value,
    autoqual_bar: i64,
    notes: Vec<&'static str>,
}

#[derive(Serialize)]
struct Entry {
    tournament: Tournament,
    otys: Oty,
    #[serde(skip)]
    qual_type: i64,
}

#[derive(Serialize)]
struct Month {
    month: u32,
    display: String,
    year: i32,
    weeks: Vec<Week>,
}

#[derive(Serialize)]
struct Week {
    date: u32,
    one_more: u32,
    tournaments: Vec<Entry>,
}

pub(crate) fn qualification_label(code: i64) -> Value {
    let label = match code {
        0 => "Regular (Points)",
        1 => "Brandeis IV",
        2 => "Yale IV",
        3 => "NorthAms",
        4 => "Expansion",
        5 => "Worlds",
        6 => "NAUDC",
        7 => "ProAms",
        8 => "Nationals",
        9 => "Novice",
        10 => "Gender Minority",
        11 => "Online No Points",
        12 => "Online Points",
        13 => "Online Proams Points",
        14 => "Online Gender Minority Points",
        15 => "BIPOC",
        16 => "USUDC",
        _ => return json!(code),
    };
    json!(label)
}

fn otys(name: &str, season: i32, qual_type: i64, autoqual_bar: i64, mut points: [bool; 3]) -> Oty {
    let name = name.to_lowercase();
    let gm = qual_type == 10
        || name.contains("gender minority")
        || name.split_whitespace().any(|s| s == "gm");
    let bipoc = qual_type == 15 || name.contains("bipoc");
    let nationals = qual_type == 8 || name.contains("nationals");
    let expansion = qual_type == 4 || name.contains("bp");
    if expansion || nationals {
        points = [false; 3];
    }
    if gm || bipoc {
        points = [false, false, true];
    }
    let mut notes = Vec::new();
    if matches!(qual_type, 1 | 2 | 3 | 5 | 6 | 16) {
        notes.push(if season < 2026 {
            "BP autoquals award individual qualification and six COTY bonus points per qualified debater."
        } else {
            "BP autoquals award individual qualification and no COTY bonus; qualifying by points or a non-BP autoqual still earns the six-point bonus."
        });
    } else if expansion {
        notes.push(
            "British Parliamentary weekends award autoqual slots, but no TOTY/SOTY/COTY points.",
        );
    }
    if nationals {
        notes.push("Nationals is championship-only: it does not award season points, only a title and autoqual bids.");
    }
    if gm {
        notes.push(if season >= 2025 {
            "Gender Minority invitationals award COTY/qual points only and autoqual finalists."
        } else {
            "Gender Minority invitationals award COTY/qual points only"
        });
    }
    if bipoc {
        notes.push(if season >= 2025 {
            "BIPOC invitationals award COTY/qual points only and autoqual finalists."
        } else {
            "BIPOC invitationals award COTY/qual points only"
        });
    }
    Oty {
        toty_points: points[0],
        soty_points: points[1],
        coty_points: points[2],
        qual_type: qualification_label(qual_type),
        autoqual_bar,
        notes,
    }
}

impl Tournament {
    pub(crate) fn from_row(row: &AnyRow, origin: &str) -> Result<Self, sqlx::Error> {
        let id = row.try_get::<i64, _>("id")?;
        let name = row.try_get::<String, _>("name")?;
        let season = row.try_get::<String, _>("season")?;
        let host = match row.try_get::<Option<i64>, _>("host_id")? {
            Some(host_id) => Some(School::new(
                host_id,
                row.try_get("host_name")?,
                row.try_get::<i64, _>("host_included")? != 0,
                origin,
            )),
            None => None,
        };
        let display = name.split(" (").next().unwrap_or_default().to_owned();
        Ok(Self {
            id,
            name,
            season_display: season
                .parse()
                .map(season_display)
                .unwrap_or_else(|_| season.clone()),
            season,
            date: row.try_get("date")?,
            display,
            host,
            num_teams: row.try_get("num_teams")?,
            num_novice_debaters: row.try_get("num_novice_debaters")?,
            url: format!("{origin}/core/tournaments/{id}"),
            api_url: format!("{origin}/api/tournaments/{id}/detail/"),
        })
    }
}

fn entry(row: &AnyRow, origin: &str, season: i32) -> Result<Entry, sqlx::Error> {
    let name: String = row.try_get("name")?;
    let qual_type = row.try_get("qual_type")?;
    let points = [
        row.try_get::<i64, _>("toty")? != 0,
        row.try_get::<i64, _>("soty")? != 0,
        row.try_get::<i64, _>("qual")? != 0,
    ];
    let otys = otys(
        &name,
        season,
        qual_type,
        row.try_get("autoqual_bar")?,
        points,
    );
    let tournament = Tournament::from_row(row, origin)?;
    Ok(Entry {
        tournament,
        otys,
        qual_type,
    })
}

pub(crate) async fn json(
    state: &AppState,
    origin: &str,
    season: i32,
    path: &str,
) -> Result<Vec<u8>, ApiError> {
    let rows = sqlx::query(AssertSqlSafe(
        QUERY.replace("{columns}", TOURNAMENT_COLUMNS),
    ))
    .bind(season.to_string())
    .fetch_all(&state.pool)
    .await?;
    let mut by_date: BTreeMap<NaiveDate, Vec<Entry>> = BTreeMap::new();
    for row in rows {
        let entry = entry(&row, origin, season)?;
        let date = NaiveDate::parse_from_str(&entry.tournament.date, "%Y-%m-%d")
            .map_err(|_| ApiError::InvalidStoredDate)?;
        by_date.entry(date).or_default().push(entry);
    }
    let mut months: BTreeMap<(i32, u32), Month> = BTreeMap::new();
    for (date, mut tournaments) in by_date {
        tournaments.sort_by(|a, b| {
            let key = |entry: &Entry| (matches!(entry.qual_type, 1 | 2), entry.qual_type);
            key(a)
                .cmp(&key(b))
                .then_with(|| a.tournament.name.cmp(&b.tournament.name))
        });
        let month = months
            .entry((date.year(), date.month()))
            .or_insert_with(|| Month {
                month: date.month(),
                year: date.year(),
                display: date.format("%B").to_string(),
                weeks: Vec::new(),
            });
        month.weeks.push(Week {
            date: date.day(),
            one_more: (date + Days::new(1)).day(),
            tournaments,
        });
    }
    Ok(serde_json::to_vec(&json!({
        "season": season.to_string(),
        "season_display": season_display(season),
        "notes": NOTES,
        "months": months.into_values().collect::<Vec<_>>(),
        "links": { "self": proxy_url(origin, path) },
    }))?)
}

pub(crate) async fn schedule(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let season = requested_season(uri.query().unwrap_or_default(), state.current_season);
    cached_json(
        state,
        headers,
        Endpoint::Schedule(season),
        uri.path_and_query().map(|p| p.as_str()).unwrap_or_default(),
    )
    .await
}

pub(crate) async fn schedule_options() -> Response {
    options(
        "Schedule Api",
        "Expose the APDA tournament schedule grouped by month/week.",
    )
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    use axum::{
        http::{StatusCode, header},
        response::{Html, IntoResponse},
    };
    use minijinja::context;

    let raw = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(key, _)| key == "season")
        .map(|(_, value)| value)
        .last()
        .map(|value| value.into_owned())
        .unwrap_or_else(|| state.current_season.to_string());
    let Ok(year) = raw.trim().parse::<i32>() else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(user) = crate::session::user(&state.pool, &headers, state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let rows = sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id, name, CAST(date AS TEXT) AS date,
                CAST(qual_type AS BIGINT) AS qual_type
         FROM core_tournament WHERE season = $1 ORDER BY date",
    )
    .bind(&raw)
    .fetch_all(&state.pool)
    .await;
    let tournaments = match rows {
        Ok(rows) => page_months(&rows),
        Err(error) => Err(error.into()),
    };
    let tournaments = match tournaments {
        Ok(tournaments) => tournaments,
        Err(error) => {
            eprintln!("Schedule page failed: {error:?}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let common = crate::web::base_context(&state, &user, &uri);
    match crate::web::render(
        "tournaments/schedule.html",
        context! {
            ..common, ..context! { current_season => raw, season_display => season_display(year), tournaments, }
        },
    ) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Schedule template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

fn page_months(rows: &[AnyRow]) -> Result<Vec<Value>, ApiError> {
    let mut months = BTreeMap::new();
    for row in rows {
        let id: i64 = row.try_get("id")?;
        let name: &str = row.try_get("name")?;
        let qualification: i64 = row.try_get("qual_type")?;
        let date = NaiveDate::parse_from_str(row.try_get("date")?, "%Y-%m-%d")
            .map_err(|_| ApiError::InvalidStoredDate)?;
        // The HTML view groups by month number, then labels it with the first
        // tournament's year. A stored season can contain dates from several years.
        let (_, weeks) = months
            .entry(date.month())
            .or_insert_with(|| (date, BTreeMap::new()));
        let (_, tournaments) = weeks
            .entry(date.day())
            .or_insert_with(|| (date, Vec::new()));
        tournaments.push((
            qualification,
            json!({"name": name, "url": format!("/core/tournaments/{id}")}),
        ));
    }

    let mut months = months.into_values().collect::<Vec<_>>();
    months.sort_by_key(|(date, _)| (date.year(), date.month()));
    Ok(months.into_iter().map(|(month, weeks)| {
        let weeks = weeks.into_values().map(|(date, mut tournaments)| {
            tournaments.sort_by_key(|(qualification, _)| (matches!(qualification, 1 | 2), *qualification));
            json!({"date": date.day(), "one_more": (date + Days::new(1)).day(),
                   "tournaments": tournaments.into_iter().map(|(_, tournament)| tournament).collect::<Vec<_>>()})
        }).collect::<Vec<_>>();
        json!({"display": month.format("%B").to_string(), "weeks": weeks})
    }).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn schedule_page_preserves_day_qualification_and_legacy_month_order() {
        sqlx::any::install_default_drivers();
        let pool = sqlx::AnyPool::connect("sqlite::memory:").await.unwrap();
        let rows = sqlx::query(
            "SELECT CAST(id AS BIGINT) AS id, name, date, CAST(qual_type AS BIGINT) AS qual_type
             FROM (
                 SELECT 1 AS id, 'First July' AS name, '2025-07-12' AS date, 0 AS qual_type
                 UNION ALL SELECT 2, 'Earlier November', '2025-11-15', 0
                 UNION ALL SELECT 3, 'Special', '2025-11-15', 2
                 UNION ALL SELECT 4, 'Second November', '2025-11-15', 0
                 UNION ALL SELECT 5, 'Second July', '2026-07-25', 0
             ) ORDER BY date, id",
        )
        .fetch_all(&pool)
        .await
        .unwrap();

        let months = page_months(&rows).unwrap();
        assert_eq!(months.len(), 2);
        assert_eq!(months[0]["display"], "July");
        assert_eq!(
            months[0]["weeks"][0]["tournaments"][0]["name"],
            "First July"
        );
        assert_eq!(
            months[0]["weeks"][1]["tournaments"][0]["name"],
            "Second July"
        );
        assert_eq!(
            months[1]["weeks"][0]["tournaments"],
            json!([
                {"name": "Earlier November", "url": "/core/tournaments/2"},
                {"name": "Second November", "url": "/core/tournaments/4"},
                {"name": "Special", "url": "/core/tournaments/3"}
            ])
        );
    }
}
