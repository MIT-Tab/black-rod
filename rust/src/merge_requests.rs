use std::collections::HashMap;

use axum::{
    Json,
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState,
    agent::{merge, mutation::Failure},
    claims, login, session, web,
};

const CREATE: &str = "/core/merge-debaters/request/";
const REVIEW: &str = "/core/merge-debaters/review/";
const INVALID: &str = "Select a valid choice. That choice is not one of the available choices.";
const DUPLICATE: &str = "A pending merge request already exists for these debaters.";
type Errors = HashMap<&'static str, Vec<&'static str>>;

#[derive(Clone, Serialize)]
pub(crate) struct Person {
    pub(crate) id: i64,
    pub(crate) name: String,
    school: Option<School>,
    season: Option<String>,
    url: String,
    counts: Counts,
}

#[derive(Clone, Serialize)]
struct School {
    id: i64,
    name: String,
}

#[derive(Clone, Serialize)]
struct Counts {
    team_results: i64,
    speaker_results: i64,
    round_stats: i64,
    videos: i64,
}

const PERSON: &str="
    SELECT CAST(d.id AS BIGINT) AS id,TRIM(d.first_name || ' ' || d.last_name) AS display_name,
        CAST(d.school_id AS BIGINT) AS school_id,d.latest_season,s.name AS school_name,
        (SELECT COUNT(DISTINCT r.id) FROM core_team_debaters m JOIN core_teamresult r ON r.team_id=m.team_id WHERE m.debater_id=d.id) AS teams,
        (SELECT COUNT(*) FROM core_speakerresult r WHERE r.debater_id=d.id) AS speakers,
        (SELECT COUNT(*) FROM core_roundstats r WHERE r.debater_id=d.id) AS rounds,
        (SELECT COUNT(*) FROM core_video v WHERE v.pm_id=d.id OR v.mg_id=d.id OR v.lo_id=d.id OR v.mo_id=d.id) AS videos
    FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
";

impl Person {
    fn read(row: AnyRow) -> Result<Person, sqlx::Error> {
        let id = row.try_get("id")?;
        let school_id: Option<i64> = row.try_get("school_id")?;
        let school_name: Option<String> = row.try_get("school_name")?;
        Ok(Self {
            id,
            name: row.try_get("display_name")?,
            season: row.try_get("latest_season")?,
            school: school_id
                .zip(school_name)
                .map(|(id, name)| School { id, name }),
            url: format!("/core/debaters/{id}"),
            counts: Counts {
                team_results: row.try_get("teams")?,
                speaker_results: row.try_get("speakers")?,
                round_stats: row.try_get("rounds")?,
                videos: row.try_get("videos")?,
            },
        })
    }

    fn school_name(&self) -> &str {
        self.school
            .as_ref()
            .map_or("", |school| school.name.as_str())
    }
}

pub(crate) async fn person(
    conn: &mut AnyConnection,
    id: i64,
    active: bool,
) -> Result<Option<Person>, ApiError> {
    let statement = format!(
        "{PERSON} WHERE d.id=$1{}",
        if active {
            " AND NOT(d.temporary OR d.synthetic)"
        } else {
            ""
        }
    );
    Ok(sqlx::query(AssertSqlSafe(statement))
        .bind(id)
        .fetch_optional(conn)
        .await?
        .map(Person::read)
        .transpose()?)
}

