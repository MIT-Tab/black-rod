use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    sync::Arc,
};

use axum::{
    Json,
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use chrono::{Datelike, NaiveDate};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState, api_response, json_error, options,
    points::{speaker_points_for_size, team_points_for_size},
    schedule::{qualification_label, requested_season, season_display},
    standings::{
        Board, Boards, LITE_DEBATER, View, available_seasons, filter_boards, lite_debater,
        lite_school,
    },
};

const TOURNAMENT: &str = "
    CAST(t.id AS BIGINT) AS tournament_id, t.name AS tournament_name,
    CAST(t.date AS TEXT) AS earned_on, CAST(t.num_teams AS BIGINT) AS num_teams
";
const TEAM_RESULTS: &str = "
    SELECT {tournament}, CAST(r.id AS BIGINT) AS result_id, CAST(r.place AS BIGINT) AS place,
           CAST(r.team_id AS BIGINT) AS source_id,
           CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost
    FROM core_teamresult r JOIN core_tournament t ON t.id = r.tournament_id
    WHERE t.season = $1 AND t.toty AND r.type_of_place = 1 AND r.counts_for_points
";
const SPEAKER_RESULTS: &str = "
    SELECT {tournament}, CAST(r.id AS BIGINT) AS result_id, CAST(r.place AS BIGINT) AS place,
           CAST(r.debater_id AS BIGINT) AS source_id,
           CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS tie
    FROM core_speakerresult r JOIN core_tournament t ON t.id = r.tournament_id
    WHERE t.season = $1 AND t.soty AND r.type_of_place = 1 AND r.counts_for_points
";
const TEAM_REAFF: &str = "
    SELECT CAST(new_team_id AS BIGINT) AS new_id, CAST(old_team_id AS BIGINT) AS old_id
    FROM core_totyreaff WHERE season = $1
";
const SPEAKER_REAFF: &str = "
    SELECT CAST(new_debater_id AS BIGINT) AS new_id, CAST(old_debater_id AS BIGINT) AS old_id
    FROM core_reaff WHERE season = $1
