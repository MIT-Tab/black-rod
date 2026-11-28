use std::collections::HashMap;

pub(crate) mod import;
use axum::{
    extract::{OriginalUri, Path, Request, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::json;
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use crate::{ApiError, AppState, is_django_integer, login, session, web};

const DETAIL: &str = "
    SELECT m.text, m.background_slide, CAST(m.date_set AS TEXT) AS date_set,
        CAST(t.id AS BIGINT) AS tournament_id, t.name AS tournament_name,
        (SELECT s.status FROM core_motionuserstatus s
          WHERE s.motion_id = m.id AND s.user_id = CAST($2 AS BIGINT)) AS motion_status
    FROM core_motion m LEFT JOIN core_tournament t ON t.id = m.tournament_id WHERE m.id = $1
";
const TOPICS: &str = "
    SELECT CAST(m.content_object_id AS BIGINT) AS motion, t.name
    FROM core_motiontopic t JOIN core_taggedmotion m ON m.tag_id = t.id
    WHERE m.content_object_id IN ({ids}) ORDER BY m.id
";

pub(crate) async fn topics_for(
    connection: &mut AnyConnection,
    motions: &[i64],
) -> Result<HashMap<i64, Vec<String>>, ApiError> {
    let mut topics = HashMap::<i64, Vec<String>>::new();
    for group in motions.chunks(200) {
        let placeholders = (1..=group.len())
            .map(|index| format!("${index}"))
            .collect::<Vec<_>>()
            .join(",");
        let statement = TOPICS.replace("{ids}", &placeholders);
        let mut query = sqlx::query(AssertSqlSafe(statement));
        for id in group {
            query = query.bind(id);
        }
        for row in query.fetch_all(&mut *connection).await? {
            topics
                .entry(row.try_get("motion")?)
                .or_default()
                .push(row.try_get("name")?);
        }
    }
    Ok(topics)
}

pub(crate) async fn detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(id) = id.parse::<i64>().ok().filter(|_| is_django_integer(&id)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let result = sqlx::query(DETAIL)
        .bind(id)
        .bind(user.id)
        .fetch_optional(&state.pool)
        .await;
    let row = match result {
        Ok(Some(row)) => row,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(error) => {
            eprintln!("Motion lookup failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let Ok(mut connection) = state.pool.acquire().await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(mut topics) = topics_for(&mut connection, &[id]).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let tags = topics.remove(&id).unwrap_or_default();
    drop(connection);
    let tournament = row.get::<Option<i64>, _>("tournament_id").map(|id| {
        json!({
            "url": format!("/core/tournaments/{id}"), "name": row.get::<&str, _>("tournament_name")
        })
    });
    let object = json!({
        "id": id,
        "text": row.get::<&str, _>("text"),
        "name": row.get::<&str, _>("text"),
        "background_slide": row.get::<&str, _>("background_slide"),
        "date_set": row.get::<Option<&str>, _>("date_set").map(web::django_date),
        "tournament": tournament,
        "tags": tags.into_iter().map(|name| json!({"name": name})).collect::<Vec<_>>(),
    });
    let buttons = if session::has_permission(&state.pool, &user, "change_motion")
        .await
        .unwrap_or(false)
    {
        vec![json!({"name":"Edit","class":"btn-info","href":format!("/core/motions/{id}/edit")})]
    } else {
        Vec::new()
    };
    let csrf = if user.is_authenticated {
        match login::csrf_tokens(&headers) {
            Ok(tokens) => Some(tokens),
            Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        }
    } else {
        None
    };
    let common = web::base_context(&state, &user, &uri);
    let values = context! {
        object, buttons,
        motion_status => row.get::<Option<&str>, _>("motion_status"),
        csrf => csrf.as_ref().map(|(_, token)| token.as_str()),
    };
    let mut response = match web::render("motions/detail.html", context! { ..common, ..values }) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Motion detail template failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    if let Some((secret, _)) = csrf {
        let secure = state
            .origin(&headers)
            .is_some_and(|url| url.starts_with("https://"));
        login::set_csrf_cookie(&mut response, &secret, secure);
    }
    response
}

pub(crate) async fn status(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    let Some(id) = id.parse::<i64>().ok().filter(|_| is_django_integer(&id)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
    let form = match login::parse_form(&parts.headers, body, 128 * 1024).await {
        Ok(fields) => fields,
        Err(status) => return status.into_response(),
    };
    let field = |key| form.get(key).map(String::as_str);
    if !login::csrf_valid(&parts.headers, field("csrfmiddlewaretoken"), &origin) {
        return login::csrf_failure();
    }
    let Ok(user) = session::user(&state.pool, &parts.headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(user_id) = user.id else {
        return login::redirect_to_login(&parts.uri);
    };
    let Ok(Some(_)) =
        sqlx::query_scalar::<_, i64>("SELECT CAST(id AS BIGINT) FROM core_motion WHERE id = $1")
            .bind(id)
            .fetch_optional(&state.pool)
            .await
    else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let result = match field("status") {
        Some(progress @ ("done" | "ignore")) => sqlx::query("
            INSERT INTO core_motionuserstatus (user_id, motion_id, status, updated_at)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, motion_id) DO UPDATE SET status = EXCLUDED.status, updated_at = EXCLUDED.updated_at
        ").bind(user_id).bind(id).bind(progress).execute(&state.pool).await,
        Some("clear") => sqlx::query("DELETE FROM core_motionuserstatus WHERE user_id = $1 AND motion_id = $2")
            .bind(user_id).bind(id).execute(&state.pool).await,
        _ => return (StatusCode::BAD_REQUEST, [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
            "Invalid status").into_response(),
    };
    if let Err(error) = result {
        eprintln!("Motion progress update failed: {error}");
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let next = field("next")
        .map(|value| login::safe_next(value, &origin))
        .filter(|next| !next.is_empty())
        .unwrap_or_else(|| format!("/core/motions/{id}"));
    (StatusCode::FOUND, [(header::LOCATION, next)]).into_response()
}

pub(crate) mod edit;