pub(crate) async fn queue(
    conn: &mut AnyConnection,
    user: i64,
    primary: &Person,
    secondary: &Person,
) -> Result<bool, ApiError> {
    let first = primary.id.min(secondary.id);
    let last = primary.id.max(secondary.id);
    sqlx::query("UPDATE core_debater SET id=id WHERE id=$1")
        .bind(first)
        .execute(&mut *conn)
        .await?;
    sqlx::query("UPDATE core_debater SET id=id WHERE id=$1")
        .bind(last)
        .execute(&mut *conn)
        .await?;
    let saved=sqlx::query("INSERT INTO core_mergedebaterrequest (
        requested_by_id,primary_debater_id,secondary_debater_id,primary_name,secondary_name,
        primary_school_name,secondary_school_name,status,created_at,denial_reason)
        SELECT $1,$2,$3,$4,$5,$6,$7,'pending',CURRENT_TIMESTAMP,''
        WHERE NOT EXISTS (SELECT 1 FROM core_mergedebaterrequest WHERE status='pending'
          AND ((primary_debater_id=$2 AND secondary_debater_id=$3) OR (primary_debater_id=$3 AND secondary_debater_id=$2)))")
        .bind(user).bind(primary.id).bind(secondary.id).bind(&primary.name).bind(&secondary.name)
        .bind(primary.school_name()).bind(secondary.school_name()).execute(conn).await?;
    Ok(saved.rows_affected() == 1)
}

async fn requests(
    state: &AppState,
    user: Option<i64>,
) -> Result<(Vec<Value>, Vec<Value>), ApiError> {
    let statement =
        "SELECT CAST(r.id AS BIGINT) AS id,CAST(r.primary_debater_id AS BIGINT) AS first,
        CAST(r.secondary_debater_id AS BIGINT) AS second,r.primary_name,r.secondary_name,
        r.primary_school_name,r.secondary_school_name,CAST(r.created_at AS TEXT) AS created_at,
        CAST(r.processed_at AS TEXT) AS processed_at,r.status,r.denial_reason,
        COALESCE(NULLIF(TRIM(u.first_name || ' ' || u.last_name),''),u.username) AS requested_by,
        COALESCE(NULLIF(TRIM(p.first_name || ' ' || p.last_name),''),p.username) AS processed_by
        FROM core_mergedebaterrequest r JOIN core_user u ON u.id=r.requested_by_id
        LEFT JOIN core_user p ON p.id=r.processed_by_id";
    let mut conn = state.pool.acquire().await?;
    let rows = if let Some(user) = user {
        sqlx::query(AssertSqlSafe(format!(
            "{statement} WHERE r.requested_by_id=$1 ORDER BY r.created_at DESC,r.id DESC LIMIT 10"
        )))
        .bind(user)
        .fetch_all(&mut *conn)
        .await?
    } else {
        let pending = sqlx::query(AssertSqlSafe(format!(
            "{statement} WHERE r.status='pending' ORDER BY r.created_at,r.id"
        )))
        .fetch_all(&mut *conn)
        .await?;
        let processed = sqlx::query(AssertSqlSafe(format!(
            "{statement} WHERE r.status<>'pending'
            ORDER BY r.processed_at DESC NULLS LAST,r.created_at DESC,r.id DESC LIMIT 25"
        )))
        .fetch_all(&mut *conn)
        .await?;
        pending.into_iter().chain(processed).collect()
    };
    let mut pending = Vec::new();
    let mut recent = Vec::new();
    for row in rows {
        let first = match row.try_get::<Option<i64>, _>("first")? {
            Some(id) => person(&mut conn, id, false).await?,
            None => None,
        };
        let second = match row.try_get::<Option<i64>, _>("second")? {
            Some(id) => person(&mut conn, id, false).await?,
            None => None,
        };
        let date = |column| -> Result<String, sqlx::Error> {
            Ok(claims::date(
                row.try_get::<Option<&str>, _>(column)?,
                "%b %d, %Y %H:%M",
            ))
        };
        let fallback = |column| -> Result<&str, sqlx::Error> {
            let value = row.try_get::<&str, _>(column)?;
            Ok(if value.is_empty() { "(deleted)" } else { value })
        };
        let status = row.try_get::<&str, _>("status")?;
        let item = json!({
            "instance":{"id":row.try_get::<i64,_>("id")?,"created_at":date("created_at")?,"processed_at":date("processed_at")?,
                "primary_debater":first,"secondary_debater":second,"status":status,"denial_reason":row.try_get::<&str,_>("denial_reason")?,
                "requested_by":row.try_get::<&str,_>("requested_by")?,"processed_by":row.try_get::<Option<&str>,_>("processed_by")?},
            "primary_display_name":first.as_ref().map(|row|row.name.as_str()).unwrap_or(fallback("primary_name")?),
            "secondary_display_name":second.as_ref().map(|row|row.name.as_str()).unwrap_or(fallback("secondary_name")?),
            "primary_display_school":first.as_ref().and_then(|row|row.school.as_ref()).map(|school|school.name.as_str())
                .unwrap_or(row.try_get::<&str,_>("primary_school_name")?),
            "secondary_display_school":second.as_ref().and_then(|row|row.school.as_ref()).map(|school|school.name.as_str())
                .unwrap_or(row.try_get::<&str,_>("secondary_school_name")?),
            "primary_counts":first.as_ref().map(|row|&row.counts),
            "secondary_counts":second.as_ref().map(|row|&row.counts),
        });
        if status == "pending" {
            pending.push(item);
        } else {
            recent.push(item);
        }
    }
    Ok((pending, recent))
}