";
const COTY_EVENTS: &str = "
    WITH events AS (
        SELECT r.id AS result_id, r.tournament_id, dtd.debater_id,
               r.place, r.ghost_points, 0 AS manual
        FROM core_teamresult r JOIN core_tournament t ON t.id = r.tournament_id
        JOIN core_team_debaters dtd ON dtd.team_id = r.team_id
        JOIN core_debater d ON d.id = dtd.debater_id
        WHERE t.season = $1 AND r.type_of_place = 1 AND r.counts_for_points
          AND NOT d.temporary AND NOT d.synthetic
        UNION ALL
        SELECT r.id, r.tournament_id, r.debater_id, NULL, FALSE, 1
        FROM core_tournamentautoqual r JOIN core_tournament t ON t.id = r.tournament_id
        WHERE t.season = $1 AND t.qual_type IN (1,2,3,4,5,6,7,8,9,10,15,16)
    )
    SELECT {tournament}, {debater}, CAST(events.result_id AS BIGINT) AS result_id,
           CAST(events.place AS BIGINT) AS place, CAST(events.manual AS BIGINT) AS manual,
           CAST(CASE WHEN events.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
           CAST(CASE WHEN t.qual THEN 1 ELSE 0 END AS BIGINT) AS qual,
           CAST(t.qual_type AS BIGINT) AS qual_type, CAST(t.autoqual_bar AS BIGINT) AS autoqual_bar
    FROM events
    JOIN core_tournament t ON t.id = events.tournament_id
    JOIN core_debater d ON d.id = events.debater_id
    JOIN core_school s ON s.id = d.school_id
    WHERE s.included_in_oty
    ORDER BY t.date, t.id, events.manual, events.result_id, d.id
";
const MARKER_LIMITS: [(&str, usize); 3] = [("toty", 5), ("soty", 6), ("coty", 0)];

pub(crate) struct Replay {
    boards: Boards,
    timeline_dates: BTreeSet<String>,
}

fn date(row: &AnyRow) -> Result<&str, sqlx::Error> {
    row.try_get("earned_on")
}

fn tournament(row: &AnyRow, origin: &str) -> Result<Value, sqlx::Error> {
    let id: i64 = row.try_get("tournament_id")?;
    let name: &str = row.try_get("tournament_name")?;
    Ok(json!({
        "id": id,
        "name": name,
        "display": name.split(" (").next().unwrap_or_default(),
        "date": date(row)?,
        "url": format!("{origin}/core/tournaments/{id}"),
    }))
}

#[derive(Clone, Copy)]
enum Results {
    Team,
    Speaker,
}

impl Results {
    fn entity(self) -> &'static str {
        match self {
            Self::Team => "team",
            Self::Speaker => "debater",
        }
    }

    async fn add_markers(
        self,
        pool: &AnyPool,
        season: i32,
        origin: &str,
        standings: &mut [Value],
        dates: &mut BTreeSet<String>,
    ) -> Result<(), ApiError> {
        let (query, reaff_query, source_key) = match self {
            Self::Team => (TEAM_RESULTS, TEAM_REAFF, "source_team_id"),
            Self::Speaker => (SPEAKER_RESULTS, SPEAKER_REAFF, "source_debater_id"),
        };
        let mut reaffs: HashMap<i64, Vec<i64>> = HashMap::new();
        let season = season.to_string();
        for row in sqlx::query(reaff_query)
            .bind(&season)
            .fetch_all(pool)
            .await?
        {
            reaffs
                .entry(row.try_get("new_id")?)
                .or_default()
                .push(row.try_get("old_id")?);
        }
        let mut markers: HashMap<i64, Vec<Value>> = HashMap::new();
        for row in sqlx::query(AssertSqlSafe(query.replace("{tournament}", TOURNAMENT)))
            .bind(&season)
            .fetch_all(pool)
            .await?
        {
            let size = row.try_get("num_teams")?;
            let place: i64 = row.try_get("place")?;
            let source_id = row.try_get::<i64, _>("source_id")?;
            let (points, kind, flag) = match self {
                Self::Team => {
                    let ghost = row.try_get::<i64, _>("ghost")? != 0;
                    (
                        team_points_for_size(size, place, ghost),
                        "ghost_points",
                        ghost,
                    )
                }
                Self::Speaker => {
                    let tie = row.try_get::<i64, _>("tie")? != 0;
                    (
                        speaker_points_for_size(size, place - i64::from(tie)),
                        "tie",
                        tie,
                    )
                }
            };
            if points > 0.0 {
                markers.entry(source_id).or_default().push(json!({
                    "points": if matches!(self, Self::Team)
                        && (place <= 2 || (size >= 80 && (place <= 8 || (place == 9 && flag))))
                    {
                        json!(points as i64)
                    } else {
                        json!(points)
                    },
                    "place": place,
                    (kind): flag,
                    "type": "Varsity",
                    "earned_on": date(&row)?,
                    "tournament": tournament(&row, origin)?,
                    "result_id": row.try_get::<i64, _>("result_id")?,
                    (source_key): source_id,
                    "from_reaff": false,
                }));
            }
        }
        for entry in standings {
            let id = integer(&entry[self.entity()]["id"]);
            let mut all = markers.get(&id).cloned().unwrap_or_default();
            for old_id in reaffs.get(&id).into_iter().flatten() {
                for old in markers.get(old_id).into_iter().flatten() {
                    let mut marker = old.clone();
                    marker["from_reaff"] = json!(true);
                    all.push(marker);
                }
            }
            all.sort_by(|a, b| {
                date_key(a)
                    .cmp(date_key(b))
                    .then_with(|| points(b).total_cmp(&points(a)))
                    .then_with(|| integer(&a["result_id"]).cmp(&integer(&b["result_id"])))
            });
            dates.extend(all.iter().map(date_key).map(str::to_owned));
            entry["all_markers"] = Value::Array(all);
        }
        Ok(())
    }
}

#[derive(Default)]
struct Qualifications {
    raw: f64,
    capped: f64,
    qualified: bool,
    bonus: bool,
}

fn integer(value: &Value) -> i64 {
    value.as_i64().unwrap_or_default()
}
fn points(marker: &Value) -> f64 {
    marker["points"].as_f64().unwrap_or_default()
}
fn date_key(marker: &Value) -> &str {
    marker["earned_on"].as_str().unwrap_or_default()
}
fn rounded(value: f64) -> f64 {
    (value * 10_000.0).round_ties_even() / 10_000.0
}

