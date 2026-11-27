use std::collections::BTreeMap;

use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use chrono::NaiveDate;
use minijinja::context;
use serde::Serialize;
use sqlx::{AnyPool, AssertSqlSafe, Row};

use crate::{
    ApiError, AppState, autocomplete, flash, is_django_integer, login, session, tag_filter, web,
};

use super::{DETAIL, TOPICS};

#[derive(Default, Serialize)]
struct Fields {
    text: String,
    background_slide: String,
    date_set: String,
    tournament: String,
    tournament_label: String,
    tags: Vec<String>,
}

impl Fields {
    async fn existing(pool: &AnyPool, id: i64) -> Result<Option<Self>, ApiError> {
        let Some(row) = sqlx::query(DETAIL)
            .bind(id)
            .bind(Option::<i64>::None)
            .fetch_optional(pool)
            .await?
        else {
            return Ok(None);
        };
        let tournament_id = row.try_get::<Option<i64>, _>("tournament_id")?;
        let tags = sqlx::query_scalar::<_, String>(TOPICS)
            .bind(id)
            .fetch_all(pool)
            .await?;
        Ok(Some(Self {
            text: row.try_get("text")?,
            background_slide: row.try_get("background_slide")?,
            date_set: row
                .try_get::<Option<String>, _>("date_set")?
                .unwrap_or_default(),
            tournament: tournament_id.map_or_else(String::new, |id| id.to_string()),
            tournament_label: row
                .try_get::<Option<String>, _>("tournament_name")?
                .unwrap_or_default(),
            tags,
        }))
    }

    fn submitted(raw: &[u8]) -> Self {
        let values = login::form_values(raw);
        let get = |key| {
            values
                .get(key)
                .map(|value| value.trim().to_owned())
                .unwrap_or_default()
        };
        let tags = tag_filter::parse(
            url::form_urlencoded::parse(raw)
                .filter(|(name, _)| name == "tags")
                .map(|(_, value)| value),
        );
        Self {
            text: get("text"),
            background_slide: get("background_slide"),
            date_set: get("date_set"),
            tournament: get("tournament"),
            tags,
            tournament_label: String::new(),
        }
    }

    async fn validate(
        &mut self,
        pool: &AnyPool,
        errors: &mut BTreeMap<&'static str, &'static str>,
    ) {
        if self.text.is_empty() {
            errors.insert("text", "This field is required.");
        } else if self.text.contains('\0') {
            errors.insert("text", "Null characters are not allowed.");
        }
        if !self.date_set.is_empty() {
            let date = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]
                .iter()
                .find_map(|format| NaiveDate::parse_from_str(&self.date_set, format).ok());
            if let Some(date) = date {
                self.date_set = date.to_string();
            } else {
                errors.insert("date_set", "Enter a valid date.");
            }
        }
        if !self.tournament.is_empty() {
            let label = if let Ok(id) = self.tournament.parse::<i64>() {
                sqlx::query_scalar::<_, String>("SELECT name FROM core_tournament WHERE id = $1")
                    .bind(id)
                    .fetch_optional(pool)
                    .await
                    .ok()
                    .flatten()
            } else {
                None
            };
            if let Some(label) = label {
                self.tournament_label = label;
            } else {
                errors.insert(
                    "tournament",
                    "Select a valid choice. That choice is not one of the available choices.",
                );
            }
        }
        if self
            .tags
            .iter()
            .any(|tag| tag.chars().count() > 100 || tag.contains('\0'))
        {
            errors.insert("tags", "Ensure each tag has at most 100 characters.");
        }
    }
}

