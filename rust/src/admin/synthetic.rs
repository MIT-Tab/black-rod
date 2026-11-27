mod cleanup;
pub(crate) mod suggestions;

pub(crate) use cleanup::page as cleanup_page;

use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use serde_json::json;

use crate::{
    AppState,
    admin::amendment,
    agent::{
        merge::{self, AuditActor},
        mutation::Failure,
    },
    flash, login,
};

const TOOLS: &str = "/core/admin-tools/";

fn notices(
    state: &AppState,
    headers: &HeaderMap,
    redirect: &str,
    notices: &[(i32, String)],
) -> Response {
    let mut response = (StatusCode::FOUND, [(header::LOCATION, redirect)]).into_response();
    if !notices.is_empty()
        && let Some(secret) = state.secret.as_deref()
    {
        let secure = state
            .origin(headers)
            .is_some_and(|url| url.starts_with("https://"));
        flash::push_all(&mut response, headers, secret, notices, secure);
    }
    response
}

async fn form(
    state: &AppState,
    request: Request,
) -> Result<(Uri, HeaderMap, axum::body::Bytes), Box<Response>> {
    let (parts, body) = request.into_parts();
    let values = to_bytes(body, 2_621_440)
        .await
        .map_err(|_| Box::new(StatusCode::PAYLOAD_TOO_LARGE.into_response()))?;
    let origin = state
        .origin(&parts.headers)
        .ok_or_else(|| Box::new(StatusCode::BAD_REQUEST.into_response()))?;
    let csrf = url::form_urlencoded::parse(&values)
        .filter(|(name, _)| name == "csrfmiddlewaretoken")
        .map(|(_, value)| value)
        .last();
    if !login::csrf_valid(&parts.headers, csrf.as_deref(), &origin) {
        return Err(Box::new(login::csrf_failure()));
    }
    Ok((parts.uri, parts.headers, values))
}

pub(crate) async fn resolve(State(state): State<AppState>, request: Request) -> Response {
    let user = match super::require(&state, request.uri(), request.headers()).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let (_, headers, body) = match form(&state, request).await {
        Ok(values) => values,
        Err(response) => return *response,
    };
    let inputs = login::form_values(&body);
    let entity = inputs
        .get("entity_type")
        .map_or("", String::as_str)
        .trim()
        .to_lowercase();
    let id = |field| {
        inputs.get(field)
            .ok_or_else(|| "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'".to_owned())
            .and_then(|value| value.trim().parse::<i64>()
                .map_err(|_| format!("invalid literal for int() with base 10: {value:?}")))
    };
    let reason = inputs.get("reason").map_or("", String::as_str).trim();
    let outcome = async {
        let (source, target) = (id("synthetic_id")?, id("target_id")?);
        let mut tx = state.pool.begin().await.map_err(|error| error.to_string())?;
        merge::resolve(&mut tx, &entity, source, target, AuditActor {
            user: user.id,
            reason,
            context: json!({"source":"admin_tools"}),
        }).await.map_err(|error| match error {
            Failure::Invalid(message) => message,
            Failure::Stale(detail) => amendment::text(&detail["detail"]),
            Failure::Database(error) => amendment::Error::from(error).message(),
        })?;
        tx.commit().await.map_err(|error| error.to_string())?;
        Ok::<_, String>(json!({"type":"resolve_synthetic","entity_type":entity,"synthetic_id":source,"target_id":target,"reason":reason}))
    }.await;
    let alerts = match outcome {
        Ok(action) => {
            amendment::invalidate(&state).await;
            let mut alerts = vec![(25, "Synthetic entity resolved successfully.".to_owned())];
            match amendment::record::append(&action).await {
                Ok(Some(path)) => alerts.push((
                    20,
                    format!("Recorded amendment action to {}.", path.display()),
                )),
                Ok(None) => {}
                Err(error) => alerts.push((
                    30,
                    format!("Synthetic entity resolved but amendment recording failed: {error}"),
                )),
            }
            alerts
        }
        Err(error) => vec![(40, format!("Synthetic resolution failed: {error}"))],
    };
    notices(&state, &headers, TOOLS, &alerts)
}
