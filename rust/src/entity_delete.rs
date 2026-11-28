use axum::{
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::json;

use crate::{
    ApiError, AppState,
    delete_relations::{self, DeleteError},
    is_django_integer, login, session, web,
};

struct Entity {
    directory: &'static str,
    model: &'static str,
    permission: &'static str,
    name: &'static str,
}

const SCHOOL: Entity = Entity {
    directory: "schools",
    model: "core_school",
    permission: "delete_school",
    name: "SELECT name FROM core_school WHERE id=$1 AND NOT temporary",
};
const DEBATER: Entity = Entity {
    directory: "debaters",
    model: "core_debater",
    permission: "delete_debater",
    name: "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1 AND NOT temporary AND NOT synthetic",
};
const TEAM: Entity = Entity {
    directory: "teams",
    model: "core_team",
    permission: "delete_team",
    name: "SELECT name FROM core_team WHERE id=$1",
};
const TOURNAMENT: Entity = Entity {
    directory: "tournaments",
    model: "core_tournament",
    permission: "delete_tournament",
    name: "SELECT name FROM core_tournament WHERE id=$1",
};

fn parse(uri: &Uri) -> Option<(&'static Entity, i64)> {
    let path = uri.path().strip_prefix("/core/")?.strip_suffix("/delete")?;
    let (name, id) = path.split_once('/')?;
    if !is_django_integer(id) {
        return None;
    }
    let entity = match name {
        "schools" => &SCHOOL,
        "debaters" => &DEBATER,
        "teams" => &TEAM,
        "tournaments" => &TOURNAMENT,
        _ => return None,
    };
    Some((entity, id.parse().ok()?))
}

pub(crate) fn reject_missing_header_csrf(
    state: &AppState,
    headers: &HeaderMap,
) -> Option<Response> {
    match state.origin(headers) {
        None => Some(StatusCode::BAD_REQUEST.into_response()),
        Some(origin) if !login::csrf_valid(headers, None, &origin) => Some(login::csrf_failure()),
        Some(_) => None,
    }
}

async fn page(
    state: &AppState,
    request: Request,
    entity: &Entity,
    id: i64,
) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    if request.method() == Method::DELETE
        && let Some(response) = reject_missing_header_csrf(state, request.headers())
    {
        return Ok(response);
    }
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    if !session::has_permission(&state.pool, &user, entity.permission).await? {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let Some(name) = sqlx::query_scalar::<_, String>(entity.name)
        .bind(id)
        .fetch_optional(&state.pool)
        .await?
    else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let (parts, body) = request.into_parts();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    if parts.method == Method::POST {
        let values = match login::parse_form(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let Some(origin) = state.origin(&parts.headers) else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            &parts.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
    }
    if matches!(parts.method, Method::POST | Method::DELETE) {
        let mut tx = state.pool.begin().await?;
        match delete_relations::delete(&mut tx, entity.model, id).await {
            Ok(()) => {
                tx.commit().await?;
                if matches!(entity.model, "core_school" | "core_debater") {
                    crate::search::invalidate(state).await;
                }
                if entity.model == "core_tournament" {
                    crate::recent_results::invalidate(state).await;
                }
                return Ok((
                    StatusCode::FOUND,
                    [(header::LOCATION, format!("/core/{}/", entity.directory))],
                )
                    .into_response());
            }
            Err(DeleteError::Absent) => return Ok(StatusCode::NOT_FOUND.into_response()),
            Err(DeleteError::Protected) => {
                return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
            }
            Err(DeleteError::Database(reason)) => return Err(reason.into()),
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let common = web::base_context(state, &user, &uri);
    let object = json!({"name":name,"url":format!("/core/{}/{id}",entity.directory)});
    let html = web::render("base/delete.html", context! {object,csrf=>token,..common}).map_err(
        |error| {
            eprintln!("Delete confirmation template failed: {error}");
            ApiError::Serialization
        },
    )?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let Some((entity, id)) = parse(request.uri()) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    page(&state, request, entity, id)
        .await
        .unwrap_or_else(|error| {
            eprintln!("Delete request failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        })
}
