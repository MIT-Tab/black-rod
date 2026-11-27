use std::collections::{HashMap, HashSet};

use axum::{
    Json,
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use chrono::Utc;
use ring::digest;
use serde_json::{Value, json};
use sqlx::{AssertSqlSafe, Row};
use subtle::ConstantTimeEq;

use crate::{ApiError, AppState};

#[derive(Default)]
pub struct PrivateTokens {
    forum: Option<digest::Digest>,
    mittab: Vec<digest::Digest>,
}

impl PrivateTokens {
    pub fn new(forum: &str, mittab: &str) -> Self {
        let forum = forum.trim();
        let hash = |token: &str| digest::digest(&digest::SHA256, token.as_bytes());
        Self {
            forum: (!forum.is_empty()).then(|| hash(&format!("Bearer {forum}"))),
            mittab: mittab
                .split(',')
                .map(str::trim)
                .filter(|token| !token.is_empty())
                .map(hash)
                .collect(),
        }
    }
}

#[derive(Clone, Copy)]
enum Service {
    Forum,
    Mittab,
}

impl Service {
    fn authorized(self, config: &PrivateTokens, headers: &HeaderMap) -> bool {
        let Some(token) = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
        else {
            return false;
        };
        match self {
            Self::Forum => config.forum.as_ref().is_some_and(|expected| {
                let candidate = digest::digest(&digest::SHA256, token.as_bytes());
                bool::from(candidate.as_ref().ct_eq(expected.as_ref()))
            }),
            Self::Mittab => {
                let Some(token) = token
                    .strip_prefix("Bearer ")
                    .map(str::trim)
                    .filter(|token| !token.is_empty())
                else {
                    return false;
                };
                let candidate = digest::digest(&digest::SHA256, token.as_bytes());
                let mut matched = subtle::Choice::from(0);
                for token in &config.mittab {
                    matched |= candidate.as_ref().ct_eq(token.as_ref());
                }
                bool::from(matched)
            }
        }
    }

    fn render(self, status: StatusCode, body: Value) -> Response {
        let mut response = (
            status,
            [(header::ALLOW, "POST, OPTIONS"), (header::VARY, "Accept")],
            Json(body),
        )
            .into_response();
        if matches!(self, Self::Forum) {
            let headers = response.headers_mut();
            headers.insert(
                header::CACHE_CONTROL,
                "max-age=0, no-cache, no-store, must-revalidate, private"
                    .parse()
                    .unwrap(),
            );
            headers.insert(
                header::EXPIRES,
                Utc::now()
                    .format("%a, %d %b %Y %H:%M:%S GMT")
                    .to_string()
                    .parse()
                    .unwrap(),
            );
        }
        response
    }

    fn error(self, status: StatusCode, detail: &str) -> Response {
        self.render(status, json!({"detail": detail}))
    }

    async fn handle(self, state: AppState, request: Request) -> Response {
        if request.method() == Method::OPTIONS {
            let (name, description) = match self {
                Self::Forum => (
                    "Forum Profiles Api",
                    "Resolve approved claims using the provider's immutable WordPress ID.",
                ),
                Self::Mittab => (
                    "Private Debater Emails Api",
                    concat!(
                        "Return private debater contact emails for trusted mit-tab instances.\n\n",
                        "Auth is intentionally independent of browser sessions. Mittab-deploy injects a\n",
                        "bearer token into each ephemeral mit-tab instance, and black-rod keeps the\n",
                        "matching token in MITTAB_PRIVATE_API_TOKENS for manual rotation.",
                    ),
                ),
            };
            return self.render(StatusCode::OK, json!({
                "name": name, "description": description,
                "renders": ["application/json", "text/html"],
                "parses": ["application/json", "application/x-www-form-urlencoded", "multipart/form-data"],
            }));
        }
        if request.method() != Method::POST {
            return self.error(
                StatusCode::METHOD_NOT_ALLOWED,
                &format!("Method \"{}\" not allowed.", request.method()),
            );
        }
        if !self.authorized(&state.private_tokens, request.headers()) {
            let reason = match self {
                Self::Forum => "Unauthorized",
                Self::Mittab => "Invalid or missing credentials.",
            };
            return self.error(StatusCode::UNAUTHORIZED, reason);
        }
        let (parts, body) = request.into_parts();
        let content_type = parts
            .headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default();
        let data = match to_bytes(body, 2 * 1024 * 1024).await {
            Ok(bytes) if bytes.is_empty() => json!({}),
            Ok(bytes) if content_type.split(';').next() == Some("application/json") => {
                match serde_json::from_slice(&bytes) {
                    Ok(data) => data,
                    Err(error) => {
                        return self.error(
                            StatusCode::BAD_REQUEST,
                            &format!("JSON parse error - {error}"),
                        );
                    }
                }
            }
            Ok(_) => {
                return self.error(
                    StatusCode::UNSUPPORTED_MEDIA_TYPE,
                    &format!("Unsupported media type \"{content_type}\" in request."),
                );
            }
            Err(_) => {
                return self.error(StatusCode::PAYLOAD_TOO_LARGE, "Request body is too large.");
            }
        };
        let result = match self {
            Self::Forum => self.forum(state, data).await,
            Self::Mittab => self.mittab(state, data).await,
        };
        match result {
            Ok(result) => self.render(StatusCode::OK, result),
            Err(PrivateError::Validation(detail)) => self.error(StatusCode::BAD_REQUEST, detail),
            Err(PrivateError::Database) => {
                self.error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error.")
            }
        }
    }

    async fn forum(self, state: AppState, data: Value) -> Result<Value, PrivateError> {
        let wordpress_id = data["wordpress_user_id"]
            .as_u64()
            .filter(|id| *id > 0)
            .ok_or(PrivateError::Validation(
                "wordpress_user_id must be a positive integer",
            ))?;
        let rows = sqlx::query("
            SELECT CAST(d.id AS BIGINT) AS id, d.first_name, d.last_name, s.name AS school
            FROM core_debater d
            LEFT JOIN core_school s ON s.id = d.school_id
            WHERE NOT d.temporary AND NOT d.synthetic AND EXISTS (
                SELECT 1 FROM socialaccount_socialaccount sa
                JOIN core_user u ON u.id = sa.user_id
                WHERE sa.provider = 'apdaonline' AND sa.uid = $1 AND u.is_active AND u.id = d.user_id
            ) ORDER BY d.last_name, d.first_name, s.name, d.id
        ").bind(wordpress_id.to_string()).fetch_all(&state.pool).await?;
        let mut debaters = Vec::with_capacity(rows.len());
        for row in rows {
            let first = row.try_get::<&str, _>("first_name")?;
            let last = row.try_get::<&str, _>("last_name")?;
            debaters.push(json!({
                "id": row.try_get::<i64, _>("id")?,
                "name": format!("{first} {last}").trim(),
                "school": row.try_get::<Option<&str>, _>("school")?,
            }));
        }
        Ok(json!({"debaters": debaters}))
    }

    async fn mittab(self, state: AppState, data: Value) -> Result<Value, PrivateError> {
        let input = match data.get("debater_ids") {
            None => &[][..],
            Some(value) => value
                .as_array()
                .map(Vec::as_slice)
                .ok_or(PrivateError::Validation("debater_ids must be a list."))?,
        };
        let mut ids = Vec::new();
        let mut seen = HashSet::new();
        for value in input {
            let id = normalized_id(value).ok_or(PrivateError::Validation(
                "debater_ids must contain only integers.",
            ))?;
            if seen.insert(id) {
                ids.push(id);
            }
        }
        if ids.len() > 200 {
            return Err(PrivateError::Validation(
                "At most 200 debater ids may be requested.",
            ));
        }
        let mut addresses = HashMap::new();
        if !ids.is_empty() {
            let placeholders = (1..=ids.len())
                .map(|index| format!("${index}"))
                .collect::<Vec<_>>()
                .join(", ");
            let query = format!(
                "
                SELECT CAST(id AS BIGINT) AS id, email FROM core_debater
                WHERE NOT synthetic AND NOT temporary AND id IN ({placeholders})
            "
            );
            let mut statement = sqlx::query(AssertSqlSafe(query));
            for id in &ids {
                statement = statement.bind(*id);
            }
            for row in statement.fetch_all(&state.pool).await? {
                addresses.insert(
                    row.try_get::<i64, _>("id")?,
                    row.try_get::<Option<String>, _>("email")?,
                );
            }
        }
        Ok(json!({"debaters": ids.into_iter().map(|id| {
            json!({"id": id, "email": addresses.remove(&id).flatten()})
        }).collect::<Vec<_>>()}))
    }
}

fn normalized_id(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64().or_else(|| {
            number
                .as_f64()
                .filter(|value| *value >= i64::MIN as f64 && *value < -(i64::MIN as f64))
                .map(|value| value as i64)
        }),
        Value::String(string) => string.trim().parse().ok(),
        Value::Bool(boolean) => Some(i64::from(*boolean)),
        _ => None,
    }
}

enum PrivateError {
    Validation(&'static str),
    Database,
}

impl From<sqlx::Error> for PrivateError {
    fn from(error: sqlx::Error) -> Self {
        let _ = ApiError::from(error);
        Self::Database
    }
}

pub(crate) async fn forum(State(state): State<AppState>, request: Request) -> Response {
    Service::Forum.handle(state, request).await
}

pub(crate) async fn mittab(State(state): State<AppState>, request: Request) -> Response {
    Service::Mittab.handle(state, request).await
}
