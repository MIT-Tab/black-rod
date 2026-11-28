use std::collections::HashMap;

use axum::{
    body::{Body, to_bytes},
    extract::{DefaultBodyLimit, FromRequest, Multipart, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use cookie::{
    Cookie, SameSite,
    time::{Duration, OffsetDateTime},
};
use minijinja::context;
use sqlx::{AnyPool, Row};
use subtle::ConstantTimeEq;
use unicode_normalization::UnicodeNormalization;

use crate::{ApiError, AppState, password, session, web};

const CSRF_ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
const CSRF_AGE: u32 = 31_449_600;
pub(crate) const SESSION_AGE: u32 = 2_592_000;
const AUTH_ERROR: &str =
    "Please enter a correct username and password. Note that both fields may be case-sensitive.";

pub(crate) fn form_values(raw: &[u8]) -> HashMap<String, String> {
    url::form_urlencoded::parse(raw).into_owned().collect()
}

pub(crate) fn safe_next(value: &str, origin: &str) -> String {
    let value = value.trim();
    if value.is_empty()
        || value.starts_with("//")
        || value.contains('\\')
        || value.chars().any(char::is_control)
    {
        return String::new();
    }
    if value.starts_with('/') {
        return value.to_owned();
    }
    let Some(base) = url::Url::parse(origin).ok() else {
        return String::new();
    };
    let parsed = match url::Url::parse(value) {
        Err(url::ParseError::RelativeUrlWithoutBase) => base.join(value),
        absolute => absolute,
    };
    let Ok(url) = parsed else {
        return String::new();
    };
    if url.origin() == base.origin() && url.username().is_empty() && url.password().is_none() {
        value.to_owned()
    } else {
        String::new()
    }
}

pub(crate) fn redirect_to_login(uri: &Uri) -> Response {
    let original = uri
        .path_and_query()
        .map(|part| part.as_str())
        .unwrap_or_default();
    // Django's redirect field retains path slashes and encodes the query delimiter.
    let encoded = url::form_urlencoded::byte_serialize(original.as_bytes())
        .collect::<String>()
        .replace("%2F", "/");
    (
        StatusCode::FOUND,
        [(header::LOCATION, format!("/auth/login?next={encoded}"))],
    )
        .into_response()
}

fn alphabet_indices(token: &str) -> Option<Vec<usize>> {
    token
        .bytes()
        .map(|byte| {
            CSRF_ALPHABET
                .iter()
                .position(|&candidate| candidate == byte)
        })
        .collect()
}

fn unmask(value: &str) -> Option<String> {
    match value.len() {
        32 => alphabet_indices(value).map(|_| value.to_owned()),
        64 => {
            let chars = alphabet_indices(value)?;
            Some(
                (0..32)
                    .map(|index| {
                        char::from(CSRF_ALPHABET[(chars[index + 32] + 62 - chars[index]) % 62])
                    })
                    .collect(),
            )
        }
        _ => None,
    }
}

fn mask(secret: &str) -> Result<String, ring::error::Unspecified> {
    let mut mask = session::random_token(CSRF_ALPHABET, 32)?;
    let cipher: String = alphabet_indices(secret)
        .expect("valid secret")
        .into_iter()
        .zip(alphabet_indices(&mask).expect("valid mask"))
        .map(|(a, b)| char::from(CSRF_ALPHABET[(a + b) % 62]))
        .collect();
    mask.push_str(&cipher);
    Ok(mask)
}

pub(crate) fn csrf_valid(headers: &HeaderMap, token: Option<&str>, origin: &str) -> bool {
    if let Some(origin_header) = headers.get(header::ORIGIN) {
        if origin_header != origin {
            return false;
        }
    } else if origin.starts_with("https://") {
        let referer = headers
            .get(header::REFERER)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| url::Url::parse(value).ok());
        if referer.is_none_or(|url| url.origin().ascii_serialization() != origin) {
            return false;
        }
    }
    let Some(secret) = session::cookie(headers, "csrftoken").and_then(unmask) else {
        return false;
    };
    let header_token = || {
        headers
            .get("x-csrftoken")
            .and_then(|value| value.to_str().ok())
    };
    let Some(provided) = token.or_else(header_token).and_then(unmask) else {
        return false;
    };
    bool::from(secret.as_bytes().ct_eq(provided.as_bytes()))
}

pub(crate) fn csrf_failure() -> Response {
    let page = crate::web::render("403_csrf.html", context! {}).unwrap_or_else(|_| {
        "<h1>Request verification failed</h1><p>Reload the page and try again.</p>".to_owned()
    });
    (StatusCode::FORBIDDEN, Html(page)).into_response()
}

pub(crate) fn set_cookie(
    response: &mut Response,
    name: &str,
    value: &str,
    age: Option<u32>,
    https: bool,
    http_only: bool,
) {
    let mut cookie = Cookie::build((name, value))
        .path("/")
        .same_site(SameSite::Lax)
        .secure(https)
        .http_only(http_only);
    if let Some(age) = age {
        let age = Duration::seconds(i64::from(age));
        cookie = cookie.max_age(age).expires(OffsetDateTime::now_utc() + age);
    }
    if let Ok(cookie) = HeaderValue::from_str(&cookie.to_string()) {
        response.headers_mut().append(header::SET_COOKIE, cookie);
    }
}

struct LoginPage<'a> {
    next: String,
    username: &'a str,
    error: &'a str,
    remember: bool,
}

