use axum::{http::HeaderMap, response::Response};
use serde_json::{Value, json};

use crate::{django_json, login, session};

const SALT: &[u8] = b"django.contrib.messagessigner";
const KEY_PREFIX: &[u8] = b"django.http.cookies";
const COOKIE: &str = "messages";

fn key(secret: &[u8]) -> Vec<u8> {
    [KEY_PREFIX, secret].concat()
}

#[derive(serde::Serialize)]
pub(crate) struct Alert {
    pub(crate) text: String,
    tags: &'static str,
}

pub(crate) fn alerts(headers: &HeaderMap, secret: &[u8]) -> Vec<Alert> {
    session::cookie(headers, COOKIE)
        .filter(|cookie| cookie.len() <= 2048)
        .and_then(|data| session::decode_signed(data, &key(secret), SALT))
        .and_then(|document| {
            document.as_array().map(|entries| {
                entries
                    .iter()
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
            })
        })
        .unwrap_or_default()
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
    let mut old = session::cookie(headers, COOKIE)
        .filter(|cookie| cookie.len() <= 2048)
        .and_then(|data| session::decode_signed(data, &key(secret), SALT))
        .and_then(|document| match document {
            Value::Array(entries) => Some(entries),
            _ => None,
        })
        .unwrap_or_default();
    old.retain(Value::is_array);
    old.push(json!(["__json_message", 0, level, message, ""]));
    let serialized = Value::Array(old).to_string();
    let escaped = django_json::ascii(&serialized);
    let encoded = session::encode_signed(&escaped, &key(secret), SALT);
    if encoded.len() <= 2048 {
        login::set_cookie(response, COOKIE, &encoded, None, secure, true);
    }
}

pub(crate) fn clear(response: &mut Response, secure: bool) {
    login::set_cookie(response, COOKIE, "", Some(0), secure, true);
}
