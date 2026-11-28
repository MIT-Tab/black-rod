use std::{
    cmp::Ordering,
    collections::{HashMap, HashSet},
};

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::json;
use sqlx::{AnyPool, Row};

use crate::{
    ApiError, AppState, login,
    points::{speaker_points_for_size, team_points_for_size},
    schedule::season_display,
    session, web,
};

const REAFFIRMATIONS: &str = "SELECT CAST(old_debater_id AS BIGINT) AS old_id,
    CAST(new_debater_id AS BIGINT) AS new_id FROM core_reaff WHERE season=$1 ORDER BY id";
const DEBATERS: &str =
    "SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name,
    CAST(s.id AS BIGINT) AS school_id, s.name AS school FROM core_debater d
    JOIN core_school s ON s.id=d.school_id WHERE s.included_in_oty";
const RESULTS: &str = "SELECT CAST(r.debater_id AS BIGINT) AS debater_id,
    CAST(t.id AS BIGINT) AS tournament_id, t.name, CAST(t.date AS TEXT) AS date,
    CAST(t.num_teams AS BIGINT) AS size, CAST(r.place AS BIGINT) AS place,
    CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS adjustment,
    CAST(1 AS BIGINT) AS speaking
    FROM core_speakerresult r JOIN core_tournament t ON t.id=r.tournament_id
    WHERE t.season=$1 AND t.soty AND r.type_of_place=1 AND r.counts_for_points
    UNION ALL
    SELECT CAST(m.debater_id AS BIGINT), CAST(t.id AS BIGINT), t.name, CAST(t.date AS TEXT),
    CAST(t.num_teams AS BIGINT), CAST(r.place AS BIGINT),
    CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT), CAST(0 AS BIGINT)
    FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
    JOIN core_team_debaters m ON m.team_id=r.team_id
    JOIN core_debater d ON d.id=m.debater_id AND NOT(d.temporary OR d.synthetic)
    WHERE t.season=$1 AND t.qual AND r.type_of_place=1 AND r.counts_for_points";

#[derive(Serialize)]
struct Debater {
    id: i64,
    name: String,
    school_id: i64,
    school: String,
}

#[derive(Clone, Serialize)]
struct Marker {
    tournament_id: i64,
    tournament: String,
    date: String,
    soty_points: f64,
    qual_points: f64,
    points: f64,
}

fn marker_order(left: &Marker, right: &Marker) -> Ordering {
    right
        .points
        .total_cmp(&left.points)
        .then_with(|| left.date.cmp(&right.date))
        .then_with(|| left.tournament_id.cmp(&right.tournament_id))
}

fn components(markers: &[Marker]) -> Vec<Marker> {
    let mut separate = Vec::new();
    for marker in markers {
        if marker.soty_points > 0.0 {
            separate.push(Marker {
                points: marker.soty_points,
                qual_points: 0.0,
                ..marker.clone()
            });
        }
        if marker.qual_points > 0.0 {
            separate.push(Marker {
                points: marker.qual_points,
                soty_points: 0.0,
                ..marker.clone()
            });
        }
    }
    separate.sort_by(marker_order);
    separate
}

#[derive(Serialize)]
struct Standing<'a> {
    debater: &'a Debater,
    counting_markers: Vec<Option<&'a Marker>>,
    points: f64,
    place: usize,
    tied: bool,
}

fn rank<'a>(
    rows: impl Iterator<Item = (&'a Debater, &'a [Marker])>,
    limit: usize,
) -> Vec<Standing<'a>> {
    let mut standings = rows
        .map(|(debater, markers)| Standing {
            debater,
            counting_markers: (0..limit).map(|i| markers.get(i)).collect(),
            points: markers.iter().take(limit).map(|marker| marker.points).sum(),
            place: 0,
            tied: false,
        })
        .collect::<Vec<_>>();
    standings.sort_by(|left, right| {
        right
            .points
            .total_cmp(&left.points)
            .then_with(|| left.debater.name.cmp(&right.debater.name))
            .then_with(|| left.debater.id.cmp(&right.debater.id))
    });
    let mut index = 0;
    while index < standings.len() {
        let points = standings[index].points;
        let len = standings[index..]
            .iter()
            .take_while(|row| row.points == points)
            .count();
        for row in &mut standings[index..index + len] {
            row.place = index + 1;
            row.tied = len > 1;
        }
        index += len;
    }
    standings
}