async fn coty(
    pool: &AnyPool,
    season: i32,
    origin: &str,
    dates: &mut BTreeSet<String>,
) -> Result<Vec<Value>, ApiError> {
    if matches!(season, 2020 | 2021) {
        return Ok(Vec::new());
    }
    let query = COTY_EVENTS
        .replace("{tournament}", TOURNAMENT)
        .replace("{debater}", LITE_DEBATER);
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(season.to_string())
        .fetch_all(pool)
        .await?;
    let mut qualifications: HashMap<i64, Qualifications> = HashMap::new();
    let mut by_school: HashMap<i64, (Value, Vec<Value>)> = HashMap::new();
    for row in rows {
        let school_id = row.try_get::<i64, _>("school_id")?;
        let debater_id = row.try_get("debater_id")?;
        let (school, markers) = by_school
            .entry(school_id)
            .or_insert_with(|| (Value::Null, Vec::new()));
        if school.is_null() {
            *school = lite_school(school_id, row.try_get("school_name")?, origin);
        }
        let state = qualifications.entry(debater_id).or_default();
        let manual = row.try_get::<i64, _>("manual")? != 0;
        let ghost = row.try_get::<i64, _>("ghost")? != 0;
        let qual = row.try_get::<i64, _>("qual")? != 0;
        let place = row.try_get::<Option<i64>, _>("place")?;
        let qual_type = row.try_get::<i64, _>("qual_type")?;
        let autoqual_bar = row.try_get::<i64, _>("autoqual_bar")?;
        let mut make_marker = |kind: &str,
                               points: f64,
                               raw: Option<f64>,
                               qual_type: Option<i64>|
         -> Result<(), ApiError> {
            let result_id = row.try_get::<i64, _>("result_id")?;
            let mut marker = json!({
                "points": rounded(points),
                "marker_type": kind,
                "place": place,
                "ghost_points": ghost,
                "earned_on": date(&row)?,
                "tournament": tournament(&row, origin)?,
                "result_id": (!manual).then_some(result_id),
                "debater": lite_debater(&row, origin)?,
            });
            if let Some(raw) = raw {
                marker["raw_points"] = json!(rounded(raw));
            }
            if let Some(qual_type) = qual_type {
                let label = match qual_type {
                    0 => json!("Points"),
                    1..=16 => qualification_label(qual_type),
                    _ => json!("Qual"),
                };
                marker["qualification"] = json!({"type": qual_type, "label": label});
            }
            markers.push(marker);
            Ok(())
        };
        if qual && let Some(place) = place {
            let raw = team_points_for_size(row.try_get("num_teams")?, place, ghost);
            let delta = raw.min((60.0 - state.capped).max(0.0));
            state.raw += raw;
            if delta > 0.0 {
                state.capped += delta;
                make_marker("qual_points", delta, Some(raw), None)?;
            }
        }
        let autoqual = manual
            || (qual_type != 8 && place.is_some_and(|place| place > 0 && place <= autoqual_bar));
        let points_qual = state.raw >= 12.0;
        let autoqual_awards_bonus = season < 2026 || !matches!(qual_type, 1 | 2 | 3 | 5 | 6 | 16);
        let new_bonus = !state.bonus && (points_qual || (autoqual && autoqual_awards_bonus));
        if new_bonus || (!state.qualified && (points_qual || autoqual)) {
            state.qualified = true;
            state.bonus |= new_bonus;
            let qualification = if autoqual && (!new_bonus || autoqual_awards_bonus) {
                qual_type
            } else {
                0
            };
            let (marker_type, marker_points) = if new_bonus {
                ("qual_bonus", 6.0)
            } else {
                ("autoqual", 0.0)
            };
            make_marker(marker_type, marker_points, None, Some(qualification))?;
        }
    }
    let mut result = Vec::new();
    for (school, mut markers) in by_school.into_values() {
        let points_total = rounded(markers.iter().map(points).sum());
        if points_total <= 0.0 {
            continue;
        }
        markers.sort_by(|a, b| {
            fn marker_order(marker: &Value) -> i32 {
                match marker["marker_type"].as_str() {
                    Some("qual_points") => 0,
                    Some("qual_bonus") => 1,
                    _ => 99,
                }
            }
            let key = |marker: &Value| {
                (
                    integer(&marker["tournament"]["id"]),
                    integer(&marker["result_id"]),
                    integer(&marker["debater"]["id"]),
                    marker_order(marker),
                )
            };
            date_key(a)
                .cmp(date_key(b))
                .then_with(|| key(a).cmp(&key(b)))
        });
        dates.extend(markers.iter().map(date_key).map(str::to_owned));
        result.push(json!({
            "season": season.to_string(),
            "season_display": season_display(season),
            "points": points_total,
            "school": school,
            "markers": [],
            "all_markers": markers,
        }));
    }
    result.sort_by(|a, b| {
        points(b)
            .total_cmp(&points(a))
            .then_with(|| entity_name(a, "school").cmp(&entity_name(b, "school")))
    });
    assign_places(&mut result);
    Ok(result)
}

