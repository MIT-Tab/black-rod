pub(crate) mod edit;

use std::collections::{HashMap, HashSet};

use axum::{
    Json,
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState, api_response, is_django_integer, json_error, options,
    schedule::{TOURNAMENT_COLUMNS, Tournament, proxy_url},
    schools::{DEBATER_COLUMNS, Debater},
    session::{self, User},
    standings::place_display,
    team_profile::{SCHOOL_COLUMNS, Team, cards, members, read_transaction, school},
};

const TOURNAMENT: &str = "
    SELECT {columns}
    FROM core_tournament t LEFT JOIN core_school s ON s.id = t.host_id
    WHERE t.id = $1
";
const AWARDS: &str = "
    SELECT CAST(r.id AS BIGINT) AS id, CAST(r.team_id AS BIGINT) AS team_id,
           CAST(r.place AS BIGINT) AS place, CAST(r.type_of_place AS BIGINT) AS division,
           CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
           t.name AS team_name
    FROM core_teamresult r JOIN core_team t ON t.id = r.team_id
    WHERE r.tournament_id = $1 AND r.place > 0 AND r.type_of_place IN (0,1)
    ORDER BY r.type_of_place DESC, r.place
";
const SPEAKERS: &str = "
    SELECT {debater}, {school}, CAST(r.id AS BIGINT) AS result_id,
           CAST(r.place AS BIGINT) AS place, CAST(r.type_of_place AS BIGINT) AS division,
           CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS tie
    FROM core_speakerresult r JOIN core_debater d ON d.id = r.debater_id
    LEFT JOIN core_school s ON s.id = d.school_id
    WHERE r.tournament_id = $1 AND r.type_of_place IN (0,1)
    ORDER BY r.type_of_place DESC, r.place
";
const TEAMS_WITH_ROUNDS: &str = "
    SELECT CAST(t.id AS BIGINT) AS id, t.name
    FROM core_team t WHERE EXISTS (SELECT 1 FROM core_round r
        WHERE r.tournament_id = $1 AND (r.gov_id = t.id OR r.opp_id = t.id))
    ORDER BY t.id
";
const VIDEO_COLUMNS: &str = "
    CAST(v.id AS BIGINT) AS id, CAST(v.round AS BIGINT) AS round,
    CAST(v.permissions AS BIGINT) AS permissions,
    CAST(v.pm_id AS BIGINT) AS pm_id, CAST(v.lo_id AS BIGINT) AS lo_id,
    CAST(v.mg_id AS BIGINT) AS mg_id, CAST(v.mo_id AS BIGINT) AS mo_id,
    v.link, v.description, v.\"case\"
";

fn debater(row: &AnyRow, origin: &str) -> Result<Value, ApiError> {
    let school = school(row, origin)?;
    Ok(serde_json::to_value(Debater::from_row(
        row,
        origin,
        school.as_ref(),
    )?)?)
}

pub(crate) async fn debater_by_id(
    conn: &mut AnyConnection,
    id: i64,
    origin: &str,
) -> Result<Value, ApiError> {
    let query = format!(
        "SELECT {DEBATER_COLUMNS}, {SCHOOL_COLUMNS} FROM core_debater d LEFT JOIN core_school s ON s.id = d.school_id WHERE d.id = $1"
    );
    let row = sqlx::query(AssertSqlSafe(query))
        .bind(id)
        .fetch_one(conn)
        .await?;
    debater(&row, origin)
}

pub(crate) fn labels(round: i64, permissions: i64) -> (Value, Value) {
    let round_label = match round {
        0 => json!("UNKNOWN"),
        1..=5 => json!(round.to_string()),
        14 => json!("6"),
        6 => json!("Varsity Octafinals"),
        7 => json!("Varsity Quarterfinals"),
        8 => json!("Varsity Semifinals"),
        9 => json!("Varsity Finals"),
        10 => json!("Novice Quarterfinals"),
        11 => json!("Novice Semifinals"),
        12 => json!("Novice Finals"),
        13 => json!("Demo Round"),
        _ => json!(round),
    };
    let permission_label = match permissions {
        0 => json!("All"),
        1 => json!("Accounts Only"),
        2 => json!("Debaters in Round"),
        _ => json!(permissions),
    };
    (round_label, permission_label)
}

