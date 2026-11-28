use std::{collections::BTreeMap, sync::LazyLock};

use axum::{
    body::to_bytes,
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use minijinja::context;
use regex::Regex;
use serde::Deserialize;
use serde_json::Value;

use crate::{AppState, json_error, login, session, web};

pub(crate) const JSON_MEDIA: &[&str] = &["application/json", "application/*", "*/*"];
pub(crate) const HTML_MEDIA: &[&str] = &["text/html", "text/*", "*/*"];

pub(crate) fn matches_media(media: &str, supported: &[&str]) -> bool {
    supported
        .iter()
        .any(|kind| media.eq_ignore_ascii_case(kind))
}

fn basic_credentials(headers: &HeaderMap) -> Result<Option<(String, String)>, &'static str> {
    let Some(authorization) = headers.get(header::AUTHORIZATION) else {
        return Ok(None);
    };
    let mut parts = authorization
        .as_bytes()
        .split(|byte| byte.is_ascii_whitespace())
        .filter(|part| !part.is_empty());
    if !parts
        .next()
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case(b"basic"))
    {
        return Ok(None);
    }
    let Some(encoded) = parts.next() else {
        return Err("Invalid basic header. No credentials provided.");
    };
    if parts.next().is_some() {
        return Err("Invalid basic header. Credentials string should not contain spaces.");
    }
    let invalid = "Invalid basic header. Credentials not correctly base64 encoded.";
    let decoded = STANDARD.decode(encoded).map_err(|_| invalid)?;
    let decoded = String::from_utf8(decoded)
        .unwrap_or_else(|error| error.as_bytes().iter().copied().map(char::from).collect());
    let (username, password) = decoded.split_once(':').ok_or(invalid)?;
    Ok(Some((username.to_owned(), password.to_owned())))
}

pub(super) async fn public_user(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<session::User, Response> {
    let internal = |_| json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error.");
    let failed = |detail| json_error(StatusCode::FORBIDDEN, detail);
    let user = session::user(&state.pool, headers, state.secret.as_deref())
        .await
        .map_err(internal)?;
    if user.is_authenticated {
        return Ok(user);
    }
    let Some((username, password)) = basic_credentials(headers).map_err(failed)? else {
        return Ok(user);
    };
    login::basic_user(&state.pool, &username, &password)
        .await
        .map_err(|_| json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error."))?
        .ok_or_else(|| failed("Invalid username/password."))
}

#[derive(Deserialize)]
struct Schema {
    name: String,
    description: String,
    shape: Option<Shape>,
}

#[derive(Deserialize)]
struct Shape {
    #[serde(default)]
    keys: Vec<String>,
    #[serde(default)]
    children: BTreeMap<String, Shape>,
    item: Option<Box<Shape>>,
}

static METADATA: LazyLock<BTreeMap<String, Schema>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../schema/drf.json")).expect("DRF schemas")
});

fn schema(path: &str) -> Option<&'static Schema> {
    let key = match path {
        "/api/schools/" => "schools",
        "/api/schools/all/" => "all-schools",
        "/api/schedule/" => "schedule",
        "/api/standings/" => "standings",
        "/api/standings/replay/" => "replay",
        "/api/standings/through-date/" => "through",
        "/api/oty-guide/" => "guide",
        "/api/private/forum-profiles/" => "forum",
        "/api/private/debater-emails/" => "emails",
        _ => match path
            .trim_matches('/')
            .split('/')
            .collect::<Vec<_>>()
            .as_slice()
        {
            ["api", "schools", _, "detail"] => "school",
            ["api", "teams", _, "detail"] => "team",
            ["api", "debaters", _, "detail"] => "debater",
            ["api", "tournaments", _, "detail"] => "tournament",
            ["api", "debaters", _] => "debaters",
            _ => return None,
        },
    };
    METADATA.get(key)
}

pub(crate) fn wants_html(headers: &HeaderMap, requested: Option<&str>) -> bool {
    let media = headers
        .get_all(header::ACCEPT)
        .iter()
        .filter_map(|entry| entry.to_str().ok())
        .flat_map(|entry| entry.split(','))
        .map(|entry| {
            let (kind, params) = entry.split_once(';').unwrap_or((entry, ""));
            let specificity = u8::from(params.split(';').any(|param| {
                let key = param
                    .trim()
                    .split_once('=')
                    .map(|(key, _)| key)
                    .unwrap_or_default();
                !key.is_empty() && !key.eq_ignore_ascii_case("q")
            }));
            let kind = kind.trim();
            let rank = if kind == "*/*" {
                0
            } else if matches_media(kind, &["text/*", "application/*"]) {
                1
            } else {
                2 + specificity
            };
            (kind, rank)
        })
        .collect::<Vec<_>>();
    let rank = |kinds: &[&str]| {
        media
            .iter()
            .filter(|(kind, _)| matches_media(kind, kinds))
            .map(|(_, rank)| *rank)
            .max()
    };
    let html = rank(HTML_MEDIA);
    let json = rank(JSON_MEDIA);
    if requested == Some("api") {
        return media.is_empty() || html.is_some();
    }
    requested != Some("json") && html > json
}

