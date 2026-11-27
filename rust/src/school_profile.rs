use axum::{
    Json,
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row};

use crate::{
    ApiError, AppState, api_response, is_django_integer, json_error, options,
    schedule::{TOURNAMENT_COLUMNS, Tournament, proxy_url, requested_season, season_display},
    schools::{DEBATER_COLUMNS, Debater, SCHOOL, School},
    standings::{available_seasons, coty_breakdowns, lite_school, place_display},
};

const HISTORY: &str = "
    SELECT CAST(id AS BIGINT) AS id, season, CAST(place AS BIGINT) AS place,
           CAST(points AS DOUBLE PRECISION) AS points,
           CAST(CASE WHEN tied THEN 1 ELSE 0 END AS BIGINT) AS tied
    FROM core_coty WHERE school_id = $1
    ORDER BY season DESC
    LIMIT $2
";

async fn history(
    pool: &AnyPool,
    school: &School,
    origin: &str,
    limit: Option<i64>,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows = sqlx::query(HISTORY)
        .bind(school.id)
        .bind(limit.unwrap_or(i64::MAX))
        .fetch_all(pool)
        .await?;
    let summary = lite_school(school.id, &school.name, origin);
    rows.iter()
        .map(|row| {
            let season: &str = row.try_get("season")?;
            let place = row.try_get::<i64, _>("place")?;
            Ok(json!({
                "season": season,
                "season_display": season.parse().map_or_else(|_| season.to_owned(), season_display),
                "place_display": place_display(place),
                "points": row.try_get::<f64, _>("points")?,
                "markers": [],
                "id": row.try_get::<i64, _>("id")?,
                "place": place,
                "tied": row.try_get::<i64, _>("tied")? != 0,
                "school": summary,
            }))
        })
        .collect()
}

async fn members(
    pool: &AnyPool,
    school: &School,
    origin: &str,
    season: i32,
) -> Result<Vec<Value>, sqlx::Error> {
    let query = format!(
        "
        SELECT {DEBATER_COLUMNS}, d.first_season
        FROM core_debater d WHERE d.school_id = $1 AND d.first_season <= $2
            AND d.latest_season >= $2 AND NOT d.synthetic AND NOT d.temporary
        ORDER BY d.id
    "
    );
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(school.id)
        .bind(season.to_string())
        .fetch_all(pool)
        .await?;
    let mut members: Vec<_> = rows
        .iter()
        .map(|row| {
            let first = row
                .try_get::<&str, _>("first_season")?
                .trim()
                .parse::<i32>();
            let years = first.map_or(0, |first| season - first + 1);
            Ok((years, Debater::from_row(row, origin, Some(school))?))
        })
        .collect::<Result<_, sqlx::Error>>()?;
    members.sort_by_key(|(years, _)| std::cmp::Reverse(*years));
    Ok(members
        .iter()
        .map(|(years, debater)| json!({"debater": debater, "years_on_team": years}))
        .collect())
}

pub(crate) fn limit(raw: &str) -> Option<i64> {
    let value = url::form_urlencoded::parse(raw.as_bytes())
        .filter(|(name, _)| name == "limit")
        .map(|(_, value)| value)
        .last()?;
    let value = value.trim().parse::<i64>().ok()?;
    (value > 0).then_some(value.min(200))
}