fn entity_name(row: &Value, entity: &str) -> String {
    row[entity]["name"]
        .as_str()
        .unwrap_or_default()
        .to_lowercase()
}

fn assign_places(rows: &mut [Value]) {
    let mut place = 1;
    let mut previous = None::<f64>;
    for index in 0..rows.len() {
        let score = points(&rows[index]);
        let tied = previous.is_some_and(|value| (score - value).abs() < 0.001);
        if tied {
            rows[index - 1]["tied"] = json!(true);
        } else {
            place = index + 1;
        }
        rows[index]["tied"] = json!(tied);
        rows[index]["place"] = json!(place);
        rows[index]["place_display"] = json!(place);
        previous = Some(score);
    }
}

async fn load(pool: &AnyPool, season: i32, origin: &str) -> Result<Arc<Replay>, ApiError> {
    let mut dates = BTreeSet::new();
    let mut teams = Board::Teams
        .rows(pool, season, origin, View::Replay)
        .await?;
    let mut speakers = Board::Speakers {
        table: "core_soty",
        markers: 6,
        online: false,
    }
    .rows(pool, season, origin, View::Replay)
    .await?;
    Results::Team
        .add_markers(pool, season, origin, &mut teams, &mut dates)
        .await?;
    Results::Speaker
        .add_markers(pool, season, origin, &mut speakers, &mut dates)
        .await?;
    let schools = coty(pool, season, origin, &mut dates).await?;
    Ok(Arc::new(Replay {
        boards: vec![("toty", teams), ("soty", speakers), ("coty", schools)],
        timeline_dates: dates,
    }))
}

fn snapshot(replay: &Replay, through: &str) -> Boards {
    let boards = replay.boards.iter().zip(MARKER_LIMITS);
    boards
        .map(|((board, rows), (_, limit))| {
            let entity = match *board {
                "toty" => "team",
                "soty" => "debater",
                _ => "school",
            };
            let mut partial = Vec::new();
            for row in rows {
                let markers = row["all_markers"]
                    .as_array()
                    .map(Vec::as_slice)
                    .unwrap_or_default();
                let mut markers: Vec<_> =
                    markers.iter().filter(|m| date_key(m) <= through).collect();
                if markers.is_empty() {
                    continue;
                }
                markers.sort_by(|a, b| {
                    points(b)
                        .total_cmp(&points(a))
                        .then_with(|| date_key(a).cmp(date_key(b)))
                        .then_with(|| integer(&a["result_id"]).cmp(&integer(&b["result_id"])))
                });
                if limit != 0 {
                    markers.truncate(limit);
                }
                let markers: Vec<_> = markers
                    .into_iter()
                    .map(|marker| {
                        let mut marker = marker.clone();
                        marker["points"] = json!(rounded(points(&marker)));
                        marker
                    })
                    .collect();
                let score = rounded(markers.iter().map(points).sum());
                let mut item = json!({
                    "id": row["id"],
                    "season": row["season"],
                    "season_display": row["season_display"],
                    "markers": markers,
                    "points": score,
                    "type": entity,
                });
                for key in ["team", "debater", "school"] {
                    if !row[key].is_null() {
                        item[key] = row[key].clone();
                    }
                }
                partial.push((item, integer(&row["place"]), entity_name(row, entity)));
            }
            partial.sort_by(|(a, place_a, name_a), (b, place_b, name_b)| {
                points(b)
                    .total_cmp(&points(a))
                    .then_with(|| place_a.cmp(place_b))
                    .then_with(|| name_a.cmp(name_b))
            });
            let mut partial: Vec<_> = partial.into_iter().map(|(item, _, _)| item).collect();
            assign_places(&mut partial);
            (*board, partial)
        })
        .collect()
}

