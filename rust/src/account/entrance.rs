use axum::{
    extract::{Request, State},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use minijinja::context;
use sqlx::Row;
use unicode_normalization::UnicodeNormalization;

use super::{
    Errors, Fields, Visit, limits, new_password, redirect, render, validate_password, visit,
};
use crate::{ApiError, AppState, flash, login, password, session};

const BACKEND: &str = "allauth.account.auth_backends.AuthenticationBackend";
const LOGIN: &str = "/accounts/login/";
const LOGOUT: &str = "/accounts/logout/";
const SIGNUP: &str = "/accounts/signup/";

pub(super) fn query_next(value: &str) -> String {
    url::form_urlencoded::Serializer::new(String::new())
        .append_pair("next", value)
        .finish()
}

pub(crate) fn email_valid(email: &str) -> bool {
    email.chars().count() <= 320 && email.parse::<lettre::Address>().is_ok()
}

pub(crate) fn normal_username(value: &str) -> String {
    value.trim().nfkc().collect()
}

fn error(errors: &mut Errors, key: &'static str, message: &str) {
    errors.entry(key).or_default().push(message.to_owned());
}

pub(super) fn passwords(fields: &Fields, attributes: [&str; 4]) -> Errors {
    let mut errors = validate_password(fields, attributes);
    if let Some(messages) = errors.get_mut("password2") {
        for message in messages {
            if message == "The two password fields didn’t match." {
                *message = "You must type the same password each time.".to_owned();
            }
        }
    }
    errors
}

async fn sign_up(
    state: &AppState,
    visit: &Visit,
    username: &str,
    email: &str,
    fields: &Fields,
    errors: &mut Errors,
) -> Result<Option<Response>, ApiError> {
    if username.is_empty() {
        error(errors, "username", "This field is required.");
    } else if username.chars().count() > 150
        || username
            .chars()
            .any(|ch| !ch.is_alphanumeric() && !"_@.+-".contains(ch))
    {
        error(
            errors,
            "username",
            "Enter a valid username. This value may contain only letters, numbers, and @/./+/-/_ characters.",
        );
    } else if sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_user WHERE LOWER(username)=LOWER($1) LIMIT 1",
    )
    .bind(username)
    .fetch_optional(&state.pool)
    .await?
    .is_some()
    {
        error(
            errors,
            "username",
            "A user with that username already exists.",
        );
    }
    if !email.is_empty() {
        if !email_valid(email) || email.chars().count() > 254 {
            error(errors, "email", "Enter a valid email address.");
        } else if sqlx::query_scalar::<_, i64>(
            "SELECT CAST(CASE WHEN
            EXISTS (SELECT 1 FROM account_emailaddress WHERE LOWER(email)=LOWER($1))
            OR EXISTS (SELECT 1 FROM core_user WHERE LOWER(email)=LOWER($1))
            THEN 1 ELSE 0 END AS BIGINT)",
        )
        .bind(email)
        .fetch_one(&state.pool)
        .await?
            != 0
        {
            error(
                errors,
                "email",
                "A user is already registered with this email address.",
            );
        }
    }
    errors.extend(passwords(fields, [username, "", "", email]));
    if !errors.is_empty() {
        return Ok(None);
    }
    let hash = new_password(&fields["password1"]).await?;
    let mut tx = state.pool.begin().await?;
    let id = sqlx::query_scalar::<_, i64>(
        "INSERT INTO core_user (username,password,first_name,last_name,email,is_superuser,
        is_staff,is_active,date_joined,can_view_private_videos)
        VALUES ($1,$2,'','',$3,FALSE,FALSE,TRUE,CURRENT_TIMESTAMP,FALSE)
        ON CONFLICT(username) DO NOTHING RETURNING CAST(id AS BIGINT)",
    )
    .bind(username)
    .bind(&hash)
    .bind(email)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(id) = id else {
        error(
            errors,
            "username",
            "A user with that username already exists.",
        );
        return Ok(None);
    };
    let address = if email.is_empty() {
        None
    } else {
        Some(
            sqlx::query_scalar::<_, i64>(
                r#"INSERT INTO account_emailaddress (user_id,email,verified,"primary")
            VALUES ($1,$2,FALSE,TRUE) RETURNING CAST(id AS BIGINT)"#,
            )
            .bind(id)
            .bind(email)
            .fetch_one(&mut *tx)
            .await?,
        )
    };
    tx.commit().await?;
    let mut response = login::sign_in(
        state,
        &visit.headers,
        visit.secure(),
        &visit.next,
        true,
        (id, &hash),
        BACKEND,
    )
    .await;
    if let Some(email_id) = address
        && std::env::var("ENV").as_deref() == Ok("production")
    {
        super::emails::send_confirmation(state, &visit.origin, email_id, email, username, true)
            .await?;
    }
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            &mut response,
            &visit.headers,
            secret,
            25,
            &format!("Successfully signed in as {username}."),
            visit.secure(),
        );
    }
    Ok(Some(response))
}

