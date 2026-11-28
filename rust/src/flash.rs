use axum::{
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, header},
    middleware::Next,
    response::Response,
};
use cookie::Cookie;
use serde_json::{Value, json};

use crate::{AppState, django_json, login, session};

const SALT: &[u8] = b"django.contrib.messagessigner";
const KEY_PREFIX: &[u8] = b"django.http.cookies";
const COOKIE: &str = "messages";

const LIMIT: usize = 2048;
const SESSION_KEY: &str = "_messages";
const MORE: &str = "__messagesnotfinished__";

#[derive(Clone)]
enum FlashUpdate {
    Store { messages: String, secure: bool },
    Clear { original: String, secure: bool },
}

struct Overflow {
    serialized: String,
    messages: Vec<Value>,
}

tokio::task_local! {
    static OVERFLOW: Overflow;
}

fn key(secret: &[u8]) -> Vec<u8> {
    [KEY_PREFIX, secret].concat()
}

#[derive(serde::Serialize)]
pub(crate) struct Alert {
    pub(crate) text: String,
    tags: &'static str,
}

fn cookie_entries(headers: &HeaderMap, secret: &[u8]) -> Vec<Value> {
    session::cookie(headers, COOKIE)
        .filter(|cookie| cookie.len() <= LIMIT)
        .and_then(|data| session::decode_signed(data, &key(secret), SALT))
        .and_then(|document| match document {
            Value::Array(entries) => Some(entries),
            _ => None,
        })
        .unwrap_or_default()
}

fn entries(headers: &HeaderMap, secret: &[u8]) -> Vec<Value> {
    let mut entries = cookie_entries(headers, secret);
    if entries.last().and_then(Value::as_str) == Some(MORE) {
        entries.pop();
        let _ = OVERFLOW.try_with(|overflow| entries.extend(overflow.messages.iter().cloned()));
    }
    entries.retain(Value::is_array);
    entries
}

pub(crate) fn alerts(headers: &HeaderMap, secret: &[u8]) -> Vec<Alert> {
    entries(headers, secret)
        .into_iter()
        .filter_map(|entry| {
            if entry[0].as_str() != Some("__json_message") {
                return None;
            }
            let tags = match entry[2].as_i64() {
                Some(10) => "debug",
                Some(25) => "success",
                Some(30) => "warning",
                Some(40) => "error",
                _ => "info",
            };
            Some(Alert {
                text: entry[3].as_str()?.to_owned(),
                tags,
            })
        })
        .collect()
}

pub(crate) fn messages(headers: &HeaderMap, secret: &[u8]) -> Vec<String> {
    alerts(headers, secret)
        .into_iter()
        .map(|notice| notice.text)
        .collect()
}

pub(crate) fn push(
    response: &mut Response,
    headers: &HeaderMap,
    secret: &[u8],
    level: i32,
    message: &str,
    secure: bool,
) {
    push_all(
        response,
        headers,
        secret,
        &[(level, message.to_owned())],
        secure,
    );
}

pub(crate) fn push_all(
    response: &mut Response,
    headers: &HeaderMap,
    secret: &[u8],
    notices: &[(i32, String)],
    secure: bool,
) {
    let mut old = entries(headers, secret);
    old.extend(
        notices
            .iter()
            .map(|(level, message)| json!(["__json_message", 0, level, message, ""])),
    );
    let serialized = django_json::ascii(&Value::Array(old).to_string());
    if let Ok(encoded) = session::encode_compressed(&serialized, &key(secret), SALT)
        && encoded.len() <= LIMIT
    {
        login::set_cookie(response, COOKIE, &encoded, None, secure, true);
        clear_overflow(response, secure);
    } else {
        response.extensions_mut().insert(FlashUpdate::Store {
            messages: serialized,
            secure,
        });
    }
}

pub(crate) fn clear(response: &mut Response, secure: bool) {
    login::set_cookie(response, COOKIE, "", Some(0), secure, true);
    clear_overflow(response, secure);
}

