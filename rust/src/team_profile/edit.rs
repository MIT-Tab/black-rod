use std::collections::{BTreeMap, BTreeSet};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use minijinja::context;
use sqlx::Row;

use crate::{ApiError, AppState, claims, is_django_integer, login, session, web};

async fn view(state: &AppState, request: Request, id: i64) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    if !session::has_permission(&state.pool, &user, "change_team").await? {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let Some(mut short_name) =
        sqlx::query_scalar::<_, String>("SELECT short_name FROM core_team WHERE id=$1")
            .bind(id)
            .fetch_optional(&state.pool)
            .await?
    else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let mut people = BTreeMap::new();
    let (parts, body) = request.into_parts();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|url| url.starts_with("https://"));
    let mut errors = BTreeMap::new();
    if parts.method == Method::POST {
        let bytes = match login::form_fields(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let values = bytes
            .iter()
            .cloned()
            .collect::<std::collections::HashMap<_, _>>();
        let Some(origin) = state.origin(&parts.headers) else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            &parts.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        short_name = values
            .get("short_name")
            .map_or("", String::as_str)
            .trim()
            .to_owned();
        if short_name.is_empty() {
            errors.insert("short_name", "This field is required.".to_owned());
        }
        if short_name.chars().count() > 128 {
            errors.insert(
                "short_name",
                format!(
                    "Ensure this value has at most 128 characters (it has {}).",
                    short_name.chars().count()
                ),
            );
        }
        if short_name.contains('\0') {
            errors.insert("short_name", "Null characters are not allowed.".to_owned());
        }
        let mut ids = BTreeSet::new();
        for (_, value) in bytes.iter().filter(|(name, _)| name == "debaters") {
            if let Ok(id) = value.parse::<i64>() {
                ids.insert(id);
            } else {
                errors.insert("debaters", format!("“{value}” is not a valid value."));
            }
        }
        for id in ids {
            let name = sqlx::query_scalar::<_,String>(
                "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1 AND NOT synthetic AND NOT temporary")
                .bind(id).fetch_optional(&state.pool).await?;
            if let Some(name) = name {
                people.insert(id, name);
            } else {
                errors.insert(
                    "debaters",
                    format!("Select a valid choice. {id} is not one of the available choices."),
                );
            }
        }
        if !errors.contains_key("debaters") {
            if people.is_empty() {
                errors.insert("debaters", "This field is required.".to_owned());
            } else if people.len() != 2 {
                errors.insert("form", "All teams must have 2 debaters".to_owned());
            }
        }
        if errors.is_empty() {
            let mut tx = state.pool.begin().await?;
            let updated = sqlx::query("UPDATE core_team SET short_name=$2 WHERE id=$1")
                .bind(id)
                .bind(&short_name)
                .execute(&mut *tx)
                .await?;
            if updated.rows_affected() == 0 {
                return Ok(StatusCode::NOT_FOUND.into_response());
            }
            let current = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(debater_id AS BIGINT) FROM core_team_debaters WHERE team_id=$1",
            )
            .bind(id)
            .fetch_all(&mut *tx)
            .await?;
            for person in current.iter().filter(|person| !people.contains_key(person)) {
                // The edit form can change only the roster members it can display.
                sqlx::query("DELETE FROM core_team_debaters WHERE team_id=$1 AND debater_id=$2
                    AND EXISTS (SELECT 1 FROM core_debater d WHERE d.id=$2 AND NOT d.synthetic AND NOT d.temporary)")
                    .bind(id).bind(person).execute(&mut *tx).await?;
            }
            for person in people.keys().filter(|person| !current.contains(person)) {
                sqlx::query("INSERT INTO core_team_debaters (team_id,debater_id) VALUES ($1,$2) ON CONFLICT DO NOTHING")
                    .bind(id).bind(person).execute(&mut *tx).await?;
            }
            claims::edit::team_name(&mut tx, id).await?;
            tx.commit().await?;
            return Ok((
                StatusCode::FOUND,
                [(header::LOCATION, format!("/core/teams/{id}"))],
            )
                .into_response());
        }
    } else {
        for row in sqlx::query(
            "SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name
            FROM core_debater d JOIN core_team_debaters m ON m.debater_id=d.id
            WHERE m.team_id=$1 AND NOT d.synthetic AND NOT d.temporary",
        )
        .bind(id)
        .fetch_all(&state.pool)
        .await?
        {
            people.insert(row.try_get("id")?, row.try_get("name")?);
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let debaters = people.into_iter().collect::<Vec<_>>();
    let fields = context! {debaters,short_name,errors,csrf=>token};
    let mut response = super::render_page(state, id, &uri, &parts.headers, Some(fields)).await;
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let path = request.uri().path();
    let raw = path
        .strip_prefix("/core/teams/")
        .and_then(|value| value.strip_suffix("/edit"))
        .unwrap_or_default();
    let Some(id) = raw.parse::<i64>().ok().filter(|_| is_django_integer(raw)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    view(&state, request, id).await.unwrap_or_else(|error| {
        eprintln!("Team edit failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
