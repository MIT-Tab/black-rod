mod signing;

use axum::http::{HeaderMap, header};
use chrono::{Duration, Utc};
use ring::{
    hmac,
    rand::{SecureRandom, SystemRandom},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json, value::RawValue};
use sqlx::{AnyConnection, AnyPool, Row};

use signing::decode_signed_bytes;
pub(crate) use signing::{decode_signed, decode_timed, encode_compressed, encode_signed, key};

const SESSION_SALT: &[u8] = b"django.contrib.sessions.SessionStoresigner";
const AUTH_SALT: &[u8] = b"django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash";

#[derive(Serialize, Default)]
pub(crate) struct User {
    pub(crate) id: Option<i64>,
    pub(crate) is_authenticated: bool,
    pub(crate) is_superuser: bool,
    pub(crate) is_staff: bool,
    pub(crate) is_school_admin: bool,
    pub(crate) has_exclusive_pre_access: bool,
    pub(crate) can_view_private_videos: bool,
    pub(crate) can_view_video: bool,
}

#[derive(Deserialize)]
struct DjangoSession {
    _auth_user_id: String,
    _auth_user_backend: String,
    _auth_user_hash: String,
}

fn decode(data: &str, secret: &[u8]) -> Option<Value> {
    decode_signed(data, secret, SESSION_SALT)
}

pub(crate) fn cookie<'a>(headers: &'a HeaderMap, key: &str) -> Option<&'a str> {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|header| header.to_str().ok())
        .flat_map(|cookie| cookie.split(';'))
        .filter_map(|field| field.trim().split_once('='))
        .filter(|(name, _)| name.trim() == key)
        .map(|(_, value)| value.trim().trim_matches('"'))
        .next_back()
}

pub(crate) fn session_id(headers: &HeaderMap) -> Option<&str> {
    cookie(headers, "sessionid").filter(|value| {
        (8..=40).contains(&value.len())
            && value
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
    })
}

pub(crate) fn random_token(
    alphabet: &[u8],
    count: usize,
) -> Result<String, ring::error::Unspecified> {
    let rng = SystemRandom::new();
    let mut token = String::with_capacity(count);
    let max = 256 - 256 % alphabet.len();
    let mut random = [0; 64];
    while token.len() < count {
        rng.fill(&mut random)?;
        for byte in random {
            let byte = usize::from(byte);
            if byte < max {
                token.push(char::from(alphabet[byte % alphabet.len()]));
                if token.len() == count {
                    break;
                }
            }
        }
    }
    Ok(token)
}

fn encode(session: &Value, secret: &[u8]) -> String {
    encode_signed(
        &crate::django_json::ascii(&session.to_string()),
        secret,
        SESSION_SALT,
    )
}

type SessionValues = std::collections::BTreeMap<String, Box<RawValue>>;

fn encode_values(values: &SessionValues, secret: &[u8]) -> Result<String, sqlx::Error> {
    let serialized =
        serde_json::to_string(values).map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
    Ok(encode_signed(
        &crate::django_json::ascii(&serialized),
        secret,
        SESSION_SALT,
    ))
}

pub(crate) struct StoredSession {
    pub(crate) values: SessionValues,
    id: String,
    original: String,
}

impl StoredSession {
    pub(crate) async fn read(
        pool: &AnyPool,
        headers: &HeaderMap,
        secret: &[u8],
    ) -> Result<Option<Self>, sqlx::Error> {
        let Some(id) = session_id(headers) else {
            return Ok(None);
        };
        let original = sqlx::query_scalar::<_, String>(
            "SELECT session_data FROM django_session WHERE session_key = $1 AND expire_date > CURRENT_TIMESTAMP"
        ).bind(id).fetch_optional(pool).await?;
        let Some(original) = original else {
            return Ok(None);
        };
        let values = decode_signed_bytes(&original, secret, SESSION_SALT)
            .and_then(|bytes| serde_json::from_slice(&bytes).ok());
        Ok(values.map(|values| Self {
            id: id.to_owned(),
            values,
            original,
        }))
    }

