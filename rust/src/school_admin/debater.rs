use std::{
    collections::{BTreeMap, HashSet},
    sync::Arc,
};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use chrono::Utc;
use minijinja::context;
use serde::Serialize;
use sqlx::{AssertSqlSafe, Row};
use tokio::sync::Mutex;

use crate::{
    ApiError, AppState, claims, flash, is_django_integer, login, outreach, schools, session, web,
};

pub(crate) type Limits = moka::future::Cache<(i64, chrono::NaiveDate), Arc<Mutex<u32>>>;

const DASHBOARD: &str = "/core/school-admin/";
const STATES: [(&str, &str); 3] = [("1", "Varsity"), ("0", "Novice"), ("2", "Dino")];
type Errors = BTreeMap<&'static str, String>;

#[derive(Clone, Copy)]
enum Action {
    Create(i64),
    Edit(i64),
}

impl Action {
    fn from_uri(uri: &Uri) -> Option<Self> {
        let path = uri.path().strip_prefix(DASHBOARD)?;
        if let Some(raw) = path
            .strip_prefix("debater/")
            .and_then(|rest| rest.strip_suffix("/edit/"))
        {
            return raw
                .parse::<i64>()
                .ok()
                .filter(|_| is_django_integer(raw))
                .map(Self::Edit);
        }
        let raw = path.strip_suffix("/debater/create/")?;
        raw.parse::<i64>()
            .ok()
            .filter(|_| is_django_integer(raw))
            .map(Self::Create)
    }
}

#[derive(Serialize)]
struct Form {
    first_name: String,
    last_name: String,
    email: String,
    first_season: String,
    latest_season: String,
    status: String,
    outreach: bool,
    judging: bool,
    regions: Vec<String>,
    alias_group: String,
    alias_label: String,
    initial_dino: bool,
}

impl Form {
    fn new(season: i32) -> Self {
        Self {
            first_name: String::new(),
            last_name: String::new(),
            email: String::new(),
            first_season: season.to_string(),
            latest_season: season.to_string(),
            status: "1".to_owned(),
            outreach: false,
            judging: false,
            regions: Vec::new(),
            alias_group: String::new(),
            alias_label: String::new(),
            initial_dino: false,
        }
    }

    fn from_row(row: &sqlx::any::AnyRow) -> Result<Self, sqlx::Error> {
        let status = row.try_get::<i64, _>("status")?.to_string();
        let initial_dino = status == "2";
        Ok(Self {
            status,
            initial_dino,
            first_name: row.try_get("first_name")?,
            last_name: row.try_get("last_name")?,
            email: row
                .try_get::<Option<String>, _>("email")?
                .unwrap_or_default(),
            first_season: row
                .try_get::<Option<String>, _>("first_season")?
                .unwrap_or_default(),
            latest_season: row
                .try_get::<Option<String>, _>("latest_season")?
                .unwrap_or_default(),
            outreach: row.try_get::<i64, _>("outreach")? != 0,
            judging: row.try_get::<i64, _>("judging")? != 0,
            regions: schools::region_list(row.try_get("region")?),
            alias_group: row
                .try_get::<Option<String>, _>("alias_group")?
                .unwrap_or_default(),
            alias_label: row
                .try_get::<Option<String>, _>("alias_label")?
                .unwrap_or_default(),
        })
    }

