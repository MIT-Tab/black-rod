pub(crate) mod debater;
pub(crate) mod roles;

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};
use sqlx::{AnyConnection, Row};

use crate::{ApiError, AppState, login, session, team_profile::read_transaction, web};

const MANAGED: &str = "SELECT CAST(s.id AS BIGINT) AS id,s.name
    FROM core_school s JOIN core_schooladmin a ON a.school_id=s.id
    WHERE a.user_id=$1 AND NOT s.temporary ORDER BY s.name";
const ALL_SCHOOLS: &str = "SELECT CAST(s.id AS BIGINT) AS id,s.name,
    (SELECT COUNT(*) FROM core_debater d WHERE d.school_id=s.id AND d.latest_season >= $1) AS active
    FROM core_school s WHERE NOT s.temporary ORDER BY active DESC,s.name LIMIT 15 OFFSET $2";
const ROSTER: &str =
    "SELECT CAST(d.id AS BIGINT) AS id,d.first_name,d.last_name,d.first_season,d.latest_season,
    CAST(d.status AS BIGINT) AS status FROM core_debater d
    WHERE d.school_id=$1 AND d.latest_season >= $2 AND NOT d.synthetic AND NOT d.temporary
    ORDER BY d.latest_season DESC,d.last_name,d.first_name";

async fn roster(
    conn: &mut AnyConnection,
    school: i64,
    after: &str,
) -> Result<Vec<Value>, ApiError> {
    let rows = sqlx::query(ROSTER)
        .bind(school)
        .bind(after)
        .fetch_all(conn)
        .await?;
    rows.iter()
        .map(|row| {
            Ok(json!({
                "id":row.try_get::<i64,_>("id")?,"first_name":row.try_get::<&str,_>("first_name")?,
                "last_name":row.try_get::<&str,_>("last_name")?,
                "first_season":row.try_get::<Option<&str>,_>("first_season")?.unwrap_or("None"),
                "latest_season":row.try_get::<Option<&str>,_>("latest_season")?.unwrap_or("None"),
                "status":row.try_get::<i64,_>("status")?,
            }))
        })
        .collect()
}

async fn page(state: &AppState, uri: &Uri, headers: &HeaderMap) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(uri));
    }
    let management = uri.path() == "/core/manage-school-admins/";
    if (management && !user.is_superuser) || (!management && !user.is_school_admin) {
        return Ok(web::permission_denied(state, &user, uri));
    }
    let mut tx = read_transaction(&state.pool).await?;
    let after = (state.current_season - 6).to_string();
    let (template, view) = if management {
        let count =
            sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM core_school WHERE NOT temporary")
                .fetch_one(&mut *tx)
                .await?;
        let pages = ((count + 14) / 15).max(1);
        let number = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
            .filter(|(name, _)| name == "page")
            .last()
            .and_then(|(_, value)| value.parse::<i64>().ok())
            .map_or(1, |page| if page <= 0 { pages } else { page.min(pages) });
        let rows = sqlx::query(ALL_SCHOOLS)
            .bind(&after)
            .bind((number - 1) * 15)
            .fetch_all(&mut *tx)
            .await?;
        let mut school_data = Vec::new();
        for record in rows {
            let id: i64 = record.try_get("id")?;
            school_data.push(json!({
                "school":{"id":id,"name":record.try_get::<&str,_>("name")?},
                "active_debater_count":record.try_get::<i64,_>("active")?,
                "admins":roles::admins(&mut tx,id).await?,
            }));
        }
        (
            "school_admin/superuser_management.html",
            context! {school_data,
            page_obj=>json!({"number":number,"has_other_pages":pages>1,"has_previous":number>1,
                "has_next":number<pages,"previous_page_number":number-1,"next_page_number":number+1,
                "paginator":{"num_pages":pages}})},
        )
    } else {
        let rows = sqlx::query(MANAGED)
            .bind(user.id)
            .fetch_all(&mut *tx)
            .await?;
        let mut schools = Vec::new();
        for record in rows {
            let id: i64 = record.try_get("id")?;
            let admins = roles::admins(&mut tx, id).await?;
            let current = admins
                .iter()
                .find(|admin| admin["user_id"].as_i64() == user.id)
                .cloned();
            let role = roles::role(&mut tx, &user, id)
                .await?
                .map(roles::Role::name);
            schools.push(json!({
                "id":id,"name":record.try_get::<&str,_>("name")?,"admins":admins,
                "current":current,"role":role,"roster":roster(&mut tx,id,&after).await?,
            }));
        }
        (
            "school_admin/dashboard.html",
            context! {schools,six_years_ago=>after,is_superuser=>user.is_superuser},
        )
    };
    tx.commit().await?;
    let Ok((csrf, token)) = login::csrf_tokens(headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let base = web::base_context(state, &user, uri);
    let security = context! {csrf=>token};
    let html = web::render(template, context! {..security,..view,..base}).map_err(|error| {
        eprintln!("School administration template failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(
        &mut response,
        &csrf,
        state
            .origin(headers)
            .is_some_and(|value| value.starts_with("https://")),
    );
    Ok(response)
}

pub(crate) async fn dashboard(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    page(&state, &uri, &headers)
        .await
        .unwrap_or_else(roles::failed)
}
