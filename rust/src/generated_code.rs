use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::value::to_raw_value;
use sqlx::{AnyConnection, AnyPool};

use crate::{
    ApiError, AppState, login,
    session::{self, StoredSession},
    web,
};

const SESSION_KEY: &str = "latest_generated_code";
const PATH: &str = "/core/generated-codes/";

async fn unique_code(conn: &mut AnyConnection, user: i64) -> Result<String, ApiError> {
    for _ in 0..16 {
        let code = session::random_token(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", 12)
            .map_err(|_| ApiError::Database)?;
        let inserted = sqlx::query(
            "INSERT INTO core_generatedcode (user_id,code,created_at)
            VALUES ($1,$2,CURRENT_TIMESTAMP) ON CONFLICT(code) DO NOTHING",
        )
        .bind(user)
        .bind(&code)
        .execute(&mut *conn)
        .await?;
        if inserted.rows_affected() == 1 {
            return Ok(code);
        }
    }
    Err(ApiError::Database)
}

async fn generate(
    state: &AppState,
    headers: &axum::http::HeaderMap,
    secret: &[u8],
    user: i64,
) -> Result<bool, ApiError> {
    for _ in 0..8 {
        let Some(mut session) = StoredSession::read(&state.pool, headers, secret).await? else {
            return Ok(false);
        };
        let mut tx = state.pool.begin().await?;
        let code = unique_code(&mut tx, user).await?;
        session
            .values
            .insert(SESSION_KEY.to_owned(), to_raw_value(&code)?);
        if session.save_on(&mut tx, secret).await? {
            tx.commit().await?;
            return Ok(true);
        }
        tx.rollback().await?;
    }
    Ok(false)
}

async fn consume(
    pool: &AnyPool,
    headers: &axum::http::HeaderMap,
    secret: &[u8],
) -> Result<Option<String>, ApiError> {
    for _ in 0..8 {
        let Some(mut session) = StoredSession::read(pool, headers, secret).await? else {
            return Ok(None);
        };
        let Some(code) = session.values.remove(SESSION_KEY) else {
            return Ok(None);
        };
        if session.save(pool, secret).await? {
            return Ok(serde_json::from_str::<String>(code.get()).ok());
        }
    }
    Ok(None)
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(user_id) = user.id.filter(|_| user.is_authenticated) else {
        return Ok(login::redirect_to_login(&uri));
    };
    let Some(secret) = state.secret.as_deref() else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let secure = state
        .origin(request.headers())
        .is_some_and(|url| url.starts_with("https://"));
    if request.method() == Method::POST {
        let Some(origin) = state.origin(request.headers()) else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        let (parts, body) = request.into_parts();
        let Ok(body) = to_bytes(body, 2_621_440).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        let values = login::form_values(&body);
        if !login::csrf_valid(
            &parts.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        if generate(state, &parts.headers, secret, user_id).await? {
            return Ok((StatusCode::FOUND, [(header::LOCATION, PATH)]).into_response());
        }
        return Ok(login::redirect_to_login(&uri));
    }
    let code = consume(&state.pool, request.headers(), secret).await?;
    let Ok((csrf, token)) = login::csrf_tokens(request.headers()) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let common = web::base_context(state, &user, &uri);
    let html = match web::render(
        "core/generated_codes.html",
        context! {generated_code=>code,csrf=>token,..common},
    ) {
        Ok(html) => html,
        Err(reason) => {
            eprintln!("Generate Code template failed: {reason}");
            return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Generate Code request failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
