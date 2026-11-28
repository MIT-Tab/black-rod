use std::sync::LazyLock;

use axum::{
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};

use crate::{AppState, agent, drf, is_django_integer, json_error, private_api, web};

fn numeric_path_segment(value: &str) -> bool {
    percent_encoding::percent_decode_str(value)
        .decode_utf8()
        .is_ok_and(|value| is_django_integer(&value))
}

pub(crate) fn public_api_path(path: &str) -> bool {
    matches!(
        path,
        "/api/schools/"
            | "/api/schools/all/"
            | "/api/schedule/"
            | "/api/oty-guide/"
            | "/api/standings/"
            | "/api/standings/replay/"
            | "/api/standings/through-date/"
    ) || path
        .strip_prefix("/api/debaters/")
        .and_then(|tail| tail.strip_suffix('/'))
        .is_some_and(numeric_path_segment)
        || [
            "/api/schools/",
            "/api/teams/",
            "/api/tournaments/",
            "/api/debaters/",
        ]
        .into_iter()
        .any(|prefix| {
            path.strip_prefix(prefix)
                .and_then(|tail| tail.strip_suffix("/detail/"))
                .is_some_and(numeric_path_segment)
        })
}

pub(crate) fn protected_api_path(path: &str) -> bool {
    if matches!(
        path,
        "/api/private/forum-profiles/" | "/api/private/debater-emails/"
    ) {
        return true;
    }
    let Some(tail) = path.strip_prefix("/api/agent/v1/") else {
        return false;
    };
    let mut segments = tail.split('/');
    match (
        segments.next(),
        segments.next(),
        segments.next(),
        segments.next(),
        segments.next(),
    ) {
        (Some(""), None, None, None, None)
        | (Some("guide" | "batches"), Some(""), None, None, None) => true,
        (Some("records"), Some(resource), Some(""), None, None) => !resource.is_empty(),
        (Some("records"), Some(resource), Some(id), Some(""), None) => {
            !resource.is_empty() && is_django_integer(id)
        }
        (Some("batches"), Some(id), Some(""), None, None)
        | (Some("batches"), Some(id), Some("undo"), Some(""), None) => is_django_integer(id),
        _ => false,
    }
}

fn normalized_public_uri(uri: &Uri) -> Option<Uri> {
    let path = uri.path();
    if !path.contains('%') {
        return None;
    }
    let target = uri.path_and_query()?.as_str();
    let decoded = percent_encoding::percent_decode_str(path)
        .decode_utf8()
        .ok()?;
    format!("{decoded}{}", &target[path.len()..]).parse().ok()
}

