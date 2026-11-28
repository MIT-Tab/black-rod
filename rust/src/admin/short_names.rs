use std::collections::{HashMap, HashSet};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use sqlx::{AnyPool, Row};

use crate::{ApiError, AppState, flash, login, web};

const QUERY: &str = "
WITH missing AS (
  SELECT id,name,short_name FROM core_school
  WHERE NOT temporary AND (short_name IS NULL OR short_name='' OR LOWER(short_name)=LOWER(name))
)
SELECT CAST(s.id AS BIGINT) AS id, s.name, s.short_name,
  (SELECT COUNT(DISTINCT a.id) FROM core_debater d
    JOIN core_team_debaters td ON td.debater_id=d.id JOIN core_toty a ON a.team_id=td.team_id
    WHERE d.school_id=s.id) AS toty_count,
  (SELECT COUNT(a.id) FROM core_debater d JOIN core_soty a ON a.debater_id=d.id
    WHERE d.school_id=s.id) AS soty_count,
  (SELECT COUNT(a.id) FROM core_debater d JOIN core_noty a ON a.debater_id=d.id
    WHERE d.school_id=s.id) AS noty_count,
  (SELECT COUNT(a.id) FROM core_debater d JOIN core_onlinequal a ON a.debater_id=d.id
    WHERE d.school_id=s.id) AS online_qual_count,
  (SELECT COUNT(a.id) FROM core_coty a WHERE a.school_id=s.id) AS coty_count
FROM missing s";

#[derive(Serialize)]
struct School {
    id: i64,
    name: String,
    short_name: String,
    error: Option<String>,
    index: usize,
    toty_count: i64,
    soty_count: i64,
    noty_count: i64,
    online_qual_count: i64,
    coty_count: i64,
    appearance_total: i64,
}

async fn schools(pool: &AnyPool) -> Result<Vec<School>, ApiError> {
    let mut schools = Vec::new();
    for row in sqlx::query(QUERY).fetch_all(pool).await? {
        let counts = [
            "toty_count",
            "soty_count",
            "noty_count",
            "online_qual_count",
            "coty_count",
        ]
        .map(|name| row.try_get::<i64, _>(name))
        .into_iter()
        .collect::<Result<Vec<_>, _>>()?;
        schools.push(School {
            id: row.try_get("id")?,
            name: row.try_get("name")?,
            short_name: row
                .try_get::<Option<String>, _>("short_name")?
                .unwrap_or_default(),
            error: None,
            index: 0,
            toty_count: counts[0],
            soty_count: counts[1],
            noty_count: counts[2],
            online_qual_count: counts[3],
            coty_count: counts[4],
            appearance_total: counts.iter().sum(),
        });
    }
    schools.sort_by(|a, b| {
        b.appearance_total
            .cmp(&a.appearance_total)
            .then(a.name.cmp(&b.name))
    });
    for (i, school) in schools.iter_mut().enumerate() {
        school.index = i;
    }
    Ok(schools)
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = match super::require(state, &uri, request.headers()).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    let mut forms = schools(&state.pool).await?;
    let (parts, body) = request.into_parts();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    if parts.method == Method::POST {
        let values = match login::parse_form(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
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
        let mut index: HashMap<_, _> = forms.iter_mut().map(|school| (school.id, school)).collect();
        let parsed_total = values
            .get("schools-TOTAL_FORMS")
            .and_then(|value| value.parse::<usize>().ok());
        let mut invalid = parsed_total.is_none();
        let mut pending = Vec::new();
        let mut seen = HashSet::new();
        for i in 0..parsed_total.unwrap_or_default().min(2000) {
            let id = values
                .get(&format!("schools-{i}-id"))
                .and_then(|value| value.parse::<i64>().ok());
            let Some(school) = id.and_then(|id| index.get_mut(&id)) else {
                invalid = true;
                continue;
            };
            if !seen.insert(school.id) {
                invalid = true;
                continue;
            }
            let entered = values
                .get(&format!("schools-{i}-short_name"))
                .map_or("", |value| value.trim());
            if entered.is_empty() {
                school.error = Some("This field is required.".to_owned());
            } else if entered.chars().count() > 64 {
                school.error = Some(format!(
                    "Ensure this value has at most 64 characters (it has {}).",
                    entered.chars().count()
                ));
            } else if entered.contains('\0') {
                school.error = Some("Null characters are not allowed.".to_owned());
            }
            invalid |= school.error.is_some();
            if school.short_name != entered {
                pending.push((school.id, entered, school.short_name.clone()));
                school.short_name = entered.to_owned();
            }
        }
        if !invalid {
            let mut changed = 0;
            let mut tx = state.pool.begin().await?;
            for (id, short_name, prior) in pending {
                changed+=sqlx::query("UPDATE core_school SET short_name=$2 WHERE id=$1 AND COALESCE(short_name,'')=$3")
                    .bind(id).bind(short_name).bind(prior).execute(&mut *tx).await?.rows_affected();
            }
            tx.commit().await?;
            let mut response =
                (StatusCode::FOUND, [(header::LOCATION, uri.path())]).into_response();
            if let Some(secret) = state.secret.as_deref() {
                let message = if changed > 0 {
                    format!("Saved {changed} short name(s).")
                } else {
                    "No updates were needed.".to_owned()
                };
                flash::push(
                    &mut response,
                    &parts.headers,
                    secret,
                    if changed > 0 { 25 } else { 20 },
                    &message,
                    secure,
                );
            }
            return Ok(response);
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let common = web::base_context(state, &user, &uri);
    let bindings =
        context! {total=>forms.len(),has_results=>!forms.is_empty(),formset=>forms,csrf=>token};
    let html = match web::render(
        "admin/school_short_name_audit.html",
        context! {..bindings,..common},
    ) {
        Ok(html) => html,
        Err(reason) => {
            eprintln!("School short name audit failed: {reason}");
            return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("School short name audit failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