async fn boards(pool: &AnyPool, season: &str) -> Result<serde_json::Value, ApiError> {
    let mut former = HashSet::new();
    let mut transfers = HashMap::new();
    let mut new_debaters = HashSet::new();
    for row in sqlx::query(REAFFIRMATIONS)
        .bind(season)
        .fetch_all(pool)
        .await?
    {
        let old_id: i64 = row.try_get("old_id")?;
        let new_id: i64 = row.try_get("new_id")?;
        former.insert(old_id);
        if new_debaters.insert(new_id) {
            transfers.insert(old_id, new_id);
        }
    }
    let mut people = HashMap::new();
    for row in sqlx::query(DEBATERS).fetch_all(pool).await? {
        let id: i64 = row.try_get("id")?;
        if !former.contains(&id) {
            people.insert(
                id,
                Debater {
                    id,
                    name: row.try_get("name")?,
                    school_id: row.try_get("school_id")?,
                    school: row.try_get("school")?,
                },
            );
        }
    }
    let mut totals: HashMap<i64, HashMap<i64, Marker>> = HashMap::new();
    for row in sqlx::query(RESULTS).bind(season).fetch_all(pool).await? {
        let id: i64 = row.try_get("debater_id")?;
        let id = transfers.get(&id).copied().unwrap_or(id);
        if !people.contains_key(&id) {
            continue;
        }
        let tournament_id = row.try_get("tournament_id")?;
        let tournament = row.try_get("name")?;
        let date = row.try_get("date")?;
        let marker = totals
            .entry(id)
            .or_default()
            .entry(tournament_id)
            .or_insert_with(|| Marker {
                tournament_id,
                tournament,
                date,
                soty_points: 0.0,
                qual_points: 0.0,
                points: 0.0,
            });
        let size = row.try_get("size")?;
        let place: i64 = row.try_get("place")?;
        let adjustment: i64 = row.try_get("adjustment")?;
        if row.try_get::<i64, _>("speaking")? != 0 {
            marker.soty_points += speaker_points_for_size(size, place - adjustment);
        } else {
            marker.qual_points += team_points_for_size(size, place, adjustment != 0);
        }
        marker.points = marker.soty_points + marker.qual_points;
    }
    let mut rows = Vec::new();
    for (id, markers) in totals {
        let mut markers = markers
            .into_values()
            .filter(|marker| marker.points > 0.0)
            .collect::<Vec<_>>();
        if !markers.is_empty() {
            markers.sort_by(marker_order);
            let separate = components(&markers);
            rows.push((&people[&id], markers, separate));
        }
    }
    let mut boards = Vec::new();
    for (id, title, description, limit) in [
        (
            "combined",
            "Original: 6 combined markers",
            "The best six tournament totals, each adding SOTY + qual points.",
            6,
        ),
        (
            "separate_9",
            "Alternative: 9 separate markers",
            "The best nine individual SOTY or qual markers across all tournaments.",
            9,
        ),
        (
            "separate_12",
            "Alternative: 12 separate markers",
            "The best twelve individual SOTY or qual markers across all tournaments.",
            12,
        ),
    ] {
        let separate = limit != 6;
        let standings = rank(
            rows.iter().map(|(debater, combined, split)| {
                (
                    *debater,
                    if separate {
                        split.as_slice()
                    } else {
                        combined.as_slice()
                    },
                )
            }),
            limit,
        );
        boards.push(
            json!({"id":id,"title":title,"description":description,"separate":separate,
            "standings":standings, "slots":(1..=limit).collect::<Vec<_>>(), "colspan":limit+4}),
        );
    }
    Ok(json!(boards))
}

fn season_label(season: &str) -> String {
    season
        .parse::<i32>()
        .map_or_else(|_| season.to_owned(), season_display)
}

async fn render(
    state: &AppState,
    uri: &axum::http::Uri,
    headers: &HeaderMap,
) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(uri));
    }
    if !user.is_superuser {
        return Ok(web::permission_denied(state, &user, uri));
    }
    let seasons: Vec<String> =
        sqlx::query_scalar("SELECT DISTINCT season FROM core_tournament ORDER BY season DESC")
            .fetch_all(&state.pool)
            .await?;
    let requested = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(name, _)| name == "season")
        .last()
        .map(|(_, season)| season.into_owned());
    let current = state.current_season.to_string();
    let season = requested
        .as_ref()
        .filter(|season| seasons.contains(season))
        .or_else(|| seasons.contains(&current).then_some(&current))
        .or_else(|| seasons.first());
    let leaderboards = match season {
        Some(season) => boards(&state.pool, season).await?,
        None => boards(&state.pool, "").await?,
    };
    let common = web::base_context(state, &user, uri);
    let bindings = context! {leaderboards, season,
        season_display => season.map_or_else(String::new, |season| season_label(season)),
        seasons => seasons.iter().map(|year| json!({"value":year,"label":season_label(year)})).collect::<Vec<_>>(),
    };
    match web::render("admin/doty.html", context! {..bindings, ..common}) {
        Ok(html) => Ok(([(header::VARY, "Cookie")], Html(html)).into_response()),
        Err(error) => {
            eprintln!("DOTY template failed: {error}");
            Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
    }
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let mut response = render(&state, &uri, &headers)
        .await
        .unwrap_or_else(|error| {
            eprintln!("DOTY preview failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        });
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("max-age=0, no-cache, no-store, must-revalidate, private"),
    );
    response.headers_mut().insert(
        header::EXPIRES,
        HeaderValue::from_str(&chrono::Utc::now().format("%a, %d %b %Y %T GMT").to_string())
            .unwrap(),
    );
    response
}