pub(super) async fn api_protocol(
    State(state): State<AppState>,
    mut request: Request,
    next: axum::middleware::Next,
) -> Response {
    let path = request.uri().path();
    let public = public_api_path(path);
    let private = matches!(
        path,
        "/api/private/forum-profiles/" | "/api/private/debater-emails/"
    ) || path.starts_with("/api/agent/v1/");
    if !public && !private {
        return next.run(request).await;
    }
    let head = *request.method() == Method::HEAD;
    let format = url::form_urlencoded::parse(request.uri().query().unwrap_or_default().as_bytes())
        .filter(|(name, _)| name == "format")
        .map(|(_, value)| value.into_owned())
        .last();
    let browse = format
        .as_deref()
        .is_none_or(|format| matches!(format, "" | "json" | "api"))
        && drf::wants_html(request.headers(), format.as_deref());
    let canonical_uri = if public {
        normalized_public_uri(request.uri())
    } else {
        None
    };
    let view = browse.then(|| {
        (
            canonical_uri
                .as_ref()
                .unwrap_or_else(|| request.uri())
                .clone(),
            request.method().clone(),
            request.headers().clone(),
        )
    });
    let mut indent = 0;
    let mut suitable = false;
    for media in request
        .headers()
        .get_all(header::ACCEPT)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
    {
        let mut parts = media.split(';');
        let media = parts.next().unwrap_or_default().trim();
        let json = drf::matches_media(media, drf::JSON_MEDIA);
        let html = drf::matches_media(media, drf::HTML_MEDIA);
        suitable |= match format.as_deref() {
            Some("json") => json,
            Some("api") => html,
            _ => json || html,
        };
        if json {
            indent = parts
                .filter_map(|part| part.trim().split_once('='))
                .filter(|(key, _)| key.trim().eq_ignore_ascii_case("indent"))
                .filter_map(|(_, value)| value.trim().trim_matches('"').parse::<u8>().ok())
                .map(|width| width.min(8))
                .next()
                .unwrap_or(indent);
        }
    }
    let error = if format
        .as_deref()
        .is_some_and(|format| !matches!(format, "" | "json" | "api"))
    {
        Some((StatusCode::NOT_FOUND, "Not found."))
    } else if !suitable && request.headers().contains_key(header::ACCEPT) {
        Some((
            StatusCode::NOT_ACCEPTABLE,
            "Could not satisfy the request Accept header.",
        ))
    } else {
        None
    };
    let response = if let Some((status, detail)) = error {
        if private {
            if path.starts_with("/api/agent/v1/") {
                agent::negotiation_failure(path, status, detail)
            } else {
                private_api::negotiation_failure(path, status, detail)
            }
        } else {
            negotiation_error(status, detail)
        }
    } else if public {
        match drf::public_user(&state, &mut request).await {
            Err(response) => response,
            Ok(user)
                if matches!(
                    *request.method(),
                    Method::GET | Method::HEAD | Method::OPTIONS
                ) =>
            {
                request.extensions_mut().insert(user);
                if let Some(uri) = canonical_uri {
                    request.extensions_mut().insert(OriginalUri(uri));
                }
                next.run(request).await
            }
            Ok(_) => json_error(
                StatusCode::METHOD_NOT_ALLOWED,
                &format!("Method \"{}\" not allowed.", request.method()),
            ),
        }
    } else {
        next.run(request).await
    };
    if head {
        let mut response = response.map(|_| axum::body::Body::empty());
        if browse {
            response.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("text/html; charset=utf-8"),
            );
        }
        return response;
    }
    if let Some((uri, method, headers)) = view {
        return drf::response(&state, &uri, &method, &headers, response).await;
    }
    if indent == 0
        || response
            .headers()
            .get(header::CONTENT_TYPE)
            .is_none_or(|value| value != "application/json")
    {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, usize::MAX).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let value: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(_) => return Response::from_parts(parts, bytes.into()),
    };
    let mut output = Vec::new();
    let spacing = [b' '; 8];
    let formatter = serde_json::ser::PrettyFormatter::with_indent(&spacing[..usize::from(indent)]);
    let mut serializer = serde_json::Serializer::with_formatter(&mut output, formatter);
    if serde::Serialize::serialize(&value, &mut serializer).is_err() {
        return Response::from_parts(parts, bytes.into());
    }
    parts.headers.remove(header::CONTENT_LENGTH);
    Response::from_parts(parts, output.into())
}

pub(super) async fn validate_host(
    State(state): State<AppState>,
    request: Request,
    next: axum::middleware::Next,
) -> Response {
    if !state.host_allowed(request.headers()) {
        return StatusCode::BAD_REQUEST.into_response();
    }
    next.run(request).await
}

pub(super) async fn report_server_errors(
    request: Request,
    next: axum::middleware::Next,
) -> Response {
    let route = request
        .extensions()
        .get::<axum::extract::MatchedPath>()
        .cloned();
    if !sentry::Hub::current()
        .client()
        .is_some_and(|client| client.is_enabled())
    {
        return next.run(request).await;
    }
    let method = request.method().clone();
    let (response, cause) = crate::diagnostics::with_cause(next.run(request)).await;
    let status = response.status();
    if status.is_server_error() {
        let route = route
            .as_ref()
            .map_or("Unmatched route", |route| route.as_str());
        sentry::capture_event(crate::diagnostics::event(route, &method, status, cause));
    }
    response
}

pub(crate) fn protect_private_response(headers: &mut HeaderMap) {
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("max-age=0, no-cache, no-store, must-revalidate, private"),
    );
    let varies_by_cookie = headers
        .get_all(header::VARY)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .any(|value| value.trim().eq_ignore_ascii_case("cookie"));
    if !varies_by_cookie {
        headers.append(header::VARY, HeaderValue::from_static("Cookie"));
    }
}

pub(super) async fn private_responses(request: Request, next: axum::middleware::Next) -> Response {
    let mut response = next.run(request).await;
    protect_private_response(response.headers_mut());
    response
}

