use std::collections::HashMap;

use axum::{
    body::{Body, to_bytes},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use minijinja::context;
use serde_json::json;
use sqlx::Row;

use super::{AdminRequest, form, redirect, schema::schema};
use crate::{ApiError, account, flash, login, password, session};

const DONE: &str = "/admin/password_change/done/";

pub(super) fn done(admin: &AdminRequest<'_>) -> Result<Response, ApiError> {
    admin.view(
        "site_admin/password_self.html",
        context! {title=>"Password change successful",done=>true},
    )
}

pub(super) async fn change(
    admin: &AdminRequest<'_>,
    target: Option<i64>,
    method: Method,
    body: Body,
) -> Result<Response, ApiError> {
    let model = schema().model("core", "user").expect("User admin schema");
    if target.is_some() && !admin.viewer.allows(model, "change") {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let actor = admin.viewer.user.id.ok_or(ApiError::Serialization)?;
    let id = target.unwrap_or(actor);
    let row = sqlx::query(
        "SELECT username,email,first_name,last_name,password FROM core_user WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(&admin.state.pool)
    .await?;
    let Some(row) = row else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let username: &str = row.try_get("username")?;
    let stored: &str = row.try_get("password")?;
    let usable = !stored.starts_with('!');
    let mut enabled = true;
    let mut errors = HashMap::new();
    if method == Method::POST {
        let Ok(bytes) = to_bytes(body, 128 * 1024).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        let posted = login::form_values(&bytes);
        let Some(origin) = admin.origin() else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            admin.headers,
            posted.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        let own = target.is_none();
        let mut comparison = stored.to_owned();
        enabled = own
            || !usable
            || posted
                .get("usable_password")
                .is_none_or(|value| value != "false");
        if enabled {
            let fields = if own {
                ["new_password1", "new_password2"]
                    .into_iter()
                    .zip(["password1", "password2"])
                    .filter_map(|(name, key)| Some((key.to_owned(), posted.get(name)?.clone())))
                    .collect()
            } else {
                posted.clone()
            };
            errors = account::validate_password(
                &fields,
                [
                    username,
                    row.try_get("first_name")?,
                    row.try_get("last_name")?,
                    row.try_get("email")?,
                ],
            );
            if let Some(messages) = errors.remove("password1") {
                if messages.as_slice() == ["This field is required."] {
                    errors.insert("password1", messages);
                } else {
                    errors.entry("password2").or_default().extend(messages);
                }
            }
            if own {
                let value = posted
                    .get("old_password")
                    .map_or("", String::as_str)
                    .as_bytes()
                    .to_vec();
                if value.is_empty() {
                    errors.insert("old_password", vec!["This field is required.".to_owned()]);
                } else {
                    let existing = stored.to_owned();
                    let matches = tokio::task::spawn_blocking(move || {
                        password::verify_and_upgrade(&value, Some(&existing))
                    })
                    .await
                    .map_err(|_| ApiError::Serialization)?;
                    match matches {
                        Some(Some(hash)) => {
                            let changed = sqlx::query(
                                "UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3",
                            )
                            .bind(&hash)
                            .bind(id)
                            .bind(stored)
                            .execute(&admin.state.pool)
                            .await?;
                            if changed.rows_affected() != 1 {
                                return Ok(StatusCode::CONFLICT.into_response());
                            }
                            comparison = hash;
                        }
                        Some(None) => {}
                        None => {
                            errors.insert("old_password",vec!["Your old password was entered incorrectly. Please enter it again.".to_owned()]);
                        }
                    }
                }
            }
            if errors.is_empty() {
                let hash = account::new_password(&fields["password1"]).await?;
                return store(admin, id, actor, target, username, &comparison, &hash).await;
            }
        } else if posted.contains_key("unset-password") {
            let random = session::random_token(
                b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                40,
            )
            .map_err(|_| ApiError::Serialization)?;
            return store(
                admin,
                id,
                actor,
                target,
                username,
                &comparison,
                &format!("!{random}"),
            )
            .await;
        } else {
            let mut response = redirect(admin.uri.path());
            if let Some(secret) = admin.state.secret.as_deref() {
                flash::push(
                    &mut response,
                    admin.headers,
                    secret,
                    40,
                    "Conflicting form data submitted. Please try again.",
                    admin.secure(),
                );
            }
            return Ok(response);
        }
    }
    if target.is_none() {
        return admin.view(
            "site_admin/password_self.html",
            context! {title=>"Password change",errors,done=>false},
        );
    }
    admin.view("site_admin/password_other.html",context!{
        title=>format!("{}: {username}",if usable{"Change password"}else{"Set password"}),target_username=>username,
        object_url=>format!("/admin/core/user/{id}/change/"),errors,usable,enabled,
    })
}

async fn store(
    admin: &AdminRequest<'_>,
    id: i64,
    actor: i64,
    target: Option<i64>,
    username: &str,
    before: &str,
    hash: &str,
) -> Result<Response, ApiError> {
    let secret = admin
        .state
        .secret
        .as_deref()
        .ok_or(ApiError::Serialization)?;
    let session = if id == actor {
        Some(
            session::StoredSession::read(&admin.state.pool, admin.headers, secret)
                .await?
                .ok_or(ApiError::Serialization)?,
        )
    } else {
        None
    };
    let mut tx = admin.state.pool.begin().await?;
    let changed = sqlx::query("UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3")
        .bind(hash)
        .bind(id)
        .bind(before)
        .execute(&mut *tx)
        .await?;
    if changed.rows_affected() != 1 {
        return Ok(StatusCode::CONFLICT.into_response());
    }
    let mut response = if target.is_some() {
        let model = schema().model("core", "user").expect("User admin schema");
        form::log(
            &mut tx,
            model,
            actor,
            id,
            username,
            2,
            json!([{"changed":{"fields":["password"]}}]),
        )
        .await?;
        let mut response = redirect(&format!("/admin/core/user/{id}/change/"));
        flash::push(
            &mut response,
            admin.headers,
            secret,
            25,
            if hash.starts_with('!') {
                "Password-based authentication was disabled."
            } else {
                "Password changed successfully."
            },
            admin.secure(),
        );
        response
    } else {
        redirect(DONE)
    };
    if let Some(session) = session {
        let (key, remember) = session.rotate_password(&mut tx, secret, hash).await?;
        login::set_cookie(
            &mut response,
            "sessionid",
            &key,
            remember.then_some(login::SESSION_AGE),
            admin.secure(),
            true,
        );
    }
    tx.commit().await?;
    Ok(response)
}