async fn save(
    pool: &AnyPool,
    id: Option<i64>,
    fields: &Fields,
) -> Result<Option<i64>, sqlx::Error> {
    let sqlite = pool.connect_options().database_url.scheme() == "sqlite";
    let date = if sqlite {
        "$3"
    } else {
        "CAST(CAST($3 AS TEXT) AS DATE)"
    };
    let date_value = (!fields.date_set.is_empty()).then_some(&fields.date_set);
    let tournament = fields.tournament.parse::<i64>().ok();
    let sql = if id.is_some() {
        format!("UPDATE core_motion SET text = $1, background_slide = $2, date_set = {date},
            tournament_id = $4, updated_at = CURRENT_TIMESTAMP WHERE id = $5 RETURNING CAST(id AS BIGINT)")
    } else {
        format!("INSERT INTO core_motion (text, background_slide, date_set, tournament_id, created_at, updated_at)
            VALUES ($1,$2,{date},$4,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT (text) DO NOTHING RETURNING CAST(id AS BIGINT)")
    };
    let mut tx = pool.begin().await?;
    let query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
        .bind(&fields.text)
        .bind(&fields.background_slide)
        .bind(date_value)
        .bind(tournament);
    let query = if let Some(id) = id {
        query.bind(id)
    } else {
        query
    };
    let result = query.fetch_optional(&mut *tx).await;
    let id = match result {
        Ok(Some(id)) => id,
        Ok(None) => return Ok(None),
        Err(error)
            if error
                .as_database_error()
                .is_some_and(|error| error.is_unique_violation()) =>
        {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(tag_id AS BIGINT) FROM core_taggedmotion WHERE content_object_id = $1",
    )
    .bind(id)
    .fetch_all(&mut *tx)
    .await?;
    let mut tags = Vec::new();
    for tag in &fields.tags {
        tags.push(autocomplete::insert_tag(&mut tx, "core_motiontopic", tag).await?);
    }
    for tag in &existing {
        if !tags.contains(tag) {
            sqlx::query(
                "DELETE FROM core_taggedmotion WHERE content_object_id = $1 AND tag_id = $2",
            )
            .bind(id)
            .bind(tag)
            .execute(&mut *tx)
            .await?;
        }
    }
    for tag in tags {
        if !existing.contains(&tag) {
            sqlx::query(
                "INSERT INTO core_taggedmotion (content_object_id, tag_id) VALUES ($1,$2)
                ON CONFLICT DO NOTHING",
            )
            .bind(id)
            .bind(tag)
            .execute(&mut *tx)
            .await?;
        }
    }
    tx.commit().await?;
    Ok(Some(id))
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let uri = request.uri().clone();
    let raw_id = uri
        .path()
        .strip_prefix("/core/motions/")
        .and_then(|path| path.strip_suffix("/edit"));
    let id = match raw_id {
        Some(id) => match id.parse::<i64>().ok().filter(|_| is_django_integer(id)) {
            Some(id) => Some(id),
            None => return StatusCode::NOT_FOUND.into_response(),
        },
        None => None,
    };
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_authenticated {
        return login::redirect_to_login(&uri);
    }
    let permission = if id.is_some() {
        "change_motion"
    } else {
        "add_motion"
    };
    if !session::has_permission(&state.pool, &user, permission)
        .await
        .unwrap_or(false)
    {
        return web::permission_denied(&state, &user, &uri);
    }
    let mut fields = if let Some(id) = id {
        match Fields::existing(&state.pool, id).await {
            Ok(Some(fields)) => fields,
            Ok(None) => return StatusCode::NOT_FOUND.into_response(),
            Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        }
    } else {
        Fields::default()
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let secure = origin.starts_with("https://");
    let (parts, body) = request.into_parts();
    let mut errors = BTreeMap::new();
    if parts.method == Method::POST {
        let Ok(body) = to_bytes(body, 2_621_440).await else {
            return StatusCode::PAYLOAD_TOO_LARGE.into_response();
        };
        let form = login::form_values(&body);
        if !login::csrf_valid(
            &parts.headers,
            form.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return login::csrf_failure();
        }
        fields = Fields::submitted(&body);
        fields.validate(&state.pool, &mut errors).await;
        if errors.is_empty() {
            match save(&state.pool, id, &fields).await {
                Ok(Some(id)) => {
                    return (
                        StatusCode::FOUND,
                        [(header::LOCATION, format!("/core/motions/{id}"))],
                    )
                        .into_response();
                }
                Ok(None) => {
                    errors.insert("text", "Motion with this Motion text already exists.");
                }
                Err(error) => {
                    eprintln!("Motion save failed: {error}");
                    return StatusCode::INTERNAL_SERVER_ERROR.into_response();
                }
            }
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::messages(&parts.headers, secret))
        .unwrap_or_default();
    let common = web::base_context(&state, &user, &uri);
    match web::render(
        "motions/form.html",
        context! {fields, errors, csrf => token, messages, ..common},
    ) {
        Ok(html) => {
            let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
            login::set_csrf_cookie(&mut response, &csrf, secure);
            if !messages.is_empty() {
                flash::clear(&mut response, secure);
            }
            response
        }
        Err(error) => {
            eprintln!("Motion form template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
