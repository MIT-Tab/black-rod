use std::sync::LazyLock;

use axum::{
    Json,
    body::{Body, to_bytes},
    extract::{OriginalUri, State},
    http::{HeaderMap, HeaderValue, Method, Request, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};
use tower::ServiceExt;

use crate::{
    AppState, drf,
    routing::{local_api, protected_api_path, public_api_path},
    web,
};

const SCHEMA: &str = include_str!("../assets/openapi.json");
const SCHEMA_MEDIA_TYPE: &str = "application/vnd.oai.openapi+json";
const DOC: &str = include_str!("../assets/llms.txt");
const EXAMPLE: &str = "Example: /llm?endpoint=/api/standings";

pub(crate) async fn documentation() -> &'static str {
    DOC
}

pub(crate) async fn manifest(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    Json(json!({
        "schema_version": "v1",
        "name_for_human": "APDA Standings & Profiles",
        "name_for_model": "apdaStandings",
        "description_for_human": concat!(
            "Search APDA standings, school rosters, debater histories, ",
            "team records, and tournament breakdowns in structured formats."
        ),
        "description_for_model": concat!(
            "Use this plugin to fetch APDA standings and entity details. ",
            "It exposes JSON endpoints for Team/Speaker of the Year boards ",
            "and detail pages for schools, debaters, teams, and tournaments."
        ),
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": format!("{origin}/.well-known/openapi.json"),
            "is_user_authenticated": false,
        },
        "logo_url": format!("{origin}/static/favicon.ico"),
        "contact_email": "info@apda.online",
        "legal_info_url": format!("{origin}/"),
    }))
    .into_response()
}

fn schema_document(state: &AppState, headers: &HeaderMap) -> Response {
    static DOCUMENT: LazyLock<Value> =
        LazyLock::new(|| serde_json::from_str(SCHEMA).expect("valid checked-in OpenAPI schema"));
    let Some(origin) = state.origin(headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let host = origin
        .split_once("://")
        .map(|(_, authority)| authority.split(':').next().unwrap_or_default())
        .unwrap_or_default();
    let mut document = DOCUMENT.clone();
    document["servers"] = json!([{"url": format!("https://{host}")}]);
    Json(document).into_response()
}

fn accepts_schema(headers: &HeaderMap) -> bool {
    !headers.contains_key(header::ACCEPT)
        || headers
            .get_all(header::ACCEPT)
            .iter()
            .filter_map(|value| value.to_str().ok())
            .flat_map(|value| value.split(','))
            .filter_map(|value| value.split(';').next())
            .any(|value| {
                [SCHEMA_MEDIA_TYPE, "application/*", "*/*"]
                    .iter()
                    .any(|media| value.trim().eq_ignore_ascii_case(media))
            })
}

pub(crate) async fn schema(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    method: Method,
    headers: HeaderMap,
) -> Response {
    let format = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(name, _)| name == "format")
        .last()
        .map(|(_, format)| format);
    let error = |status, detail| (status, Json(json!({"detail": detail}))).into_response();
    let mut response = if format.is_some_and(|format| !matches!(format.as_ref(), "" | "json")) {
        error(StatusCode::NOT_FOUND, "Not found.".to_owned())
    } else if !accepts_schema(&headers) {
        error(
            StatusCode::NOT_ACCEPTABLE,
            "Could not satisfy the request Accept header.".to_owned(),
        )
    } else {
        let mut response = match drf::public_user(&state, &headers).await {
            Err(response) => response,
            Ok(_) => match method {
                Method::HEAD | Method::GET => schema_document(&state, &headers),
                Method::OPTIONS => Json(json!({
                    "name": "Plugin Schema",
                    "description": "Return the OpenAPI schema in JSON with stable server metadata.",
                    "renders": [SCHEMA_MEDIA_TYPE],
                    "parses": ["application/json", "application/x-www-form-urlencoded", "multipart/form-data"],
                }))
                .into_response(),
                _ => error(
                    StatusCode::METHOD_NOT_ALLOWED,
                    format!("Method \"{method}\" not allowed."),
                ),
            },
        };
        response
            .headers_mut()
            .insert(header::VARY, HeaderValue::from_static("Cookie"));
        response
    };
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static(SCHEMA_MEDIA_TYPE),
    );
    response.headers_mut().insert(
        header::ALLOW,
        HeaderValue::from_static("GET, HEAD, OPTIONS"),
    );
    if method == Method::HEAD {
        *response.body_mut() = Body::empty();
    }
    response
}

