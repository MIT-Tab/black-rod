use axum::{
    extract::{Request, State},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::Value;
use sqlx::Row;

use super::{redirect, render, visit};
use crate::{ApiError, AppState, flash, login, session};

const LINK: &str = "/accounts/3rdparty/";

#[derive(Serialize)]
struct Connection {
    id: i64,
    provider: String,
    label: String,
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    if uri.path() == "/accounts/social/connections/" {
        return Ok((StatusCode::MOVED_PERMANENTLY, [(header::LOCATION, LINK)]).into_response());
    }
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(id) = user.id else {
        return Ok(login::redirect_to_login(&uri));
    };
    let visit = match visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    let mut error = None;
    if let Some(fields) = &visit.fields {
        let selected = fields
            .get("account")
            .and_then(|value| value.parse::<i64>().ok());
        if let Some(selected) = selected {
            let mut tx = state.pool.begin().await?;
            let password = sqlx::query_scalar::<_, String>(
                "UPDATE core_user SET id=id WHERE id=$1 RETURNING password",
            )
            .bind(id)
            .fetch_one(&mut *tx)
            .await?;
            let accounts = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM socialaccount_socialaccount WHERE user_id=$1",
            )
            .bind(id)
            .fetch_all(&mut *tx)
            .await?;
            if !accounts.contains(&selected) {
                error =
                    Some("Select a valid choice. That choice is not one of the available choices.");
            } else if accounts.len() == 1 && password.starts_with('!') {
                error = Some("Your account has no password set up.");
            } else {
                sqlx::query("DELETE FROM socialaccount_socialtoken WHERE account_id=$1")
                    .bind(selected)
                    .execute(&mut *tx)
                    .await?;
                sqlx::query("DELETE FROM socialaccount_socialaccount WHERE user_id=$1 AND id=$2")
                    .bind(id)
                    .bind(selected)
                    .execute(&mut *tx)
                    .await?;
                tx.commit().await?;
                let mut response = redirect(LINK);
                if let Some(secret) = state.secret.as_deref() {
                    flash::push(
                        &mut response,
                        &visit.headers,
                        secret,
                        20,
                        "The third-party account has been disconnected.",
                        visit.secure(),
                    );
                }
                return Ok(response);
            }
        } else {
            error = Some("This field is required.");
        }
    }
    let rows = sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id,provider,uid,CAST(extra_data AS TEXT) AS extra
        FROM socialaccount_socialaccount WHERE user_id=$1 ORDER BY id",
    )
    .bind(id)
    .fetch_all(&state.pool)
    .await?;
    let accounts = rows
        .iter()
        .map(|row| {
            let provider: &str = row.try_get("provider")?;
            let uid: &str = row.try_get("uid")?;
            let extra =
                serde_json::from_str::<Value>(row.try_get::<&str, _>("extra")?).unwrap_or_default();
            let label = [
                "username",
                "userName",
                "user_name",
                "login",
                "handle",
                "email",
                "display_name",
                "name",
            ]
            .iter()
            .filter_map(|key| extra[*key].as_str())
            .find(|value| !value.is_empty())
            .unwrap_or(uid);
            Ok(Connection {
                id: row.try_get("id")?,
                provider: if provider == "apdaonline" {
                    "APDAOnline".to_owned()
                } else {
                    provider.to_owned()
                },
                label: label.to_owned(),
            })
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()?;
    render(
        state,
        &user,
        "account/connections.html",
        &visit,
        context! {title=>"Account Connections",accounts,error},
    )
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Account connections failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
