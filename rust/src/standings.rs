use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    sync::Arc,
};

use axum::{
    Json,
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState, api_response, json_error, options,
    schedule::{proxy_url, qualification_label, requested_season, season_display},
    schools::{DEBATER_COLUMNS, Debater, School},
};

const GENERATION: &str = ":1:black-rod:rust:standings-generation:v1";

pub(crate) async fn generation(state: &AppState) -> Option<uuid::Uuid> {
    let Some(cache) = &state.memcached else {
        return Some(uuid::Uuid::nil());
    };
    cache.generation(GENERATION).await
}

pub(crate) async fn invalidate_native(state: &AppState) {
    state.home.invalidate_all();
    state.standings.invalidate_all();
    state.replay.invalidate_all();
    state.snapshots.invalidate_all();
    if let Some(cache) = &state.memcached {
        let _ = cache.advance_generation(GENERATION).await;
    }
}

pub(crate) async fn invalidate(state: &AppState, season: &str, boards: &[&str]) {
    use md5::{Digest, Md5};

    let legacy = async {
        if let Some(cache) = &state.memcached {
            let hash = Md5::digest(format!("{season}:"));
            let mut deletes = tokio::task::JoinSet::new();
            let keys = boards
                .iter()
                .map(|board| format!(":1:template.cache.{board}.{hash:x}"))
                .chain([
                    format!(":1:api:standings:{season}"),
                    format!(":1:api:standings_replay:{season}"),
                ]);
            for key in keys {
                let cache = cache.clone();
                deletes.spawn(async move {
                    cache.invalidate_raw(&key).await;
                });
            }
            while deletes.join_next().await.is_some() {}
        }
    };
    tokio::join!(invalidate_native(state), legacy);
    if let Ok(year) = season.parse() {
        crate::live::invalidate(state, year).await;
    }
}

pub(crate) const STORED_BOARDS: &[&str] = &["toty", "soty", "coty", "noty", "online_quals"];
const MARKER_NAMES: [&str; 6] = ["one", "two", "three", "four", "five", "six"];
const ONLINE_QUAL_BAR: f64 = 10.0;

#[derive(Serialize)]
struct Marker {
    slot: usize,
    points: f64,
    tournament: Value,
}

#[derive(Serialize)]
struct Standing {
    season: String,
    season_display: String,
    place_display: String,
    points: f64,
    markers: Vec<Marker>,
    #[serde(flatten)]
    entity: Value,
}

