mod provider;

use std::collections::HashMap;

use axum::{
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::{AnyPool, Row};

use unicode_normalization::UnicodeNormalization;

use crate::{ApiError, AppState, account, flash, login, session, web};

pub use provider::OAuth;
use provider::{Profile, Token};

pub(crate) async fn start(
    state: &AppState,
    headers: &HeaderMap,
    origin: &str,
    next: &str,
    remember: bool,
) -> Response {
    start_process(state, headers, origin, next, remember, false).await
}

async fn start_process(
    state: &AppState,
    headers: &HeaderMap,
    origin: &str,
    next: &str,
    remember: bool,
    connect: bool,
) -> Response {
    let (Some(config), Some(secret)) = (&state.oauth, state.secret.as_deref()) else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let state_id = session::random_token(
        b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        16,
    );
    let Ok(state_id) = state_id else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let pending = if connect {
        session::oauth_connect(&state.pool, headers, secret, &state_id, next).await
    } else {
        session::oauth_start(&state.pool, headers, secret, &state_id, next, remember).await
    };
    let pending = match pending {
        Ok(Some(pending)) => pending,
        Ok(None) => return StatusCode::CONFLICT.into_response(),
        Err(error) => {
            let code = error
                .as_database_error()
                .and_then(|error| error.code())
                .unwrap_or_default();
            eprintln!("OAuth state creation failed (database code {code})");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let authorize = config.authorization_url(origin, &state_id);
    let mut response = (
        StatusCode::FOUND,
        [
            (header::LOCATION, authorize.as_str()),
            (header::VARY, "Cookie"),
        ],
    )
        .into_response();
    pending.set_cookie(&mut response, origin.starts_with("https://"));
    response
}

#[derive(Debug)]
enum AccountError {
    Invalid,
    ExistingEmail,
    ExistingUsername,
    ConnectedOther,
    Database,
}

impl From<sqlx::Error> for AccountError {
    fn from(error: sqlx::Error) -> Self {
        if error
            .as_database_error()
            .is_some_and(|error| error.is_unique_violation())
        {
            return Self::ExistingEmail;
        }
        let code = error
            .as_database_error()
            .and_then(|error| error.code())
            .unwrap_or_default();
        eprintln!("OAuth account write failed (database code {code})");
        Self::Database
    }
}

pub(crate) async fn sync_video_access(pool: &AnyPool, id: i64) -> Result<(), ApiError> {
    let account = sqlx::query_scalar::<_, String>(
        "SELECT CAST(extra_data AS TEXT) FROM socialaccount_socialaccount
         WHERE provider='apdaonline' AND user_id=$1 ORDER BY id LIMIT 1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await?;
    if let Some(account) = account {
        let profile: Value = serde_json::from_str(&account)?;
        let can_view = profile["user_roles"]
            .as_array()
            .is_some_and(|roles| roles.iter().any(|role| role == "private_side_viewer"));
        sqlx::query("UPDATE core_user SET can_view_private_videos=$1 WHERE id=$2 AND can_view_private_videos<>$1")
            .bind(can_view)
            .bind(id)
            .execute(pool)
            .await?;
    }
    Ok(())
}

fn signup_username_base(first: &str, last: &str, email: &str) -> String {
    [first, last, email]
        .into_iter()
        .find_map(|source| {
            let ascii = source.nfkd().filter(char::is_ascii).collect::<String>();
            let filtered = ascii
                .split('@')
                .next()
                .unwrap_or_default()
                .chars()
                .filter(|ch| {
                    ch.is_ascii_alphanumeric() || ch.is_ascii_whitespace() || "_+.-".contains(*ch)
                })
                .collect::<String>()
                .to_ascii_lowercase();
            let username = filtered.split_whitespace().collect::<Vec<_>>().join("_");
            (!username.is_empty()).then(|| username.chars().take(130).collect())
        })
        .unwrap_or_else(|| "user".to_owned())
}

async fn save(
    pool: &AnyPool,
    profile: &Profile,
    fields: &Value,
    token: &Token,
    connecting: Option<i64>,
    chosen_username: Option<&str>,
) -> Result<(i64, String), AccountError> {
    let uid = profile.uid().ok_or(AccountError::Invalid)?;
    let sqlite = pool.connect_options().database_url.scheme() == "sqlite";
    let mut tx = pool.begin().await?;
    let existing = sqlx::query(
        "
        SELECT CAST(sa.id AS BIGINT) AS account, CAST(u.id AS BIGINT) AS id, u.password,
               CAST(CASE WHEN u.is_active THEN 1 ELSE 0 END AS BIGINT) AS active
        FROM socialaccount_socialaccount sa JOIN core_user u ON u.id = sa.user_id
        WHERE sa.provider = 'apdaonline' AND sa.uid = $1
    ",
    )
    .bind(&uid)
    .fetch_optional(&mut *tx)
    .await?;
    let (id, password, account) = if let Some(row) = existing {
        if row.try_get::<i64, _>("active")? == 0 {
            return Err(AccountError::Invalid);
        }
        let account = row.try_get::<i64, _>("account")?;
        let id = row.try_get::<i64, _>("id")?;
        if connecting.is_some_and(|user| user != id) {
            return Err(AccountError::ConnectedOther);
        }

        let password = row.try_get::<String, _>("password")?;
        let update = if sqlite {
            "UPDATE socialaccount_socialaccount SET extra_data = $1, last_login = CURRENT_TIMESTAMP WHERE id = $2"
        } else {
            "UPDATE socialaccount_socialaccount SET extra_data = CAST($1 AS JSONB), last_login = CURRENT_TIMESTAMP WHERE id = $2"
        };
        sqlx::query(update)
            .bind(fields.to_string())
            .bind(account)
            .execute(&mut *tx)
            .await?;
        (id, password, account)
    } else if let Some(id) = connecting {
        let password = sqlx::query_scalar::<_, String>(
            "SELECT password FROM core_user WHERE id=$1 AND is_active",
        )
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(AccountError::Invalid)?;
        let statement = if sqlite {
            "INSERT INTO socialaccount_socialaccount (provider,uid,last_login,date_joined,user_id,extra_data)
             VALUES ('apdaonline',$1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,$2,$3) RETURNING CAST(id AS BIGINT)"
        } else {
            "INSERT INTO socialaccount_socialaccount (provider,uid,last_login,date_joined,user_id,extra_data)
             VALUES ('apdaonline',$1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,$2,CAST($3 AS JSONB)) RETURNING CAST(id AS BIGINT)"
        };
        let account = sqlx::query_scalar::<_, i64>(statement)
            .bind(&uid)
            .bind(id)
            .bind(fields.to_string())
            .fetch_one(&mut *tx)
            .await?;
        (id, password, account)
    } else {
        let email = profile
            .user_email
            .as_deref()
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        if email.chars().count() > 254 {
            return Err(AccountError::Invalid);
        }
        if !email.is_empty() {
            let existing_email = sqlx::query_scalar::<_, i64>("
                SELECT CAST(CASE WHEN EXISTS (SELECT 1 FROM account_emailaddress WHERE LOWER(email) = $1)
                    OR EXISTS (SELECT 1 FROM core_user WHERE LOWER(email) = $1) THEN 1 ELSE 0 END AS BIGINT)
            ").bind(&email).fetch_one(&mut *tx).await?;
            if existing_email != 0 {
                return Err(AccountError::ExistingEmail);
            }
        }
        let base: String = profile
            .user_nicename
            .chars()
            .filter(|ch| ch.is_alphanumeric() || "_@.+-".contains(*ch))
            .take(130)
            .collect();
        let (first, last) = profile
            .display_name
            .split_once(' ')
            .unwrap_or((&profile.display_name, ""));
        let first = first.chars().take(150).collect::<String>();
        let last = last.chars().take(150).collect::<String>();
        let password = format!(
            "!{}",
            session::random_token(
                b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                40
            )
            .map_err(|_| AccountError::Invalid)?
        );
        let fallback = signup_username_base(&first, &last, &email);
        let initial = chosen_username.or_else(|| (!base.is_empty()).then_some(base.as_str()));
        let candidates = initial.map(str::to_owned).into_iter().chain(
            std::iter::once(fallback.clone())
                .chain((1..100).map(|suffix| format!("{fallback}{suffix}"))),
        );
        let mut id = None;
        for username in candidates.take(if chosen_username.is_some() { 1 } else { 101 }) {
            id = sqlx::query_scalar::<_, i64>("
                INSERT INTO core_user (username, password, first_name, last_name, email, is_superuser, is_staff,
                    is_active, date_joined, can_view_private_videos)
                SELECT $1, $2, $3, $4, $5, FALSE, FALSE, TRUE, CURRENT_TIMESTAMP, $6
                WHERE NOT EXISTS (SELECT 1 FROM core_user WHERE LOWER(username)=LOWER($1))
                ON CONFLICT(username) DO NOTHING RETURNING CAST(id AS BIGINT)
            ").bind(username).bind(&password).bind(&first).bind(&last).bind(&email)
                .bind(profile.can_view_video()).fetch_optional(&mut *tx).await?;
            if id.is_some() {
                break;
            }
        }
        let id = id.ok_or(if chosen_username.is_some() {
            AccountError::ExistingUsername
        } else {
            AccountError::Invalid
        })?;
        if !email.is_empty() {
            sqlx::query("INSERT INTO account_emailaddress (user_id, email, verified, \"primary\") VALUES ($1, $2, FALSE, TRUE)")
                .bind(id).bind(&email).execute(&mut *tx).await?;
        }
        let insert = if sqlite {
            "INSERT INTO socialaccount_socialaccount (provider, uid, last_login, date_joined, user_id, extra_data)
             VALUES ('apdaonline', $1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $2, $3) RETURNING CAST(id AS BIGINT)"
        } else {
            "INSERT INTO socialaccount_socialaccount (provider, uid, last_login, date_joined, user_id, extra_data)
             VALUES ('apdaonline', $1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $2, CAST($3 AS JSONB)) RETURNING CAST(id AS BIGINT)"
        };
        let account = sqlx::query_scalar::<_, i64>(insert)
            .bind(&uid)
            .bind(id)
            .bind(fields.to_string())
            .fetch_one(&mut *tx)
            .await?;
        (id, password, account)
    };

    sqlx::query("UPDATE core_user SET can_view_private_videos = $1 WHERE id = $2")
        .bind(profile.can_view_video())
        .bind(id)
        .execute(&mut *tx)
        .await?;
    let expiry = token
        .expires_in
        .and_then(chrono::Duration::try_seconds)
        .and_then(|duration| chrono::Utc::now().checked_add_signed(duration))
        .map(|expiry| {
            let expiry = expiry.format("%Y-%m-%d %H:%M:%S%.6f").to_string();
            if sqlite {
                expiry
            } else {
                format!("{expiry}+00")
            }
        });
    let expiry_sql = if sqlite {
        "$3"
    } else {
        "CAST($3 AS TIMESTAMPTZ)"
    };
    let id_of_token = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM socialaccount_socialtoken WHERE account_id = $1 AND app_id IS NULL ORDER BY id LIMIT 1"
    ).bind(account).fetch_optional(&mut *tx).await?;
    let query = if id_of_token.is_some() {
        format!("UPDATE socialaccount_socialtoken SET token = $1, token_secret = CASE WHEN $2 = '' THEN token_secret ELSE $2 END,
                 expires_at = {expiry_sql} WHERE id = $4")
    } else {
        format!("INSERT INTO socialaccount_socialtoken (token, token_secret, expires_at, account_id, app_id)
                 VALUES ($1, $2, {expiry_sql}, $4, NULL)")
    };
    sqlx::query(sqlx::AssertSqlSafe(query))
        .bind(&token.access_token)
        .bind(&token.refresh_token)
        .bind(expiry)
        .bind(id_of_token.unwrap_or(account))
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok((id, password))
}

fn redirect(target: &str) -> Response {
    (StatusCode::FOUND, [(header::LOCATION, target)]).into_response()
}

pub(crate) async fn legacy(OriginalUri(uri): OriginalUri) -> Response {
    let target = uri
        .path()
        .replace("/accounts/social/", "/accounts/3rdparty/");
    (StatusCode::MOVED_PERMANENTLY, [(header::LOCATION, target)]).into_response()
}

const PENDING_SIGNUP: &str = "socialaccount_sociallogin";
const SIGNUP_PAGE: &str = "/accounts/3rdparty/signup/";
const BACKEND: &str = "allauth.account.auth_backends.AuthenticationBackend";

async fn pending_signup(
    state: &AppState,
    headers: &HeaderMap,
    provider: &OAuth,
    profile: &Profile,
    fields: &Value,
    token: &Token,
    context: &session::OAuthContext,
) -> Result<Response, ApiError> {
    let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
    let Some(mut session) = session::StoredSession::read(&state.pool, headers, secret).await?
    else {
        return Ok(redirect("/accounts/login/"));
    };
    let expires = token
        .expires_in
        .and_then(chrono::Duration::try_seconds)
        .and_then(|duration| chrono::Utc::now().checked_add_signed(duration))
        .map(|time| time.to_rfc3339());
    let uid = profile.uid().ok_or(ApiError::Serialization)?;
    let (first_name, last_name) = profile
        .display_name
        .split_once(' ')
        .unwrap_or((&profile.display_name, ""));
    let email = profile.user_email.as_deref().unwrap_or_default();
    let addresses = if email.is_empty() {
        Vec::new()
    } else {
        vec![json!({"email":email,"primary":true,"verified":false})]
    };
    let value = json!({
        "provider":{"id":"apdaonline","app.client_id":provider.client_id()},
        "account":{"provider":"apdaonline","uid":uid,"extra_data":fields},
        "user":{"username":profile.user_nicename,"email":email,
            "first_name":first_name,"last_name":last_name,"can_view_private_videos":profile.can_view_video()},
        "state":{"next":context.next,"process":"login","remember":context.remember},
        "email_addresses":addresses,"phone":null,"phone_verified":false,
        "token":{"token":token.access_token,"token_secret":token.refresh_token,"expires_at":expires},
    });
    session.values.insert(
        PENDING_SIGNUP.to_owned(),
        serde_json::value::to_raw_value(&value)?,
    );
    let Some(session) = session.refresh(&state.pool, secret).await? else {
        return Ok(StatusCode::CONFLICT.into_response());
    };
    let mut response = redirect(SIGNUP_PAGE);
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    session.set_cookie(&mut response, secure);
    Ok(response)
}

async fn signup_page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let Some(secret) = state.secret.as_deref() else {
        return Ok(redirect("/accounts/login/"));
    };
    let Some(stored) = session::StoredSession::read(&state.pool, request.headers(), secret).await?
    else {
        return Ok(redirect("/accounts/login/"));
    };
    let Some(pending) = stored
        .values
        .get(PENDING_SIGNUP)
        .and_then(|value| serde_json::from_str::<Value>(value.get()).ok())
    else {
        return Ok(redirect("/accounts/login/"));
    };
    let fields = &pending["account"]["extra_data"];
    let Ok(mut profile) = serde_json::from_value::<Profile>(fields.clone()) else {
        return Ok(redirect("/accounts/login/"));
    };
    let user = session::user(&state.pool, request.headers(), Some(secret)).await?;
    if user.is_authenticated {
        return Ok(redirect("/accounts/3rdparty/"));
    }
    let visit = match account::visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    let mut username = pending["user"]["username"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let mut email = pending["user"]["email"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let mut username_error = None;
    let mut email_error = None;
    if let Some(form) = &visit.fields {
        username = account::normal_username(form.get("username").map_or("", String::as_str));
        email = form
            .get("email")
            .map_or("", String::as_str)
            .trim()
            .to_lowercase();
        if username.is_empty() {
            username_error = Some("This field is required.");
        } else if username.chars().count() > 150
            || username
                .chars()
                .any(|ch| !ch.is_alphanumeric() && !"_@.+-".contains(ch))
        {
            username_error = Some(
                "Enter a valid username. This value may contain only letters, numbers, and @/./+/-/_ characters.",
            );
        } else if sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_user WHERE LOWER(username)=LOWER($1) LIMIT 1",
        )
        .bind(&username)
        .fetch_optional(&state.pool)
        .await?
        .is_some()
        {
            username_error = Some("A user with that username already exists.");
        }
        if !email.is_empty() && (!account::email_valid(&email) || email.chars().count() > 254) {
            email_error = Some("Enter a valid email address.");
        }
        if username_error.is_none() && email_error.is_none() {
            profile.user_email = Some(email.clone());
            let token = Token {
                access_token: pending["token"]["token"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                refresh_token: pending["token"]["token_secret"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                expires_in: pending["token"]["expires_at"]
                    .as_str()
                    .and_then(|time| chrono::DateTime::parse_from_rfc3339(time).ok())
                    .map(|time| time.signed_duration_since(chrono::Utc::now()).num_seconds()),
            };
            match save(&state.pool, &profile, fields, &token, None, Some(&username)).await {
                Ok((id, password)) => {
                    let next = login::safe_next(
                        pending["state"]["next"].as_str().unwrap_or_default(),
                        &visit.origin,
                    );
                    let remember = pending["state"]["remember"].as_bool().unwrap_or(true);
                    let mut response = login::sign_in(
                        state,
                        &visit.headers,
                        visit.secure(),
                        &next,
                        remember,
                        (id, &password),
                        BACKEND,
                    )
                    .await;
                    flash::push(
                        &mut response,
                        &visit.headers,
                        secret,
                        25,
                        &format!("Successfully signed in as {username}."),
                        visit.secure(),
                    );
                    return Ok(response);
                }
                Err(AccountError::ExistingUsername) => {
                    username_error = Some("A user with that username already exists.")
                }
                Err(AccountError::ExistingEmail) => {
                    email_error = Some(
                        "An account already exists with this email address. Please sign in to that account first, then connect your APDAOnline account.",
                    )
                }
                Err(_) => return Err(ApiError::Serialization),
            }
        }
    }
    let site_name = sqlx::query_scalar::<_, String>("SELECT name FROM django_site WHERE id=1")
        .fetch_optional(&state.pool)
        .await?
        .unwrap_or_default();
    account::render(
        state,
        &user,
        "socialaccount/signup.html",
        &visit,
        minijinja::context! {
            title=>"Signup",site_name,username,email,username_error,email_error
        },
    )
}

pub(crate) async fn social_signup(State(state): State<AppState>, request: Request) -> Response {
    signup_page(&state, request)
        .await
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

async fn failed(state: &AppState, uri: &axum::http::Uri, headers: &HeaderMap) -> Response {
    let mut request = Request::new(axum::body::Body::empty());
    *request.uri_mut() = uri.clone();
    *request.headers_mut() = headers.clone();
    crate::account::entrance(State(state.clone()), request).await
}

pub(crate) async fn provider(State(state): State<AppState>, request: Request) -> Response {
    let result = async {
        let uri = request.uri().clone();
        let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
        let query = login::form_values(uri.query().unwrap_or_default().as_bytes());
        let connect = query.get("process").is_some_and(|value| value == "connect");
        if connect && !user.is_authenticated {
            return Ok(login::redirect_to_login(&uri));
        }
        let Some(origin) = state.origin(request.headers()) else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        let next = query
            .get("next")
            .map(|value| login::safe_next(value, &origin))
            .unwrap_or_default();
        let secure = origin.starts_with("https://");
        let (parts, body) = request.into_parts();
        if parts.method == axum::http::Method::POST {
            let Ok(body) = axum::body::to_bytes(body, 128 * 1024).await else {
                return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
            };
            let form = login::form_values(&body);
            if !login::csrf_valid(
                &parts.headers,
                form.get("csrfmiddlewaretoken").map(String::as_str),
                &origin,
            ) {
                return Ok(login::csrf_failure());
            }
            return Ok(start_process(&state, &parts.headers, &origin, &next, true, connect).await);
        }
        let (csrf, token) =
            login::csrf_tokens(&parts.headers).map_err(|_| ApiError::Serialization)?;
        let base = web::base_context(&state, &user, &uri);
        let html = web::render(
            "socialaccount/login.html",
            minijinja::context! {csrf=>token,..base},
        )
        .map_err(|error| {
            eprintln!("OAuth confirmation template failed: {error}");
            ApiError::Serialization
        })?;
        let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
        login::set_csrf_cookie(&mut response, &csrf, secure);
        Ok::<_, ApiError>(response)
    }
    .await;
    result.unwrap_or_else(|error| {
        eprintln!("OAuth confirmation failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}

pub(crate) async fn callback(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(origin) = state.origin(&headers) else {
        return failed(&state, &uri, &headers).await;
    };
    let (Some(config), Some(secret)) = (&state.oauth, state.secret.as_deref()) else {
        return failed(&state, &uri, &headers).await;
    };
    let params: HashMap<_, _> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let Some(state_id) = params.get("state") else {
        return failed(&state, &uri, &headers).await;
    };
    let response = session::consume_oauth(&state.pool, &headers, secret, state_id).await;
    let (context, refreshed) = match response {
        Ok(Some(context)) => context,
        Ok(None) => return failed(&state, &uri, &headers).await,
        Err(_) => {
            eprintln!("OAuth session state could not be retrieved");
            return failed(&state, &uri, &headers).await;
        }
    };
    let mut response = async {
        if params
            .get("error")
            .is_some_and(|error| error.as_ref() == "access_denied")
        {
            return redirect("/accounts/3rdparty/login/cancelled/");
        }
        if params.contains_key("error") {
            return failed(&state, &uri, &headers).await;
        }
        let Some(code) = params.get("code").filter(|code| !code.is_empty()) else {
            return failed(&state, &uri, &headers).await;
        };
        let Some((token, fields, profile)) = config.profile(&origin, code).await else {
            eprintln!("OAuth provider rejected the exchange or sent an invalid response");
            return failed(&state, &uri, &headers).await;
        };
        let connecting = if context.connect {
            match session::user(&state.pool, &headers, Some(secret)).await {
                Ok(user) if user.is_authenticated => user.id,
                _ => return failed(&state, &uri, &headers).await,
            }
        } else {
            None
        };
        let (id, password) =
            match save(&state.pool, &profile, &fields, &token, connecting, None).await {
                Ok(credentials) => credentials,
                Err(AccountError::ExistingEmail) => {
                    return match pending_signup(
                        &state, &headers, config, &profile, &fields, &token, &context,
                    )
                    .await
                    {
                        Ok(response) => response,
                        Err(_) => failed(&state, &uri, &headers).await,
                    };
                }
                Err(AccountError::ExistingUsername) => return failed(&state, &uri, &headers).await,
                Err(AccountError::ConnectedOther) => {
                    let mut response = (
                        StatusCode::FOUND,
                        [(header::LOCATION, "/accounts/3rdparty/")],
                    )
                        .into_response();
                    flash::push(
                        &mut response,
                        &headers,
                        secret,
                        40,
                        "The third-party account is already connected to a different account.",
                        origin.starts_with("https://"),
                    );
                    return response;
                }
                Err(AccountError::Invalid) => return failed(&state, &uri, &headers).await,
                Err(AccountError::Database) => return failed(&state, &uri, &headers).await,
            };
        let next = login::safe_next(&context.next, &origin);
        if connecting.is_some() {
            let mut response = (
                StatusCode::FOUND,
                [(
                    header::LOCATION,
                    if next.is_empty() {
                        "/accounts/3rdparty/"
                    } else {
                        &next
                    },
                )],
            )
                .into_response();
            flash::push(
                &mut response,
                &headers,
                secret,
                20,
                "The third-party account has been connected.",
                origin.starts_with("https://"),
            );
            return response;
        }
        login::sign_in(
            &state,
            &headers,
            origin.starts_with("https://"),
            &next,
            context.remember,
            (id, &password),
            BACKEND,
        )
        .await
    }
    .await;
    if !response.status().is_server_error()
        && !response
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .any(|value| {
                value
                    .to_str()
                    .is_ok_and(|value| value.starts_with("sessionid="))
            })
    {
        refreshed.set_cookie(&mut response, origin.starts_with("https://"));
    }
    response
}
