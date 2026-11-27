pub(crate) mod short_names;
pub(crate) mod speaks;

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;

use crate::{
    AppState, login,
    session::{self, User},
    web,
};

pub(crate) async fn require(
    state: &AppState,
    uri: &Uri,
    headers: &HeaderMap,
) -> Result<User, Box<Response>> {
    let user = session::user(&state.pool, headers, state.secret.as_deref())
        .await
        .map_err(|_| Box::new(StatusCode::INTERNAL_SERVER_ERROR.into_response()))?;
    if !user.is_authenticated {
        return Err(Box::new(login::redirect_to_login(uri)));
    }
    if !user.is_superuser {
        return Err(Box::new(web::permission_denied(state, &user, uri)));
    }
    Ok(user)
}

pub(crate) async fn tools(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let user = match require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let Ok(username) =
        sqlx::query_scalar::<_, String>("SELECT username FROM core_user WHERE id=$1")
            .bind(user.id)
            .fetch_one(&state.pool)
            .await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok((csrf, token)) = login::csrf_tokens(&headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let recording =
        std::env::var("ENV").is_ok_and(|mode| mode.trim().eq_ignore_ascii_case("development"));
    let path = std::env::var("ROUND_AMENDMENTS_FILE")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| {
            std::env::current_dir()
                .unwrap_or_default()
                .join("apda/round-amendments.local.json")
                .to_string_lossy()
                .into_owned()
        });
    let common = web::base_context(&state, &user, &uri);
    let bindings = context! {username, csrf=>token, round_amendment_errors=>Vec::<String>::new(), round_amendment_recording=>context!{enabled=>recording,path}};
    let html = match web::render("admin/admin_tools.html", context! {..bindings,..common}) {
        Ok(html) => html,
        Err(reason) => {
            eprintln!("Admin tools template failed: {reason}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    let secure = state
        .origin(&headers)
        .is_some_and(|url| url.starts_with("https://"));
    login::set_csrf_cookie(&mut response, &csrf, secure);
    response
}
