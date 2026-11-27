mod connections;
mod emails;
mod entrance;
pub(crate) mod limits;
mod mail;
mod passwords;
mod reauthentication;
mod reset;

use std::collections::HashMap;

use axum::{
    body::to_bytes,
    extract::Request,
    http::{HeaderMap, Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::{Value, context};

use crate::{ApiError, AppState, flash, login, session::User, web};

pub(crate) use connections::endpoint as connections;
pub(crate) use emails::endpoint as emails;
pub(crate) use entrance::{email_valid, endpoint as entrance, normal_username};
pub(crate) use passwords::change;
pub(crate) use reauthentication::endpoint as reauthenticate;
pub(crate) use reset::{legacy::page as legacy_passwords, page as reset};

type Fields = HashMap<String, String>;
type Errors = HashMap<&'static str, Vec<String>>;

pub(crate) struct Visit {
    pub(crate) headers: HeaderMap,
    pub(crate) origin: String,
    pub(crate) fields: Option<Fields>,
    pub(crate) next: String,
}

impl Visit {
    pub(crate) fn secure(&self) -> bool {
        self.origin.starts_with("https://")
    }
}

pub(crate) async fn visit(state: &AppState, request: Request) -> Result<Visit, Box<Response>> {
    let (parts, body) = request.into_parts();
    let origin = state
        .origin(&parts.headers)
        .ok_or_else(|| Box::new(StatusCode::BAD_REQUEST.into_response()))?;
    let query = login::form_values(parts.uri.query().unwrap_or_default().as_bytes());
    let fields = if parts.method == Method::POST {
        let bytes = to_bytes(body, 128 * 1024)
            .await
            .map_err(|_| Box::new(StatusCode::PAYLOAD_TOO_LARGE.into_response()))?;
        let form = login::form_values(&bytes);
        if !login::csrf_valid(
            &parts.headers,
            form.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Err(Box::new(login::csrf_failure()));
        }
        Some(form)
    } else {
        None
    };
    let next = fields
        .as_ref()
        .and_then(|form| form.get("next"))
        .or_else(|| query.get("next"))
        .map_or_else(String::new, |next| login::safe_next(next, &origin));
    Ok(Visit {
        headers: parts.headers,
        origin,
        fields,
        next,
    })
}

pub(crate) fn render(
    state: &AppState,
    user: &User,
    page: &str,
    visit: &Visit,
    bindings: Value,
) -> Result<Response, ApiError> {
    let (secret, token) =
        login::csrf_tokens(&visit.headers).map_err(|_| ApiError::Serialization)?;
    let notices = state
        .secret
        .as_deref()
        .map(|secret| flash::messages(&visit.headers, secret))
        .unwrap_or_default();
    let html = web::render(
        page,
        context! {
            csrf=>token, user, next=>&visit.next, messages=>&notices, bundles=>state.bundles.as_ref(), ..bindings
        },
    )
    .map_err(|error| {
        eprintln!("Account template failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &secret, visit.secure());
    if !notices.is_empty() {
        flash::clear(&mut response, visit.secure());
    }
    Ok(response)
}

pub(crate) async fn unavailable_login_code() -> Response {
    redirect("/accounts/login/")
}

fn redirect(url: &str) -> Response {
    (StatusCode::FOUND, [(header::LOCATION, url)]).into_response()
}

pub(crate) fn validate_password(fields: &Fields, attributes: [&str; 4]) -> Errors {
    passwords::validation(fields, attributes)
}

pub(crate) async fn new_password(value: &str) -> Result<String, ApiError> {
    passwords::new_hash(value).await
}