fn proxy_result(
    status: StatusCode,
    title: &str,
    message: &str,
    details: &str,
    payload: Option<&str>,
) -> Response {
    match web::render(
        "api/llm_proxy.html",
        context! { title, message, details, payload },
    ) {
        Ok(html) if status == StatusCode::OK => {
            (status, [(header::VARY, "Cookie")], Html(html)).into_response()
        }
        Ok(html) => (status, Html(html)).into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

fn proxy_error(status: StatusCode, message: &str, details: &str) -> Response {
    proxy_result(status, "Unable to fetch endpoint", message, details, None)
}

pub(crate) async fn proxy(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let endpoint = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(name, _)| name == "endpoint")
        .map(|(_, value)| value)
        .last()
        .unwrap_or_default();
    if endpoint.is_empty() {
        return proxy_error(
            StatusCode::BAD_REQUEST,
            "Missing endpoint parameter",
            "Usage: /llm?endpoint=/api/standings",
        );
    }
    if !endpoint.starts_with('/') {
        return proxy_error(
            StatusCode::BAD_REQUEST,
            "Endpoint must start with / (relative path only)",
            EXAMPLE,
        );
    }
    if !endpoint.starts_with("/api/")
        && !endpoint.starts_with("/.well-known/")
        && !matches!(endpoint.as_ref(), "/ai-plugin.json" | "/openapi.json")
    {
        return proxy_error(
            StatusCode::BAD_REQUEST,
            "Only /api/ and whitelisted /.well-known/ endpoints are allowed",
            EXAMPLE,
        );
    }
    if endpoint.contains("..") || endpoint.contains("//") {
        return proxy_error(
            StatusCode::BAD_REQUEST,
            "Path traversal patterns are not allowed",
            "",
        );
    }
    let (path, query) = endpoint.split_once('?').unwrap_or((&endpoint, ""));
    let well_known = matches!(
        path,
        "/.well-known/openapi.json" | "/.well-known/ai-plugin.json"
    );
    if !public_api_path(path) && !protected_api_path(path) && !well_known {
        return proxy_error(
            StatusCode::NOT_FOUND,
            &format!("The path {endpoint} could not be found."),
            "",
        );
    }
    let target = proxy_target(path, query);
    let mut builder = Request::builder().uri(target);
    if let Some(host) = headers.get(header::HOST) {
        builder = builder.header(header::HOST, host);
    }
    if let Some(cookie) = headers.get(header::COOKIE) {
        builder = builder.header(header::COOKIE, cookie);
    }
    let Ok(request) = builder.body(Body::empty()) else {
        return proxy_error(StatusCode::BAD_REQUEST, "Invalid endpoint", "");
    };
    // Dispatch against local routes so the proxy does not make an outbound connection.
    let Ok(response) = local_api(state).oneshot(request).await;
    let status = response.status();
    if status != StatusCode::OK {
        return proxy_error(
            status,
            &format!(
                "The API returned status {status_code} while requesting {endpoint}. Please verify the path or try again later.",
                status_code = status.as_u16(),
            ),
            "",
        );
    }
    let Ok(body) = to_bytes(response.into_body(), 32 * 1024 * 1024).await else {
        return proxy_error(
            StatusCode::BAD_GATEWAY,
            "Unable to read the API response.",
            "",
        );
    };
    let Ok(data) = serde_json::from_slice::<Value>(&body) else {
        return proxy_error(
            StatusCode::BAD_GATEWAY,
            &format!(
                "The endpoint {endpoint} returned a successful response, but it was not JSON."
            ),
            "",
        );
    };
    let Ok(payload) = serde_json::to_string_pretty(&data) else {
        return proxy_error(
            StatusCode::BAD_GATEWAY,
            "Unable to encode the API response.",
            "",
        );
    };
    proxy_result(
        StatusCode::OK,
        &format!("API Response: {endpoint}"),
        "",
        "",
        Some(&payload),
    )
}

fn proxy_target(path: &str, query: &str) -> String {
    let mut params = Vec::<(_, _)>::new();
    for (key, value) in url::form_urlencoded::parse(query.as_bytes()) {
        if let Some((_, existing)) = params.iter_mut().find(|(name, _)| name == &key) {
            *existing = value;
        } else {
            params.push((key, value));
        }
    }
    let mut encoded = url::form_urlencoded::Serializer::new(String::new());
    for (key, value) in params {
        if key != "format" || value != "api" {
            encoded.append_pair(&key, &value);
        }
    }
    let encoded = encoded.finish();
    if encoded.is_empty() {
        path.to_owned()
    } else {
        format!("{path}?{encoded}")
    }
}