pub(crate) async fn videos(
    conn: &mut AnyConnection,
    tournament: &Tournament,
    origin: &str,
    user: &User,
    participant: Option<i64>,
) -> Result<Vec<Value>, ApiError> {
    let query = format!("SELECT {VIDEO_COLUMNS} FROM core_video v WHERE v.tournament_id = $1
        AND (v.permissions = 0 OR CAST($2 AS BIGINT) = 1 OR (v.permissions = 1 AND CAST($3 AS BIGINT) = 1))
        AND (CAST($4 AS BIGINT) IS NULL OR $4 IN (v.pm_id, v.mg_id, v.lo_id, v.mo_id))
        ORDER BY v.id");
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(tournament.id)
        .bind(i64::from(user.can_view_video))
        .bind(i64::from(user.can_view_private_videos))
        .bind(participant)
        .fetch_all(&mut *conn)
        .await?;
    let mut videos = Vec::new();
    let mut people = HashMap::new();
    for row in rows {
        let id: i64 = row.try_get("id")?;
        let round: i64 = row.try_get("round")?;
        let permissions: i64 = row.try_get("permissions")?;
        let (round_label, permission_label) = labels(round, permissions);
        let mut participants = serde_json::Map::new();
        for role in ["pm", "mg", "lo", "mo"] {
            let id: i64 = row.try_get(format!("{role}_id").as_str())?;
            let person = match people.entry(id) {
                std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                std::collections::hash_map::Entry::Vacant(entry) => {
                    entry.insert(debater_by_id(&mut *conn, id, origin).await?)
                }
            };
            participants.insert(role.into(), person.clone());
        }
        let tags = sqlx::query_scalar::<_, String>(
            "
            SELECT t.name FROM taggit_tag t JOIN taggit_taggeditem ti ON ti.tag_id = t.id
            JOIN django_content_type ct ON ct.id = ti.content_type_id
            WHERE ct.app_label = 'core' AND ct.model = 'video' AND ti.object_id = $1
            ORDER BY ti.id
        ",
        )
        .bind(id)
        .fetch_all(&mut *conn)
        .await?;
        videos.push(json!({
            "id": id, "round": round_label, "round_code": round,
            "link": row.try_get::<&str, _>("link")?,
            "description": row.try_get::<&str, _>("description")?,
            "case": row.try_get::<&str, _>("case")?,
            "permissions": permission_label, "tournament": tournament,
            "participants": participants,
            "tags": tags, "url": format!("{origin}/core/videos/{id}"),
        }));
    }
    Ok(videos)
}

fn division_label(division: i64) -> &'static str {
    if division == 1 { "Varsity" } else { "Novice" }
}

fn hybrid(people: &[Value]) -> bool {
    people
        .iter()
        .map(|person| person["school_id"].as_i64())
        .collect::<HashSet<_>>()
        .len()
        == 2
}

async fn team_awards(
    conn: &mut AnyConnection,
    tournament: &Tournament,
    origin: &str,
) -> Result<(Vec<Value>, Vec<Value>), ApiError> {
    let rows = sqlx::query(AWARDS)
        .bind(tournament.id)
        .fetch_all(&mut *conn)
        .await?;
    let mut varsity_ids = HashSet::new();
    let mut varsity = Vec::new();
    let mut novice = Vec::new();
    for row in rows {
        let division = row.try_get::<i64, _>("division")?;
        let team_id = row.try_get::<i64, _>("team_id")?;
        if division == 0 && varsity_ids.contains(&team_id) {
            continue;
        }
        let people = members(&mut *conn, team_id, origin).await?;
        let team = Team::new(
            team_id,
            row.try_get("team_name")?,
            hybrid(&people),
            &people,
            origin,
        );
        let place: i64 = row.try_get("place")?;
        let result = json!({
            "id": row.try_get::<i64, _>("id")?, "place": place,
            "place_display": place_display(place),
            "type": division_label(division), "type_code": division,
            "ghost_points": row.try_get::<i64, _>("ghost")? != 0,
            "team": team, "tournament": tournament,
        });
        if division == 1 {
            varsity_ids.insert(team_id);
            varsity.push(result);
        } else {
            novice.push(result);
        }
    }
    Ok((varsity, novice))
}

