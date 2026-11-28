use std::{collections::BTreeMap, sync::LazyLock};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use regex::Regex;
use serde::Serialize;
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use crate::{
    ApiError, AppState, directory::TOURNAMENT_TYPES, is_django_integer, login, session, web,
};

const DIRECTORY: &str = "/core/tournaments/";
const NAME_SUFFIXES: [(&str, &str); 3] =
    [("0", ""), ("1", " (Elections)"), ("2", " (APDA Meeting)")];
static PARENTHESES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*\([^)]*\)").unwrap());
type Errors = BTreeMap<&'static str, String>;

#[derive(Serialize)]
struct Fields {
    host: String,
    host_name: String,
    season: String,
    date: String,
    num_teams: String,
    num_novice_debaters: String,
    qual_type: String,
    name_suffix: String,
    manual_name: String,
    short_name: String,
}

impl Default for Fields {
    fn default() -> Self {
        Self {
            host: String::new(),
            host_name: String::new(),
            season: String::new(),
            date: String::new(),
            num_teams: "-1".to_owned(),
            num_novice_debaters: "-1".to_owned(),
            qual_type: "0".to_owned(),
            name_suffix: "0".to_owned(),
            manual_name: String::new(),
            short_name: String::new(),
        }
    }
}

impl Fields {
    async fn load(state: &AppState, id: i64) -> Result<Option<Self>, ApiError> {
        let row=sqlx::query("SELECT CAST(t.host_id AS TEXT) AS host,s.name AS host_name, t.season,
            CAST(t.date AS TEXT) AS date, CAST(t.num_teams AS TEXT) AS num_teams,
            CAST(t.num_novice_debaters AS TEXT) AS num_novice_debaters, CAST(t.qual_type AS TEXT) AS qual_type,
            CAST(t.name_suffix AS TEXT) AS name_suffix,t.manual_name,t.short_name
            FROM core_tournament t LEFT JOIN core_school s ON s.id=t.host_id WHERE t.id=$1")
            .bind(id).fetch_optional(&state.pool).await?;
        let Some(row) = row else { return Ok(None) };
        Ok(Some(Self {
            host: row
                .try_get::<Option<String>, _>("host")?
                .unwrap_or_default(),
            host_name: row
                .try_get::<Option<String>, _>("host_name")?
                .unwrap_or_default(),
            season: row.try_get("season")?,
            date: row.try_get("date")?,
            num_teams: row.try_get("num_teams")?,
            num_novice_debaters: row.try_get("num_novice_debaters")?,
            qual_type: row.try_get("qual_type")?,
            name_suffix: row.try_get("name_suffix")?,
            manual_name: row.try_get("manual_name")?,
            short_name: row.try_get("short_name")?,
        }))
    }

    async fn submitted(
        state: &AppState,
        params: &std::collections::HashMap<String, String>,
    ) -> Result<(Self, Errors), ApiError> {
        let get = |name| {
            params
                .get(name)
                .map_or("", String::as_str)
                .trim()
                .to_owned()
        };
        let mut fields = Self {
            host: get("host"),
            host_name: String::new(),
            season: get("season"),
            date: get("date"),
            num_teams: get("num_teams"),
            num_novice_debaters: get("num_novice_debaters"),
            qual_type: get("qual_type"),
            name_suffix: get("name_suffix"),
            manual_name: get("manual_name"),
            short_name: get("short_name"),
        };
        let mut errors = Errors::new();
        for (name, raw) in [
            ("host", &fields.host),
            ("season", &fields.season),
            ("date", &fields.date),
            ("num_teams", &fields.num_teams),
            ("num_novice_debaters", &fields.num_novice_debaters),
            ("qual_type", &fields.qual_type),
            ("name_suffix", &fields.name_suffix),
        ] {
            if raw.is_empty() {
                errors.insert(name, "This field is required.".to_owned());
            }
        }
        if !fields.host.is_empty() {
            let host = if let Ok(id) = fields.host.parse::<i64>() {
                sqlx::query_scalar::<_, String>(
                    "SELECT name FROM core_school WHERE id=$1 AND NOT temporary",
                )
                .bind(id)
                .fetch_optional(&state.pool)
                .await?
            } else {
                None
            };
            if let Some(host) = host {
                fields.host_name = host;
            } else {
                errors.insert(
                    "host",
                    "Select a valid choice. That choice is not one of the available choices."
                        .to_owned(),
                );
            }
        }
        let season = fields.season.parse::<i32>().is_ok_and(|year| {
            (2004..=state.current_season).contains(&year) && year.to_string() == fields.season
        });
        let selected_type = TOURNAMENT_TYPES
            .iter()
            .any(|(kind, _)| kind.to_string() == fields.qual_type);
        let suffix = NAME_SUFFIXES
            .iter()
            .any(|(choice, _)| *choice == fields.name_suffix);
        for (name, raw, valid) in [
            ("season", &fields.season, season),
            ("qual_type", &fields.qual_type, selected_type),
            ("name_suffix", &fields.name_suffix, suffix),
        ] {
            if !raw.is_empty() && !valid {
                errors.insert(
                    name,
                    format!("Select a valid choice. {raw} is not one of the available choices."),
                );
            }
        }
        if !fields.date.is_empty() {
            let date = crate::django_date::form(&fields.date);
            if let Some(date) = date {
                fields.date = date.to_string();
            } else {
                errors.insert("date", "Enter a valid date.".to_owned());
            }
        }
        for (name, value) in [
            ("num_teams", &mut fields.num_teams),
            ("num_novice_debaters", &mut fields.num_novice_debaters),
        ] {
            if !value.is_empty() {
                let raw = value.strip_suffix(".0").unwrap_or(value);
                match raw.parse::<i64>() {
                    Ok(number) if i32::try_from(number).is_err() => {
                        let (test, limit) = if number < 0 {
                            ("greater than or equal to", i32::MIN)
                        } else {
                            ("less than or equal to", i32::MAX)
                        };
                        errors.insert(name, format!("Ensure this value is {test} {limit}."));
                    }
                    Ok(number) => *value = number.to_string(),
                    Err(_) => {
                        errors.insert(name, "Enter a whole number.".to_owned());
                    }
                }
            }
        }
        for (name, value) in [
            ("manual_name", &fields.manual_name),
            ("short_name", &fields.short_name),
        ] {
            if value.chars().count() > 128 {
                errors.insert(
                    name,
                    format!(
                        "Ensure this value has at most 128 characters (it has {}).",
                        value.chars().count()
                    ),
                );
            } else if value.contains('\0') {
                errors.insert(name, "Null characters are not allowed.".to_owned());
            }
        }
        Ok((fields, errors))
    }
}

fn event_suffix(code: i64) -> &'static str {
    match code {
        1 | 2 => " IV",
        3 => " NorthAms",
        4 => " (Expansion)",
        5 => " WUDC",
        6 => " NAUDC",
        7 => " ProAms",
        8 => " Nationals",
        9 => " Novice",
        10 => " (Gender Minority)",
        11 => " (Online No Points)",
        12 => " (Online)",
        13 => " (Online Proams)",
        14 => " (Online Gender Minority)",
        15 => " (BIPOC)",
        16 => " USUDC",
        _ => "",
    }
}

