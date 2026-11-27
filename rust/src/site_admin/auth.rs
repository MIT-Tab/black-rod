use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use minijinja::context;
use unicode_normalization::UnicodeNormalization;

use super::{AdminRequest, HOME, Viewer, redirect};
use crate::{ApiError, AppState, login as browser_login, session};

pub(crate) async fn login(State(state): State<AppState>, request: Request) -> Response {
    match login_page(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Django admin login failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn login_page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let (parts, body) = request.into_parts();
    let viewer = Viewer::load(state, &parts.headers).await?;
    let admin = AdminRequest {
        state,
        headers: &parts.headers,
        uri: &parts.uri,
        viewer,
    };
    if parts.method == Method::GET && admin.viewer.staff() {
        return Ok(redirect(HOME));
    }
    let Some(origin) = admin.origin() else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let query = browser_login::form_values(parts.uri.query().unwrap_or_default().as_bytes());
    let mut next = query.get("next").map_or_else(
        || HOME.to_owned(),
        |value| browser_login::safe_next(value, &origin),
    );
    let mut submitted = String::new();
    let mut form_error = "";
    let mut field_errors = std::collections::HashMap::new();
    if parts.method == Method::POST {
        let Ok(body) = to_bytes(body, 128 * 1024).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        let form = browser_login::form_values(&body);
        if !browser_login::csrf_valid(
            &parts.headers,
            form.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(browser_login::csrf_failure());
        }
        if let Some(value) = form.get("next") {
            next = browser_login::safe_next(value, &origin);
        }
        submitted = form
            .get("username")
            .map_or("", String::as_str)
            .trim()
            .nfkc()
            .collect();
        let entered = form.get("password").map_or("", String::as_str);
        if submitted.is_empty() {
            field_errors.insert("username", "This field is required.");
        }
        if entered.is_empty() {
            field_errors.insert("password", "This field is required.");
        }
        if field_errors.is_empty() {
            if let Some(verified) = browser_login::authenticate(&state.pool, &submitted, entered)
                .await?
                .filter(|account| account.staff)
            {
                return Ok(browser_login::finish_password_login(
                    state,
                    &parts.headers,
                    admin.secure(),
                    if next.is_empty() { HOME } else { &next },
                    true,
                    verified,
                )
                .await);
            }
            form_error = "Please enter the correct username and password for a staff account. Note that both fields may be case-sensitive.";
        }
    }
    admin.view(
        "site_admin/login.html",
        context! {
            title=>"Log in", next, entered=>submitted, form_error, field_errors,
            app_path=>parts.uri.path_and_query().map(|part|part.as_str()).unwrap_or("/admin/login/")
        },
    )
}

pub(crate) async fn logout(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let Some(origin) = state.origin(&parts.headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let Ok(content) = to_bytes(body, 128 * 1024).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let fields = browser_login::form_values(&content);
    if !browser_login::csrf_valid(
        &parts.headers,
        fields.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return browser_login::csrf_failure();
    }
    match session::delete(&state.pool, &parts.headers).await {
        Ok(()) => {
            let mut response = redirect("/");
            browser_login::set_cookie(
                &mut response,
                "sessionid",
                "",
                Some(0),
                origin.starts_with("https://"),
                true,
            );
            response
        }
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}
