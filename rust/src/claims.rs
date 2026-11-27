use std::collections::HashMap;

use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{Duration, NaiveDateTime, Utc};
use minijinja::context;
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState, flash, login,
    session::{self, User},
    web,
};

const PROFILE: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name,
        COALESCE(d.first_season, '') AS first_season, COALESCE(d.latest_season,'') AS latest_season,
        CAST(d.status AS BIGINT) AS status, d.paradigm,
        CAST(CASE WHEN d.dino_to_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS outreach,
        CAST(CASE WHEN d.dino_judge_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS judge,
        CAST(s.id AS BIGINT) AS school_id, s.name AS school
    FROM core_debater d LEFT JOIN core_school s ON d.school_id = s.id
    WHERE d.user_id = $1 AND NOT d.temporary AND NOT d.synthetic ORDER BY d.latest_season DESC
";
const CLAIMS: &str = "
    SELECT CAST(c.id AS BIGINT) AS id, c.status, c.denial_reason,
        CAST(c.created_at AS TEXT) AS created_at, CAST(c.processed_at AS TEXT) AS processed_at,
        CAST(d.id AS BIGINT) AS debater_id, TRIM(d.first_name || ' ' || d.last_name) AS debater_name,
        d.first_season, d.latest_season, CAST(s.id AS BIGINT) AS school_id, s.name AS school,
        TRIM(u.first_name || ' ' || u.last_name) AS requested_name, u.username AS requested_username,
        u.email AS requested_email, TRIM(p.first_name || ' ' || p.last_name) AS processed_name, p.username AS processed_username
    FROM core_claimdebaterrequest c JOIN core_debater d ON d.id = c.debater_id
    JOIN core_user u ON u.id = c.requested_by_id LEFT JOIN core_user p ON p.id = c.processed_by_id
    LEFT JOIN core_school s ON s.id = d.school_id
";

fn school(row: &AnyRow) -> Result<Option<Value>, ApiError> {
    let id: Option<i64> = row.try_get("school_id")?;
    let name: Option<&str> = row.try_get("school")?;
    Ok(id
        .zip(name)
        .map(|(id, name)| json!({"id":id,"name":name,"url":format!("/core/schools/{id}")})))
}

pub(crate) fn date(value: Option<&str>, fmt: &str) -> String {
    value
        .and_then(|input| {
            let naive = input.split('+').next().unwrap_or(input);
            NaiveDateTime::parse_from_str(naive, "%Y-%m-%d %H:%M:%S%.f")
                .ok()
                .or_else(|| NaiveDateTime::parse_from_str(naive, "%Y-%m-%dT%H:%M:%S%.f").ok())
        })
        .map(|date| date.format(fmt).to_string())
        .unwrap_or_default()
}

fn claim(row: &AnyRow) -> Result<Value, ApiError> {
    let id: i64 = row.try_get("id")?;
    let debater: i64 = row.try_get("debater_id")?;
    let created = row.try_get::<Option<&str>, _>("created_at")?;
    let processed = row.try_get::<Option<&str>, _>("processed_at")?;
    let requested_username: &str = row.try_get("requested_username")?;
    let requested_name: &str = row.try_get("requested_name")?;
    let processed_username = row
        .try_get::<Option<&str>, _>("processed_username")?
        .unwrap_or_default();
    let processed_name = row
        .try_get::<Option<&str>, _>("processed_name")?
        .filter(|name| !name.is_empty())
        .unwrap_or(processed_username);
    Ok(json!({
        "id":id,"status":row.try_get::<&str,_>("status")?, "denial_reason":row.try_get::<&str,_>("denial_reason")?,
        "created_at":date(created,"%b %d, %Y"), "created_at_full":date(created,"%b %d, %Y %H:%M"),
        "processed_at":date(processed,"%b %d, %Y %H:%M"),
        "debater":{"id":debater,"name": row.try_get::<&str,_>("debater_name")?,"school":school(row)?,
            "first_season":row.try_get::<Option<&str>,_>("first_season")?.unwrap_or_default(),
            "latest_season":row.try_get::<Option<&str>,_>("latest_season")?.unwrap_or_default(),
            "url":format!("/core/debaters/{debater}")},
        "requested_by":{"name":if requested_name.is_empty() {requested_username} else {requested_name},
            "email":row.try_get::<&str,_>("requested_email")?},
        "processed_by":{"name":processed_name},
    }))
}

async fn profile_data(
    pool: &AnyPool,
    id: i64,
) -> Result<(Vec<Value>, Vec<Value>, Vec<Value>), ApiError> {
    let rows = sqlx::query(PROFILE).bind(id).fetch_all(pool).await?;
    let mut profiles = Vec::new();
    for row in &rows {
        let id: i64 = row.try_get("id")?;
        let status: i64 = row.try_get("status")?;
        let label = match status {
            0 => json!("Novice"),
            1 => json!("Varsity"),
            2 => json!("Dino"),
            _ => json!(status),
        };
        profiles.push(json!({
            "id":id,"status":label,"is_dino":status==2, "name":row.try_get::<&str,_>("name")?,
            "school":school(row)?,"first_season":row.try_get::<&str,_>("first_season")?,
            "latest_season":row.try_get::<&str,_>("latest_season")?,"paradigm":row.try_get::<Option<&str>,_>("paradigm")?,
            "dino_to_contact_opt_in":row.try_get::<i64,_>("outreach")?!=0,
            "dino_judge_contact_opt_in":row.try_get::<i64,_>("judge")?!=0,
            "url": format!("/core/debaters/{id}"),
        }));
    }
    let sql = format!("{CLAIMS} WHERE c.requested_by_id = $1 ORDER BY c.created_at DESC LIMIT 20");
    let rows = sqlx::query(AssertSqlSafe(sql))
        .bind(id)
        .fetch_all(pool)
        .await?;
    let (mut pending, mut recent) = (Vec::new(), Vec::new());
    for row in &rows {
        let item = claim(row)?;
        if item["status"] == "pending" {
            pending.push(item);
        } else {
            recent.push(item);
        }
    }
    Ok((profiles, pending, recent))
}

pub(crate) fn render(
    state: &AppState,
    user: &User,
    uri: &Uri,
    headers: &HeaderMap,
    template: &str,
    values: minijinja::Value,
    csrf: Option<&str>,
) -> Response {
    let notices = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(headers, secret))
        .unwrap_or_default();
    let common = web::base_context(state, user, uri);
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    let message_context = context! {messages => &notices};
    match web::render(template, context! {..values, ..message_context, ..common}) {
        Ok(html) => {
            let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
            if let Some(csrf) = csrf {
                login::set_csrf_cookie(&mut response, csrf, secure);
            }
            if !notices.is_empty() {
                flash::clear(&mut response, secure);
            }
            response
        }
        Err(error) => {
            eprintln!("Claim profile template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn profile(State(state): State<AppState>, request: Request) -> Response {
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(id) = user.id else {
        return login::redirect_to_login(request.uri());
    };
    let (claimed, pending, recent) = match profile_data(&state.pool, id).await {
        Ok(data) => data,
        Err(error) => {
            eprintln!("Claim profile query failed: {error:?}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    render(
        &state,
        &user,
        request.uri(),
        request.headers(),
        "debater/my_profile.html",
        context! {claimed_debaters=>claimed,pending_requests=>pending,recent_requests=>recent},
        None,
    )
}

async fn rate_limited<'a>(
    executor: impl sqlx::Executor<'a, Database = sqlx::Any>,
    user: i64,
) -> Result<bool, sqlx::Error> {
    let times = sqlx::query_scalar::<_, String>(
        "SELECT CAST(created_at AS TEXT) FROM core_claimdebaterrequest
        WHERE requested_by_id=$1 ORDER BY created_at DESC LIMIT 5",
    )
    .bind(user)
    .fetch_all(executor)
    .await?;
    if times.len() < 5 {
        return Ok(false);
    }
    let mut newer = Utc::now().naive_utc();
    for time in times {
        let naive = time.split('+').next().unwrap_or(&time);
        let Ok(older) = NaiveDateTime::parse_from_str(naive, "%Y-%m-%d %H:%M:%S%.f") else {
            return Ok(false);
        };
        if newer - older >= Duration::days(1) {
            return Ok(false);
        }
        newer = older;
    }
    Ok(true)
}

const INVALID: &str = "Select a valid choice. That choice is not one of the available choices.";
const LIMITED: &str = "You have reached the daily limit for claim requests. Please try again tomorrow or contact an administrator.";

async fn submit_claim(
    pool: &AnyPool,
    user: i64,
    school: i64,
    debater: i64,
) -> Result<Option<&'static str>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    // Lock the requesting user and candidate profile in a stable order.
    sqlx::query("UPDATE core_user SET is_active=is_active WHERE id=$1")
        .bind(user)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE core_debater SET user_id=user_id WHERE id=$1")
        .bind(debater)
        .execute(&mut *tx)
        .await?;
    let candidate = sqlx::query(
        "
        SELECT CAST(d.school_id AS BIGINT) AS school, CAST(d.user_id AS BIGINT) AS owner,
            CAST(CASE WHEN EXISTS(SELECT 1 FROM core_claimdebaterrequest c WHERE c.debater_id=d.id
                AND c.status='pending') THEN 1 ELSE 0 END AS BIGINT) AS pending
        FROM core_debater d WHERE d.id=$1 AND NOT d.synthetic AND NOT d.temporary
    ",
    )
    .bind(debater)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(candidate) = candidate else {
        return Ok(Some(INVALID));
    };
    if candidate.try_get::<Option<i64>, _>("owner")?.is_some() {
        return Ok(Some(
            "This debater has already been claimed by another user.",
        ));
    }
    if candidate.try_get::<Option<i64>, _>("school")? != Some(school) {
        return Ok(Some(
            "The selected debater does not belong to the selected school.",
        ));
    }
    if candidate.try_get::<i64, _>("pending")? != 0 {
        return Ok(Some(
            "A pending claim request already exists for this debater.",
        ));
    }
    if rate_limited(&mut *tx, user).await? {
        return Ok(Some(LIMITED));
    }
    sqlx::query("INSERT INTO core_claimdebaterrequest (requested_by_id,debater_id,status,created_at,denial_reason)
        VALUES ($1,$2,'pending',CURRENT_TIMESTAMP,'')").bind(user).bind(debater).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(None)
}

pub(crate) fn redirect(
    state: &AppState,
    headers: &HeaderMap,
    path: &str,
    level: i32,
    message: &str,
) -> Response {
    let mut response = (StatusCode::FOUND, [(header::LOCATION, path)]).into_response();
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            &mut response,
            headers,
            secret,
            level,
            message,
            state
                .origin(headers)
                .is_some_and(|origin| origin.starts_with("https://")),
        );
    }
    response
}

type Errors = HashMap<&'static str, Vec<&'static str>>;

async fn selected(state: &AppState, field: &str, id: Option<i64>) -> Option<(i64, String)> {
    let id = id?;
    let sql = if field == "school" {
        "SELECT name FROM core_school WHERE id=$1 AND NOT temporary AND NOT synthetic"
    } else {
        "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1 AND NOT temporary AND NOT synthetic"
    };
    sqlx::query_scalar::<_, String>(sql)
        .bind(id)
        .fetch_optional(&state.pool)
        .await
        .ok()
        .flatten()
        .map(|name| (id, name))
}

pub(crate) async fn create(State(state): State<AppState>, request: Request) -> Response {
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(id) = user.id else {
        return login::redirect_to_login(request.uri());
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
    let mut errors = Errors::new();
    let mut school = None;
    let mut debater = None;
    if parts.method == Method::POST {
        let Ok(body) = to_bytes(body, 128 * 1024).await else {
            return StatusCode::PAYLOAD_TOO_LARGE.into_response();
        };
        let fields = login::form_values(&body);
        if !login::csrf_valid(
            &parts.headers,
            fields.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return login::csrf_failure();
        }
        let value = |field| {
            fields
                .get(field)
                .map(|value| value.trim())
                .unwrap_or_default()
        };
        school = selected(&state, "school", value("school").parse().ok()).await;
        debater = selected(&state, "debater", value("debater").parse().ok()).await;
        for (field, choice) in [("school", &school), ("debater", &debater)] {
            if choice.is_none() {
                errors.insert(
                    field,
                    vec![if value(field).is_empty() {
                        "This field is required."
                    } else {
                        INVALID
                    }],
                );
            }
        }
        if let (Some((school, _)), Some((debater, _))) = (&school, &debater) {
            match submit_claim(&state.pool, id, *school, *debater).await {
                Ok(None) => {
                    return redirect(
                        &state,
                        &parts.headers,
                        "/core/my-profile/",
                        25,
                        "Claim request submitted. A site administrator will review it shortly.",
                    );
                }
                Ok(Some(error)) => {
                    let field = if error == LIMITED || error.starts_with("The selected debater") {
                        "non_field_errors"
                    } else {
                        "debater"
                    };
                    errors.insert(field, vec![error]);
                }
                Err(error) => {
                    eprintln!("Claim submission failed: {error}");
                    return StatusCode::INTERNAL_SERVER_ERROR.into_response();
                }
            }
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let limited = rate_limited(&state.pool, id).await.unwrap_or_default();
    render(
        &state,
        &user,
        &parts.uri,
        &parts.headers,
        "debater/claim_debater_request_form.html",
        context! {
            rate_limit_reached=>limited, csrf=>token, errors, school, debater,
        },
        Some(&csrf),
    )
}

async fn review_queue(pool: &AnyPool) -> Result<(Vec<Value>, Vec<Value>), ApiError> {
    let pending = format!("{CLAIMS} WHERE c.status='pending' ORDER BY c.created_at");
    let processed = format!(
        "{CLAIMS} WHERE c.status<>'pending' ORDER BY c.processed_at DESC, c.created_at DESC LIMIT 25"
    );
    let pending = sqlx::query(AssertSqlSafe(pending))
        .fetch_all(pool)
        .await?
        .iter()
        .map(claim)
        .collect::<Result<Vec<_>, _>>()?;
    let processed = sqlx::query(AssertSqlSafe(processed))
        .fetch_all(pool)
        .await?
        .iter()
        .map(claim)
        .collect::<Result<Vec<_>, _>>()?;
    Ok((pending, processed))
}

async fn process_claim(
    pool: &AnyPool,
    id: i64,
    reviewer: i64,
    approve: bool,
    reason: &str,
) -> Result<(i32, String), sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::query("UPDATE core_claimdebaterrequest SET status=status WHERE id=$1")
        .bind(id)
        .execute(&mut *tx)
        .await?;
    let record = sqlx::query(
        "
        SELECT c.status, CAST(d.id AS BIGINT) AS debater, CAST(u.id AS BIGINT) AS requested_by,
               TRIM(d.first_name || ' ' || d.last_name) AS name, u.username, u.email
        FROM core_claimdebaterrequest c JOIN core_debater d ON d.id=c.debater_id
        JOIN core_user u ON u.id=c.requested_by_id WHERE c.id=$1
    ",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(record) = record else {
        return Ok((40, "Claim request could not be found.".to_owned()));
    };
    if record.try_get::<&str, _>("status")? != "pending" {
        return Ok((
            30,
            "This claim request has already been processed.".to_owned(),
        ));
    }
    let (status, level, message) = if approve {
        let debater: i64 = record.try_get("debater")?;
        let requested: i64 = record.try_get("requested_by")?;
        let email: &str = record.try_get("email")?;
        let name: &str = record.try_get("name")?;
        let username: &str = record.try_get("username")?;
        let result = sqlx::query(
            "UPDATE core_debater SET user_id=$2,
            email=CASE WHEN COALESCE(email,'')='' AND $3<>'' THEN $3 ELSE email END
            WHERE id=$1 AND user_id IS NULL",
        )
        .bind(debater)
        .bind(requested)
        .bind(email.trim())
        .execute(&mut *tx)
        .await?;
        if result.rows_affected() == 0 {
            return Ok((
                40,
                format!("{name} has already been claimed by another user."),
            ));
        }
        (
            "approved",
            25,
            format!("Approved claim request. {name} is now linked to {username}."),
        )
    } else {
        ("denied", 20, "Claim request denied.".to_owned())
    };
    sqlx::query("UPDATE core_claimdebaterrequest SET status=$2,processed_by_id=$3,processed_at=CURRENT_TIMESTAMP,
        denial_reason=CASE WHEN $2='denied' THEN $4 ELSE denial_reason END WHERE id=$1")
        .bind(id).bind(status).bind(reviewer).bind(reason).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok((level, message))
}

pub(crate) async fn review(State(state): State<AppState>, request: Request) -> Response {
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(reviewer) = user.id else {
        return login::redirect_to_login(request.uri());
    };
    if !user.is_superuser {
        return web::permission_denied(&state, &user, request.uri());
    }
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
    if parts.method == Method::POST {
        let Ok(body) = to_bytes(body, 128 * 1024).await else {
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
        let path = "/core/claim-debater/review/";
        let action = form.get("action").map(String::as_str);
        let id = form.get("request_id").and_then(|id| id.parse::<i64>().ok());
        let result = if let (Some(id), Some(action @ ("approve" | "deny"))) = (id, action) {
            let reason = form
                .get("denial_reason")
                .map(|value| value.trim())
                .unwrap_or_default();
            process_claim(&state.pool, id, reviewer, action == "approve", reason).await
        } else {
            Ok((40, "Invalid request submission.".to_owned()))
        };
        return match result {
            Ok((level, message)) => redirect(&state, &parts.headers, path, level, &message),
            Err(error) => {
                eprintln!("Claim review failed: {error}");
                StatusCode::INTERNAL_SERVER_ERROR.into_response()
            }
        };
    }
    let Ok((pending, processed)) = review_queue(&state.pool).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    render(
        &state,
        &user,
        &parts.uri,
        &parts.headers,
        "admin/claim_debater_requests.html",
        context! {
            csrf=>token,pending_requests=>pending,processed_requests=>processed,
        },
        Some(&csrf),
    )
}

pub(crate) mod edit;