async fn speaker_awards(
    conn: &mut AnyConnection,
    tournament: &Tournament,
    origin: &str,
) -> Result<(Vec<Value>, Vec<Value>), ApiError> {
    let query = SPEAKERS
        .replace("{debater}", DEBATER_COLUMNS)
        .replace("{school}", SCHOOL_COLUMNS);
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(tournament.id)
        .fetch_all(conn)
        .await?;
    let mut varsity = Vec::new();
    let mut novice = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        let division = row.try_get::<i64, _>("division")?;
        let place = row.try_get::<i64, _>("place")?;
        let tie = row.try_get::<i64, _>("tie")? != 0;
        let next = rows.get(index + 1);
        let tie_display = if let Some(next) = next {
            tie || (next.try_get::<i64, _>("division")? == division
                && next.try_get::<i64, _>("tie")? != 0)
        } else {
            tie
        };
        let display_place = place - i64::from(if division == 1 { tie } else { tie_display });
        let payload = json!({
            "id": row.try_get::<i64, _>("result_id")?, "place": place,
            "type": division_label(division), "type_code": division, "tie": tie,
            "debater": debater(row, origin)?, "tournament": tournament,
            "display_place": display_place, "tie_display": tie_display,
        });
        if division == 1 {
            varsity.push(payload);
        } else {
            novice.push(payload);
        }
    }
    Ok((varsity, novice))
}

pub(crate) async fn tournament(
    conn: &mut AnyConnection,
    id: i64,
    origin: &str,
) -> Result<Tournament, ApiError> {
    let row = sqlx::query(AssertSqlSafe(
        TOURNAMENT.replace("{columns}", TOURNAMENT_COLUMNS),
    ))
    .bind(id)
    .fetch_optional(conn)
    .await?
    .ok_or(ApiError::NotFound("Tournament not found"))?;
    Ok(Tournament::from_row(&row, origin)?)
}

async fn load(
    state: &AppState,
    id: i64,
    origin: &str,
    user: &User,
    path: &str,
) -> Result<Value, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let tournament = tournament(&mut tx, id, origin).await?;
    let (varsity, novice) = team_awards(&mut tx, &tournament, origin).await?;
    let (varsity_speakers, novice_speakers) = speaker_awards(&mut tx, &tournament, origin).await?;
    let has_rounds = sqlx::query_scalar::<_, i64>(
        "
        SELECT CAST(CASE WHEN EXISTS (SELECT 1 FROM core_round WHERE tournament_id = $1)
                         THEN 1 ELSE 0 END AS BIGINT)
    ",
    )
    .bind(id)
    .fetch_one(&mut *tx)
    .await?
        != 0;
    let mut team_tab_cards = Vec::new();
    if has_rounds && user.is_authenticated {
        let teams = sqlx::query(TEAMS_WITH_ROUNDS)
            .bind(id)
            .fetch_all(&mut *tx)
            .await?;
        for row in teams {
            let team_id: i64 = row.try_get("id")?;
            let name: &str = row.try_get("name")?;
            let people = members(&mut tx, team_id, origin).await?;
            let team = Team::new(team_id, name, hybrid(&people), &people, origin);
            let mut team_cards = cards(&mut tx, team_id, &people, origin, Some(id)).await?;
            let card = team_cards
                .remove(&id)
                .map(|card| card.rounds)
                .unwrap_or_default();
            team_tab_cards.push(json!({"team": team, "tab_card": card}));
        }
    }
    let videos = videos(&mut tx, &tournament, origin, user, None).await?;
    tx.commit().await?;
    Ok(json!({
        "tournament": tournament,
        "team_awards": {"varsity": varsity, "novice": novice},
        "speaker_awards": {"varsity": varsity_speakers, "novice": novice_speakers},
        "tab_cards_available": has_rounds, "team_tab_cards": team_tab_cards,
        "videos": videos, "links": {"self": proxy_url(origin, path)},
    }))
}

pub(crate) async fn detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    if !is_django_integer(&id) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Ok(id) = id.parse() else {
        return json_error(StatusCode::NOT_FOUND, "Tournament not found");
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error.");
    };
    let path = uri
        .path_and_query()
        .map(|path| path.as_str())
        .unwrap_or_default();
    match load(&state, id, &origin, &user, path).await {
        Ok(result) => api_response(Json(result)),
        Err(ApiError::NotFound(detail)) => json_error(StatusCode::NOT_FOUND, detail),
        Err(_) => json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error."),
    }
}

pub(crate) async fn detail_options(Path(id): Path<String>) -> Response {
    if !is_django_integer(&id) {
        return StatusCode::NOT_FOUND.into_response();
    }
    options(
        "Tournament Detail Api",
        "Provide tournament metadata, awards, speaker results, tab cards, and videos.",
    )
}