fn escape(output: &mut String, value: &str) {
    for ch in value.chars() {
        match ch {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '"' => output.push_str("&quot;"),
            '\'' => output.push_str("&#x27;"),
            value => output.push(value),
        }
    }
}

fn pretty(value: &Value, shape: Option<&Shape>, indent: usize, output: &mut String) {
    match value {
        Value::Object(fields) => {
            output.push('{');
            let mut remaining = fields.keys().map(String::as_str).collect::<Vec<_>>();
            let ordered = shape
                .map(|shape| {
                    shape
                        .keys
                        .iter()
                        .map(String::as_str)
                        .filter(|key| fields.contains_key(*key))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            remaining.retain(|key| !ordered.contains(key));
            let mut entries = ordered.into_iter().chain(remaining).peekable();
            while let Some(key) = entries.next() {
                output.push('\n');
                output.push_str(&" ".repeat(indent + 4));
                output.push_str(&serde_json::to_string(key).expect("JSON key"));
                output.push_str(": ");
                pretty(
                    &fields[key],
                    shape.and_then(|shape| shape.children.get(key)),
                    indent + 4,
                    output,
                );
                if entries.peek().is_some() {
                    output.push(',');
                }
            }
            if !fields.is_empty() {
                output.push('\n');
                output.push_str(&" ".repeat(indent));
            }
            output.push('}');
        }
        Value::Array(values) => {
            output.push('[');
            let mut values = values.iter().peekable();
            while let Some(value) = values.next() {
                output.push('\n');
                output.push_str(&" ".repeat(indent + 4));
                pretty(
                    value,
                    shape.and_then(|shape| shape.item.as_deref()),
                    indent + 4,
                    output,
                );
                if values.peek().is_some() {
                    output.push(',');
                }
            }
            if !value.as_array().is_none_or(Vec::is_empty) {
                output.push('\n');
                output.push_str(&" ".repeat(indent));
            }
            output.push(']');
        }
        value => output.push_str(&value.to_string()),
    }
}

fn linkified(content: &str) -> String {
    static LINKS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"https?://[^\s"<>\\]+"#).expect("HTTP links"));
    let mut last = 0;
    let mut output = String::new();
    for link in LINKS.find_iter(content) {
        escape(&mut output, &content[last..link.start()]);
        output.push_str("<a href=\"");
        let href = link.as_str();
        let quoted = url::Url::parse(href).ok().and_then(|url| {
            let query = url.query()?;
            let start = href.find('?')?;
            let pairs = url::form_urlencoded::parse(query.as_bytes());
            let query = url::form_urlencoded::Serializer::new(String::new())
                .extend_pairs(pairs)
                .finish()
                .replace('*', "%2A")
                .replace("%7E", "~");
            let fragment = url
                .fragment()
                .map_or_else(String::new, |fragment| format!("#{fragment}"));
            Some(format!("{}?{query}{fragment}", &href[..start]))
        });
        escape(&mut output, quoted.as_deref().unwrap_or(href));
        output.push_str("\" rel=\"nofollow\">");
        escape(&mut output, link.as_str());
        output.push_str("</a>");
        last = link.end();
    }
    escape(&mut output, &content[last..]);
    output
}

fn format_url(uri: &Uri, kind: &str) -> String {
    let mut entries = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(key, _)| key != "format")
        .map(|(a, b)| (a.into_owned(), b.into_owned()))
        .collect::<Vec<_>>();
    entries.push(("format".to_owned(), kind.to_owned()));
    entries.sort_by(|left, right| left.0.cmp(&right.0));
    let query = url::form_urlencoded::Serializer::new(String::new())
        .extend_pairs(entries)
        .finish();
    format!("{}?{query}", uri.path())
}

fn canonical_header(name: &str) -> String {
    if name == "www-authenticate" {
        return "WWW-Authenticate".to_owned();
    }
    name.split('-')
        .map(|part| {
            let mut letters = part.chars();
            letters.next().map_or_else(String::new, |letter| {
                letter.to_uppercase().chain(letters).collect()
            })
        })
        .collect::<Vec<_>>()
        .join("-")
}