    async fn submitted(
        state: &AppState,
        bytes: &[(String, String)],
        initial_dino: bool,
    ) -> Result<(Self, Errors), ApiError> {
        let values = bytes
            .iter()
            .cloned()
            .collect::<std::collections::HashMap<_, _>>();
        let field = |name| {
            values
                .get(name)
                .map_or("", String::as_str)
                .trim()
                .to_owned()
        };
        let on = |name| {
            matches!(
                field(name).to_ascii_lowercase().as_str(),
                "on" | "true" | "1" | "yes"
            )
        };
        let status = field("status");
        let outreach = status == "2" && on("dino_to_contact_opt_in");
        let judging = status == "2" && on("dino_judge_contact_opt_in");
        let mut errors = Errors::new();
        let mut regions = Vec::new();
        let mut seen = HashSet::new();
        for (_, value) in bytes.iter().filter(|(name, _)| name == "region") {
            if !outreach::REGIONS.contains(&value.as_ref()) {
                errors.insert(
                    "region",
                    format!("Select a valid choice. {value} is not one of the available choices."),
                );
            } else if (outreach || judging) && seen.insert(value.clone()) {
                regions.push(value.clone());
            }
        }
        let mut fields = Self {
            first_name: field("first_name"),
            last_name: field("last_name"),
            email: field("email"),
            first_season: field("first_season"),
            latest_season: field("latest_season"),
            status,
            outreach,
            judging,
            regions,
            alias_group: field("alias_group"),
            alias_label: String::new(),
            initial_dino,
        };
        for (key, value, max, required) in [
            ("first_name", &fields.first_name, 32, true),
            ("last_name", &fields.last_name, 32, false),
            ("email", &fields.email, 254, false),
        ] {
            if value.is_empty() && required {
                errors.insert(key, "This field is required.".to_owned());
            }
            if value.chars().count() > max {
                errors.insert(
                    key,
                    format!(
                        "Ensure this value has at most {max} characters (it has {}).",
                        value.chars().count()
                    ),
                );
            }
            if value.contains('\0') {
                errors.insert(key, "Null characters are not allowed.".to_owned());
            }
        }
        if !fields.email.is_empty() && !crate::account::contact_email_valid(&fields.email) {
            errors.insert("email", "Enter a valid email address.".to_owned());
        }
        for (key, value, min) in [
            ("first_season", &fields.first_season, 2004),
            (
                "latest_season",
                &fields.latest_season,
                state.current_season - 6,
            ),
        ] {
            if value.is_empty() {
                errors.insert(key, "This field is required.".to_owned());
            } else if !value.parse::<i32>().is_ok_and(|year| {
                (min..=state.current_season).contains(&year) && year.to_string() == *value
            }) {
                errors.insert(
                    key,
                    format!("Select a valid choice. {value} is not one of the available choices."),
                );
            }
        }
        if !errors.contains_key("first_season")
            && !errors.contains_key("latest_season")
            && fields.first_season > fields.latest_season
        {
            errors.insert(
                "form",
                "First season cannot be after latest season.".to_owned(),
            );
        }
        if !STATES.iter().any(|(code, _)| *code == fields.status) {
            errors.insert(
                "status",
                if fields.status.is_empty() {
                    "This field is required.".to_owned()
                } else {
                    format!(
                        "Select a valid choice. {} is not one of the available choices.",
                        fields.status
                    )
                },
            );
        }
        if !fields.alias_group.is_empty() {
            let label = if let Ok(id) = fields.alias_group.parse::<i64>() {
                sqlx::query_scalar::<_, String>(
                    "SELECT label FROM core_debateraliasgroup WHERE id=$1",
                )
                .bind(id)
                .fetch_optional(&state.pool)
                .await?
            } else {
                None
            };
            if let Some(label) = label {
                fields.alias_label = label;
            } else {
                errors.insert(
                    "alias_group",
                    "Select a valid choice. That choice is not one of the available choices."
                        .to_owned(),
                );
            }
        }
        Ok((fields, errors))
    }
}

enum Save {
    Saved,
    Invalid,
    Missing,
    Limited,
}

fn daily_limit() -> Response {
    (StatusCode::FORBIDDEN, "You have reached the daily limit of 5 new debaters. Please contact a site administrator if you need to create more.").into_response()
}