fn resolve_date(value: &str) -> Option<NaiveDate> {
    if value.len() != 10
        || value.bytes().enumerate().any(|(index, byte)| {
            if matches!(index, 4 | 7) {
                byte != b'-'
            } else {
                !byte.is_ascii_digit()
            }
        })
    {
        return None;
    }
    let date = NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()?;
    (date.year() > 0).then_some(date)
}

pub(crate) async fn handle(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let query = uri.query().unwrap_or_default();
    let season = requested_season(query, state.current_season);
    let path = uri
        .path_and_query()
        .map(|path| path.as_str())
        .unwrap_or_default();
    let through = if uri.path() == "/api/standings/through-date/" {
        let (mut through, mut date) = (None, None);
        for (name, value) in url::form_urlencoded::parse(query.as_bytes()) {
            match name.as_ref() {
                "through" => through = Some(value),
                "date" => date = Some(value),
                _ => (),
            }
        }
        let Some(value) = through
            .filter(|value| !value.is_empty())
            .or(date)
            .filter(|value| !value.is_empty())
        else {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Provide a through=YYYY-MM-DD query parameter.",
            );
        };
        let Some(date) = resolve_date(&value) else {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Invalid through date; use YYYY-MM-DD.",
            );
        };
        Some(date.to_string())
    } else {
        None
    };
    let generation = crate::standings::generation(&state).await;
    let load = load(&state.pool, season, &origin);
    let replay = match generation {
        Some(generation) => {
            state
                .replay
                .try_get_with((generation, origin.clone(), season), load)
                .await
        }
        None => load.await.map_err(Arc::new),
    };
    let Ok(replay) = replay else {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error.");
    };
    let marker_limits = MARKER_LIMITS.into_iter().collect();
    let html = format!("{origin}/standings/replay/?season={season}");
    #[derive(Serialize)]
    struct Payload<'a> {
        season: String,
        season_display: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        available_seasons: Option<Vec<Value>>,
        #[serde(skip_serializing_if = "Option::is_none")]
        timeline_dates: Option<&'a BTreeSet<String>>,
        #[serde(skip_serializing_if = "Option::is_none")]
        through: Option<String>,
        marker_limits: BTreeMap<&'static str, usize>,
        standings: BTreeMap<&'static str, &'a [Value]>,
        links: Value,
    }
    let season_display = season_display(season);
    if let Some(through) = through {
        let compute = async { Arc::new(snapshot(&replay, &through)) };
        let partial = match generation {
            Some(generation) => {
                state
                    .snapshots
                    .get_with(
                        (generation, origin.clone(), season, through.clone()),
                        compute,
                    )
                    .await
            }
            None => compute.await,
        };
        api_response(Json(Payload {
            season: season.to_string(),
            season_display,
            through: Some(through),
            marker_limits,
            standings: filter_boards(&partial, &uri),
            available_seasons: None,
            timeline_dates: None,
            links: json!({
                "self": format!("{origin}{path}"),
                "replay_api": format!("{origin}/api/standings/replay/?season={season}"),
                "replay_html": html,
                "full_standings": format!("{origin}/api/standings/?season={season}"),
            }),
        }))
    } else {
        api_response(Json(Payload {
            season: season.to_string(),
            season_display,
            available_seasons: Some(available_seasons(state.current_season)),
            timeline_dates: Some(&replay.timeline_dates),
            through: None,
            marker_limits,
            standings: filter_boards(&replay.boards, &uri),
            links: json!({"self": format!("{origin}{path}"), "html": html}),
        }))
    }
}

pub(crate) async fn replay_options() -> Response {
    options(
        "Season Standings Replay Api",
        "Return TOTY/SOTY/COTY standings along with all season markers for replay visualizations.",
    )
}

pub(crate) async fn through_options() -> Response {
    options(
        "Standings Through Date Api",
        "Return standings snapshots through a supplied ISO date.",
    )
}
