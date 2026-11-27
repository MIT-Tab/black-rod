use std::{collections::HashMap, time::Duration};

use axum::{
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;
use sqlx::{AnyPool, Row};
use url::Url;

use crate::{
    ApiError, AppState, flash,
    login::{self, SESSION_AGE},
    session, web,
};

const CALLBACK: &str = "/accounts/apdaonline/login/callback/";
const DEFAULT_BASE: &str = "https://apda.online/";

pub struct OAuth {
    base: Url,
    client_id: String,
    secret: String,
    client: Client,
}

impl OAuth {
    pub fn new(
        client_id: String,
        secret: String,
        base: Option<String>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let base = Url::parse(base.as_deref().unwrap_or(DEFAULT_BASE))?;
        let loopback = matches!(base.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"));
        if !(base.scheme() == "https" || (base.scheme() == "http" && loopback))
            || base.host().is_none()
        {
            return Err("Invalid APDAOnline OAuth base URL".into());
        }
        Ok(Self {
            base,
            client_id,
            secret,
            client: Client::builder()
                .timeout(Duration::from_secs(30))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
        })
    }

    fn endpoint(&self, name: &str) -> Url {
        self.base.join(name).expect("absolute OAuth base URL")
    }
}

pub(crate) async fn start(state: &AppState, origin: &str, next: &str, remember: bool) -> Response {
    start_process(state, origin, next, remember, None).await
}

async fn start_process(
    state: &AppState,
    origin: &str,
    next: &str,
    remember: bool,
    connecting: Option<&HeaderMap>,
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
    let pending = if let Some(headers) = connecting {
        session::oauth_connect(&state.pool, headers, secret, &state_id, next).await
    } else {
        session::oauth_start(&state.pool, secret, &state_id, next, remember)
            .await
            .map(Some)
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
    let mut authorize = config.endpoint("oauth/authorize");
    authorize
        .query_pairs_mut()
        .append_pair("client_id", &config.client_id)
        .append_pair("redirect_uri", &format!("{origin}{CALLBACK}"))
        .append_pair("scope", "")
        .append_pair("response_type", "code")
        .append_pair("state", &state_id);
    let mut response = (
        StatusCode::FOUND,
        [
            (header::LOCATION, authorize.as_str()),
            (header::VARY, "Cookie"),
        ],
    )
        .into_response();
    if connecting.is_none() {
        login::set_cookie(
            &mut response,
            "sessionid",
            &pending,
            Some(SESSION_AGE),
            origin.starts_with("https://"),
            true,
        );
    }
    response
}

#[derive(Deserialize)]
struct Token {
    access_token: String,
    #[serde(default)]
    refresh_token: String,
    #[serde(default)]
    expires_in: Option<i64>,
}

#[derive(Deserialize)]
struct Profile {
    #[serde(rename = "ID")]
    id: Value,
    user_nicename: String,
    user_email: String,
    display_name: String,
    user_roles: Vec<String>,
}

impl Profile {
    fn uid(&self) -> Option<String> {
        match &self.id {
            Value::String(id) if id.bytes().all(|byte| byte.is_ascii_digit()) && !id.is_empty() => {
                Some(id.clone())
            }
            Value::Number(id) if id.as_u64().is_some() => Some(id.to_string()),
            _ => None,
        }
    }

    fn can_view_video(&self) -> bool {
        self.user_roles
            .iter()
            .any(|role| role == "private_side_viewer")
    }
}

async fn profile(config: &OAuth, origin: &str, code: &str) -> Option<(Token, Value, Profile)> {
    let callback = format!("{origin}{CALLBACK}");
    let token = config
        .client
        .post(config.endpoint("oauth/token"))
        .form(&[
            ("client_id", config.client_id.as_str()),
            ("client_secret", config.secret.as_str()),
            ("grant_type", "authorization_code"),
            ("redirect_uri", callback.as_str()),
            ("code", code),
        ])
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?
        .json::<Token>()
        .await
        .ok()?;
    let fields = config
        .client
        .get(config.endpoint("oauth/me"))
        .bearer_auth(&token.access_token)
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?
        .json::<Value>()
        .await
        .ok()?;
    let profile = serde_json::from_value(fields.clone()).ok()?;
    Some((token, fields, profile))
}

#[derive(Debug)]
enum AccountError {
    Invalid,
    ExistingEmail,
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

async fn save(
    pool: &AnyPool,
    profile: &Profile,
    fields: &Value,
    token: &Token,
    connecting: Option<i64>,
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
        let email = profile.user_email.trim().to_lowercase();
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
        let base = if base.is_empty() { "user" } else { &base };
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
        let mut id = None;
        for suffix in 0..100 {
            let username = if suffix == 0 {
                base.to_owned()
            } else {
                format!("{base}{suffix}")
            };
            id = sqlx::query_scalar::<_, i64>("
                INSERT INTO core_user (username, password, first_name, last_name, email, is_superuser, is_staff,
                    is_active, date_joined, can_view_private_videos)
                VALUES ($1, $2, $3, $4, $5, FALSE, FALSE, TRUE, CURRENT_TIMESTAMP, $6)
                ON CONFLICT(username) DO NOTHING RETURNING CAST(id AS BIGINT)
            ").bind(username).bind(&password).bind(&first).bind(&last).bind(&email)
                .bind(profile.can_view_video()).fetch_optional(&mut *tx).await?;
            if id.is_some() {
                break;
            }
        }
        let id = id.ok_or(AccountError::Invalid)?;
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

fn failed(status: StatusCode) -> Response {
    (status, Html("<h1>Social Network Login Failure</h1><p>An error occurred while attempting to login via your social network account.</p>")).into_response()
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
            return Ok(start_process(
                &state,
                &origin,
                &next,
                true,
                connect.then_some(&parts.headers),
            )
            .await);
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
        return failed(StatusCode::BAD_REQUEST);
    };
    let (Some(config), Some(secret)) = (&state.oauth, state.secret.as_deref()) else {
        return failed(StatusCode::SERVICE_UNAVAILABLE);
    };
    let params: HashMap<_, _> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let Some(state_id) = params.get("state") else {
        return failed(StatusCode::BAD_REQUEST);
    };
    let response = session::consume_oauth(&state.pool, &headers, secret, state_id).await;
    let context = match response {
        Ok(Some(context)) => context,
        Ok(None) => return failed(StatusCode::BAD_REQUEST),
        Err(_) => {
            eprintln!("OAuth session state could not be retrieved");
            return failed(StatusCode::INTERNAL_SERVER_ERROR);
        }
    };
    if params.contains_key("error") {
        return failed(StatusCode::BAD_REQUEST);
    }
    let Some(code) = params.get("code").filter(|code| !code.is_empty()) else {
        return failed(StatusCode::BAD_REQUEST);
    };
    let Some((token, fields, profile)) = profile(config, &origin, code).await else {
        eprintln!("OAuth provider rejected the exchange or sent an invalid response");
        return failed(StatusCode::BAD_GATEWAY);
    };
    let connecting = if context.connect {
        match session::user(&state.pool, &headers, Some(secret)).await {
            Ok(user) if user.is_authenticated => user.id,
            _ => return failed(StatusCode::FORBIDDEN),
        }
    } else {
        None
    };
    let (id, password) = match save(&state.pool, &profile, &fields, &token, connecting).await {
        Ok(credentials) => credentials,
        Err(AccountError::ExistingEmail) => {
            return (
                StatusCode::FOUND,
                [(header::LOCATION, "/accounts/social/signup/")],
            )
                .into_response();
        }
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
        Err(AccountError::Invalid) => return failed(StatusCode::BAD_REQUEST),
        Err(AccountError::Database) => return failed(StatusCode::INTERNAL_SERVER_ERROR),
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
        "allauth.account.auth_backends.AuthenticationBackend",
    )
    .await
}