fn name(path: &str) -> Option<&'static str> {
    if let Some(meta) = schema(path) {
        return Some(&meta.name);
    }
    if !path.starts_with("/api/agent/v1/") {
        return None;
    }
    Some(
        match path
            .trim_end_matches('/')
            .split('/')
            .collect::<Vec<_>>()
            .as_slice()
        {
            ["", "api", "agent", "v1"] => "Agent Discovery",
            ["", "api", "agent", "v1", "guide"] => "Agent Guide",
            ["", "api", "agent", "v1", "records", _]
            | ["", "api", "agent", "v1", "records", _, _] => "Agent Records",
            ["", "api", "agent", "v1", "batches"] | ["", "api", "agent", "v1", "batches", _] => {
                "Agent Batches"
            }
            ["", "api", "agent", "v1", "batches", _, "undo"] => "Agent Undo",
            _ => return None,
        },
    )
}

fn breadcrumbs(uri: &Uri) -> Vec<minijinja::Value> {
    let format = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter_map(|(key, value)| (key == "format").then_some(value))
        .last();
    let query = format
        .map(|format| {
            url::form_urlencoded::Serializer::new(String::new())
                .append_pair("format", &format)
                .finish()
        })
        .unwrap_or_default();
    let mut last = None;
    let mut breadcrumbs = Vec::new();
    let path = uri.path();
    for (index, _) in path.match_indices('/').rev() {
        let parent = &path[..=index];
        if let Some(name) = name(parent)
            && last != Some(name)
        {
            last = Some(name);
            breadcrumbs.push(context! {name,url=>if query.is_empty() { parent.to_owned() } else { format!("{parent}?{query}") }});
        }
    }
    breadcrumbs.reverse();
    breadcrumbs
}

pub(crate) async fn response(
    state: &AppState,
    uri: &Uri,
    method: &Method,
    request: &HeaderMap,
    result: Response,
) -> Response {
    let meta = schema(uri.path());
    let Some(name) = name(uri.path()) else {
        return result;
    };
    if result
        .headers()
        .get(header::CONTENT_TYPE)
        .is_none_or(|value| value != "application/json")
    {
        return result;
    }
    let Ok((csrf, token)) = login::csrf_tokens(request) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let secure = state
        .origin(request)
        .is_some_and(|origin| origin.starts_with("https://"));
    let (mut parts, body) = result.into_parts();
    let Ok(bytes) = to_bytes(body, 64 * 1024 * 1024).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let value = match serde_json::from_slice::<Value>(&bytes) {
        Ok(value) => value,
        Err(_) => return Response::from_parts(parts, bytes.into()),
    };
    let mut content = String::new();
    pretty(
        &value,
        meta.and_then(|meta| meta.shape.as_ref()),
        0,
        &mut content,
    );
    let body = linkified(&content);
    let description = meta
        .filter(|_| {
            !matches!(
                parts.status,
                StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
            )
        })
        .map(|meta| {
            let mut text = String::new();
            escape(&mut text, &meta.description);
            if text.is_empty() {
                text
            } else {
                format!("<p>{}</p>", text.replace('\n', "<br>"))
            }
        })
        .unwrap_or_default();
    let headers = [
        "allow",
        "cache-control",
        "content-type",
        "expires",
        "last-modified",
        "vary",
        "www-authenticate",
    ]
    .into_iter()
    .filter_map(|key| {
        let value = parts.headers.get(key)?.to_str().ok()?;
        let value = if key == "vary" && value == "Accept, Cookie" {
            "Accept"
        } else {
            value
        };
        Some(context! {name=>canonical_header(key),value})
    })
    .collect::<Vec<_>>();
    let allow = parts
        .headers
        .get(header::ALLOW)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    let can_get = allow.split(", ").any(|method| method == "GET");
    let forbidden = matches!(
        parts.status,
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
    );
    let can_options = !forbidden;
    let raw_form = !forbidden && allow.split(", ").any(|method| method == "POST");
    let status = parts.status.as_u16();
    let reason = parts.status.canonical_reason().unwrap_or_default();
    let full_path = uri
        .path_and_query()
        .map(|target| target.as_str())
        .unwrap_or(uri.path());
    let formats = ["json", "api"]
        .into_iter()
        .map(|name| context! {name,url=>format_url(uri,name)})
        .collect::<Vec<_>>();
    let breadcrumbs = breadcrumbs(uri);
    let message = web::render(
        "rest_framework/browse.html",
        context! {
            name, description, body, headers, can_get, can_options, raw_form, full_path, formats,breadcrumbs,
            method=>method.as_str(), status,reason,csrf=>token,code_style=>include_str!("../schema/drf-highlight.css"),
        },
    );
    let Ok(message) = message else {
        return Response::from_parts(parts, bytes.into());
    };
    parts.headers.remove(header::CONTENT_LENGTH);
    parts.headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    if parts
        .headers
        .get(header::VARY)
        .is_some_and(|value| value == "Accept")
    {
        parts
            .headers
            .insert(header::VARY, HeaderValue::from_static("Accept, Cookie"));
    }
    let mut response = Response::from_parts(parts, Html(message).into_response().into_body());
    login::set_csrf_cookie(&mut response, &csrf, secure);
    response
}