pub(crate) async fn page(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    use axum::{http::header, response::Html};
    let Some(id) = id.parse::<i64>().ok().filter(|_| is_django_integer(&id)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let notices = state
        .secret
        .as_deref()
        .map(|secret| crate::flash::alerts(&headers, secret))
        .unwrap_or_default();
    match html(&state, &user, id, &uri, &notices, None).await {
        Ok(html) => {
            let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
            if !notices.is_empty() {
                let secure = state
                    .origin(&headers)
                    .is_some_and(|origin| origin.starts_with("https://"));
                crate::flash::clear(&mut response, secure);
            }
            response
        }
        Err(ApiError::NotFound(_)) => StatusCode::NOT_FOUND.into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn html(
    state: &AppState,
    user: &User,
    id: i64,
    uri: &axum::http::Uri,
    messages: &[crate::flash::Alert],
    editing: Option<minijinja::Value>,
) -> Result<String, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let tournament = tournament(&mut tx, id, "").await?;
    let (varsity_team_results, novice_team_results) = team_awards(&mut tx, &tournament, "").await?;
    let (mut varsity_speaker_results, mut novice_speaker_results) =
        speaker_awards(&mut tx, &tournament, "").await?;
    for row in varsity_speaker_results
        .iter_mut()
        .chain(novice_speaker_results.iter_mut())
    {
        row["place"] = row["display_place"].clone();
        row["tie"] = row["tie_display"].clone();
    }
    let recipients_rows = sqlx::query(
        "
        SELECT CAST(d.id AS BIGINT) AS id, d.first_name, d.last_name
        FROM core_tournamentautoqual a JOIN core_debater d ON d.id = a.debater_id
        WHERE a.tournament_id = $1 ORDER BY d.last_name, d.first_name, d.id
    ",
    )
    .bind(id)
    .fetch_all(&mut *tx)
    .await?;
    let mut recipients = Vec::new();
    for row in recipients_rows {
        let id: i64 = row.try_get("id")?;
        let first: &str = row.try_get("first_name")?;
        let last: &str = row.try_get("last_name")?;
        recipients.push(json!({"debater": {"name":format!("{first} {last}").trim(), "url":format!("/core/debaters/{id}")}}));
    }
    // The historical tournament table lists titles and participants. Links to
    // videos are resolved by the video view, which checks access separately.
    let visible_metadata_user = User {
        can_view_video: true,
        ..User::default()
    };
    let mut metadata = videos(&mut tx, &tournament, "", &visible_metadata_user, None).await?;
    for row in &mut metadata {
        if let Some(map) = row.as_object_mut() {
            map.remove("link");
            map.remove("case");
            map.remove("description");
        }
        for role in ["pm", "mg", "lo", "mo"] {
            row[role] = row["participants"][role].clone();
        }
        row["tags"] = json!(
            row["tags"]
                .as_array()
                .map(|tags| tags
                    .iter()
                    .map(|name| json!({"name":name}))
                    .collect::<Vec<_>>())
                .unwrap_or_default()
        );
    }
    tx.commit().await?;
    let mut buttons = Vec::new();
    for (permission, name, suffix, class) in [
        ("remove_tournament", "Delete", "delete", "btn-danger"),
        ("change_tournament", "Edit", "edit", "btn-info"),
    ] {
        if session::has_permission(&state.pool, user, permission).await? {
            buttons.push(json!({"name":name,"class":class,"href":format!("/core/tournaments/{id}/{suffix}")}));
        }
    }
    let perms = if session::has_permission(&state.pool, user, "change_tournament").await? {
        vec!["core.change_tournament"]
    } else {
        Vec::new()
    };
    let ctx = crate::web::base_context(state, user, uri);
    let specific = minijinja::context! {
        object => tournament, perms, buttons, recipients, varsity_team_results, novice_team_results,
        varsity_speaker_results, novice_speaker_results, videos => metadata,
        tab_cards_search_enabled => false, messages,
    };
    let (template, bindings) = match editing {
        Some(form) => (
            "tournaments/update.html",
            minijinja::context! {..form,..specific,..ctx},
        ),
        None => (
            "tournaments/detail.html",
            minijinja::context! {..specific,..ctx},
        ),
    };
    crate::web::render(template, bindings).map_err(|error| {
        eprintln!("Tournament template failed: {error}");
        ApiError::Serialization
    })
}
