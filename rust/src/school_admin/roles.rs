use axum::{
    Json,
    body::to_bytes,
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::{AnyConnection, Row};

use crate::{
    ApiError, AppState, login,
    session::{self, User},
    web,
};

const ADMIN: &str = "SELECT CAST(a.id AS BIGINT) AS id, CAST(a.school_id AS BIGINT) AS school_id,
    CAST(a.user_id AS BIGINT) AS user_id,CAST(CASE WHEN a.\"primary\" THEN 1 ELSE 0 END AS BIGINT) AS \"primary\",
    u.username,u.email FROM core_schooladmin a JOIN core_user u ON u.id=a.user_id";

#[derive(Clone, Copy, PartialEq)]
pub(super) enum Role {
    Superuser,
    Primary,
    Admin,
}

impl Role {
    pub(super) fn name(self) -> &'static str {
        match self {
            Self::Superuser => "superuser",
            Self::Primary => "primary",
            Self::Admin => "admin",
        }
    }
}

pub(super) async fn role(
    conn: &mut AnyConnection,
    user: &User,
    school: i64,
) -> Result<Option<Role>, ApiError> {
    if user.is_superuser {
        return Ok(Some(Role::Superuser));
    }
    let role = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(CASE WHEN \"primary\" THEN 1 ELSE 0 END AS BIGINT)
        FROM core_schooladmin WHERE user_id=$1 AND school_id=$2",
    )
    .bind(user.id)
    .bind(school)
    .fetch_optional(conn)
    .await?;
    Ok(role.map(|value| {
        if value == 1 {
            Role::Primary
        } else {
            Role::Admin
        }
    }))
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error":message}))).into_response()
}

pub(super) fn failed(error: ApiError) -> Response {
    eprintln!("School administration request failed: {error:?}");
    StatusCode::INTERNAL_SERVER_ERROR.into_response()
}

async fn lock(conn: &mut AnyConnection, school: i64) -> Result<bool, ApiError> {
    Ok(
        sqlx::query("UPDATE core_school SET id=id WHERE id=$1 AND NOT temporary")
            .bind(school)
            .execute(conn)
            .await?
            .rows_affected()
            == 1,
    )
}

async fn add(
    state: &AppState,
    user: &User,
    school: i64,
    person: i64,
) -> Result<Response, ApiError> {
    let mut tx = state.pool.begin().await?;
    if !lock(&mut tx, school).await? {
        return Ok(StatusCode::NOT_FOUND.into_response());
    }
    let Some(account) = sqlx::query("SELECT username,email FROM core_user WHERE id=$1")
        .bind(person)
        .fetch_optional(&mut *tx)
        .await?
    else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    if role(&mut tx, user, school).await?.is_none() {
        return Ok(error(
            StatusCode::FORBIDDEN,
            "You cannot manage admins for this school.",
        ));
    }
    let existing=sqlx::query("SELECT CAST(id AS BIGINT) AS id,CAST(CASE WHEN \"primary\" THEN 1 ELSE 0 END AS BIGINT) AS \"primary\"
        FROM core_schooladmin WHERE school_id=$1 AND user_id=$2")
        .bind(school).bind(person).fetch_optional(&mut *tx).await?;
    let (id, primary, created) = if let Some(record) = existing {
        (
            record.try_get::<i64, _>("id")?,
            record.try_get::<i64, _>("primary")? != 0,
            false,
        )
    } else {
        let id=sqlx::query_scalar::<_,i64>("INSERT INTO core_schooladmin (school_id,user_id,\"primary\",created_at)
            VALUES ($1,$2,NOT EXISTS(SELECT 1 FROM core_schooladmin WHERE school_id=$1 AND \"primary\"),CURRENT_TIMESTAMP)
            RETURNING CAST(id AS BIGINT)")
            .bind(school).bind(person).fetch_one(&mut *tx).await?;
        let primary = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(CASE WHEN \"primary\" THEN 1 ELSE 0 END AS BIGINT)
            FROM core_schooladmin WHERE id=$1",
        )
        .bind(id)
        .fetch_one(&mut *tx)
        .await?
            != 0;
        (id, primary, true)
    };
    tx.commit().await?;
    Ok(Json(json!({"success":true,"created":created,"admin":{
        "id":id,"user_id":person,"username":account.try_get::<&str,_>("username")?,
        "email":account.try_get::<&str,_>("email")?,"primary":primary,
    }}))
    .into_response())
}

async fn modify(
    state: &AppState,
    user: &User,
    admin: i64,
    promote: bool,
) -> Result<Response, ApiError> {
    let mut tx = state.pool.begin().await?;
    let Some(school) = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(school_id AS BIGINT) FROM core_schooladmin WHERE id=$1",
    )
    .bind(admin)
    .fetch_optional(&mut *tx)
    .await?
    else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    if !lock(&mut tx, school).await? {
        return Ok(StatusCode::NOT_FOUND.into_response());
    }
    let Some(record)=sqlx::query("SELECT CAST(user_id AS BIGINT) AS user_id,
        CAST(CASE WHEN \"primary\" THEN 1 ELSE 0 END AS BIGINT) AS \"primary\" FROM core_schooladmin WHERE id=$1 AND school_id=$2")
        .bind(admin).bind(school).fetch_optional(&mut *tx).await? else{return Ok(StatusCode::NOT_FOUND.into_response())};
    let Some(role) = role(&mut tx, user, school).await? else {
        return Ok(error(
            StatusCode::FORBIDDEN,
            "You cannot manage admins for this school.",
        ));
    };
    let target = record.try_get::<i64, _>("user_id")?;
    let self_service = Some(target) == user.id;
    if promote {
        if role == Role::Admin {
            return Ok(error(
                StatusCode::FORBIDDEN,
                "Only primaries or superusers can set the primary.",
            ));
        }
        if role == Role::Primary && self_service {
            return Ok(error(
                StatusCode::BAD_REQUEST,
                "You are already the primary admin.",
            ));
        }
        let previous = sqlx::query_scalar::<_, i64>(
            "UPDATE core_schooladmin SET \"primary\"=FALSE
            WHERE school_id=$1 AND id<>$2 AND \"primary\" RETURNING CAST(id AS BIGINT)",
        )
        .bind(school)
        .bind(admin)
        .fetch_optional(&mut *tx)
        .await?;
        sqlx::query("UPDATE core_schooladmin SET \"primary\"=TRUE WHERE id=$1")
            .bind(admin)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(Json(json!({"success":true,"primary_admin_id":admin,"demoted_admin_id":previous,"school_id":school})).into_response())
    } else {
        if role == Role::Admin && !self_service {
            return Ok(error(
                StatusCode::FORBIDDEN,
                "You can only remove yourself.",
            ));
        }
        if role == Role::Primary && self_service {
            return Ok(error(
                StatusCode::BAD_REQUEST,
                "Transfer primary status before removing yourself.",
            ));
        }
        if record.try_get::<i64, _>("primary")? != 0 {
            return Ok(error(
                StatusCode::BAD_REQUEST,
                "Assign a new primary before removing this admin.",
            ));
        }
        sqlx::query("DELETE FROM core_schooladmin WHERE id=$1")
            .bind(admin)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(Json(json!({"success":true})).into_response())
    }
}

async fn save(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(origin) = state.origin(request.headers()) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let (parts, body) = request.into_parts();
    let Ok(bytes) = to_bytes(body, 2_621_440).await else {
        return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
    };
    let fields = login::form_values(&bytes);
    if !login::csrf_valid(
        &parts.headers,
        fields.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return Ok(login::csrf_failure());
    }
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    if !user.is_superuser && !user.is_school_admin {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let raw = |key| fields.get(key).map_or("", String::as_str);
    if uri.path().ends_with("/add/") {
        let (school, person) = (raw("school_id"), raw("user_id"));
        if school.is_empty() || person.is_empty() {
            return Ok(error(
                StatusCode::BAD_REQUEST,
                "Missing school_id or user_id",
            ));
        }
        let (Ok(school), Ok(person)) = (school.parse::<i64>(), person.parse::<i64>()) else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        add(state, &user, school, person).await
    } else {
        let id = raw("school_admin_id");
        if id.is_empty() {
            return Ok(error(StatusCode::BAD_REQUEST, "Missing school_admin_id"));
        }
        let Ok(id) = id.parse::<i64>() else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        modify(state, &user, id, uri.path().ends_with("/primary/")).await
    }
}

pub(crate) async fn action(State(state): State<AppState>, request: Request) -> Response {
    save(&state, request).await.unwrap_or_else(failed)
}

pub(super) async fn admins(conn: &mut AnyConnection, school: i64) -> Result<Vec<Value>, ApiError> {
    let query =
        format!("{ADMIN} WHERE a.school_id=$1 ORDER BY a.\"primary\" DESC,LOWER(u.username)");
    let rows = sqlx::query(sqlx::AssertSqlSafe(query))
        .bind(school)
        .fetch_all(conn)
        .await?;
    rows.iter().map(|row| Ok(json!({
        "id":row.try_get::<i64,_>("id")?, "user_id":row.try_get::<i64,_>("user_id")?,
        "user":{"id":row.try_get::<i64,_>("user_id")?, "username":row.try_get::<&str,_>("username")?,
            "email":row.try_get::<&str,_>("email")?},
        "primary":row.try_get::<i64,_>("primary")?!=0,
    }))).collect()
}

async fn lookup(
    state: &AppState,
    uri: &axum::http::Uri,
    headers: &HeaderMap,
) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    if !user.is_superuser && !user.is_school_admin {
        return Ok(Json(json!({"results":[],"pagination":{"more":false}})).into_response());
    }
    let params = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<std::collections::HashMap<_, _>>();
    let page = params
        .get("page")
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(1)
        .max(1);
    let term = params
        .get("q")
        .map_or("", String::as_str)
        .replace('!', "!!")
        .replace('%', "!%")
        .replace('_', "!_");
    let rows = sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id,username,email FROM core_user
        WHERE LOWER(username) LIKE LOWER($1) ESCAPE '!' OR LOWER(email) LIKE LOWER($1) ESCAPE '!'
        OR LOWER(first_name) LIKE LOWER($1) ESCAPE '!' OR LOWER(last_name) LIKE LOWER($1) ESCAPE '!'
        ORDER BY username LIMIT 11 OFFSET $2",
    )
    .bind(format!("%{term}%"))
    .bind(page.saturating_sub(1).saturating_mul(10))
    .fetch_all(&state.pool)
    .await?;
    let results = rows
        .iter()
        .take(10)
        .map(|row| {
            let id = row.try_get::<i64, _>("id")?.to_string();
            let name = row.try_get::<&str, _>("username")?;
            let email = row.try_get::<&str, _>("email")?;
            let label = if email.is_empty() {
                name.to_owned()
            } else {
                format!("{name} ({email})")
            };
            Ok::<Value, sqlx::Error>(json!({"id":id,"text":label,"selected_text":label}))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Json(json!({"results":results,"pagination":{"more":rows.len()>10}})).into_response())
}

pub(crate) async fn autocomplete(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    lookup(&state, &uri, &headers).await.unwrap_or_else(failed)
}