pub(super) async fn security_headers(request: Request, next: axum::middleware::Next) -> Response {
    let path = request.uri().path();
    let head = *request.method() == Method::HEAD;
    let may_embed = path == "/core/widgets/recent-results/";
    let summernote =
        path.starts_with("/summernote/editor/") || path == "/summernote/upload_attachment/";
    let mut response = next.run(request).await;
    if !response.headers().contains_key(header::CONTENT_TYPE) {
        let template = match response.status() {
            StatusCode::BAD_REQUEST => Some("400.html"),
            StatusCode::FORBIDDEN => Some("403.html"),
            StatusCode::NOT_FOUND => Some("404.html"),
            StatusCode::INTERNAL_SERVER_ERROR => Some("500.html"),
            _ => None,
        };
        if let Some(template) = template
            && let Ok(page) = web::render(template, minijinja::context! {})
        {
            let headers = response.headers_mut();
            headers.insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("text/html; charset=utf-8"),
            );
            headers.remove(header::CONTENT_LENGTH);
            if !head {
                *response.body_mut() = axum::body::Body::from(page);
            }
        }
    }
    let redirect = response.status() == StatusCode::MOVED_PERMANENTLY;
    let embeddable_response = may_embed && response.status().is_success();
    let headers = response.headers_mut();
    let same_origin = summernote
        && headers
            .get(header::X_FRAME_OPTIONS)
            .is_some_and(|value| value == "SAMEORIGIN");
    if !redirect && !embeddable_response && !same_origin {
        headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    }
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers
        .entry(header::REFERRER_POLICY)
        .or_insert(HeaderValue::from_static("same-origin"));
    headers.insert(
        "cross-origin-opener-policy",
        HeaderValue::from_static("same-origin"),
    );
    response
}

static SLASH_ROUTES: LazyLock<regex::RegexSet> = LazyLock::new(|| {
    regex::RegexSet::new(include_str!("../../schema/slash_routes.txt").lines())
        .expect("Django append-slash route patterns")
});

pub(super) async fn append_slash(OriginalUri(uri): OriginalUri) -> Response {
    missing_route(&uri)
}

pub(crate) fn missing_route(uri: &Uri) -> Response {
    let path = uri.path();
    if path.ends_with('/') {
        return StatusCode::NOT_FOUND.into_response();
    }
    let decoded = percent_encoding::percent_decode_str(path).decode_utf8_lossy();
    if path != "/admin" && !SLASH_ROUTES.is_match(&format!("{decoded}/")) {
        return StatusCode::NOT_FOUND.into_response();
    }
    slash_redirect(uri)
}

pub(crate) fn slash_redirect(uri: &Uri) -> Response {
    let mut location = uri.path().to_owned();
    location.push('/');
    if let Some(query) = uri.query() {
        location.push('?');
        location.push_str(query);
    }
    (
        StatusCode::MOVED_PERMANENTLY,
        [
            (header::LOCATION, location),
            (header::CONTENT_TYPE, "text/html; charset=utf-8".into()),
        ],
    )
        .into_response()
}

fn negotiation_error(status: StatusCode, detail: &str) -> Response {
    let mut response = json_error(status, detail);
    response
        .headers_mut()
        .insert(header::VARY, HeaderValue::from_static("Accept"));
    response
}

pub(crate) fn limit(raw: &str) -> Option<i64> {
    let value = url::form_urlencoded::parse(raw.as_bytes())
        .filter(|(name, _)| name == "limit")
        .map(|(_, value)| value)
        .last()?;
    let value = value.trim();
    let value = value.strip_prefix('+').unwrap_or(value);
    let mut digits = 0;
    let mut limit = 0;
    let mut previous_digit = false;
    for ch in value.chars() {
        if ch == '_' && previous_digit {
            previous_digit = false;
            continue;
        }
        let digit = crate::decimal_digit(ch)?;
        digits += 1;
        limit = (limit * 10 + digit).min(200);
        previous_digit = true;
    }
    (previous_digit && digits <= 4300 && limit > 0).then_some(limit)
}

#[cfg(test)]
mod tests {
    use super::limit;

    #[test]
    fn caps_large_valid_limits_and_rejects_invalid_integer_syntax() {
        assert_eq!(limit("limit=1&limit=%2B1_0"), Some(10));
        assert_eq!(limit("limit=%D9%A2"), Some(2));
        assert_eq!(limit("limit=999999999999999999999"), Some(200));
        assert_eq!(limit(&format!("limit={}", "9".repeat(4301))), None);
        for invalid in ["_1", "1_", "1__0", "-1", "0", "2.1", "%C2%B2"] {
            assert_eq!(limit(&format!("limit={invalid}")), None);
        }
    }
}