    pub(crate) fn string(&self, key: &str) -> Option<String> {
        serde_json::from_str(self.values.get(key)?.get()).ok()
    }

    pub(crate) fn remove(&mut self, key: &str) {
        self.values.remove(key);
    }

    pub(crate) fn set_string(&mut self, key: &str, value: &str) -> Result<(), sqlx::Error> {
        let value = serde_json::value::to_raw_value(value)
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        self.values.insert(key.to_owned(), value);
        Ok(())
    }

    pub(crate) async fn rotate_password(
        mut self,
        conn: &mut AnyConnection,
        secret: &[u8],
        password_hash: &str,
    ) -> Result<(String, bool), sqlx::Error> {
        let id = random_token(b"abcdefghijklmnopqrstuvwxyz0123456789", 32)
            .map_err(|_| sqlx::Error::Protocol("Unable to generate a session key".into()))?;
        let digest = hmac::sign(&key(AUTH_SALT, secret), password_hash.as_bytes());
        let auth = serde_json::value::to_raw_value(&hex::encode(digest))
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        self.values.insert("_auth_user_hash".to_owned(), auth);
        let remember = self
            .values
            .get("_session_expiry")
            .is_none_or(|value| value.get() != "0");
        let data = encode_values(&self.values, secret)?;
        let changed = sqlx::query(
            "UPDATE django_session SET session_key=$1,session_data=$2
            WHERE session_key=$3 AND session_data=$4 AND expire_date>CURRENT_TIMESTAMP",
        )
        .bind(&id)
        .bind(data)
        .bind(self.id)
        .bind(self.original)
        .execute(conn)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(sqlx::Error::RowNotFound);
        }
        Ok((id, remember))
    }

    pub(crate) async fn save(self, pool: &AnyPool, secret: &[u8]) -> Result<bool, sqlx::Error> {
        self.save_on(&mut *pool.acquire().await?, secret).await
    }

    pub(crate) async fn save_on(
        self,
        conn: &mut AnyConnection,
        secret: &[u8],
    ) -> Result<bool, sqlx::Error> {
        let updated = encode_values(&self.values, secret)?;
        let result = sqlx::query(
            "UPDATE django_session SET session_data = $1
            WHERE session_key = $2 AND session_data = $3 AND expire_date > CURRENT_TIMESTAMP",
        )
        .bind(updated)
        .bind(self.id)
        .bind(self.original)
        .execute(conn)
        .await?;
        Ok(result.rows_affected() == 1)
    }
}

pub(crate) async fn create_anonymous(
    pool: &AnyPool,
    secret: &[u8],
    payload: &Value,
) -> Result<String, sqlx::Error> {
    let id = random_token(b"abcdefghijklmnopqrstuvwxyz0123456789", 32)
        .map_err(|_| sqlx::Error::Protocol("Unable to generate a session key".into()))?;
    let mut expiry = (Utc::now() + Duration::days(30))
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string();
    let query = if pool.connect_options().database_url.scheme() == "sqlite" {
        "INSERT INTO django_session (session_key, session_data, expire_date) VALUES ($1, $2, $3)"
    } else {
        expiry.push_str("+00");
        "INSERT INTO django_session (session_key, session_data, expire_date) VALUES ($1, $2, CAST($3 AS TIMESTAMPTZ))"
    };
    sqlx::query(query)
        .bind(&id)
        .bind(encode(payload, secret))
        .bind(expiry)
        .execute(pool)
        .await?;
    Ok(id)
}