async fn save(
    state: &AppState,
    action: Action,
    school: i64,
    owner: i64,
    form: &Form,
    errors: &mut Errors,
    cache_key: Option<&str>,
) -> Result<Save, ApiError> {
    let mut tx = state.pool.begin().await?;
    let quota = if let (Some(cache), Some(key)) = (&state.memcached, cache_key) {
        sqlx::query("UPDATE core_user SET is_active=is_active WHERE id=$1")
            .bind(owner)
            .execute(&mut *tx)
            .await?;
        let count = cache.quota_count(key).await?;
        if count >= 5 {
            return Ok(Save::Limited);
        }
        Some((cache, key, count))
    } else {
        None
    };
    sqlx::query("UPDATE core_school SET id=id WHERE id=$1")
        .bind(school)
        .execute(&mut *tx)
        .await?;
    let membership = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_schooladmin WHERE school_id=$1 AND user_id=$2",
    )
    .bind(school)
    .bind(owner)
    .fetch_optional(&mut *tx)
    .await?;
    if membership.is_none() {
        return Ok(Save::Missing);
    }
    let region = if state.pool.connect_options().database_url.scheme() == "sqlite" {
        "$8"
    } else {
        "CAST(CAST($8 AS TEXT) AS JSONB)"
    };
    let create = matches!(action, Action::Create(_));
    if create {
        let duplicate=sqlx::query_scalar::<_,i64>("SELECT CAST(id AS BIGINT) FROM core_debater
            WHERE school_id=$1 AND NOT temporary AND NOT synthetic AND LOWER(first_name)=LOWER($2) AND LOWER(last_name)=LOWER($3) LIMIT 1")
            .bind(school).bind(&form.first_name).bind(&form.last_name).fetch_optional(&mut *tx).await?;
        if duplicate.is_some() {
            errors.insert(
                "form",
                "A debater with this name already exists at this school.".to_owned(),
            );
            return Ok(Save::Invalid);
        }
    }
    let sql = if create {
        format!("INSERT INTO core_debater (first_name,last_name,email,status,first_season,latest_season,
            alias_group_id,region,dino_to_contact_opt_in,dino_judge_contact_opt_in,school_id,elo_manual_opt,temporary,synthetic)
            VALUES ($1,$2,$3,$4,$5,$6,$7,{region},$9,$10,$11,'',FALSE,FALSE) RETURNING CAST(id AS BIGINT)")
    } else {
        format!("UPDATE core_debater SET first_name=$1,last_name=$2,email=$3,status=$4,first_season=$5,
            latest_season=$6,alias_group_id=$7,region={region},dino_to_contact_opt_in=$9,dino_judge_contact_opt_in=$10
            WHERE school_id=$11 AND id=$12 AND latest_season>=$13 RETURNING CAST(id AS BIGINT)")
    };
    let first = claims::edit::sanitize_name(&form.first_name, "Removed");
    let last = claims::edit::sanitize_name(&form.last_name, "");
    let mut query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
        .bind(first)
        .bind(last)
        .bind((!form.email.is_empty()).then_some(form.email.as_str()))
        .bind(form.status.parse::<i64>().ok())
        .bind(&form.first_season)
        .bind(&form.latest_season)
        .bind(form.alias_group.parse::<i64>().ok())
        .bind(serde_json::to_string(&form.regions)?)
        .bind(form.outreach)
        .bind(form.judging)
        .bind(school);
    if let Action::Edit(id) = action {
        query = query.bind(id).bind((state.current_season - 6).to_string());
    }
    let Some(id) = query.fetch_optional(&mut *tx).await? else {
        return Ok(Save::Missing);
    };
    if !create {
        claims::edit::team_names(&mut tx, id).await?;
    }
    if let Some((cache, key, count)) = quota {
        cache.record_quota(key, count + 1).await?;
    }
    tx.commit().await?;
    crate::search::invalidate(state).await;
    Ok(Save::Saved)
}

async fn view(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let Some(action) = Action::from_uri(&uri) else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(owner) = user.id.filter(|_| user.is_authenticated) else {
        return Ok(login::redirect_to_login(&uri));
    };
    if !user.is_school_admin {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let mut fields = Form::new(state.current_season);
    let (school_id, name) = match action {
        Action::Create(id) => (id, String::new()),
        Action::Edit(id) => {
            let row=sqlx::query("SELECT d.first_name,d.last_name,d.email,d.first_season,d.latest_season,
                CAST(d.school_id AS BIGINT) AS school_id,CAST(d.status AS BIGINT) AS status,
                CAST(d.alias_group_id AS TEXT) AS alias_group,g.label AS alias_label,
                CAST(CASE WHEN d.dino_to_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS outreach,
                CAST(CASE WHEN d.dino_judge_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS judging,CAST(d.region AS TEXT) AS region
                FROM core_debater d JOIN core_schooladmin a ON a.school_id=d.school_id AND a.user_id=$2
                LEFT JOIN core_debateraliasgroup g ON g.id=d.alias_group_id
                WHERE d.id=$1 AND d.latest_season>=$3 AND NOT d.synthetic AND NOT d.temporary")
                .bind(id).bind(owner).bind((state.current_season-6).to_string()).fetch_optional(&state.pool).await?;
            let Some(row) = row else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            fields = Form::from_row(&row)?;
            (
                row.try_get::<i64, _>("school_id")?,
                format!("{} {}", fields.first_name, fields.last_name)
                    .trim()
                    .to_owned(),
            )
        }
    };
    let school=sqlx::query("SELECT s.name,CAST(a.id AS BIGINT) AS admin_id FROM core_school s
        LEFT JOIN core_schooladmin a ON a.school_id=s.id AND a.user_id=$2 WHERE s.id=$1 AND NOT s.temporary")
        .bind(school_id).bind(owner).fetch_optional(&state.pool).await?;
    let Some(school) = school else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    if school.try_get::<Option<i64>, _>("admin_id")?.is_none() {
        return Ok((
            StatusCode::FORBIDDEN,
            "You do not have permission to create debaters for this school.",
        )
            .into_response());
    }
    let school_name: &str = school.try_get("name")?;
    let create = matches!(action, Action::Create(_));
    let mut errors = Errors::new();
    let (parts, body) = request.into_parts();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|origin| origin.starts_with("https://"));
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
        let mut count = if create {
            let key = (owner, Utc::now().date_naive());
            let cell = state
                .school_admin_limits
                .get_with(key, async { Arc::new(Mutex::new(0)) })
                .await;
            let guard = cell.lock_owned().await;
            let shared = format!(":1:debater_create_count_{}_{}", key.0, key.1);
            let current = if let Some(cache) = &state.memcached {
                cache.quota_count(&shared).await?
            } else {
                *guard
            };
            if current >= 5 {
                return Ok(daily_limit());
            }
            Some((guard, shared))
        } else {
            None
        };
        (fields, errors) = Form::submitted(state, &bytes, fields.initial_dino).await?;
        if errors.is_empty() {
            let cache_key = count.as_ref().map(|(_, key)| key.as_str());
            match save(
                state,
                action,
                school_id,
                owner,
                &fields,
                &mut errors,
                cache_key,
            )
            .await?
            {
                Save::Saved => {
                    if state.memcached.is_none()
                        && let Some((count, _)) = count.as_mut()
                    {
                        **count += 1;
                    }
                    return Ok((StatusCode::FOUND, [(header::LOCATION, DASHBOARD)]).into_response());
                }
                Save::Missing => return Ok(StatusCode::NOT_FOUND.into_response()),
                Save::Limited => return Ok(daily_limit()),
                Save::Invalid => {}
            }
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let alerts = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(&parts.headers, secret))
        .unwrap_or_default();
    let bindings = context! {fields=>&fields, errors, is_create=>create, school=>context!{name=>school_name}, object=>context!{name},
        show_region=>fields.outreach || fields.judging, csrf=>token, messages=>&alerts, statuses=>STATES,
        first_choices=>web::seasons(state.current_season),
        latest_choices=>web::seasons(state.current_season).into_iter().take(7).collect::<Vec<_>>(),
        region_choices=>outreach::REGIONS.iter().map(|value|(*value,schools::region_label(value))).collect::<Vec<_>>(),
    };
    let common = web::base_context(state, &user, &uri);
    let html = web::render(
        "school_admin/debater_form.html",
        context! {..bindings,..common},
    )
    .map_err(|error| {
        eprintln!("School administration debater form failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    if !alerts.is_empty() {
        flash::clear(&mut response, secure);
    }
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    match view(&state, request).await {
        Ok(response) => response,
        Err(ApiError::QuotaUnavailable) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
        Err(error) => super::roles::failed(error),
    }
}
