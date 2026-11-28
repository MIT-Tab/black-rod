pub(crate) mod legacy;

use std::collections::BTreeSet;

use axum::{
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use chrono::Utc;
use minijinja::context;
use ring::hmac;
use serde_json::json;
use sqlx::Row;
use subtle::ConstantTimeEq;

use super::{Errors, limits, mail, passwords, redirect, render, visit};
use crate::{ApiError, AppState, flash, login, session};

const DONE: &str = "/accounts/password/reset/done/";
const KEY_PREFIX: &str = "/accounts/password/reset/key/";
const KEY_DONE: &str = "/accounts/password/reset/key/done/";
const SESSION_KEY: &str = "_password_reset_key";
const RESET_SALT: &[u8] = b"django.contrib.auth.tokens.PasswordResetTokenGenerator";
const EPOCH: i64 = 978_307_200;
const MAX_AGE: i64 = 3 * 24 * 60 * 60;

struct ResetUser {
    id: i64,
    username: String,
    email: String,
    first: String,
    last: String,
    hash: String,
    last_login: String,
    emails: BTreeSet<String>,
}

impl ResetUser {
    async fn read(state: &AppState, id: i64) -> Result<Option<Self>, ApiError> {
        let row = sqlx::query("SELECT username,email,first_name,last_name,password,CAST(last_login AS TEXT) AS last_login
            FROM core_user WHERE id=$1").bind(id).fetch_optional(&state.pool).await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let email: String = row.try_get("email")?;
        let mut emails = sqlx::query_scalar::<_, String>(
            "SELECT email FROM account_emailaddress WHERE user_id=$1",
        )
        .bind(id)
        .fetch_all(&state.pool)
        .await?
        .into_iter()
        .collect::<BTreeSet<_>>();
        if !email.is_empty() {
            emails.insert(email.clone());
        }
        let timestamp: Option<&str> = row.try_get("last_login")?;
        let last_login = timestamp
            .and_then(|timestamp| timestamp.get(..19))
            .unwrap_or_default()
            .replace('T', " ");
        Ok(Some(Self {
            id,
            username: row.try_get("username")?,
            email,
            first: row.try_get("first_name")?,
            last: row.try_get("last_name")?,
            hash: row.try_get("password")?,
            last_login,
            emails,
        }))
    }

    fn token(&self, timestamp: i64, secret: &[u8]) -> String {
        self.token_with_emails(timestamp, secret, true)
    }

    fn token_with_emails(&self, timestamp: i64, secret: &[u8], allauth: bool) -> String {
        let mail = self
            .emails
            .iter()
            .filter(|_| allauth)
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join("|");
        let material = format!(
            "{}{}{}{timestamp}{}{mail}",
            self.id, self.hash, self.last_login, self.email
        );
        let signature = hex::encode(hmac::sign(
            &session::key(RESET_SALT, secret),
            material.as_bytes(),
        ));
        let short: String = signature.chars().step_by(2).collect();
        format!("{}-{short}", base36(timestamp))
    }

    fn valid(&self, candidate: &str, secret: &[u8]) -> bool {
        self.valid_token(candidate, secret, true)
    }

    fn valid_token(&self, candidate: &str, secret: &[u8], allauth: bool) -> bool {
        let Some((stamp, _)) = candidate.split_once('-') else {
            return false;
        };
        let Ok(stamp) = i64::from_str_radix(stamp, 36) else {
            return false;
        };
        let age = (Utc::now().timestamp() - EPOCH).saturating_sub(stamp);
        if age > MAX_AGE || stamp < 0 {
            return false;
        }
        bool::from(
            candidate
                .as_bytes()
                .ct_eq(self.token_with_emails(stamp, secret, allauth).as_bytes()),
        )
    }
}

fn base36(mut number: i64) -> String {
    const ALPHABET: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";
    let mut result = Vec::new();
    loop {
        result.push(ALPHABET[(number % 36) as usize]);
        number /= 36;
        if number == 0 {
            break;
        }
    }
    result.reverse();
    String::from_utf8(result).expect("base-36 digits are ASCII")
}

async fn send_reset(
    state: &AppState,
    origin: &str,
    to: &str,
    secret: &[u8],
) -> Result<(), ApiError> {
    let mut recipients = Vec::new();
    let mut verified = false;
    for (id, preferred) in sqlx::query_as::<_,(i64,i64)>("SELECT CAST(u.id AS BIGINT),
        CAST(CASE WHEN EXISTS(SELECT 1 FROM account_emailaddress e WHERE e.user_id=u.id AND LOWER(e.email)=$1 AND e.verified)
            THEN 1 ELSE 0 END AS BIGINT)
        FROM core_user u WHERE u.is_active AND (LOWER(u.email)=$1 OR EXISTS(
            SELECT 1 FROM account_emailaddress e WHERE e.user_id=u.id AND LOWER(e.email)=$1))
        ORDER BY u.id").bind(to).fetch_all(&state.pool).await?
    {
        if preferred == 1 && !verified {
            verified = true;
            recipients.clear();
        }
        if !verified || preferred == 1 {
            recipients.push(id);
        }
    }
    let site = mail::site(state).await?;
    if recipients.is_empty() {
        return mail::send(to,&site,"Unknown Account",format!(
            "Hello from {site}!\n\nYou are receiving this email because you or someone else tried to request a password for your user account. However, we do not have any record of a user with email {to} in our database.\n\nIf you intended to create an account, you can sign up at {origin}/accounts/signup/.\n\nThank you for using {site}!\n"
        )).await;
    }
    for id in recipients {
        let Some(person) = ResetUser::read(state, id).await? else {
            continue;
        };
        let token = person.token(Utc::now().timestamp() - EPOCH, secret);
        let uid = base36(id);
        let link = format!("{origin}{KEY_PREFIX}{uid}-{token}/");
        mail::send(to,&site,"Password Reset Email",format!(
            "Hello from {site}!\n\nYou're receiving this email because you or someone else has requested a password reset for your user account.\nIt can be safely ignored if you did not request a password reset. Click the link below to reset your password.\n\n{link}\n\nIn case you forgot, your username is {}.\n\nThank you for using {site}!\n",person.username
        )).await?;
    }
    Ok(())
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    match response(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Account password reset failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn response(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let ip = limits::ip(state, &request);
    let path = request.uri().path().to_owned();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let visit = match visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    if path == KEY_DONE {
        return render(
            state,
            &user,
            "account/password.html",
            &visit,
            context! {
                title=>"Change Password",heading=>"Change Password",completed=>true
            },
        );
    }
    if let Some(key) = path
        .strip_prefix(KEY_PREFIX)
        .and_then(|value| value.strip_suffix('/'))
    {
        if visit.fields.is_some()
            && limits::consume(state, "reset_password_from_key", &ip, 20, 60)
                .await
                .is_none()
        {
            return Ok(limits::denied());
        }
        return from_key(state, &user, &visit, key, &path).await;
    }
    let mut errors = Errors::new();
    let mut email = "";
    if path != DONE
        && let Some(fields) = &visit.fields
    {
        if limits::consume(state, "reset_password", &ip, 20, 60)
            .await
            .is_none()
        {
            return Ok(limits::denied());
        }
        email = fields.get("email").map_or("", String::as_str).trim();
        if email.is_empty() {
            errors.insert("email", vec!["This field is required.".to_owned()]);
        } else if !super::entrance::email_valid(email) {
            errors.insert("email", vec!["Enter a valid email address.".to_owned()]);
        } else {
            let sent = email.to_lowercase();
            if limits::consume(state, "reset_password", &limits::digest(&sent), 5, 60)
                .await
                .is_none()
            {
                return Ok(limits::denied());
            }
            let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
            send_reset(state, &visit.origin, &sent, secret).await?;
            return Ok(redirect(if visit.next.is_empty() {
                DONE
            } else {
                &visit.next
            }));
        }
    }
    let username = if let Some(id) = user.id {
        sqlx::query_scalar::<_, String>("SELECT username FROM core_user WHERE id=$1")
            .bind(id)
            .fetch_optional(&state.pool)
            .await?
            .unwrap_or_default()
    } else {
        String::new()
    };
    render(
        state,
        &user,
        "account/reset.html",
        &visit,
        context! {
            title=>"Password Reset",sent=>path==DONE,username,email,errors
        },
    )
}

async fn from_key(
    state: &AppState,
    user: &session::User,
    visit: &super::Visit,
    key: &str,
    path: &str,
) -> Result<Response, ApiError> {
    let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
    let (uid, supplied) = key.split_once('-').unwrap_or(("", ""));
    let candidate = i64::from_str_radix(uid, 36).ok().filter(|id| *id > 0);
    let person = if let Some(id) = candidate {
        ResetUser::read(state, id).await?
    } else {
        None
    };
    let staged = supplied == "set-password";
    let mut session = session::StoredSession::read(&state.pool, &visit.headers, secret).await?;
    let token = if staged {
        session
            .as_ref()
            .and_then(|data| data.string(SESSION_KEY))
            .unwrap_or_default()
    } else {
        supplied.to_owned()
    };
    let mut errors = Errors::new();
    let mut valid = false;
    if let Some(person) = person.filter(|person| person.valid(&token, secret)) {
        valid = true;
        if !staged {
            let mut location = format!("{KEY_PREFIX}{uid}-set-password/");
            if !visit.next.is_empty() {
                location.push('?');
                location.push_str(
                    &url::form_urlencoded::Serializer::new(String::new())
                        .append_pair("next", &visit.next)
                        .finish(),
                );
            }
            let mut response = redirect(&location);
            if let Some(session) = session.as_mut() {
                session.set_string(SESSION_KEY, &token)?;
            }
            if let Some(session) = session {
                let Some(session) = session.refresh(&state.pool, secret).await? else {
                    return Ok(StatusCode::CONFLICT.into_response());
                };
                session.set_cookie(&mut response, visit.secure());
            } else {
                let session_id =
                    session::create_anonymous(&state.pool, secret, &json!({SESSION_KEY:token}))
                        .await?;
                login::set_cookie(
                    &mut response,
                    "sessionid",
                    &session_id,
                    Some(login::SESSION_AGE),
                    visit.secure(),
                    true,
                );
            }
            return Ok(response);
        }
        if user
            .id
            .is_some_and(|authenticated| authenticated != person.id)
        {
            session::delete(&state.pool, &visit.headers).await?;
            let session_id =
                session::create_anonymous(&state.pool, secret, &json!({SESSION_KEY:token})).await?;
            let mut response = redirect(path);
            login::set_cookie(
                &mut response,
                "sessionid",
                &session_id,
                Some(login::SESSION_AGE),
                visit.secure(),
                true,
            );
            return Ok(response);
        }
        if let Some(fields) = &visit.fields {
            errors = passwords::validation(
                fields,
                [&person.username, &person.first, &person.last, &person.email],
            );
            if errors.is_empty() {
                let hash = passwords::new_hash(&fields["password1"]).await?;
                let changed =
                    sqlx::query("UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3")
                        .bind(&hash)
                        .bind(person.id)
                        .bind(person.hash)
                        .execute(&state.pool)
                        .await?;
                if changed.rows_affected() != 1 {
                    return Ok(StatusCode::CONFLICT.into_response());
                }
                let mut response = redirect(if visit.next.is_empty() {
                    KEY_DONE
                } else {
                    &visit.next
                });
                flash::push(
                    &mut response,
                    &visit.headers,
                    secret,
                    25,
                    "Password successfully changed.",
                    visit.secure(),
                );
                return Ok(response);
            }
        }
    }
    render(
        state,
        user,
        "account/password.html",
        visit,
        context! {
            title=>"Change Password",heading=>if valid{"Change Password"}else{"Bad Token"},
            reset_stage=>true,action=>path,invalid_token=>!valid,password_label=>"New Password",errors
        },
    )
}
