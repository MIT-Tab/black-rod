use axum::{
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};

use super::{limits, redirect, render, visit};
use crate::{ApiError, AppState, login, password, session, web};

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let account = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(id) = account.id else {
        return Ok(login::redirect_to_login(&uri));
    };
    let stored = sqlx::query_scalar::<_, String>("SELECT password FROM core_user WHERE id=$1")
        .bind(id)
        .fetch_one(&state.pool)
        .await?;
    if stored.starts_with('!') {
        return Ok(web::permission_denied(state, &account, &uri));
    }
    let visit = match visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    let mut error = None;
    if let Some(fields) = &visit.fields {
        if limits::consume(state, "reauthenticate", &limits::user(id), 10, 60)
            .await
            .is_none()
        {
            return Ok(limits::denied());
        }
        let provided = fields.get("password").map_or("", String::as_str);
        if provided.is_empty() {
            error = Some("This field is required.");
        } else {
            let entered = provided.as_bytes().to_owned();
            let compare = stored.clone();
            let verified = tokio::task::spawn_blocking(move || {
                password::verify_and_upgrade(&entered, Some(&compare))
            })
            .await
            .map_err(|_| ApiError::Serialization)?;
            if let Some(upgrade) = verified {
                let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
                let mut current = session::StoredSession::read(&state.pool, &visit.headers, secret)
                    .await?
                    .ok_or(ApiError::Serialization)?;
                let key = "account_authentication_methods";
                let mut methods = current
                    .values
                    .get(key)
                    .and_then(|value| serde_json::from_str::<Vec<Value>>(value.get()).ok())
                    .unwrap_or_default();
                methods.push(json!({"method":"password","at":chrono::Utc::now().timestamp_micros() as f64/1e6,"reauthenticated":true}));
                current
                    .values
                    .insert(key.to_owned(), serde_json::value::to_raw_value(&methods)?);
                let mut response = redirect(if visit.next.is_empty() {
                    "/"
                } else {
                    &visit.next
                });
                if let Some(upgrade) = upgrade {
                    let mut tx = state.pool.begin().await?;
                    let changed =
                        sqlx::query("UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3")
                            .bind(&upgrade)
                            .bind(id)
                            .bind(stored)
                            .execute(&mut *tx)
                            .await?;
                    if changed.rows_affected() != 1 {
                        return Ok(StatusCode::CONFLICT.into_response());
                    }
                    let (key, remember) =
                        current.rotate_password(&mut tx, secret, &upgrade).await?;
                    tx.commit().await?;
                    login::set_cookie(
                        &mut response,
                        "sessionid",
                        &key,
                        remember.then_some(login::SESSION_AGE),
                        visit.secure(),
                        true,
                    );
                } else if !current.save(&state.pool, secret).await? {
                    return Ok(StatusCode::CONFLICT.into_response());
                }
                return Ok(response);
            }
            error = Some("Incorrect password.");
        }
    }
    render(
        state,
        &account,
        "account/reauthenticate.html",
        &visit,
        context! {title=>"Confirm Access",error},
    )
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Account reauthentication failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