async fn load(
    state: &AppState,
    origin: &str,
    id: i64,
    season: i32,
    max: Option<i64>,
    path: &str,
) -> Result<Value, ApiError> {
    let row = sqlx::query(SCHOOL)
        .bind(id)
        .fetch_optional(&state.pool)
        .await?
        .ok_or(ApiError::NotFound("School not found"))?;
    let school = School::from_row(&row, origin)?;
    let query = format!(
        "
        SELECT {TOURNAMENT_COLUMNS}
        FROM core_tournament t JOIN core_school s ON s.id = t.host_id
        WHERE t.host_id = $1 ORDER BY t.date DESC LIMIT $2
    "
    );
    let tournaments = sqlx::query(AssertSqlSafe(query))
        .bind(id)
        .bind(max.unwrap_or(i64::MAX))
        .fetch_all(&state.pool)
        .await?
        .iter()
        .map(|row| Tournament::from_row(row, origin))
        .collect::<Result<Vec<_>, _>>()?;
    let history = history(&state.pool, &school, origin, max).await?;
    let members = members(&state.pool, &school, origin, season).await?;
    let breakdown = coty_breakdowns(&state.pool, season, origin, Some(id), !path.is_empty())
        .await?
        .remove(&id)
        .unwrap_or_default();
    let label = season_display(season);
    Ok(json!({
        "school": school,
        "season": season.to_string(),
        "season_display": label,
        "available_seasons": available_seasons(state.current_season),
        "coty_history": history,
        "hosted_tournaments": tournaments,
        "season_summary": {
            "season": season.to_string(), "season_display": label,
            "members": members, "coty_breakdown": breakdown,
        },
        "links": {"self": proxy_url(origin, path)},
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
        return json_error(StatusCode::NOT_FOUND, "School not found");
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let query = uri.query().unwrap_or_default();
    let season = requested_season(query, state.current_season);
    let path = uri
        .path_and_query()
        .map(|path| path.as_str())
        .unwrap_or_default();
    match load(&state, &origin, id, season, limit(query), path).await {
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
        "School Detail Api",
        "Season-aware school profile with COTY history, hosted tournaments, and roster info.",
    )
}

pub(crate) async fn page(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(id) = id.parse::<i64>().ok().filter(|_| is_django_integer(&id)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    render_page(&state, id, &uri, &headers, None).await
}

pub(crate) async fn render_page(
    state: &AppState,
    id: i64,
    uri: &axum::http::Uri,
    headers: &HeaderMap,
    editing: Option<minijinja::Value>,
) -> Response {
    use axum::{http::header, response::Html};
    use minijinja::context;
    let valid = sqlx::query_scalar::<_, i64>("SELECT CAST(id AS BIGINT) FROM core_school WHERE id = $1 AND NOT synthetic AND NOT temporary")
        .bind(id).fetch_optional(&state.pool).await;
    match valid {
        Ok(Some(_)) => (),
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(error) => {
            let _ = ApiError::from(error);
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    }
    let query: std::collections::HashMap<_, _> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let raw = query
        .get("season")
        .map(|value| value.as_ref())
        .unwrap_or_default();
    if raw.is_empty() && editing.is_none() {
        return (
            StatusCode::FOUND,
            [(
                header::LOCATION,
                format!("/core/schools/{id}?season={}", state.current_season),
            )],
        )
            .into_response();
    }
    let season = raw
        .parse::<i32>()
        .ok()
        .filter(|year| raw == year.to_string());
    let loaded = load(
        state,
        "",
        id,
        season.unwrap_or(state.current_season),
        None,
        "",
    )
    .await;
    let Ok(loaded) = loaded else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(user) = crate::session::user(&state.pool, headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let mut buttons = Vec::new();
    for (permission, name, suffix, class) in [
        ("remove_school", "Delete", "delete", "btn-danger"),
        ("change_school", "Edit", "edit", "btn-info"),
    ] {
        if crate::session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false)
        {
            buttons.push(json!({"name": name, "href": format!("/core/schools/{id}/{suffix}"), "class": class}));
        }
    }
    let summary = &loaded["season_summary"];
    let mut debaters = Vec::new();
    let mut quals = Vec::new();
    if season.is_some() {
        if let Some(members) = summary["members"].as_array() {
            debaters.extend(members.iter().map(|row| {
                let mut debater = row["debater"].clone();
                debater["years_on_team"] = row["years_on_team"].clone();
                debater
            }));
        }
        if let Some(rows) = summary["coty_breakdown"].as_array() {
            quals.extend(rows.iter());
        }
    }
    let default = query
        .get("default")
        .map(|value| value.as_ref())
        .unwrap_or("members");
    let common = crate::web::base_context(state, &user, uri);
    let props = context! { current_season => raw, default, buttons, debaters, quals,
    object => &loaded["school"], cotys => &loaded["coty_history"],
    tournaments => &loaded["hosted_tournaments"] };
    let template = if editing.is_some() {
        "schools/update.html"
    } else {
        "schools/detail.html"
    };
    let bindings = match editing {
        Some(form) => context! {..form,..props,..common},
        None => context! {..props,..common},
    };
    match crate::web::render(template, bindings) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("School template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
