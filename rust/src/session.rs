use std::io::{Read, Write};

use axum::http::{HeaderMap, header};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{Duration, Utc};
use flate2::{Compression, read::ZlibDecoder, write::ZlibEncoder};
use ring::{
    digest, hmac,
    rand::{SecureRandom, SystemRandom},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json, value::RawValue};
use sqlx::{AnyConnection, AnyPool, Row};

const SESSION_SALT: &[u8] = b"django.contrib.sessions.SessionStoresigner";
const AUTH_SALT: &[u8] = b"django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash";
const MAX_SESSION_SIZE: u64 = 1024 * 1024;

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

pub(crate) fn key(salt: &[u8], secret: &[u8]) -> hmac::Key {
    let mut hash = digest::Context::new(&digest::SHA256);
    hash.update(salt);
    hash.update(secret);
    hmac::Key::new(hmac::HMAC_SHA256, hash.finish().as_ref())
}

fn decode(data: &str, secret: &[u8]) -> Option<Value> {
    decode_signed(data, secret, SESSION_SALT)
}

pub(crate) fn decode_signed(data: &str, secret: &[u8], salt: &[u8]) -> Option<Value> {
    let bytes = decode_signed_bytes(data, secret, salt)?;
    serde_json::from_slice(&bytes).ok()
}