async fn schools(state: &AppState, user: i64, restricted: bool) -> Result<Vec<School>, ApiError> {
    let restriction = if restricted {
        " AND EXISTS(SELECT 1 FROM core_schooladmin admin WHERE admin.school_id=s.id AND admin.user_id=$3)"
    } else {
        ""
    };
    let sql=format!("SELECT CAST(s.id AS BIGINT),s.name FROM core_school s WHERE NOT s.temporary
        AND EXISTS(SELECT 1 FROM core_debater d WHERE d.school_id=s.id AND NOT(d.temporary OR d.synthetic) AND d.latest_season IN ($1,$2))
        {restriction} ORDER BY s.name");
    let mut query = sqlx::query_as::<_, (i64, String)>(AssertSqlSafe(sql))
        .bind(state.current_season.to_string())
        .bind((state.current_season - 1).to_string());
    if restricted {
        query = query.bind(user);
    }
    Ok(query
        .fetch_all(&state.pool)
        .await?
        .into_iter()
        .map(|(id, name)| School { id, name })
        .collect())
}

async fn mutate_review(
    state: &AppState,
    actor: i64,
    values: &HashMap<String, String>,
) -> Result<(i32, String), ApiError> {
    let action = values.get("action").map_or("", String::as_str);
    let Some(id) = values
        .get("request_id")
        .and_then(|id| id.parse::<i64>().ok())
        .filter(|_| matches!(action, "approve" | "deny" | "swap"))
    else {
        return Ok((40, "Invalid request submission.".to_owned()));
    };
    let mut tx = state.pool.begin().await?;
    sqlx::query("UPDATE core_mergedebaterrequest SET id=id WHERE id=$1")
        .bind(id)
        .execute(&mut *tx)
        .await?;
    let row=sqlx::query("SELECT status,CAST(primary_debater_id AS BIGINT) AS first,CAST(secondary_debater_id AS BIGINT) AS second
        FROM core_mergedebaterrequest WHERE id=$1").bind(id).fetch_optional(&mut *tx).await?;
    let Some(row) = row else {
        return Ok((40, "Merge request could not be found.".to_owned()));
    };
    if row.try_get::<&str, _>("status")? != "pending" {
        return Ok((
            30,
            "This merge request has already been processed.".to_owned(),
        ));
    }
    if action == "swap" {
        sqlx::query("UPDATE core_mergedebaterrequest SET primary_debater_id=secondary_debater_id,
            secondary_debater_id=primary_debater_id, primary_name=secondary_name, secondary_name=primary_name,
            primary_school_name=secondary_school_name, secondary_school_name=primary_school_name WHERE id=$1")
            .bind(id).execute(&mut *tx).await?;
        tx.commit().await?;
        return Ok((25, "Debaters swapped successfully.".to_owned()));
    }
    let first = match row.try_get::<Option<i64>, _>("first")? {
        Some(id) => person(&mut tx, id, false).await?,
        None => None,
    };
    let second = match row.try_get::<Option<i64>, _>("second")? {
        Some(id) => person(&mut tx, id, false).await?,
        None => None,
    };
    let approved = action == "approve";
    let seasons = if approved {
        let Some((primary, secondary)) = first.as_ref().zip(second.as_ref()) else {
            return Ok((40,"Merge failed: One of the selected debaters no longer exists. Please refresh the page and try again.".to_owned()));
        };
        match merge::merge_debaters(&mut tx, primary.id, secondary.id).await {
            Ok(seasons) => seasons,
            Err(error) => {
                let detail=match error{
                    Failure::Invalid(message)=>format!("Merge failed: {message}"),
                    Failure::Database(ApiError::Constraint)=>"Merge failed: A database constraint was violated. This usually means the debaters have conflicting records that cannot be automatically merged. Please contact a technical administrator for assistance.".to_owned(),
                    _=>{eprintln!("Merge request did not complete");"An unexpected error occurred while processing the merge request. The merge has been cancelled and no changes were made. Please contact a technical administrator if this problem persists.".to_owned()}
                };
                return Ok((40, detail));
            }
        }
    } else {
        Vec::new()
    };
    let reason = if approved {
        ""
    } else {
        values
            .get("denial_reason")
            .map_or("", String::as_str)
            .trim()
    };
    sqlx::query("UPDATE core_mergedebaterrequest SET status=$2,denial_reason=$3,processed_by_id=$4,processed_at=CURRENT_TIMESTAMP,
        primary_name=COALESCE($5,primary_name),primary_school_name=COALESCE($6,primary_school_name),
        secondary_name=COALESCE($7,secondary_name),secondary_school_name=COALESCE($8,secondary_school_name),
        secondary_debater_id=CASE WHEN CAST($9 AS BOOLEAN) THEN NULL ELSE secondary_debater_id END WHERE id=$1")
        .bind(id).bind(if approved{"approved"}else{"denied"}).bind(reason).bind(actor)
        .bind(first.as_ref().map(|row|&row.name)).bind(first.as_ref().and_then(|row|row.school.as_ref()).map(|row|&row.name))
        .bind(second.as_ref().map(|row|&row.name)).bind(second.as_ref().and_then(|row|row.school.as_ref()).map(|row|&row.name))
        .bind(approved).execute(&mut *tx).await?;
    tx.commit().await?;
    if approved {
        state.search.invalidate_all();
        for season in seasons {
            crate::standings::invalidate(state, &season, crate::standings::STORED_BOARDS).await;
        }
        crate::recent_results::invalidate(state).await;
        crate::elo::invalidate(state).await;
        let first = first.as_ref().map_or("Unknown", |row| row.name.as_str());
        let second = second.as_ref().map_or("Unknown", |row| row.name.as_str());
        Ok((25, format!("Merged {second} into {first}.")))
    } else {
        Ok((20, "Merge request denied.".to_owned()))
    }
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    match content(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Debater merge request failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn content(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(id) = user.id else {
        return Ok(login::redirect_to_login(request.uri()));
    };
    let review = request.uri().path() == REVIEW;
    if !user.is_superuser && (review || !user.is_school_admin) {
        return Ok(web::permission_denied(state, &user, request.uri()));
    }
    let (parts, body) = request.into_parts();
    let mut values = HashMap::new();
    if parts.method == Method::POST {
        let Ok(body) = to_bytes(body, 2_621_440).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        values = login::form_values(&body);
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
        if review {
            let (level, message) = mutate_review(state, id, &values).await?;
            return Ok(claims::redirect(
                state,
                &parts.headers,
                REVIEW,
                level,
                &message,
            ));
        }
    }
    let (csrf, token) = login::csrf_tokens(&parts.headers).map_err(|_| ApiError::Serialization)?;
    let (pending, recent) = requests(state, (!review).then_some(id)).await?;
    if review {
        return Ok(claims::render(
            state,
            &user,
            &parts.uri,
            &parts.headers,
            "admin/merge_debater_requests.html",
            context! {csrf=>token,pending_requests=>pending,processed_requests=>recent},
            Some(&csrf),
        ));
    }
    let first_schools = schools(state, id, !user.is_superuser).await?;
    let second_schools = if user.is_superuser {
        first_schools.clone()
    } else {
        schools(state, id, false).await?
    };
    let allowed = [
        state.current_season.to_string(),
        (state.current_season - 1).to_string(),
    ];
    let mut error = Errors::new();
    let mut chosen = Vec::new();
    let mut selected_school = Vec::new();
    let mut conn = state.pool.acquire().await?;
    for (field, person_field, eligible) in [
        ("school_one", "debater_one", &first_schools),
        ("school_two", "debater_two", &second_schools),
    ] {
        let school = values
            .get(field)
            .and_then(|value| value.parse::<i64>().ok());
        selected_school.push(school);
        let person = match values
            .get(person_field)
            .and_then(|value| value.parse::<i64>().ok())
        {
            Some(id) => person(&mut conn, id, true).await?,
            None => None,
        };
        if parts.method == Method::POST {
            if !eligible.iter().any(|allowed| Some(allowed.id) == school) {
                error.insert(
                    field,
                    vec![if values.get(field).is_none_or(String::is_empty) {
                        "This field is required."
                    } else {
                        INVALID
                    }],
                );
            }
            if !person.as_ref().is_some_and(|row| {
                row.school
                    .as_ref()
                    .is_some_and(|row| Some(row.id) == school)
                    && row
                        .season
                        .as_ref()
                        .is_some_and(|season| allowed.contains(season))
            }) {
                error.insert(
                    person_field,
                    vec![if values.get(person_field).is_none_or(String::is_empty) {
                        "This field is required."
                    } else {
                        INVALID
                    }],
                );
            }
        }
        chosen.push(person);
    }
    drop(conn);
    let keep = values.get("keep_debater").map_or("", String::as_str);
    if parts.method == Method::POST {
        if !matches!(keep, "debater_one" | "debater_two") {
            error.insert(
                "keep_debater",
                vec![if keep.is_empty() {
                    "This field is required."
                } else {
                    "Please choose which record to keep."
                }],
            );
        }
        if error.is_empty()
            && let Some((first, second)) = chosen[0].as_ref().zip(chosen[1].as_ref())
        {
            if first.id == second.id {
                error.insert("non_field_errors", vec!["Debaters must be different."]);
            } else {
                let (primary, secondary) = if keep == "debater_one" {
                    (first, second)
                } else {
                    (second, first)
                };
                let mut tx = state.pool.begin().await?;
                if queue(&mut tx, id, primary, secondary).await? {
                    tx.commit().await?;
                    return Ok(claims::redirect(
                        state,
                        &parts.headers,
                        CREATE,
                        25,
                        "Merge request submitted. A site administrator will review it shortly.",
                    ));
                }
                error.insert("non_field_errors", vec![DUPLICATE]);
            }
        }
    }
    let fields = context! {
        school_one=>context!{selected=>selected_school[0],options=>first_schools,errors=>error.get("school_one")},
        school_two=>context!{selected=>selected_school[1],options=>second_schools,errors=>error.get("school_two")},
        debater_one=>context!{selected=>&chosen[0],errors=>error.get("debater_one")},
        debater_two=>context!{selected=>&chosen[1],errors=>error.get("debater_two")},
        keep_debater=>context!{errors=>error.get("keep_debater")},
        non_field_errors=>error.get("non_field_errors"),
    };
    let choices = [
        (
            "debater_one",
            chosen[0]
                .as_ref()
                .map_or("Debater A", |row| row.name.as_str()),
        ),
        (
            "debater_two",
            chosen[1]
                .as_ref()
                .map_or("Debater B", |row| row.name.as_str()),
        ),
    ];
    Ok(claims::render(
        state,
        &user,
        &parts.uri,
        &parts.headers,
        "school_admin/merge_debater_request_form.html",
        context! {
            csrf=>token,form=>fields,keep, choices,allowed_seasons=>allowed,
            debater_one=>&chosen[0],debater_two=>&chosen[1],
            debater_one_counts=>chosen[0].as_ref().map(|row|&row.counts),debater_two_counts=>chosen[1].as_ref().map(|row|&row.counts),
            user_pending_requests=>pending,user_recent_requests=>recent,
        },
        Some(&csrf),
    ))
}

pub(crate) async fn autocomplete(State(state): State<AppState>, request: Request) -> Response {
    match autocomplete_data(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Debater merge autocomplete failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn autocomplete_data(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(request.uri()));
    }
    if !user.is_superuser && !user.is_school_admin {
        return Ok(web::permission_denied(state, &user, request.uri()));
    }
    let args = login::form_values(request.uri().query().unwrap_or_default().as_bytes());
    let empty = || Json(json!({"results":[],"pagination":{"more":false}})).into_response();
    let forward = match args
        .get("forward")
        .map(|input| serde_json::from_str::<Value>(input))
    {
        Some(Ok(Value::Object(data))) => data,
        Some(_) => return Ok(StatusCode::BAD_REQUEST.into_response()),
        None => return Ok(empty()),
    };
    let Some(school) = forward
        .get("school")
        .and_then(crate::tournament_import::as_id)
    else {
        return Ok(empty());
    };
    let search = args
        .get("q")
        .map_or("", String::as_str)
        .trim()
        .to_lowercase();
    let number = search.parse::<i64>().ok();
    let page = args
        .get("page")
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or(1)
        .max(1);
    let terms = if search.is_empty() {
        Vec::new()
    } else {
        std::iter::once(search.as_str())
            .chain(search.split_whitespace())
            .collect::<Vec<_>>()
    };
    let mut data = Vec::new();
    let parts = if number.is_some() {
        " AND d.id=$4".to_owned()
    } else if terms.is_empty() {
        String::new()
    } else {
        let matches=terms.iter().enumerate().map(|(index,term)|{
                let escaped=term.replace('\\',"\\\\").replace('%',"\\%").replace('_',"\\_");
                data.push(format!("%{escaped}%"));
                let idx=index+4;
                format!("LOWER(d.first_name) LIKE ${idx} ESCAPE '\\' OR LOWER(d.last_name) LIKE ${idx} ESCAPE '\\'
                    OR LOWER(s.name) LIKE ${idx} ESCAPE '\\'")
            }).collect::<Vec<_>>();
        format!(" AND ({})", matches.join(" OR "))
    };
    let sql=format!("SELECT CAST(d.id AS TEXT),TRIM(d.first_name || ' ' || d.last_name),COALESCE(s.name,'Unaffiliated')
        FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id WHERE d.school_id=$1
        AND NOT(d.temporary OR d.synthetic) AND d.latest_season IN ($2,$3)
        {parts} ORDER BY d.last_name,d.first_name,d.id LIMIT 26 OFFSET {}",25*u64::from(page-1));
    let mut query = sqlx::query_as::<_, (String, String, String)>(AssertSqlSafe(sql))
        .bind(school)
        .bind(state.current_season.to_string())
        .bind((state.current_season - 1).to_string());
    if let Some(number) = number {
        query = query.bind(number);
    }
    for term in data {
        query = query.bind(term);
    }
    let rows = query.fetch_all(&state.pool).await?;
    let more = rows.len() > 25;
    let results = rows
        .into_iter()
        .take(25)
        .map(|(id, name, school)| {
            json!({"text":format!("<{id}> {name} ({school})"),
        "selected_text":format!("<{id}> {name} ({school})"),"id":id})
        })
        .collect::<Vec<_>>();
    Ok(Json(json!({"results":results,"pagination":{"more":more}})).into_response())
}