pub(crate) async fn oauth_start(
    pool: &AnyPool,
    headers: &HeaderMap,
    secret: &[u8],
    state_id: &str,
    next: &str,
    remember: bool,
) -> Result<Option<String>, sqlx::Error> {
    if let Some(session) = StoredSession::read(pool, headers, secret).await? {
        return stash_oauth(session, pool, secret, state_id, next, Some(remember)).await;
    }
    let session_key = random_token(b"abcdefghijklmnopqrstuvwxyz0123456789", 32)
        .map_err(|_| sqlx::Error::Protocol("Unable to generate an OAuth session key".into()))?;
    let now = Utc::now();
    let value = json!({
        "socialaccount_states": {(state_id): [{"process": "login", "next": next, "data": {"results_remember": remember}}, now.timestamp()]},
        "apda_remember_login": remember,
    });
    let data = encode(&value, secret);
    let mut expiry = (now + Duration::minutes(10))
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string();
    let query = if pool.connect_options().database_url.scheme() == "sqlite" {
        "INSERT INTO django_session (session_key, session_data, expire_date) VALUES ($1, $2, $3)"
    } else {
        expiry.push_str("+00");
        "INSERT INTO django_session (session_key, session_data, expire_date) VALUES ($1, $2, CAST($3 AS TIMESTAMPTZ))"
    };
    sqlx::query(query)
        .bind(&session_key)
        .bind(data)
        .bind(expiry)
        .execute(pool)
        .await?;
    Ok(Some(session_key))
}

pub(crate) async fn oauth_connect(
    pool: &AnyPool,
    headers: &HeaderMap,
    secret: &[u8],
    state_id: &str,
    next: &str,
) -> Result<Option<String>, sqlx::Error> {
    let Some(session) = StoredSession::read(pool, headers, secret).await? else {
        return Ok(None);
    };
    stash_oauth(session, pool, secret, state_id, next, None).await
}

async fn stash_oauth(
    mut session: StoredSession,
    pool: &AnyPool,
    secret: &[u8],
    state_id: &str,
    next: &str,
    remember: Option<bool>,
) -> Result<Option<String>, sqlx::Error> {
    let process = if let Some(remember) = remember {
        let value = serde_json::value::to_raw_value(&remember)
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        session
            .values
            .insert("apda_remember_login".to_owned(), value);
        "login"
    } else {
        "connect"
    };
    let mut states: serde_json::Map<String, Value> = session
        .values
        .get("socialaccount_states")
        .and_then(|states| serde_json::from_str(states.get()).ok())
        .unwrap_or_default();
    let now = Utc::now().timestamp() as f64;
    states.retain(|_, state| state[1].as_f64().is_some_and(|time| now - time <= 600.0));
    if states.len() >= 10
        && let Some(oldest) = states
            .iter()
            .min_by(|(_, a), (_, b)| {
                a[1].as_f64()
                    .unwrap_or_default()
                    .total_cmp(&b[1].as_f64().unwrap_or_default())
            })
            .map(|(key, _)| key.clone())
    {
        states.remove(&oldest);
    }
    let data = remember.map_or_else(
        || json!({}),
        |remember| json!({"results_remember": remember}),
    );
    states.insert(
        state_id.to_owned(),
        json!([{"process":process,"next":next,"data":data},now]),
    );
    let state = serde_json::value::to_raw_value(&states)
        .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
    session
        .values
        .insert("socialaccount_states".to_owned(), state);
    let key = session.id.clone();
    Ok(session.save(pool, secret).await?.then_some(key))
}

pub(crate) struct OAuthContext {
    pub(crate) next: String,
    pub(crate) remember: bool,
    pub(crate) connect: bool,
}