pub(crate) struct Naming<'a> {
    pub(crate) season: &'a str,
    pub(crate) date: &'a str,
    pub(crate) name_suffix: &'a str,
    pub(crate) manual_name: &'a str,
    pub(crate) short_name: &'a str,
}

pub(crate) async fn names(
    conn: &mut AnyConnection,
    host_id: i64,
    code: i64,
    id: Option<i64>,
    event: Naming<'_>,
) -> Result<(String, String), ApiError> {
    let row = sqlx::query("SELECT name,short_name FROM core_school WHERE id=$1")
        .bind(host_id)
        .fetch_one(&mut *conn)
        .await?;
    let host_name: &str = row.try_get("name")?;
    let host_short: Option<&str> = row.try_get("short_name")?;
    let mut suffix = event_suffix(code).to_owned();
    if code == 0 {
        let dates=sqlx::query_scalar::<_,String>("SELECT CAST(date AS TEXT) FROM core_tournament
            WHERE host_id=$1 AND season=$2 AND qual_type=0 AND (CAST($3 AS BIGINT) IS NULL OR id<>$3)")
            .bind(host_id).bind(event.season).bind(id).fetch_all(&mut *conn).await?;
        if !dates.is_empty() {
            let prior = dates
                .iter()
                .filter(|date| date.as_str() < event.date)
                .count();
            suffix = format!(" {}", "I".repeat(prior + 1));
        }
    }
    let name_suffix = NAME_SUFFIXES
        .iter()
        .find(|(key, _)| *key == event.name_suffix)
        .map_or("", |(_, label)| *label);
    let name = if event.manual_name.is_empty() {
        format!("{host_name}{suffix}{name_suffix}")
    } else {
        event.manual_name.to_owned()
    };
    let short_name = if event.short_name.is_empty() {
        let base = host_short
            .filter(|value| !value.is_empty())
            .unwrap_or(host_name);
        let raw = format!("{base}{suffix}");
        PARENTHESES
            .replace_all(&raw, "")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        event.short_name.to_owned()
    };
    Ok((name, short_name))
}

pub(crate) struct Scoring {
    pub(crate) toty: bool,
    pub(crate) soty: bool,
    pub(crate) qual: bool,
    pub(crate) online: Option<bool>,
    pub(crate) bar: Option<i64>,
}

pub(crate) fn scoring(code: i64, season: &str) -> Scoring {
    let toty = matches!(code, 0 | 4 | 12);
    let soty = matches!(code, 0 | 4 | 7 | 12 | 13);
    let qual = matches!(code, 0 | 4 | 7 | 10 | 12 | 13 | 14 | 15);
    let online = match code {
        11 => Some(false),
        12..=14 => Some(true),
        _ => None,
    };
    let bar = match code {
        1 => Some(4_i64),
        2 | 3 | 6 => Some(8),
        4 => Some(1),
        5 => Some(48),
        16 => Some(0),
        10 | 15 => Some(
            if season.trim().parse::<i64>().is_ok_and(|year| year < 2025) {
                0
            } else {
                2
            },
        ),
        _ => None,
    };
    Scoring {
        toty,
        soty,
        qual,
        online,
        bar,
    }
}

async fn save(state: &AppState, fields: &Fields, id: Option<i64>) -> Result<Option<i64>, ApiError> {
    let mut tx = state.pool.begin().await?;
    let host = fields.host.parse::<i64>().ok();
    let code = fields.qual_type.parse::<i64>().unwrap_or_default();
    let date = if state.pool.connect_options().database_url.scheme() == "sqlite" {
        "$4"
    } else {
        "CAST(CAST($4 AS TEXT) AS DATE)"
    };
    let host_id = sqlx::query_scalar::<_, i64>(
        "UPDATE core_school SET id=id WHERE id=$1 RETURNING CAST(id AS BIGINT)",
    )
    .bind(host)
    .fetch_one(&mut *tx)
    .await?;
    let (name, short_name) = names(
        &mut tx,
        host_id,
        code,
        id,
        Naming {
            season: &fields.season,
            date: &fields.date,
            name_suffix: &fields.name_suffix,
            manual_name: &fields.manual_name,
            short_name: &fields.short_name,
        },
    )
    .await?;
    let Scoring {
        toty,
        soty,
        qual,
        online,
        bar,
    } = scoring(code, &fields.season);
    let sql = if id.is_some() {
        format!("UPDATE core_tournament SET name=$1,short_name=$2,manual_name=$3,date={date},
            host_id=$5,season=$6,num_teams=$7,num_novice_debaters=$8,qual_type=$9,name_suffix=$10,toty=$11,soty=$12,qual=$13,
            noty=FALSE,online_qual_points=COALESCE($14,online_qual_points),autoqual_bar=COALESCE($15,autoqual_bar)
            WHERE id=$16 RETURNING CAST(id AS BIGINT)")
    } else {
        format!("INSERT INTO core_tournament (name,short_name,manual_name,date,host_id,season,num_teams,num_novice_debaters,
            qual_type,name_suffix,toty,soty,qual,noty,online_qual_points,autoqual_bar,num_rounds,num_novice_teams,num_debaters)
            VALUES ($1,$2,$3,{date},$5,$6,$7,$8,$9,$10,$11,$12,$13,FALSE,COALESCE($14,FALSE),COALESCE($15,0),5,-1,-1)
            RETURNING CAST(id AS BIGINT)")
    };
    let mut statement = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
        .bind(name)
        .bind(short_name)
        .bind(&fields.manual_name)
        .bind(&fields.date)
        .bind(host_id)
        .bind(&fields.season)
        .bind(fields.num_teams.parse::<i64>().ok())
        .bind(fields.num_novice_debaters.parse::<i64>().ok())
        .bind(code)
        .bind(fields.name_suffix.parse::<i64>().ok())
        .bind(toty)
        .bind(soty)
        .bind(qual)
        .bind(online)
        .bind(bar);
    if let Some(id) = id {
        statement = statement.bind(id)
    }
    let result = statement.fetch_optional(&mut *tx).await?;
    tx.commit().await?;
    if result.is_some() {
        crate::recent_results::invalidate(state).await;
    }
    Ok(result)
}

async fn view(state: &AppState, request: Request, id: Option<i64>) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    let permission = if id.is_some() {
        "change_tournament"
    } else {
        "add_tournament"
    };
    if !session::has_permission(&state.pool, &user, permission).await? {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let mut fields = if let Some(id) = id {
        let Some(found) = Fields::load(state, id).await? else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        found
    } else {
        Fields::default()
    };
    let (parts, body) = request.into_parts();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|url| url.starts_with("https://"));
    let mut errors = Errors::new();
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
        (fields, errors) = Fields::submitted(state, &values).await?;
        if errors.is_empty() {
            let Some(id) = save(state, &fields, id).await? else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            return Ok((
                StatusCode::FOUND,
                [(header::LOCATION, format!("{DIRECTORY}{id}"))],
            )
                .into_response());
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let bindings = context! {fields,errors,csrf=>token, tournament_types=>TOURNAMENT_TYPES.iter().map(|(id,name)|(id.to_string(),*name)).collect::<Vec<_>>(),
    name_suffixes=>NAME_SUFFIXES, edit_seasons=>web::seasons(state.current_season),messages=>Vec::<String>::new()};
    let html = if let Some(id) = id {
        super::html(state, &user, id, &uri, &[], Some(bindings)).await?
    } else {
        let common = web::base_context(state, &user, &uri);
        web::render("tournaments/create.html", context! {..common,..bindings}).map_err(|error| {
            eprintln!("Tournament create template failed: {error}");
            ApiError::Serialization
        })?
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let path = request.uri().path();
    let id = if let Some(raw) = path
        .strip_prefix(DIRECTORY)
        .and_then(|path| path.strip_suffix("/edit"))
    {
        let Some(id) = raw.parse::<i64>().ok().filter(|_| is_django_integer(raw)) else {
            return StatusCode::NOT_FOUND.into_response();
        };
        Some(id)
    } else {
        None
    };
    view(&state, request, id).await.unwrap_or_else(|error| {
        eprintln!("Tournament form failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
