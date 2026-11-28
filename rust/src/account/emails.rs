use axum::{
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use sqlx::{Any, AnyConnection, Executor, Row, any::AnyRow};

use super::{Visit, entrance::email_valid, limits, mail, redirect, render, visit};
use crate::{
    ApiError, AppState, flash, login,
    session::{self, User},
};

const EMAIL: &str = "/accounts/email/";
const SALT: &[u8] = b"accountsigner";
const MAX_AGE: i64 = 60 * 60 * 24 * 3;

#[derive(Serialize)]
struct Address {
    id: i64,
    user: i64,
    email: String,
    username: String,
    verified: bool,
    primary: bool,
}

const ADDRESS: &str = r#"
    SELECT CAST(a.id AS BIGINT) AS id,CAST(a.user_id AS BIGINT) AS user_id,a.email,u.username,
        CASE WHEN a.verified THEN 1 ELSE 0 END AS verified,
        CASE WHEN a."primary" THEN 1 ELSE 0 END AS primary_flag
    FROM account_emailaddress a JOIN core_user u ON u.id=a.user_id
"#;

impl Address {
    fn read(row: &AnyRow) -> Result<Self, sqlx::Error> {
        Ok(Self {
            id: row.try_get("id")?,
            user: row.try_get("user_id")?,
            email: row.try_get("email")?,
            username: row.try_get("username")?,
            verified: row.try_get::<i32, _>("verified")? == 1,
            primary: row.try_get::<i32, _>("primary_flag")? == 1,
        })
    }
}

async fn addresses<'e>(
    db: impl Executor<'e, Database = Any>,
    user: i64,
) -> Result<Vec<Address>, ApiError> {
    let statement = format!("{ADDRESS} WHERE a.user_id=$1 ORDER BY a.email");
    Ok(sqlx::query(sqlx::AssertSqlSafe(statement))
        .bind(user)
        .fetch_all(db)
        .await?
        .iter()
        .map(Address::read)
        .collect::<Result<_, _>>()?)
}

pub(super) async fn send_confirmation(
    state: &AppState,
    origin: &str,
    id: i64,
    email: &str,
    username: &str,
    _signup: bool,
) -> Result<(), ApiError> {
    if limits::consume(
        state,
        "confirm_email",
        &limits::digest(&email.to_lowercase()),
        1,
        180,
    )
    .await
    .is_none()
    {
        return Ok(());
    }
    let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
    let site = mail::site(state).await?;
    let domain = sqlx::query_scalar::<_, String>("SELECT domain FROM django_site WHERE id=1")
        .fetch_optional(&state.pool)
        .await?
        .unwrap_or_else(|| "example.com".to_owned());
    let key = session::encode_signed(&id.to_string(), secret, SALT);
    let link = format!("{origin}/accounts/confirm-email/{key}/");
    let body = format!(
        "Hello from {site}!\n\nYou're receiving this email because user {username} has given your email address to register an account on {domain}.\n\nTo confirm this is correct, go to {link}\n\nThank you for using {site}!\n{domain}\n"
    );
    mail::send(email, &site, "Please Confirm Your Email Address", body).await
}

fn notice(state: &AppState, visit: &Visit, response: &mut Response, level: i32, message: &str) {
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            response,
            &visit.headers,
            secret,
            level,
            message,
            visit.secure(),
        );
    }
}

fn changed(state: &AppState, visit: &Visit, message: &str) -> Response {
    let mut result = redirect(EMAIL);
    notice(state, visit, &mut result, 25, message);
    result
}

async fn lock_user(conn: &mut AnyConnection, user: i64) -> Result<(), ApiError> {
    sqlx::query("UPDATE core_user SET id=id WHERE id=$1")
        .bind(user)
        .execute(conn)
        .await?;
    Ok(())
}