pub(crate) async fn consume_oauth(
    pool: &AnyPool,
    headers: &HeaderMap,
    secret: &[u8],
    state_id: &str,
) -> Result<Option<OAuthContext>, sqlx::Error> {
    let Some(mut session) = StoredSession::read(pool, headers, secret).await? else {
        return Ok(None);
    };
    let Some(mut states) = session
        .values
        .get("socialaccount_states")
        .and_then(|states| {
            serde_json::from_str::<serde_json::Map<String, Value>>(states.get()).ok()
        })
    else {
        return Ok(None);
    };
    let state = states.remove(state_id);
    let Some(state) = state else {
        return Ok(None);
    };
    let Some(stashed_at) = state[1].as_f64() else {
        return Ok(None);
    };
    if Utc::now().timestamp() as f64 - stashed_at > 600.0 {
        return Ok(None);
    }
    let connect = match state[0]["process"].as_str() {
        Some("connect") => true,
        None | Some("login") => false,
        _ => return Ok(None),
    };
    let remaining = serde_json::value::to_raw_value(&states)
        .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
    session
        .values
        .insert("socialaccount_states".to_owned(), remaining);
    let remember = state[0]["data"]["results_remember"]
        .as_bool()
        .or_else(|| {
            session
                .values
                .get("apda_remember_login")
                .and_then(|value| serde_json::from_str::<bool>(value.get()).ok())
        })
        .unwrap_or(true);
    if !session.save(pool, secret).await? {
        return Ok(None);
    }
    let next = state[0]["next"].as_str().unwrap_or_default().to_owned();
    Ok(Some(OAuthContext {
        next,
        remember,
        connect,
    }))
}

pub(crate) async fn create(
    pool: &AnyPool,
    headers: &HeaderMap,
    secret: &[u8],
    id: i64,
    password_hash: &str,
    remember: bool,
    backend: &str,
) -> Result<String, sqlx::Error> {
    let mut old = StoredSession::read(pool, headers, secret).await?;
    let user_id = id.to_string();
    let auth_key = key(AUTH_SALT, secret);
    let keep_key = old.as_ref().is_some_and(|session| {
        session.string("_auth_user_id").as_deref() == Some(&user_id)
            && session
                .string("_auth_user_hash")
                .and_then(|hash| hex::decode(hash).ok())
                .is_some_and(|hash| {
                    hmac::verify(&auth_key, password_hash.as_bytes(), &hash).is_ok()
                })
    });
    let mut values = old
        .as_mut()
        .filter(|session| keep_key || !session.values.contains_key("_auth_user_id"))
        .map(|session| std::mem::take(&mut session.values))
        .unwrap_or_default();
    values.remove("apda_remember_login");
    values.remove("socialaccount_sociallogin");
    let session_key = if let Some(previous) = old.as_ref().filter(|_| keep_key) {
        previous.id.clone()
    } else {
        random_token(b"abcdefghijklmnopqrstuvwxyz0123456789", 32)
            .map_err(|_| sqlx::Error::Protocol("Unable to generate a session key".into()))?
    };
    let fields = [
        ("_auth_user_id", json!(user_id)),
        ("_auth_user_backend", json!(backend)),
        (
            "_auth_user_hash",
            json!(hex::encode(hmac::sign(&auth_key, password_hash.as_bytes()))),
        ),
        (
            "_session_expiry",
            json!(if remember {
                crate::login::SESSION_AGE
            } else {
                0
            }),
        ),
    ];
    for (field, value) in fields {
        let raw = serde_json::value::to_raw_value(&value)
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        values.insert(field.to_owned(), raw);
    }
    let now = Utc::now();
    let session_data = encode_values(&values, secret)?;
    let mut expiry = (now + Duration::days(30))
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string();
    let sqlite = pool.connect_options().database_url.scheme() == "sqlite";
    if !sqlite {
        expiry.push_str("+00");
    }
    let query = if sqlite {
        "INSERT INTO django_session (session_key, session_data, expire_date) VALUES ($1, $2, $3)"
    } else {
        "INSERT INTO django_session (session_key, session_data, expire_date) VALUES ($1, $2, CAST($3 AS TIMESTAMPTZ))"
    };
    let mut tx = pool.begin().await?;
    let current = sqlx::query("UPDATE core_user SET last_login = CURRENT_TIMESTAMP WHERE id = $1 AND is_active AND password = $2")
        .bind(id).bind(password_hash).execute(&mut *tx).await?;
    if current.rows_affected() != 1 {
        return Err(sqlx::Error::RowNotFound);
    }
    if let Some(previous) = old {
        let changed = sqlx::query(
            "DELETE FROM django_session WHERE session_key=$1 AND session_data=$2 AND expire_date>CURRENT_TIMESTAMP",
        )
        .bind(previous.id)
        .bind(previous.original)
        .execute(&mut *tx)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(sqlx::Error::RowNotFound);
        }
    }
    sqlx::query(query)
        .bind(&session_key)
        .bind(session_data)
        .bind(expiry)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(session_key)
}

