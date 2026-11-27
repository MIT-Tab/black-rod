use std::{collections::HashMap, num::NonZeroU32};

use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{Duration, Utc};
use minijinja::context;
use ring::pbkdf2;
use sqlx::Row;
use subtle::ConstantTimeEq;
use unicode_normalization::UnicodeNormalization;

use crate::{AppState, session, web};

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
    let Some(url) = url::Url::parse(value).ok() else {
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
    (
        StatusCode::FORBIDDEN,
        Html("<h1>Forbidden (403)</h1><p>CSRF verification failed. Request aborted.</p>"),
    )
        .into_response()
}

pub(crate) fn set_cookie(
    response: &mut Response,
    name: &str,
    value: &str,
    age: Option<u32>,
    https: bool,
    http_only: bool,
) {
    let mut cookie = format!("{name}={value}; Path=/; SameSite=Lax");
    if let Some(age) = age {
        let expires =
            (Utc::now() + Duration::seconds(i64::from(age))).format("%a, %d %b %Y %H:%M:%S GMT");
        cookie.push_str(&format!("; Max-Age={age}; Expires={expires}"));
    }
    if https {
        cookie.push_str("; Secure");
    }
    if http_only {
        cookie.push_str("; HttpOnly");
    }
    if let Ok(cookie) = HeaderValue::from_str(&cookie) {
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
            bundles => state.bundles.as_ref(),
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

fn verify_password(password: &[u8], encoded: &str) -> bool {
    let mut parts = encoded.split('$');
    let algorithm = match parts.next() {
        Some("pbkdf2_sha256") => pbkdf2::PBKDF2_HMAC_SHA256,
        Some("pbkdf2_sha1") => pbkdf2::PBKDF2_HMAC_SHA1,
        _ => return false,
    };
    let Some((iterations, salt, expected)) = parts
        .next()
        .and_then(|value| value.parse::<NonZeroU32>().ok())
        .filter(|value| value.get() <= 10_000_000)
        .zip(parts.next())
        .zip(parts.next().and_then(|value| STANDARD.decode(value).ok()))
        .map(|((iterations, salt), expected)| (iterations, salt, expected))
    else {
        return false;
    };
    pbkdf2::verify(algorithm, iterations, salt.as_bytes(), password, &expected).is_ok()
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
    let Ok(body) = to_bytes(body, 128 * 1024).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let form = form_values(&body);
    let field = |name: &str| form.get(name).map(String::as_str);
    if !csrf_valid(&parts.headers, field("csrfmiddlewaretoken"), &origin) {
        return csrf_failure();
    }
    let next = field("next")
        .map(|value| safe_next(value, &origin))
        .unwrap_or(next);
    let remember =
        field("remember").is_some_and(|value| !matches!(value, "" | "0" | "false" | "False"));
    if field("provider") == Some("apdaonline") {
        return crate::oauth::start(&state, &origin, &next, remember).await;
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
    let row = sqlx::query("SELECT CAST(id AS BIGINT) AS id, password FROM core_user WHERE username = $1 AND is_active")
        .bind(username).fetch_optional(&state.pool).await;
    let candidate = match row {
        Ok(row) => row.and_then(|row| {
            Some((
                row.try_get::<i64, _>("id").ok()?,
                row.try_get::<String, _>("password").ok()?,
            ))
        }),
        Err(error) => {
            eprintln!("Login lookup failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let hash = candidate.as_ref().map(|(_, hash)| hash.clone());
    let password = password.as_bytes().to_owned();
    let valid = tokio::task::spawn_blocking(move || {
        if let Some(hash) = hash.as_ref().filter(|hash| hash.starts_with("pbkdf2_")) {
            verify_password(&password, hash)
        } else {
            let mut compare = [0; 32];
            pbkdf2::derive(
                pbkdf2::PBKDF2_HMAC_SHA256,
                NonZeroU32::new(1_000_000).unwrap(),
                b"Black Rod dummy password salt",
                &password,
                &mut compare,
            );
            false
        }
    })
    .await
    .unwrap_or_default();
    let Some((id, hash)) = candidate.filter(|_| valid) else {
        return page(
            &state,
            &parts.headers,
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
    sign_in(
        &state,
        &parts.headers,
        secure,
        &next,
        remember,
        (id, &hash),
        "django.contrib.auth.backends.ModelBackend",
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
    let Ok(body) = to_bytes(body, 128 * 1024).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let form = form_values(&body);
    if !csrf_valid(
        &parts.headers,
        form.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return csrf_failure();
    }
    if session::delete(&state.pool, &parts.headers).await.is_err() {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let mut response = (
        StatusCode::FOUND,
        [(header::LOCATION, "/"), (header::VARY, "Cookie")],
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
    fn csrf_token_round_trips_mask_without_leaking_secret() {
        let secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";
        let token = mask(secret).unwrap();
        assert_eq!(token.len(), 64);
        assert_eq!(unmask(&token).as_deref(), Some(secret));
        assert!(unmask(&format!("{token}!")).is_none());
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
    }
}