async fn primary(conn: &mut AnyConnection, address: &Address) -> Result<(), ApiError> {
    sqlx::query(
        r#"UPDATE account_emailaddress SET "primary"=FALSE WHERE user_id=$1 AND "primary""#,
    )
    .bind(address.user)
    .execute(&mut *conn)
    .await?;
    let changed =
        sqlx::query(r#"UPDATE account_emailaddress SET "primary"=TRUE WHERE id=$1 AND user_id=$2"#)
            .bind(address.id)
            .bind(address.user)
            .execute(&mut *conn)
            .await?;
    if changed.rows_affected() != 1 {
        return Err(ApiError::Constraint);
    }
    sqlx::query("UPDATE core_user SET email=$1 WHERE id=$2")
        .bind(&address.email)
        .bind(address.user)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

async fn confirm(
    state: &AppState,
    user: User,
    visit: Visit,
    path: &str,
) -> Result<Response, ApiError> {
    let key = path
        .strip_prefix("/accounts/confirm-email/")
        .unwrap_or_default()
        .trim_end_matches('/');
    let id = state
        .secret
        .as_deref()
        .and_then(|secret| session::decode_timed::<i64>(key, secret, SALT, MAX_AGE));
    let address = if let Some(id) = id {
        let statement = format!("{ADDRESS} WHERE a.id=$1 AND NOT a.verified");
        sqlx::query(sqlx::AssertSqlSafe(statement))
            .bind(id)
            .fetch_optional(&state.pool)
            .await?
            .as_ref()
            .map(Address::read)
            .transpose()?
    } else {
        None
    };
    if visit.fields.is_some() && address.is_none() {
        return Ok(StatusCode::NOT_FOUND.into_response());
    }
    let switch_user = address
        .as_ref()
        .zip(user.id)
        .is_some_and(|(email, signed_in)| email.user != signed_in);
    if switch_user {
        session::delete(&state.pool, &visit.headers).await?;
    }
    let view = if switch_user { User::default() } else { user };
    let mut can_confirm = false;
    if let Some(address) = &address {
        can_confirm = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM account_emailaddress
            WHERE LOWER(email)=LOWER($1) AND verified AND user_id<>$2 LIMIT 1",
        )
        .bind(&address.email)
        .bind(address.user)
        .fetch_optional(&state.pool)
        .await?
        .is_none();
        if can_confirm && visit.fields.is_some() {
            let mut tx = state.pool.begin().await?;
            lock_user(&mut tx, address.user).await?;
            let validation = sqlx::query(
                "UPDATE account_emailaddress SET verified=TRUE WHERE id=$1 AND NOT verified",
            )
            .bind(address.id)
            .execute(&mut *tx)
            .await;
            match validation {
                Ok(update) if update.rows_affected() == 1 => (),
                Ok(_) => return Ok(StatusCode::NOT_FOUND.into_response()),
                Err(error)
                    if error
                        .as_database_error()
                        .is_some_and(|error| error.is_unique_violation()) =>
                {
                    return Ok(StatusCode::CONFLICT.into_response());
                }
                Err(error) => return Err(error.into()),
            }
            let user_primary=sqlx::query_scalar::<_,i64>(r#"SELECT CAST(id AS BIGINT) FROM account_emailaddress WHERE user_id=$1 AND "primary" LIMIT 1"#)
                .bind(address.user).fetch_optional(&mut *tx).await?;
            if user_primary.is_none() {
                primary(&mut tx, address).await?;
            }
            tx.commit().await?;
            let target = if !visit.next.is_empty() {
                &visit.next
            } else if view.is_authenticated {
                "/"
            } else {
                "/accounts/login/"
            };
            let mut response = redirect(target);
            notice(
                state,
                &visit,
                &mut response,
                25,
                &format!("You have confirmed {}.", address.email),
            );
            if switch_user {
                login::set_cookie(
                    &mut response,
                    "sessionid",
                    "",
                    Some(0),
                    visit.secure(),
                    true,
                );
            }
            return Ok(response);
        }
    }
    let mut response = render(
        state,
        &view,
        "account/email.html",
        &visit,
        context! {
            title=>"Confirm Email Address",confirming=>true,confirm_url=>path,address,can_confirm,
        },
    )?;
    if switch_user {
        login::set_cookie(
            &mut response,
            "sessionid",
            "",
            Some(0),
            visit.secure(),
            true,
        );
    }
    Ok(response)
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if uri.path() == EMAIL && !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    let visit = match visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    if uri.path() != EMAIL {
        return confirm(state, user, visit, uri.path()).await;
    }
    let id = user.id.ok_or(ApiError::Serialization)?;
    if visit.fields.is_some()
        && limits::consume(state, "manage_email", &limits::user(id), 10, 60)
            .await
            .is_none()
    {
        return Ok(limits::denied());
    }
    let mut list = addresses(&state.pool, id).await?;
    let mut error = None;
    let mut entered = String::new();
    let mut warning = None;
    if let Some(fields) = &visit.fields {
        let input = fields.get("email").map_or("", |value| value.trim());
        let email = input.to_lowercase();
        if fields.contains_key("action_add") {
            entered = input.to_owned();
            if email.is_empty() {
                error = Some("This field is required.");
            } else if !email_valid(&email) || email.chars().count() > 254 {
                error = Some("Enter a valid email address.");
            } else if list
                .iter()
                .any(|address| address.email.eq_ignore_ascii_case(&email))
            {
                error = Some("This email address is already associated with this account.");
            } else if sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM account_emailaddress
                WHERE LOWER(email)=LOWER($1) AND verified LIMIT 1",
            )
            .bind(&email)
            .fetch_optional(&state.pool)
            .await?
            .is_some()
            {
                error = Some("This email address is already associated with another account.");
            } else {
                let username =
                    sqlx::query_scalar::<_, String>("SELECT username FROM core_user WHERE id=$1")
                        .bind(id)
                        .fetch_one(&state.pool)
                        .await?;
                let mail_id = sqlx::query_scalar::<_, i64>(
                    r#"INSERT INTO account_emailaddress (user_id,email,verified,"primary")
                    VALUES ($1,$2,FALSE,FALSE) RETURNING CAST(id AS BIGINT)"#,
                )
                .bind(id)
                .bind(&email)
                .fetch_one(&state.pool)
                .await?;
                send_confirmation(state, &visit.origin, mail_id, &email, &username, false).await?;
                return Ok(changed(
                    state,
                    &visit,
                    &format!("Confirmation email sent to {email}."),
                ));
            }
        } else if fields.contains_key("action_send") {
            if let Some(address) = list
                .iter()
                .find(|address| address.email.eq_ignore_ascii_case(&email))
            {
                send_confirmation(
                    state,
                    &visit.origin,
                    address.id,
                    &address.email,
                    &address.username,
                    false,
                )
                .await?;
                return Ok(changed(
                    state,
                    &visit,
                    &format!("Confirmation email sent to {}.", address.email),
                ));
            }
        } else if fields.contains_key("action_primary") || fields.contains_key("action_remove") {
            let mut tx = state.pool.begin().await?;
            lock_user(&mut tx, id).await?;
            list = addresses(&mut *tx, id).await?;
            if let Some(address) = list
                .iter()
                .find(|address| address.email.eq_ignore_ascii_case(&email))
            {
                if fields.contains_key("action_primary") {
                    if !address.verified && list.iter().any(|other| other.verified) {
                        warning = Some("Your primary email address must be verified.".to_owned());
                    } else {
                        primary(&mut tx, address).await?;
                        tx.commit().await?;
                        return Ok(changed(state, &visit, "Primary email address set."));
                    }
                } else if address.primary && list.len() > 1 {
                    warning = Some(format!(
                        "You cannot remove your primary email address ({}).",
                        address.email
                    ));
                } else {
                    sqlx::query("DELETE FROM account_emailconfirmation WHERE email_address_id=$1")
                        .bind(address.id)
                        .execute(&mut *tx)
                        .await?;
                    sqlx::query("DELETE FROM account_emailaddress WHERE id=$1 AND user_id=$2")
                        .bind(address.id)
                        .bind(id)
                        .execute(&mut *tx)
                        .await?;
                    sqlx::query(
                        "UPDATE core_user SET email='' WHERE id=$1 AND LOWER(email)=LOWER($2)",
                    )
                    .bind(id)
                    .bind(&address.email)
                    .execute(&mut *tx)
                    .await?;
                    tx.commit().await?;
                    return Ok(changed(
                        state,
                        &visit,
                        &format!("Removed email address {}.", address.email),
                    ));
                }
            }
            tx.commit().await?;
        }
    }
    let mut response = render(
        state,
        &user,
        "account/email.html",
        &visit,
        context! {
            title=>"Email Addresses",addresses=>list,confirming=>false,entered,error,
        },
    )?;
    if let Some(warning) = warning {
        notice(state, &visit, &mut response, 40, &warning);
    }
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Account email page failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