pub(crate) async fn delete(pool: &AnyPool, headers: &HeaderMap) -> Result<(), sqlx::Error> {
    if let Some(id) = session_id(headers) {
        sqlx::query("DELETE FROM django_session WHERE session_key = $1")
            .bind(id)
            .execute(pool)
            .await?;
    }
    Ok(())
}

pub(crate) async fn user(
    pool: &AnyPool,
    headers: &HeaderMap,
    secret: Option<&[u8]>,
) -> Result<User, sqlx::Error> {
    let (Some(secret), Some(session_id)) = (secret, session_id(headers)) else {
        return Ok(User::default());
    };
    let Some(session_data): Option<String> = sqlx::query_scalar(
        "
        SELECT session_data FROM django_session
        WHERE session_key = $1 AND expire_date > CURRENT_TIMESTAMP
    ",
    )
    .bind(session_id)
    .fetch_optional(pool)
    .await?
    else {
        return Ok(User::default());
    };
    let Some(session) = decode(&session_data, secret)
        .and_then(|data| serde_json::from_value::<DjangoSession>(data).ok())
    else {
        return Ok(User::default());
    };
    if !matches!(
        session._auth_user_backend.as_str(),
        "django.contrib.auth.backends.ModelBackend"
            | "allauth.account.auth_backends.AuthenticationBackend"
    ) {
        return Ok(User::default());
    }
    let (Ok(user_id), Ok(auth_hash)) = (
        session._auth_user_id.parse::<i64>(),
        hex::decode(session._auth_user_hash),
    ) else {
        return Ok(User::default());
    };
    let Some(row) = sqlx::query(
        "
        SELECT u.password,
               CAST(CASE WHEN u.is_superuser THEN 1 ELSE 0 END AS BIGINT) AS superuser,
               CAST(CASE WHEN u.is_staff THEN 1 ELSE 0 END AS BIGINT) AS staff,
               CAST(CASE WHEN EXISTS (
                   SELECT 1 FROM core_schooladmin a WHERE a.user_id = u.id
               ) THEN 1 ELSE 0 END AS BIGINT) AS school_admin,
               CAST(CASE WHEN u.is_superuser OR EXISTS (
                   SELECT 1 FROM auth_permission p
                   JOIN django_content_type ct ON ct.id = p.content_type_id
                   WHERE ct.app_label = 'core' AND p.codename = 'exclusive_pre_access'
                     AND (
                        EXISTS (SELECT 1 FROM core_user_user_permissions up
                                WHERE up.user_id = u.id AND up.permission_id = p.id)
                        OR EXISTS (SELECT 1 FROM core_user_groups ug JOIN auth_group_permissions gp
                                   ON gp.group_id = ug.group_id
                                   WHERE ug.user_id = u.id AND gp.permission_id = p.id)
                     )
               ) THEN 1 ELSE 0 END AS BIGINT) AS exclusive_pre_access,
               CAST(CASE WHEN u.is_superuser OR EXISTS (
                   SELECT 1 FROM auth_permission p
                   JOIN django_content_type ct ON ct.id = p.content_type_id
                   WHERE ct.app_label = 'core' AND p.codename = 'view_video'
                     AND (
                        EXISTS (SELECT 1 FROM core_user_user_permissions up
                                WHERE up.user_id = u.id AND up.permission_id = p.id)
                        OR EXISTS (SELECT 1 FROM core_user_groups ug JOIN auth_group_permissions gp
                                   ON gp.group_id = ug.group_id
                                   WHERE ug.user_id = u.id AND gp.permission_id = p.id)
                     )
               ) THEN 1 ELSE 0 END AS BIGINT) AS view_video,
               CAST(CASE WHEN u.can_view_private_videos THEN 1 ELSE 0 END AS BIGINT) AS private_videos
        FROM core_user u WHERE u.id = $1 AND u.is_active
    ",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await?
    else {
        return Ok(User::default());
    };
    let password_hash: &str = row.try_get("password")?;
    if hmac::verify(
        &key(AUTH_SALT, secret),
        password_hash.as_bytes(),
        &auth_hash,
    )
    .is_err()
    {
        return Ok(User::default());
    }
    Ok(User {
        id: Some(user_id),
        is_authenticated: true,
        is_superuser: row.try_get::<i64, _>("superuser")? != 0,
        is_staff: row.try_get::<i64, _>("staff")? != 0,
        is_school_admin: row.try_get::<i64, _>("school_admin")? != 0,
        has_exclusive_pre_access: row.try_get::<i64, _>("exclusive_pre_access")? != 0,
        can_view_private_videos: row.try_get::<i64, _>("private_videos")? != 0,
        can_view_video: row.try_get::<i64, _>("view_video")? != 0,
    })
}

pub(crate) async fn has_permission(
    pool: &AnyPool,
    user: &User,
    code: &str,
) -> Result<bool, sqlx::Error> {
    if user.is_superuser {
        return Ok(true);
    }
    let Some(id) = user.id else { return Ok(false) };
    let (app, code) = code.split_once('.').unwrap_or(("core", code));
    let result = sqlx::query_scalar::<_, i64>("
        SELECT CAST(CASE WHEN EXISTS (
            SELECT 1 FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
            WHERE ct.app_label = $3 AND p.codename = $2 AND (
                EXISTS (SELECT 1 FROM core_user_user_permissions up WHERE up.user_id = $1 AND up.permission_id = p.id)
                OR EXISTS (SELECT 1 FROM core_user_groups ug JOIN auth_group_permissions gp ON gp.group_id = ug.group_id
                           WHERE ug.user_id = $1 AND gp.permission_id = p.id)
            )
        ) THEN 1 ELSE 0 END AS BIGINT)
    ").bind(id).bind(code).bind(app).fetch_one(pool).await?;
    Ok(result != 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Generated by Django's SessionStore with a disposable test key.
    const SIGNED: &str = ".eJyrVopPLC3JiC8tTi2Kz0xRslIyMVLSQRZMSkzOTs0DyaRkJeal5-sl5-eVFGUm6YGU6EFli_V881NSc5ygalEMyEgszgDqLk7MLchJ1QXzdJSS83MLilKLizOTclKBkhXDBCjVAgBiNo6L:1xWlUV:pj-or-6zN4ABXu7QagoNGYCAcKL19g8bM2Nf5dD9yu0";

    #[test]
    fn verifies_django_signature_before_deserialization() {
        let session = decode(SIGNED, b"local-rust-session-test").expect("valid Django session");
        assert_eq!(session["_auth_user_id"], "42");
        assert!(decode(SIGNED, b"wrong-secret").is_none());
        let mut altered = SIGNED.to_owned();
        altered.push('x');
        assert!(decode(&altered, b"local-rust-session-test").is_none());
    }

    #[tokio::test]
    async fn consuming_oauth_preserves_django_session_fields() {
        sqlx::any::install_default_drivers();
        let pool = sqlx::any::AnyPoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        sqlx::query("CREATE TABLE django_session (session_key TEXT PRIMARY KEY, session_data TEXT, expire_date TEXT)")
            .execute(&pool).await.unwrap();
        let secret = b"disposable-oauth-test-key";
        let number = "340282366920938463463374607431768211455";
        let payload = format!(
            r#"{{"grid_seed":{number},"title":"Caf\u00e9","socialaccount_sociallogin":{{"disposable_signup":"pending"}},"apda_remember_login":false,"socialaccount_states":{{"used":[{{"process":"connect","next":"/caf\u00e9/"}},{}],"other":[{{"process":"login"}},{}]}}}}"#,
            Utc::now().timestamp(),
            Utc::now().timestamp(),
        );
        sqlx::query(
            "INSERT INTO django_session VALUES ('oauthfixture', $1, '2099-01-01 00:00:00')",
        )
        .bind(encode_signed(&payload, secret, SESSION_SALT))
        .execute(&pool)
        .await
        .unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(
            header::COOKIE,
            header::HeaderValue::from_static("sessionid=oauthfixture"),
        );
        for (tab, next) in [("one", "/standings/"), ("two", "/calendar/")] {
            assert_eq!(
                oauth_start(&pool, &headers, secret, tab, next, false)
                    .await
                    .unwrap()
                    .as_deref(),
                Some("oauthfixture")
            );
        }
        let context = consume_oauth(&pool, &headers, secret, "used")
            .await
            .unwrap()
            .unwrap();
        assert!(context.connect && !context.remember);
        assert_eq!(context.next, "/café/");
        assert!(
            consume_oauth(&pool, &headers, secret, "used")
                .await
                .unwrap()
                .is_none()
        );

        let other_tab = consume_oauth(&pool, &headers, secret, "one")
            .await
            .unwrap()
            .unwrap();
        assert!(!other_tab.connect);
        assert_eq!(other_tab.next, "/standings/");
        let stored = StoredSession::read(&pool, &headers, secret)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(stored.values["grid_seed"].get(), number);
        assert_eq!(stored.string("title").as_deref(), Some("Café"));
        let bytes = decode_signed_bytes(&stored.original, secret, SESSION_SALT).unwrap();
        assert!(bytes.is_ascii());
        let states: Value =
            serde_json::from_str(stored.values["socialaccount_states"].get()).unwrap();
        assert!(states.get("used").is_none());
        assert!(states.get("other").is_some());
        assert!(states.get("two").is_some());

        sqlx::query("CREATE TABLE core_user (id INTEGER PRIMARY KEY, password TEXT, last_login TEXT, is_active BOOLEAN)")
            .execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO core_user VALUES (1, 'fixture-hash', NULL, 1), (2, 'another-fixture', NULL, 1)")
            .execute(&pool).await.unwrap();
        let backend = "django.contrib.auth.backends.ModelBackend";
        let id = create(&pool, &headers, secret, 1, "fixture-hash", true, backend)
            .await
            .unwrap();
        assert_ne!(
            id, "oauthfixture",
            "anonymous sign-in rotates the session key"
        );
        headers.insert(header::COOKIE, format!("sessionid={id}").parse().unwrap());
        let kept = create(&pool, &headers, secret, 1, "fixture-hash", false, backend)
            .await
            .unwrap();
        assert_eq!(
            kept, id,
            "same-account sign-in retains the existing session"
        );
        let preserved = StoredSession::read(&pool, &headers, secret)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(preserved.values["grid_seed"].get(), number);
        assert_eq!(preserved.values["_session_expiry"].get(), "0");
        assert!(!preserved.values.contains_key("apda_remember_login"));
        assert!(!preserved.values.contains_key("socialaccount_sociallogin"));
        let last_tab = consume_oauth(&pool, &headers, secret, "two")
            .await
            .unwrap()
            .unwrap();
        assert!(!last_tab.remember);
        let replacement = create(&pool, &headers, secret, 2, "another-fixture", true, backend)
            .await
            .unwrap();
        assert_ne!(replacement, id, "changing accounts starts a new session");
        headers.insert(
            header::COOKIE,
            format!("sessionid={replacement}").parse().unwrap(),
        );
        let replacement = StoredSession::read(&pool, &headers, secret)
            .await
            .unwrap()
            .unwrap();
        assert!(!replacement.values.contains_key("grid_seed"));
    }

    #[test]
    fn cookie_selection_matches_django_for_duplicate_and_invalid_keys() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::COOKIE,
            header::HeaderValue::from_static("k=1; sessionid=12345678; sessionid=\"abcdef01\""),
        );
        assert_eq!(session_id(&headers), Some("abcdef01"));
        headers.insert(
            header::COOKIE,
            header::HeaderValue::from_static("sessionid=abc!ef01"),
        );
        assert_eq!(session_id(&headers), None);
    }
}