pub(crate) type Boards = Vec<(&'static str, Vec<Value>)>;

pub(crate) fn place_display(place: i64) -> String {
    match place {
        0 => String::new(),
        ..=1 => "1st".into(),
        2 => "2nd".into(),
        3..=4 => "Semi-Finalist".into(),
        5..=8 => "Quarter-Finalist".into(),
        9..=16 => "Octo-Finalist".into(),
        17..=32 => "Double Octo-Finalist".into(),
        33..=64 => "Quadruple Octo-Finalist".into(),
        _ => format!("Top {place}"),
    }
}

pub(crate) const LITE_DEBATER: &str = "
    CAST(d.id AS BIGINT) AS debater_id, d.first_name, d.last_name,
    CAST(s.id AS BIGINT) AS school_id, s.name AS school_name
";

pub(crate) fn lite_debater(row: &AnyRow, origin: &str) -> Result<Value, sqlx::Error> {
    let id = row.try_get::<i64, _>("debater_id")?;
    let first_name = row.try_get::<&str, _>("first_name")?;
    let last_name = row.try_get::<&str, _>("last_name")?;
    Ok(json!({
        "id": id,
        "name": format!("{first_name} {last_name}").trim(),
        "school_id": row.try_get::<Option<i64>, _>("school_id")?,
        "school_name": row.try_get::<Option<&str>, _>("school_name")?,
        "url": format!("{origin}/core/debaters/{id}"),
    }))
}

pub(crate) fn lite_school(id: i64, name: &str, origin: &str) -> Value {
    json!({"id": id, "name": name, "url": format!("{origin}/core/schools/{id}")})
}

async fn team_members(
    pool: &AnyPool,
    standings: &[AnyRow],
    origin: &str,
) -> Result<HashMap<i64, Vec<Value>>, sqlx::Error> {
    let mut seen = BTreeSet::new();
    let teams = standings
        .iter()
        .map(|row| row.try_get::<i64, _>("team_id"))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .filter(|id| seen.insert(*id))
        .collect::<Vec<_>>();
    let mut result: HashMap<i64, Vec<Value>> = HashMap::new();
    for teams in teams.chunks(900) {
        let parameters = (1..=teams.len())
            .map(|number| format!("${number}"))
            .collect::<Vec<_>>()
            .join(",");
        // Match Django's related-member query. Its database order is observable
        // in the serialized arrays and can differ from sorting debater IDs.
        let statement = format!(
            "
            SELECT CAST(td.team_id AS BIGINT) AS team_id, {LITE_DEBATER}
            FROM core_debater d
            JOIN core_team_debaters td ON td.debater_id = d.id
            LEFT JOIN core_school s ON s.id = d.school_id
            WHERE NOT (d.temporary OR d.synthetic) AND td.team_id IN ({parameters})
        "
        );
        let mut query = sqlx::query(AssertSqlSafe(statement));
        for team in teams {
            query = query.bind(team);
        }
        for row in query.fetch_all(pool).await? {
            result
                .entry(row.try_get("team_id")?)
                .or_default()
                .push(lite_debater(&row, origin)?);
        }
    }
    Ok(result)
}

pub(crate) async fn coty_breakdowns(
    pool: &AnyPool,
    season: i32,
    origin: &str,
    school: Option<i64>,
    alphabetize_qualifications: bool,
) -> Result<HashMap<i64, Vec<Value>>, ApiError> {
    let season_string = season.to_string();
    let quals = sqlx::query(
        "
        SELECT CAST(q.debater_id AS BIGINT) AS debater_id,
               CAST(q.qual_type AS BIGINT) AS qual_type
        FROM core_qual q
        JOIN core_debater d ON d.id = q.debater_id
        WHERE q.season = $1
          AND (d.school_id = $2 OR (CAST($2 AS BIGINT) IS NULL AND EXISTS (
            SELECT 1 FROM core_coty c WHERE c.school_id = d.school_id AND c.season = $1
          )))
    ",
    )
    .bind(&season_string)
    .bind(school)
    .fetch_all(pool)
    .await?;
    let mut qual_types: HashMap<i64, BTreeSet<i64>> = HashMap::new();
    for row in quals {
        qual_types
            .entry(row.try_get("debater_id")?)
            .or_default()
            .insert(row.try_get("qual_type")?);
    }
    let query = format!("
        SELECT {DEBATER_COLUMNS}, CAST(s.id AS BIGINT) AS school_id, s.name AS school_name,
               CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included,
               CAST(p.points AS DOUBLE PRECISION) AS points
        FROM core_debater d
        JOIN core_school s ON s.id = d.school_id
        LEFT JOIN core_qualpoints p ON p.debater_id = d.id AND p.season = $1
        WHERE (s.id = $2 OR (CAST($2 AS BIGINT) IS NULL AND EXISTS (
            SELECT 1 FROM core_coty c WHERE c.school_id = s.id AND c.season = $1
        )))
          AND (p.points > 0 OR EXISTS (SELECT 1 FROM core_qual q WHERE q.debater_id = d.id AND q.season = $1))
        ORDER BY p.points DESC NULLS LAST, d.id, p.id
    ");
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(&season_string)
        .bind(school)
        .fetch_all(pool)
        .await?;
    let mut result: HashMap<i64, Vec<Value>> = HashMap::new();
    for row in rows {
        let school = School::new(
            row.try_get("school_id")?,
            row.try_get("school_name")?,
            row.try_get::<i64, _>("included")? != 0,
            origin,
        );
        let id = row.try_get("id")?;
        let types = qual_types.get(&id);
        let points = row.try_get::<Option<f64>, _>("points")?.unwrap_or(0.0);
        let bonus = types.is_some_and(|types| {
            types
                .iter()
                .any(|code| season < 2026 || !matches!(code, 1 | 2 | 3 | 5 | 6 | 16))
        });
        let mut named_quals = types
            .into_iter()
            .flatten()
            .copied()
            .filter(|code| *code > 0)
            .map(qualification_label)
            .filter_map(|label| label.as_str().map(str::to_owned))
            .collect::<Vec<_>>();
        if alphabetize_qualifications {
            named_quals.sort_unstable();
            named_quals.dedup();
        }
        result.entry(school.id).or_default().push(json!({
            "debater": Debater::from_row(&row, origin, Some(&school))?,
            "points": points,
            "qualled": types.is_some(),
            "contribution": if points >= 60.0 {
                json!(60 + if bonus { 6 } else { 0 })
            } else {
                json!(points + if bonus { 6.0 } else { 0.0 })
            },
            "auto_quals": named_quals.join(", "),
        }));
    }
    Ok(result)
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum View {
    Api,
    Replay,
    Home,
}

pub(crate) enum Board {
    Teams,
    Speakers {
        table: &'static str,
        markers: usize,
        online: bool,
    },
    Schools,
}

impl Board {
    pub(crate) async fn rows(
        &self,
        pool: &AnyPool,
        season: i32,
        origin: &str,
        view: View,
    ) -> Result<Vec<Value>, ApiError> {
        let (table, entity_columns, entity_join, marker_count) = match self {
            Self::Teams => (
                "core_toty",
                "CAST(t.id AS BIGINT) AS team_id, t.name AS team_name",
                "JOIN core_team t ON t.id = b.team_id",
                5,
            ),
            Self::Speakers { table, markers, .. } => (
                *table,
                LITE_DEBATER,
                "JOIN core_debater d ON d.id = b.debater_id LEFT JOIN core_school s ON s.id = d.school_id",
                if view == View::Home { 6 } else { *markers },
            ),
            Self::Schools => (
                "core_coty",
                "CAST(s.id AS BIGINT) AS school_id, s.name AS school_name",
                "JOIN core_school s ON s.id = b.school_id",
                0,
            ),
        };
        let markers = &MARKER_NAMES[..marker_count];
        // Django joins the sixth TOTY marker for the public list, although it
        // only serializes five. The extra join can affect the order of exact ties.
        let selected_markers =
            if matches!(view, View::Api | View::Replay) && matches!(self, Self::Teams) {
                &MARKER_NAMES[..]
            } else {
                markers
            };
        let mut columns = String::new();
        let mut joins = String::new();
        for marker in selected_markers {
            columns.push_str(&format!(
                ",
                CAST(b.marker_{marker} AS DOUBLE PRECISION) AS points_{marker},
                CAST(m_{marker}.id AS BIGINT) AS id_{marker},
                m_{marker}.name AS name_{marker},
                CAST(m_{marker}.date AS TEXT) AS date_{marker}"
            ));
            joins.push_str(&format!(
                " LEFT JOIN core_tournament m_{marker} ON m_{marker}.id = b.tournament_{marker}_id"
            ));
        }
        let order = match view {
            // The list slices these results after sorting. A new tie-breaker
            // could change which entry appears at an existing client's limit.
            View::Api | View::Replay => "b.points DESC, b.place",
            View::Home => "b.points DESC",
        };
        let query = format!("
            SELECT CAST(b.id AS BIGINT) AS standing_id,
                   CAST(b.place AS BIGINT) AS place, CAST(b.points AS DOUBLE PRECISION) AS points,
                   CAST(CASE WHEN b.tied THEN 1 ELSE 0 END AS BIGINT) AS tied, {entity_columns}{columns}
            FROM {table} b {entity_join} {joins}
            WHERE b.season = $1
            ORDER BY {order}
        ");
        let season_string = season.to_string();
        let rows = sqlx::query(AssertSqlSafe(query))
            .bind(&season_string)
            .fetch_all(pool)
            .await?;
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        let team_members = if matches!(self, Self::Teams) {
            team_members(pool, &rows, origin).await?
        } else {
            HashMap::new()
        };
        let breakdowns = if matches!(self, Self::Schools) {
            coty_breakdowns(pool, season, origin, None, view != View::Home).await?
        } else {
            HashMap::new()
        };
        let mut result = Vec::with_capacity(rows.len());
        for row in rows {
            let points: f64 = row.try_get("points")?;
            let mut entity = match self {
                Self::Teams => {
                    let id = row.try_get::<i64, _>("team_id")?;
                    json!({"team": {
                        "id": id,
                        "name": row.try_get::<&str, _>("team_name")?,
                        "debaters": team_members.get(&id).map(Vec::as_slice).unwrap_or_default(),
                        "url": format!("{origin}/core/teams/{id}"),
                    }})
                }
                Self::Speakers { online, .. } => {
                    let school = match row.try_get::<Option<i64>, _>("school_id")? {
                        Some(id) => lite_school(id, row.try_get("school_name")?, origin),
                        None => Value::Null,
                    };
                    let mut result =
                        json!({"debater": lite_debater(&row, origin)?, "school": school});
                    if *online {
                        result["qualified"] = json!(points >= ONLINE_QUAL_BAR);
                    }
                    result
                }
                Self::Schools => {
                    let id = row.try_get("school_id")?;
                    json!({
                        "school": lite_school(id, row.try_get("school_name")?, origin),
                        "breakdown": breakdowns.get(&id).map(Vec::as_slice).unwrap_or_default(),
                    })
                }
            };
            if view != View::Api {
                entity["id"] = json!(row.try_get::<i64, _>("standing_id")?);
                entity["place"] = json!(row.try_get::<i64, _>("place")?);
                entity["tied"] = json!(row.try_get::<i64, _>("tied")? != 0);
            }
            let mut tournament_markers = Vec::new();
            for (index, marker) in markers.iter().enumerate() {
                let points: f64 = row.try_get(format!("points_{marker}").as_str())?;
                if view == View::Home {
                    entity[format!("marker_{marker}")] = json!(points);
                    entity[format!("tournament_{marker}")] = json!({"display": "", "url": ""});
                }
                if points == 0.0 {
                    continue;
                }
                let Some(id) = row.try_get::<Option<i64>, _>(format!("id_{marker}").as_str())?
                else {
                    continue;
                };
                let name = row.try_get::<&str, _>(format!("name_{marker}").as_str())?;
                let tournament = json!({
                        "id": id,
                        "name": name,
                        "display": name.split(" (").next().unwrap_or_default(),
                        "date": row.try_get::<Option<&str>, _>(format!("date_{marker}").as_str())?,
                        "url": format!("{origin}/core/tournaments/{id}"),
                });
                if view == View::Home {
                    entity[format!("tournament_{marker}")] = tournament.clone();
                }
                tournament_markers.push(Marker {
                    slot: index + 1,
                    points,
                    tournament,
                });
            }
            result.push(serde_json::to_value(Standing {
                season: season_string.clone(),
                season_display: season_display(season),
                place_display: place_display(row.try_get("place")?),
                points,
                markers: tournament_markers,
                entity,
            })?);
        }
        Ok(result)
    }
}

fn render_noty(season: i32) -> bool {
    season <= 2020
}
fn using_online_quals(season: i32) -> bool {
    matches!(season, 2020 | 2021)
}

pub(crate) async fn boards(
    pool: &AnyPool,
    season: i32,
    origin: &str,
    view: View,
) -> Result<Arc<Boards>, ApiError> {
    let mut boards = vec![
        ("toty", Board::Teams),
        (
            "soty",
            Board::Speakers {
                table: "core_soty",
                markers: 6,
                online: false,
            },
        ),
        ("coty", Board::Schools),
    ];
    if render_noty(season) || view == View::Home {
        boards.push((
            "noty",
            Board::Speakers {
                table: "core_noty",
                markers: 5,
                online: false,
            },
        ));
    }
    if using_online_quals(season) {
        boards.push((
            "online_quals",
            Board::Speakers {
                table: "core_onlinequal",
                markers: 6,
                online: true,
            },
        ));
    }
    let mut result = Vec::with_capacity(boards.len());
    for (name, board) in boards {
        result.push((name, board.rows(pool, season, origin, view).await?));
    }
    Ok(Arc::new(result))
}

pub(crate) fn filter_boards<'a>(
    boards: &'a Boards,
    uri: &Uri,
) -> BTreeMap<&'static str, &'a [Value]> {
    let mut selected = BTreeSet::new();
    let raw = uri.query().unwrap_or_default();
    let limit = crate::routing::limit(raw).map(|limit| limit as usize);
    for (key, value) in url::form_urlencoded::parse(raw.as_bytes()) {
        if key == "board" {
            for board in value
                .split(',')
                .map(|name| name.trim().to_ascii_lowercase())
            {
                if let Some(name) = STORED_BOARDS.iter().copied().find(|name| *name == board) {
                    selected.insert(name);
                }
            }
        }
    }
    let filtered = boards
        .iter()
        .filter(|(name, _)| selected.contains(name))
        .count();
    boards
        .iter()
        .filter(|(name, _)| filtered == 0 || selected.contains(name))
        .map(|(name, rows)| (*name, &rows[..rows.len().min(limit.unwrap_or(rows.len()))]))
        .collect()
}

pub(crate) async fn cached_boards(
    state: &AppState,
    season: i32,
    origin: &str,
    view: View,
) -> Result<Arc<Boards>, Arc<ApiError>> {
    let load = boards(&state.pool, season, origin, view);
    match (generation(state).await, view) {
        (Some(generation), View::Home) => state.home.try_get_with((generation, season), load).await,
        (Some(generation), View::Api) => {
            state
                .standings
                .try_get_with((generation, origin.to_owned(), season), load)
                .await
        }
        _ => load.await.map_err(Arc::new),
    }
}

pub(crate) async fn standings(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let season = requested_season(uri.query().unwrap_or_default(), state.current_season);
    let boards = cached_boards(&state, season, &origin, View::Api).await;
    let Ok(boards) = boards else {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error.");
    };
    let endpoint = uri
        .path_and_query()
        .map(|path| path.as_str())
        .unwrap_or_default();
    let available_seasons = available_seasons(state.current_season);
    #[derive(Serialize)]
    struct Standings<'a> {
        season: String,
        season_display: String,
        available_seasons: Vec<Value>,
        render_noty: bool,
        using_online_quals: bool,
        online_qual_bar: u8,
        standings: BTreeMap<&'static str, &'a [Value]>,
        links: Value,
    }
    api_response(Json(Standings {
        season: season.to_string(),
        season_display: season_display(season),
        available_seasons,
        render_noty: render_noty(season),
        using_online_quals: using_online_quals(season),
        online_qual_bar: ONLINE_QUAL_BAR as u8,
        standings: filter_boards(&boards, &uri),
        links: json!({"self": proxy_url(&origin, endpoint)}),
    }))
}

pub(crate) fn available_seasons(current_season: i32) -> Vec<Value> {
    (2004..=current_season)
        .rev()
        .map(|year| json!({"value": year.to_string(), "label": season_display(year)}))
        .collect()
}

pub(crate) async fn standings_options() -> Response {
    options(
        "Season Standings Api",
        "Return TOTY/COTY/SOTY/NOTY/Online standings in a machine-friendly format.\nRequires a season query parameter (defaults to CURRENT_SEASON).",
    )
}