async fn sign_in(
    state: &AppState,
    visit: &Visit,
    ip: &str,
    username: &str,
    fields: &Fields,
    remember: bool,
    errors: &mut Errors,
) -> Result<Option<Response>, ApiError> {
    if username.is_empty() {
        error(errors, "login", "This field is required.");
    }
    let password = fields.get("password").map_or("", String::as_str);
    if password.is_empty() {
        error(errors, "password", "This field is required.");
    }
    if !errors.is_empty() {
        return Ok(None);
    }
    let site = sqlx::query_scalar::<_, String>("SELECT domain FROM django_site WHERE id=1")
        .fetch_optional(&state.pool)
        .await?
        .unwrap_or_default();
    let key = limits::digest(&format!("{site}:{}", username.to_lowercase()));
    let retries_ip = limits::consume(state, "login_failed", ip, 10, 60).await;
    let retries_address = if retries_ip.is_some() {
        limits::consume(state, "login_failed", &key, 5, 300).await
    } else {
        None
    };
    let (Some(retries_ip), Some(retries_address)) = (retries_ip, retries_address) else {
        error(
            errors,
            "__all__",
            "Too many failed login attempts. Try again later.",
        );
        return Ok(None);
    };
    let row=sqlx::query("SELECT CAST(id AS BIGINT) as id,username,password,
        CASE WHEN is_active THEN 1 ELSE 0 END AS active FROM core_user
        WHERE LOWER(username)=LOWER($1) ORDER BY CASE WHEN username=$1 THEN 0 ELSE 1 END,id LIMIT 1")
        .bind(username).fetch_optional(&state.pool).await?;
    let stored = row
        .as_ref()
        .map(|row| row.try_get::<String, _>("password"))
        .transpose()?;
    let compare = stored.clone();
    let candidate = password.as_bytes().to_owned();
    let verified = tokio::task::spawn_blocking(move || {
        password::verify_and_upgrade(&candidate, compare.as_deref())
    })
    .await
    .map_err(|_| ApiError::Serialization)?;
    let (Some(row), Some(mut stored), Some(upgrade)) = (row, stored, verified) else {
        error(
            errors,
            "__all__",
            "The username and/or password you specified are not correct.",
        );
        return Ok(None);
    };
    retries_ip.rollback(state).await;
    retries_address.rollback(state).await;
    let active = row.try_get::<i32, _>("active")? == 1;
    if !active {
        return Ok(Some(redirect("/accounts/inactive/")));
    }
    let id = row.try_get::<i64, _>("id")?;
    if let Some(hash) = upgrade {
        let changed = sqlx::query("UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3")
            .bind(&hash)
            .bind(id)
            .bind(&stored)
            .execute(&state.pool)
            .await?;
        if changed.rows_affected() != 1 {
            return Ok(Some(StatusCode::CONFLICT.into_response()));
        }
        stored = hash;
    }
    crate::oauth::sync_video_access(&state.pool, id).await?;
    let mut response = login::sign_in(
        state,
        &visit.headers,
        visit.secure(),
        &visit.next,
        remember,
        (id, &stored),
        BACKEND,
    )
    .await;
    if let Some(secret) = state.secret.as_deref() {
        let name: &str = row.try_get("username")?;
        flash::push(
            &mut response,
            &visit.headers,
            secret,
            25,
            &format!("Successfully signed in as {name}."),
            visit.secure(),
        );
    }
    Ok(Some(response))
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let ip = limits::ip(state, &request);
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let context = match visit(state, request).await {
        Ok(context) => context,
        Err(response) => return Ok(*response),
    };
    let (kind, title) = match uri.path() {
        LOGIN => ("login", "Sign In"),
        SIGNUP => ("signup", "Sign Up"),
        LOGOUT => ("logout", "Sign Out"),
        "/accounts/inactive/" => ("inactive", "Account Inactive"),
        "/accounts/confirm-email/" => ("verification", "Verify Your Email Address"),
        "/accounts/3rdparty/login/cancelled/" => ("social-cancelled", "Login Cancelled"),
        _ => ("social-error", "Third-Party Login Failure"),
    };
    let destination = if context.next.is_empty() {
        "/"
    } else {
        &context.next
    };
    if matches!(kind, "login" | "signup") && user.is_authenticated {
        return Ok(redirect(destination));
    }
    if kind == "logout" {
        if !user.is_authenticated {
            return Ok(redirect(destination));
        }
        if context.fields.is_some() {
            session::delete(&state.pool, &context.headers).await?;
            let mut response = redirect(destination);
            login::set_cookie(
                &mut response,
                "sessionid",
                "",
                Some(0),
                context.secure(),
                true,
            );
            if let Some(secret) = state.secret.as_deref() {
                flash::push(
                    &mut response,
                    &context.headers,
                    secret,
                    25,
                    "You have signed out.",
                    context.secure(),
                );
            }
            return Ok(response);
        }
    }
    let redirect_query = if context.next.is_empty() {
        String::new()
    } else {
        query_next(&context.next)
    };
    let next_query = if redirect_query.is_empty() {
        String::new()
    } else {
        format!("?{redirect_query}")
    };
    let provider_query = if redirect_query.is_empty() {
        String::new()
    } else {
        format!("&{redirect_query}")
    };
    let mut errors = Errors::new();
    let mut username = String::new();
    let mut email = String::new();
    let mut remember = false;
    if let Some(fields) = &context.fields {
        let maximum = match kind {
            "login" => Some(30),
            "signup" => Some(20),
            _ => None,
        };
        if let Some(maximum) = maximum
            && limits::consume(state, kind, &ip, maximum, 60)
                .await
                .is_none()
        {
            return Ok(limits::denied());
        }
        let name = if kind == "signup" {
            "username"
        } else {
            "login"
        };
        username = normal_username(fields.get(name).map_or("", String::as_str));
        email = fields
            .get("email")
            .map_or("", String::as_str)
            .trim()
            .to_lowercase();
        remember = fields
            .get("remember")
            .is_some_and(|value| !matches!(value.as_str(), "" | "0" | "false" | "False"));
        let response = match kind {
            "signup" => sign_up(state, &context, &username, &email, fields, &mut errors).await?,
            "login" => {
                sign_in(
                    state,
                    &context,
                    &ip,
                    &username,
                    fields,
                    remember,
                    &mut errors,
                )
                .await?
            }
            _ => None,
        };
        if let Some(response) = response {
            return Ok(response);
        }
    } else if kind == "signup" {
        email = login::form_values(uri.query().unwrap_or_default().as_bytes())
            .remove("email")
            .filter(|value| email_valid(value))
            .unwrap_or_default();
    }
    let mut response = render(
        state,
        &user,
        "account/entrance.html",
        &context,
        context! {
            title,head_title=>if kind=="signup"{"Signup"}else{title},kind,username,email,remember,errors,next_query,provider_query,
        },
    )?;
    if kind == "social-error" {
        *response.status_mut() = StatusCode::UNAUTHORIZED;
    }
    if matches!(kind, "social-error" | "social-cancelled") {
        return Ok(response);
    }
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "max-age=0, no-cache, no-store, must-revalidate, private"
            .parse()
            .expect("account cache"),
    );
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Account entry page failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
