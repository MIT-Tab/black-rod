use axum::{
    extract::{OriginalUri, Request, State},
    http::{HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};

use crate::{AppState, agent, drf, is_django_integer, json_error, private_api, web};

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
        .is_some_and(is_django_integer)
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
                .is_some_and(is_django_integer)
        })
}

pub(super) async fn api_protocol(
    State(state): State<AppState>,
    request: Request,
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
    let view = browse.then(|| {
        (
            request.uri().clone(),
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
        if let Some("*/*" | "application/*" | "application/json" | "text/html" | "text/*") =
            parts.next().map(str::trim)
        {
            suitable = true;
            indent = parts
                .filter_map(|part| part.trim().strip_prefix("indent="))
                .filter_map(|value| value.trim_matches('"').parse::<u8>().ok())
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
    } else if (!suitable || (format.as_deref() == Some("api") && !browse))
        && request.headers().contains_key(header::ACCEPT)
    {
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
    } else if public
        && !matches!(
            *request.method(),
            Method::GET | Method::HEAD | Method::OPTIONS
        )
    {
        json_error(
            StatusCode::METHOD_NOT_ALLOWED,
            &format!("Method \"{}\" not allowed.", request.method()),
        )
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
    let response = next.run(request).await;
    if response.status().is_server_error() {
        let route = route
            .as_ref()
            .map_or("Unmatched route", |route| route.as_str());
        sentry::capture_message(
            &format!("HTTP {} in {route}", response.status().as_u16()),
            sentry::Level::Error,
        );
    }
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
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(
        "cross-origin-opener-policy",
        HeaderValue::from_static("same-origin"),
    );
    response
}

pub(super) async fn append_slash(OriginalUri(uri): OriginalUri) -> Response {
    if let Some(id) = uri.path().strip_prefix("/api/debaters/")
        && !is_django_integer(id)
    {
        return StatusCode::NOT_FOUND.into_response();
    }
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