fn clear_overflow(response: &mut Response, secure: bool) {
    let _ = OVERFLOW.try_with(|overflow| {
        response.extensions_mut().insert(FlashUpdate::Clear {
            original: overflow.serialized.clone(),
            secure,
        });
    });
}

async fn read_overflow(state: &AppState, headers: &HeaderMap, secret: &[u8]) -> Option<Overflow> {
    if cookie_entries(headers, secret)
        .last()
        .and_then(Value::as_str)
        != Some(MORE)
    {
        return None;
    }
    let stored = session::StoredSession::read(&state.pool, headers, secret)
        .await
        .ok()??;
    let serialized = stored.string(SESSION_KEY)?;
    let messages = serde_json::from_str(&serialized).ok()?;
    Some(Overflow {
        serialized,
        messages,
    })
}

async fn persist(
    state: &AppState,
    response: &mut Response,
    mut headers: HeaderMap,
    secret: &[u8],
    update: FlashUpdate,
) -> Result<(), sqlx::Error> {
    if let Some(id) = response
        .headers()
        .get_all(header::SET_COOKIE)
        .iter()
        .filter_map(|value| Cookie::parse(value.to_str().ok()?).ok())
        .filter(|cookie| cookie.name() == "sessionid")
        .map(|cookie| cookie.value().to_owned())
        .next_back()
    {
        headers.remove(header::COOKIE);
        if !id.is_empty()
            && let Ok(cookie) = HeaderValue::from_str(&format!("sessionid={id}"))
        {
            headers.insert(header::COOKIE, cookie);
        }
    }
    let mut stored = false;
    for _ in 0..3 {
        let session = session::StoredSession::read(&state.pool, &headers, secret).await?;
        match (&update, session) {
            (FlashUpdate::Store { messages, secure }, Some(mut session)) => {
                session.set_string(SESSION_KEY, messages)?;
                if let Some(session) = session.refresh(&state.pool, secret).await? {
                    session.set_cookie(response, *secure);
                    stored = true;
                    break;
                }
            }
            (FlashUpdate::Store { messages, secure }, None) => {
                let payload = json!({ SESSION_KEY: messages });
                let id = session::create_anonymous(&state.pool, secret, &payload).await?;
                login::set_cookie(
                    response,
                    "sessionid",
                    &id,
                    Some(login::SESSION_AGE),
                    *secure,
                    true,
                );
                stored = true;
                break;
            }
            (FlashUpdate::Clear { original, secure }, Some(mut session)) => {
                if session.string(SESSION_KEY).as_ref() != Some(original) {
                    return Ok(());
                }
                session.remove(SESSION_KEY);
                if let Some(session) = session.refresh(&state.pool, secret).await? {
                    session.set_cookie(response, *secure);
                    return Ok(());
                }
            }
            (FlashUpdate::Clear { .. }, None) => return Ok(()),
        }
    }
    if !stored {
        return Err(sqlx::Error::RowNotFound);
    }
    if let FlashUpdate::Store { secure, .. } = update {
        let sentinel = session::encode_signed(&json!([MORE]).to_string(), &key(secret), SALT);
        login::set_cookie(response, COOKIE, &sentinel, None, secure, true);
    }
    Ok(())
}

pub(crate) async fn middleware(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Response {
    let Some(secret) = state.secret.as_deref() else {
        return next.run(request).await;
    };
    let mut cookies = HeaderMap::new();
    for value in request.headers().get_all(header::COOKIE) {
        cookies.append(header::COOKIE, value.clone());
    }
    let mut response = if let Some(overflow) = read_overflow(&state, &cookies, secret).await {
        OVERFLOW.scope(overflow, next.run(request)).await
    } else {
        next.run(request).await
    };
    if let Some(update) = response.extensions_mut().remove::<FlashUpdate>()
        && let Err(error) = persist(&state, &mut response, cookies, secret, update).await
    {
        eprintln!("Could not update session message storage: {error}");
    }
    response
}