fn decode_signed_bytes(data: &str, secret: &[u8], salt: &[u8]) -> Option<Vec<u8>> {
    if data.len() as u64 > MAX_SESSION_SIZE {
        return None;
    }
    let (signed, signature) = data.rsplit_once(':')?;
    let signature = URL_SAFE_NO_PAD.decode(signature).ok()?;
    hmac::verify(&key(salt, secret), signed.as_bytes(), &signature).ok()?;
    let (payload, _) = signed.rsplit_once(':')?;
    let contents = if let Some(payload) = payload.strip_prefix('.') {
        let compressed = URL_SAFE_NO_PAD.decode(payload).ok()?;
        let mut inflated = Vec::new();
        ZlibDecoder::new(compressed.as_slice())
            .take(MAX_SESSION_SIZE + 1)
            .read_to_end(&mut inflated)
            .ok()?;
        if inflated.len() as u64 > MAX_SESSION_SIZE {
            return None;
        }
        inflated
    } else {
        URL_SAFE_NO_PAD.decode(payload).ok()?
    };
    Some(contents)
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

fn timestamp(mut time: u64) -> String {
    const ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    let mut encoded = Vec::new();
    loop {
        encoded.push(char::from(ALPHABET[(time % 62) as usize]));
        time /= 62;
        if time == 0 {
            break;
        }
    }
    encoded.into_iter().rev().collect()
}

fn encode(session: &Value, secret: &[u8]) -> String {
    encode_signed(&session.to_string(), secret, SESSION_SALT)
}

pub(crate) fn encode_signed(payload: &str, secret: &[u8], salt: &[u8]) -> String {
    sign_payload(&URL_SAFE_NO_PAD.encode(payload), secret, salt)
}

pub(crate) fn encode_compressed(
    payload: &str,
    secret: &[u8],
    salt: &[u8],
) -> std::io::Result<String> {
    let mut compressor = ZlibEncoder::new(Vec::new(), Compression::default());
    compressor.write_all(payload.as_bytes())?;
    let compressed = compressor.finish()?;
    Ok(if compressed.len() + 1 < payload.len() {
        sign_payload(
            &format!(".{}", URL_SAFE_NO_PAD.encode(compressed)),
            secret,
            salt,
        )
    } else {
        encode_signed(payload, secret, salt)
    })
}

fn sign_payload(payload: &str, secret: &[u8], salt: &[u8]) -> String {
    let signed = format!("{payload}:{}", timestamp(Utc::now().timestamp() as u64));
    let signature = URL_SAFE_NO_PAD.encode(hmac::sign(&key(salt, secret), signed.as_bytes()));
    format!("{signed}:{signature}")
}

pub(crate) fn decode_timed<T: serde::de::DeserializeOwned>(
    signed: &str,
    secret: &[u8],
    salt: &[u8],
    max_age: i64,
) -> Option<T> {
    let bytes = decode_signed_bytes(signed, secret, salt)?;
    let (_, timestamp) = signed.rsplit_once(':')?.0.rsplit_once(':')?;
    let timestamp = timestamp.bytes().try_fold(0_i64, |acc, byte| {
        let digit = match byte {
            b'0'..=b'9' => byte - b'0',
            b'A'..=b'Z' => byte - b'A' + 10,
            b'a'..=b'z' => byte - b'a' + 36,
            _ => return None,
        };
        acc.checked_mul(62)?.checked_add(i64::from(digit))
    })?;
    if Utc::now().timestamp().saturating_sub(timestamp) > max_age {
        return None;
    }
    serde_json::from_slice(&bytes).ok()
}

pub(crate) struct StoredSession {
    pub(crate) values: std::collections::BTreeMap<String, Box<RawValue>>,
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
        let serialized = serde_json::to_string(&self.values)
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        let data = encode_signed(
            &crate::django_json::ascii(&serialized),
            secret,
            SESSION_SALT,
        );
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
        let document = serde_json::to_string(&self.values)
            .map(|value| crate::django_json::ascii(&value))
            .map_err(|error| sqlx::Error::Protocol(error.to_string()))?;
        let updated = encode_signed(&document, secret, SESSION_SALT);
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
    secret: &[u8],
    state_id: &str,
    next: &str,
    remember: bool,
) -> Result<String, sqlx::Error> {
    let session_key = random_token(b"abcdefghijklmnopqrstuvwxyz0123456789", 32)
        .map_err(|_| sqlx::Error::Protocol("Unable to generate an OAuth session key".into()))?;
    let now = Utc::now();
    let value = json!({
        "socialaccount_states": {(state_id): [{"process": "login", "next": next, "data": {}}, now.timestamp()]},
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
    Ok(session_key)
}

pub(crate) async fn consume_oauth(
    pool: &AnyPool,
    headers: &HeaderMap,
    secret: &[u8],
    state_id: &str,
) -> Result<Option<(String, bool)>, sqlx::Error> {
    let Some(session_key) = session_id(headers) else {
        return Ok(None);
    };
    let data = sqlx::query_scalar::<_, String>("SELECT session_data FROM django_session WHERE session_key = $1 AND expire_date > CURRENT_TIMESTAMP")
        .bind(session_key).fetch_optional(pool).await?;
    let Some((data, mut payload)) =
        data.and_then(|data| decode(&data, secret).map(|payload| (data, payload)))
    else {
        return Ok(None);
    };
    let state = payload["socialaccount_states"]
        .as_object_mut()
        .and_then(|states| states.remove(state_id));
    let Some(state) = state else {
        return Ok(None);
    };
    let Some(stashed_at) = state[1].as_f64() else {
        return Ok(None);
    };
    if Utc::now().timestamp() as f64 - stashed_at > 600.0 {
        return Ok(None);
    }
    if state[0]["process"]
        .as_str()
        .is_some_and(|process| process != "login")
    {
        return Ok(None);
    }
    let changed = sqlx::query(
        "UPDATE django_session SET session_data = $1 WHERE session_key = $2 AND session_data = $3",
    )
    .bind(encode(&payload, secret))
    .bind(session_key)
    .bind(data)
    .execute(pool)
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(None);
    }
    let next = state[0]["next"].as_str().unwrap_or_default().to_owned();
    let remember = payload["apda_remember_login"].as_bool().unwrap_or(true);
    Ok(Some((next, remember)))
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
    let session_key = random_token(b"abcdefghijklmnopqrstuvwxyz0123456789", 32)
        .map_err(|_| sqlx::Error::Protocol("Unable to generate a session key".into()))?;
    let mut session = json!({
        "_auth_user_id": id.to_string(),
        "_auth_user_backend": backend,
        "_auth_user_hash": hex::encode(hmac::sign(&key(AUTH_SALT, secret), password_hash.as_bytes())),
    });
    if !remember {
        session["_session_expiry"] = json!(0);
    }
    let now = Utc::now();
    let session_data = encode(&session, secret);
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
    sqlx::query(query)
        .bind(&session_key)
        .bind(session_data)
        .bind(expiry)
        .execute(&mut *tx)
        .await?;
    if let Some(old_key) = session_id(headers) {
        sqlx::query("DELETE FROM django_session WHERE session_key = $1")
            .bind(old_key)
            .execute(&mut *tx)
            .await?;
    }
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
               CAST(CASE WHEN u.is_superuser OR EXISTS (
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