pub(crate) fn csrf_tokens(
    headers: &HeaderMap,
) -> Result<(String, String), ring::error::Unspecified> {
    let secret = match session::cookie(headers, "csrftoken").and_then(unmask) {
        Some(secret) => secret,
        None => session::random_token(CSRF_ALPHABET, 32)?,
    };
    let token = mask(&secret)?;
    Ok((secret, token))
}

pub(crate) fn set_csrf_cookie(response: &mut Response, secret: &str, secure: bool) {
    set_cookie(response, "csrftoken", secret, Some(CSRF_AGE), secure, false);
}

async fn page(state: &AppState, headers: &HeaderMap, https: bool, page: LoginPage<'_>) -> Response {
    let Ok((csrf, token)) = csrf_tokens(headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(user) = session::user(&state.pool, headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let path = "/auth/login/";
    let html = web::render(
        "auth/login.html",
        context! {
            request => context! { path, user, GET => context! { q => "" } },
            left_menu => web::menu(&user, path),
            bundles => state.bundles().as_ref(),
            csrf => token,
            next => page.next,
            username => page.username,
            remember => page.remember,
            error => page.error,
        },
    );
    let html = match html {
        Ok(html) => html,
        Err(error) => {
            eprintln!("Login template failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let mut response = (
        [
            (header::VARY, "Cookie"),
            (
                header::CACHE_CONTROL,
                "max-age=0, no-cache, no-store, must-revalidate, private",
            ),
        ],
        Html(html),
    )
        .into_response();
    set_csrf_cookie(&mut response, &csrf, https);
    response
}

const MAX_FORM_FIELDS: usize = 10_240;

pub(crate) async fn form_fields(
    headers: &HeaderMap,
    body: Body,
    limit: usize,
) -> Result<Vec<(String, String)>, StatusCode> {
    let Some(content_type) = headers.get(header::CONTENT_TYPE) else {
        return Ok(Vec::new());
    };
    let media_type = content_type
        .to_str()
        .unwrap_or_default()
        .split(';')
        .next()
        .unwrap_or_default()
        .trim();
    if media_type.eq_ignore_ascii_case("multipart/form-data") {
        let mut request = Request::new(body);
        request
            .headers_mut()
            .insert(header::CONTENT_TYPE, content_type.clone());
        DefaultBodyLimit::max(limit).apply(&mut request);
        let mut parts = Multipart::from_request(request, &())
            .await
            .map_err(|_| StatusCode::BAD_REQUEST)?;
        let mut fields = Vec::new();
        while let Some(field) = parts.next_field().await.map_err(|error| error.status())? {
            if field.file_name().is_none() {
                if fields.len() == MAX_FORM_FIELDS {
                    return Err(StatusCode::BAD_REQUEST);
                }
                let Some(name) = field.name().map(str::to_owned) else {
                    continue;
                };
                let text = field.text().await.map_err(|error| error.status())?;
                fields.push((name, text));
            }
        }
        return Ok(fields);
    }
    if !media_type.eq_ignore_ascii_case("application/x-www-form-urlencoded") {
        return Ok(Vec::new());
    }
    let body = to_bytes(body, limit)
        .await
        .map_err(|_| StatusCode::PAYLOAD_TOO_LARGE)?;
    if body
        .iter()
        .filter(|byte| **byte == b'&')
        .nth(MAX_FORM_FIELDS - 1)
        .is_some()
    {
        return Err(StatusCode::BAD_REQUEST);
    }
    Ok(url::form_urlencoded::parse(&body).into_owned().collect())
}

pub(crate) async fn parse_form(
    headers: &HeaderMap,
    body: Body,
    limit: usize,
) -> Result<HashMap<String, String>, StatusCode> {
    Ok(form_fields(headers, body, limit)
        .await?
        .into_iter()
        .collect())
}

pub(crate) async fn view(State(state): State<AppState>, request: Request) -> Response {
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let secure = origin.starts_with("https://");
    let query = form_values(request.uri().query().unwrap_or_default().as_bytes());
    let next = query
        .get("next")
        .map(|value| safe_next(value, &origin))
        .unwrap_or_default();
    if request.method() != Method::POST {
        return page(
            &state,
            request.headers(),
            secure,
            LoginPage {
                next,
                username: "",
                error: "",
                remember: true,
            },
        )
        .await;
    }
    let (parts, body) = request.into_parts();
    let headers = parts.headers;
    let form = match parse_form(&headers, body, 128 * 1024).await {
        Ok(form) => form,
        Err(status) => return status.into_response(),
    };
    let field = |name: &str| form.get(name).map(String::as_str);
    if !csrf_valid(&headers, field("csrfmiddlewaretoken"), &origin) {
        return csrf_failure();
    }
    let next = field("next")
        .map(|value| safe_next(value, &origin))
        .unwrap_or(next);
    let remember =
        field("remember").is_some_and(|value| !matches!(value, "" | "0" | "false" | "False"));
    if field("provider") == Some("apdaonline") {
        return crate::oauth::start(&state, &headers, &origin, &next, remember).await;
    }
    if state.secret.is_none() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let normalized_username: String = field("username")
        .unwrap_or_default()
        .trim()
        .nfkc()
        .collect();
    let username = normalized_username.as_str();
    let password = field("password").unwrap_or_default();
    let verified = match authenticate(&state.pool, username, password).await {
        Ok(verified) => verified,
        Err(error) => {
            eprintln!("Login verification failed: {error:?}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let Some(verified) = verified else {
        return page(
            &state,
            &headers,
            secure,
            LoginPage {
                next,
                username,
                error: AUTH_ERROR,
                remember,
            },
        )
        .await;
    };
    finish_password_login(&state, &headers, secure, &next, remember, verified).await
}

pub(crate) struct VerifiedPassword {
    id: i64,
    hash: String,
    upgrade: Option<String>,
    backend: &'static str,
    pub(crate) staff: bool,
}

pub(crate) async fn authenticate(
    pool: &AnyPool,
    username: &str,
    password: &str,
) -> Result<Option<VerifiedPassword>, ApiError> {
    let rows = sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id, username, password,
            CAST(CASE WHEN is_staff THEN 1 ELSE 0 END AS BIGINT) AS staff
        FROM core_user WHERE is_active AND (
            username = $1 OR id = (
                SELECT id FROM core_user WHERE UPPER(username) = UPPER($1) ORDER BY id LIMIT 1
            )
        ) ORDER BY CASE WHEN username = $1 THEN 0 ELSE 1 END",
    )
    .bind(username)
    .fetch_all(pool)
    .await?;
    let candidates = rows
        .iter()
        .map(|row| {
            Ok((
                row.try_get::<i64, _>("id")?,
                row.try_get::<String, _>("password")?,
                row.try_get::<&str, _>("username")? == username,
                row.try_get::<i64, _>("staff")? != 0,
            ))
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()?;
    let password = password.as_bytes().to_owned();
    password::run_blocking(move || {
        if candidates.is_empty() {
            password::verify_and_upgrade(&password, None);
        }
        candidates.into_iter().find_map(|(id, hash, exact, staff)| {
            let upgrade = password::verify_and_upgrade(&password, Some(&hash))?;
            let backend = if exact {
                "django.contrib.auth.backends.ModelBackend"
            } else {
                "allauth.account.auth_backends.AuthenticationBackend"
            };
            Some(VerifiedPassword {
                id,
                hash,
                upgrade,
                backend,
                staff,
            })
        })
    })
    .await
    .map_err(|_| ApiError::Serialization)
}

impl VerifiedPassword {
    async fn apply_upgrade(&mut self, pool: &AnyPool) -> Result<bool, sqlx::Error> {
        let Some(upgraded) = self.upgrade.take() else {
            return Ok(true);
        };
        let changed = sqlx::query(
            "UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3 AND is_active",
        )
        .bind(&upgraded)
        .bind(self.id)
        .bind(&self.hash)
        .execute(pool)
        .await?;
        if changed.rows_affected() != 1 {
            return Ok(false);
        }
        self.hash = upgraded;
        Ok(true)
    }
}

pub(crate) async fn basic_user(
    pool: &AnyPool,
    username: &str,
    password: &str,
) -> Result<Option<session::User>, ApiError> {
    let Some(mut verified) = authenticate(pool, username, password).await? else {
        return Ok(None);
    };
    if !verified.apply_upgrade(pool).await? {
        return Ok(None);
    }
    Ok(session::active_user(pool, verified.id)
        .await?
        .filter(|(_, hash)| *hash == verified.hash)
        .map(|(user, _)| user))
}

pub(crate) async fn finish_password_login(
    state: &AppState,
    headers: &HeaderMap,
    secure: bool,
    next: &str,
    remember: bool,
    mut verified: VerifiedPassword,
) -> Response {
    match verified.apply_upgrade(&state.pool).await {
        Ok(true) => (),
        Ok(false) => return StatusCode::CONFLICT.into_response(),
        Err(error) => {
            eprintln!("Password hash upgrade failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    }
    sign_in(
        state,
        headers,
        secure,
        next,
        remember,
        (verified.id, &verified.hash),
        verified.backend,
    )
    .await
}

pub(crate) async fn sign_in(
    state: &AppState,
    headers: &HeaderMap,
    secure: bool,
    next: &str,
    remember: bool,
    account: (i64, &str),
    backend: &str,
) -> Response {
    let Some(secret) = state.secret.as_deref() else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let result = session::create(
        &state.pool,
        headers,
        secret,
        account.0,
        account.1,
        remember,
        backend,
    )
    .await;
    let Ok(session_key) = result else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(csrf) = session::random_token(CSRF_ALPHABET, 32) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let location = if next.is_empty() { "/" } else { next };
    let mut response = (
        StatusCode::FOUND,
        [(header::LOCATION, location), (header::VARY, "Cookie")],
    )
        .into_response();
    set_cookie(
        &mut response,
        "sessionid",
        &session_key,
        remember.then_some(SESSION_AGE),
        secure,
        true,
    );
    set_cookie(
        &mut response,
        "csrftoken",
        &csrf,
        Some(CSRF_AGE),
        secure,
        false,
    );
    response
}

pub(crate) async fn logout(State(state): State<AppState>, request: Request) -> Response {
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
    let headers = parts.headers;
    let query = form_values(parts.uri.query().unwrap_or_default().as_bytes());
    let form = match parse_form(&headers, body, 128 * 1024).await {
        Ok(form) => form,
        Err(status) => return status.into_response(),
    };
    if !csrf_valid(
        &headers,
        form.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return csrf_failure();
    }
    if session::delete(&state.pool, &headers).await.is_err() {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let next = form
        .get("next")
        .or_else(|| query.get("next"))
        .map(|value| safe_next(value, &origin))
        .unwrap_or_default();
    let location = if next.is_empty() { "/" } else { &next };
    let mut response = (
        StatusCode::FOUND,
        [
            (header::LOCATION, location),
            (header::VARY, "Cookie"),
            (
                header::CACHE_CONTROL,
                "max-age=0, no-cache, no-store, must-revalidate, private",
            ),
        ],
    )
        .into_response();
    set_cookie(
        &mut response,
        "sessionid",
        "",
        Some(0),
        origin.starts_with("https://"),
        true,
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookie_builder_preserves_session_and_csrf_attributes() {
        let mut response = StatusCode::OK.into_response();
        set_cookie(&mut response, "sessionid", "session123", None, true, true);
        set_cookie(
            &mut response,
            "csrftoken",
            "csrf123",
            Some(CSRF_AGE),
            true,
            false,
        );
        let mut cookies = response
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .map(|value| Cookie::parse(value.to_str().unwrap()).unwrap());
        let session = cookies.next().unwrap();
        assert_eq!(session.name_value(), ("sessionid", "session123"));
        assert_eq!(session.secure(), Some(true));
        assert_eq!(session.http_only(), Some(true));
        assert_eq!(session.path(), Some("/"));
        assert_eq!(session.same_site(), Some(SameSite::Lax));
        assert_eq!(session.max_age(), None);
        assert_eq!(session.expires(), None);

        let csrf = cookies.next().unwrap();
        assert_eq!(csrf.name_value(), ("csrftoken", "csrf123"));
        assert_eq!(csrf.secure(), Some(true));
        assert_ne!(csrf.http_only(), Some(true));
        assert_eq!(csrf.path(), Some("/"));
        assert_eq!(csrf.same_site(), Some(SameSite::Lax));
        assert_eq!(csrf.max_age(), Some(Duration::seconds(i64::from(CSRF_AGE))));
        assert!(csrf.expires().is_some());
        assert!(cookies.next().is_none());
    }

    #[test]
    fn csrf_token_round_trips_mask_without_leaking_secret() {
        let secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";
        let token = mask(secret).unwrap();
        assert_eq!(token.len(), 64);
        assert_eq!(unmask(&token).as_deref(), Some(secret));
        assert!(unmask(&format!("{token}!")).is_none());
    }

    #[tokio::test]
    async fn form_fields_apply_djangos_field_count_to_both_encodings() {
        for (multipart, content_type) in [
            (false, "application/x-www-form-urlencoded"),
            (true, "multipart/form-data; boundary=parity-form"),
        ] {
            let mut headers = HeaderMap::new();
            headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
            for count in [MAX_FORM_FIELDS, MAX_FORM_FIELDS + 1] {
                let text = if multipart {
                    let part = "--parity-form\r\nContent-Disposition: form-data; name=field\r\n\r\nvalue\r\n";
                    part.repeat(count) + "--parity-form--\r\n"
                } else {
                    "field=value&"
                        .repeat(count)
                        .trim_end_matches('&')
                        .to_owned()
                };
                let result = form_fields(&headers, Body::from(text), 1_048_576).await;
                if count == MAX_FORM_FIELDS {
                    assert_eq!(result.unwrap().len(), count);
                } else {
                    assert_eq!(result, Err(StatusCode::BAD_REQUEST));
                }
            }
        }
    }

    #[test]
    fn continuation_checks_normalized_origins() {
        for input in [
            "//other.test/example",
            "https://other.test",
            "/\\other.test",
            "javascript:alert(1)",
            "/p\nnext",
        ] {
            assert_eq!(safe_next(input, "https://results.test"), "");
        }
        assert_eq!(
            safe_next("/games/?next=1", "https://results.test"),
            "/games/?next=1"
        );
        assert_eq!(
            safe_next("https://results.test/games/", "https://results.test"),
            "https://results.test/games/"
        );
        for path in ["../games/", "games/?next=1", "?page=2", "#results"] {
            assert_eq!(safe_next(path, "https://results.test"), path);
        }
    }
}
