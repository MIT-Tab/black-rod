use std::{
    collections::{BTreeMap, HashSet},
    sync::LazyLock,
};

use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use minijinja::context;
use regex::Regex;
use serde_json::{Value, json};
use sqlx::{AnyConnection, AnyPool, AssertSqlSafe, Row};

use super::{Errors, redirect, render};
use crate::{ApiError, AppState, is_django_integer, login, schools, session, web};

const QUERY: &str = "
    SELECT CAST(d.user_id AS BIGINT) AS owner, d.first_name, d.last_name, d.email, d.paradigm,
        d.first_season, d.latest_season, d.elo_manual_opt, CAST(d.status AS BIGINT) AS status,
        CAST(CASE WHEN d.dino_to_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS outreach,
        CAST(CASE WHEN d.dino_judge_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS judge,
        CAST(d.region AS TEXT) AS region, s.name AS school
    FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
    WHERE d.id=$1 AND NOT d.temporary AND NOT d.synthetic
";
static MARKUP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<!--.*?-->|<![^>]*>|</?[a-zA-Z][^>]*>").unwrap());
pub(crate) fn sanitize_name(value: &str, fallback: &str) -> String {
    let stripped = MARKUP.replace_all(value, "");
    let stripped = stripped.trim();
    if stripped.is_empty() {
        fallback.to_owned()
    } else {
        stripped.to_owned()
    }
}

const STATES: [(&str, &str); 3] = [("1", "Varsity"), ("0", "Novice"), ("2", "Dino")];
const ELO: [(&str, &str); 3] = [("", "Unset"), ("opt_in", "Opt-in"), ("opt_out", "Opt-out")];
const REGIONS: [&str; 7] = [
    "north", "south", "central", "midwest", "west", "online", "other",
];
const NAME: [&str; 2] = ["first_name", "last_name"];

#[derive(Default)]
struct Profile {
    text: BTreeMap<&'static str, String>,
    status: String,
    outreach: bool,
    judging: bool,
    regions: Vec<String>,
}

impl Profile {
    fn original(row: &sqlx::any::AnyRow) -> Result<Self, sqlx::Error> {
        let mut text = BTreeMap::new();
        for field in [
            "first_name",
            "last_name",
            "email",
            "paradigm",
            "first_season",
            "latest_season",
            "elo_manual_opt",
        ] {
            text.insert(
                field,
                row.try_get::<Option<String>, _>(field)?.unwrap_or_default(),
            );
        }
        Ok(Self {
            text,
            status: row.try_get::<i64, _>("status")?.to_string(),
            outreach: row.try_get::<i64, _>("outreach")? != 0,
            judging: row.try_get::<i64, _>("judge")? != 0,
            regions: schools::region_list(row.try_get("region")?),
        })
    }

    fn submitted(data: &[u8]) -> Self {
        let values = login::form_values(data);
        let get = |field| {
            values
                .get(field)
                .map(|value| value.trim())
                .unwrap_or_default()
        };
        let checked = |field| {
            matches!(
                get(field).to_ascii_lowercase().as_str(),
                "true" | "1" | "on" | "yes"
            )
        };
        let mut text = BTreeMap::new();
        for field in [
            "first_name",
            "last_name",
            "email",
            "paradigm",
            "first_season",
            "latest_season",
            "elo_manual_opt",
        ] {
            text.insert(field, get(field).to_owned());
        }
        let mut seen = HashSet::new();
        let regions = url::form_urlencoded::parse(data)
            .filter(|(key, _)| key == "region")
            .map(|(_, value)| value.into_owned())
            .filter(|value| seen.insert(value.clone()))
            .collect();
        Self {
            text,
            status: get("status").to_owned(),
            outreach: checked("dino_to_contact_opt_in"),
            judging: checked("dino_judge_contact_opt_in"),
            regions,
        }
    }

    fn validate(&mut self, initial: &Profile, season: i32) -> Errors {
        let mut errors = Errors::new();
        if self.text["first_name"].is_empty() {
            errors.insert("first_name", vec!["This field is required."]);
        }
        for field in NAME {
            if self.text[field].chars().count() > 32 {
                errors.insert(field, vec!["Ensure this value has at most 32 characters."]);
            }
            if self.text[field].contains('\0') {
                errors.insert(field, vec!["Null characters are not allowed."]);
            }
        }
        if !self.text["email"].is_empty() {
            let raw = &self.text["email"];
            let valid = raw.rsplit_once('@').is_some_and(|(user, domain)| {
                !user.is_empty() && (domain.contains('.') || domain == "localhost")
            });
            if !valid
                || raw.chars().count() > 254
                || raw.chars().any(char::is_whitespace)
                || raw.contains('\0')
            {
                errors.insert("email", vec!["Enter a valid email address."]);
            }
        }
        if !self.text["paradigm"].is_empty() {
            let raw = self.text.get_mut("paradigm").unwrap();
            if !raw.contains("://") {
                *raw = format!("http://{raw}");
            }
            let valid = url::Url::parse(raw).is_ok_and(|url| {
                matches!(url.scheme(), "http" | "https" | "ftp" | "ftps")
                    && url
                        .host_str()
                        .is_some_and(|host| host.contains('.') || host == "localhost")
            });
            if !valid
                || raw.chars().count() > 500
                || raw.chars().any(char::is_whitespace)
                || raw.contains('\0')
            {
                errors.insert("paradigm", vec!["Enter a valid URL."]);
            } else if !raw.contains("docs.google.com") {
                errors.insert("paradigm",vec!["Paradigm must be a Google Docs link. Please ensure it's a link to a Google Doc."]);
            }
        }
        if !STATES.iter().any(|(code, _)| *code == self.status) {
            errors.insert(
                "status",
                vec![if self.status.is_empty() {
                    "This field is required."
                } else {
                    super::INVALID
                }],
            );
        }
        if !ELO
            .iter()
            .any(|(code, _)| *code == self.text["elo_manual_opt"])
        {
            errors.insert("elo_manual_opt", vec![super::INVALID]);
        }
        for field in ["first_season", "latest_season"] {
            let value = &self.text[field];
            if !value.is_empty()
                && value != &initial.text[field]
                && !value
                    .parse::<i32>()
                    .is_ok_and(|year| (2004..=season).contains(&year) && year.to_string() == *value)
            {
                errors.insert(field, vec![super::INVALID]);
            }
        }
        if !self.text["first_season"].is_empty() && !self.text["latest_season"].is_empty() {
            match (
                self.text["first_season"].parse::<i32>(),
                self.text["latest_season"].parse::<i32>(),
            ) {
                (Ok(first), Ok(last)) if first > last => {
                    errors.insert(
                        "non_field_errors",
                        vec!["First season cannot be after latest season."],
                    );
                }
                (Err(_), _) | (_, Err(_)) => {
                    errors.insert(
                        "non_field_errors",
                        vec!["Season values must be valid years."],
                    );
                }
                _ => (),
            }
        }
        if self
            .regions
            .iter()
            .any(|region| !REGIONS.contains(&region.as_str()))
        {
            errors.insert("region", vec![super::INVALID]);
        }
        errors
    }

    fn form(&self, initial: &Self, season: i32, errors: &Errors) -> Value {
        let mut form = serde_json::Map::new();
        let field = |key: &str,
                     label: &str,
                     kind: &str,
                     value: &str,
                     help: &str,
                     max: usize,
                     options: Vec<(String, String)>| {
            json!({
                "name":key,"id_for_label":format!("id_{key}"),"label":label,"kind":kind,"value":value,
                "help_text":help,"max":max,"options":options,"errors":errors.get(key),
                "required":matches!(key,"first_name"|"status"),
            })
        };
        for (key, label, kind, max, help) in [
            ("first_name", "First Name", "text", 32, ""),
            ("last_name", "Last Name", "text", 32, ""),
            ("email", "Email", "email", 254, ""),
            (
                "paradigm",
                "Paradigm (Google Doc Link)",
                "url",
                500,
                "Link to your Google Doc paradigm. Make sure sharing is enabled so others can view it.",
            ),
        ] {
            form.insert(
                key.into(),
                field(key, label, kind, &self.text[key], help, max, Vec::new()),
            );
        }
        for (key, label) in [
            ("first_season", "First Season"),
            ("latest_season", "Latest Season"),
        ] {
            let mut options = vec![(String::new(), "Select season".to_owned())];
            options.extend(web::seasons(season));
            let value = &initial.text[key];
            if !value.is_empty() && !options.iter().any(|(option, _)| option == value) {
                let label = value
                    .parse()
                    .map(crate::schedule::season_display)
                    .unwrap_or_else(|_| value.clone());
                options.push((value.clone(), label));
            }
            form.insert(
                key.into(),
                field(key, label, "select", &self.text[key], "", 0, options),
            );
        }
        let pairs = |choices: &[(&str, &str)]| {
            choices
                .iter()
                .map(|(value, label)| ((*value).to_owned(), (*label).to_owned()))
                .collect()
        };
        form.insert(
            "status".into(),
            field(
                "status",
                "Status",
                "select",
                &self.status,
                "",
                0,
                pairs(&STATES),
            ),
        );
        form.insert(
            "elo_manual_opt".into(),
            field(
                "elo_manual_opt",
                "ELO Inclusion Override",
                "select",
                &self.text["elo_manual_opt"],
                "Override the default ELO inclusion heuristic for this profile.",
                0,
                pairs(&ELO),
            ),
        );
        form.insert(
            "dino_to_contact_opt_in".into(),
            field(
                "dino_to_contact_opt_in",
                "I'm open to TO outreach",
                "checkbox",
                if self.outreach { "on" } else { "" },
                "Check this if you'd like tournaments to reach out when they need TOs.",
                0,
                Vec::new(),
            ),
        );
        form.insert("dino_judge_contact_opt_in".into(),field("dino_judge_contact_opt_in","I'm open to judging outreach","checkbox",
            if self.judging {"on"} else {""},"Check this if you'd like tournaments to reach out when they need judges. (Dinos only)",0,Vec::new()));
        let mut region = field(
            "region",
            "Where are you located?",
            "regions",
            "",
            "Let tournaments know your general region for outreach planning.",
            0,
            REGIONS
                .into_iter()
                .map(|key| (key.to_owned(), schools::region_label(key).to_owned()))
                .collect(),
        );
        region["selected"] = json!(self.regions);
        form.insert("region".into(), region);
        form.insert(
            "show_judge_contact_opt_in".into(),
            json!(self.status == "2"),
        );
        form.insert(
            "show_region_field".into(),
            json!(self.outreach || self.judging),
        );
        form.insert(
            "non_field_errors".into(),
            json!(errors.get("non_field_errors")),
        );
        Value::Object(form)
    }
}

pub(crate) async fn team_names(conn: &mut AnyConnection, id: i64) -> Result<(), sqlx::Error> {
    let teams = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(team_id AS BIGINT) FROM core_team_debaters WHERE debater_id=$1",
    )
    .bind(id)
    .fetch_all(&mut *conn)
    .await?;
    for team in teams {
        team_name(conn, team).await?;
    }
    Ok(())
}

pub(crate) async fn team_name(conn: &mut AnyConnection, team: i64) -> Result<(), sqlx::Error> {
    let roster = sqlx::query(
        "
            SELECT d.first_name,d.last_name,COALESCE(s.name,'Unaffiliated') AS school
            FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id
            LEFT JOIN core_school s ON s.id=d.school_id WHERE m.team_id=$1 ORDER BY m.id
        ",
    )
    .bind(team)
    .fetch_all(&mut *conn)
    .await?;
    let Some(first) = roster.first() else {
        return Ok(());
    };
    let Some(last) = roster.last() else {
        return Ok(());
    };
    let first: &str = first.try_get("school")?;
    let last: &str = last.try_get("school")?;
    let school = if first == last {
        first.to_owned()
    } else {
        format!("{first} / {last}")
    };
    let mut initials = String::new();
    for person in &roster {
        let last: &str = person.try_get("last_name")?;
        let first: &str = person.try_get("first_name")?;
        initials.push(
            last.chars()
                .next()
                .or_else(|| first.chars().next())
                .unwrap_or('?'),
        );
    }
    sqlx::query("UPDATE core_team SET name=$2 WHERE id=$1")
        .bind(team)
        .bind(format!("{school} {initials}").trim())
        .execute(&mut *conn)
        .await?;
    Ok(())
}

async fn save(
    pool: &AnyPool,
    profile: &Profile,
    id: i64,
    viewer: &session::User,
) -> Result<bool, ApiError> {
    let sqlite = pool.connect_options().database_url.scheme() == "sqlite";
    let region = if sqlite {
        "$11"
    } else {
        "CAST(CAST($11 AS TEXT) AS JSONB)"
    };
    let sql=format!("UPDATE core_debater SET first_name=$2,last_name=$3,email=$4,paradigm=$5,
        first_season=$6,latest_season=$7,elo_manual_opt=$8,status=$9,
        dino_to_contact_opt_in=(CAST($10 AS BIGINT)<>0),region={region},dino_judge_contact_opt_in=(CAST($12 AS BIGINT)<>0)
        WHERE id=$1 AND NOT synthetic AND NOT temporary AND (CAST($13 AS BIGINT)<>0 OR user_id=$14)");
    let judge = profile.status == "2" && profile.judging;
    let regions = if profile.outreach || judge {
        profile.regions.clone()
    } else {
        Vec::new()
    };
    let option = |key| {
        let value = profile.text[key].as_str();
        (!value.is_empty()).then_some(value)
    };
    let first = sanitize_name(&profile.text["first_name"], "Removed");
    let last = sanitize_name(&profile.text["last_name"], "");
    let mut tx = pool.begin().await?;
    let updated = sqlx::query(AssertSqlSafe(sql))
        .bind(id)
        .bind(first)
        .bind(last)
        .bind(option("email"))
        .bind(option("paradigm"))
        .bind(&profile.text["first_season"])
        .bind(&profile.text["latest_season"])
        .bind(&profile.text["elo_manual_opt"])
        .bind(profile.status.parse::<i64>().unwrap_or(1))
        .bind(i64::from(profile.outreach))
        .bind(serde_json::to_string(&regions)?)
        .bind(i64::from(judge))
        .bind(i64::from(viewer.is_superuser))
        .bind(viewer.id)
        .execute(&mut *tx)
        .await?;
    if updated.rows_affected() == 0 {
        return Ok(false);
    }
    team_names(&mut tx, id).await?;
    tx.commit().await?;
    Ok(true)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let raw = request
        .uri()
        .path()
        .strip_prefix("/core/debater/")
        .and_then(|path| path.strip_suffix("/edit-profile/"))
        .unwrap_or_default();
    let Some(id) = raw.parse::<i64>().ok().filter(|_| is_django_integer(raw)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_authenticated {
        return login::redirect_to_login(request.uri());
    }
    let row = match sqlx::query(QUERY)
        .bind(id)
        .fetch_optional(&state.pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    if !user.is_superuser && row.get::<Option<i64>, _>("owner") != user.id {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Ok(initial) = Profile::original(&row) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
    let path = format!("/core/debaters/{id}");
    let (fields, errors) = if parts.method == Method::POST {
        let Ok(body) = to_bytes(body, 128 * 1024).await else {
            return StatusCode::PAYLOAD_TOO_LARGE.into_response();
        };
        let values = login::form_values(&body);
        if !login::csrf_valid(
            &parts.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return login::csrf_failure();
        }
        let mut update = Profile::submitted(&body);
        let errors = update.validate(&initial, state.current_season);
        if errors.is_empty() {
            return match save(&state.pool, &update, id, &user).await {
                Ok(true) => {
                    state.search.invalidate(&()).await;
                    redirect(
                        &state,
                        &parts.headers,
                        &path,
                        25,
                        "Profile updated successfully!",
                    )
                }
                Ok(false) => StatusCode::NOT_FOUND.into_response(),
                Err(error) => {
                    eprintln!("Profile update failed: {error:?}");
                    StatusCode::INTERNAL_SERVER_ERROR.into_response()
                }
            };
        }
        (Some(update), errors)
    } else {
        (None, Errors::new())
    };
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let form = fields
        .as_ref()
        .unwrap_or(&initial)
        .form(&initial, state.current_season, &errors);
    let status = STATES
        .iter()
        .find(|(code, _)| *code == initial.status)
        .map(|(_, label)| *label)
        .unwrap_or_default();
    let name = format!(
        "{} {}",
        initial.text["first_name"], initial.text["last_name"]
    );
    let object = json!({"name":name.trim(),"url":path,"school":{"name":row.get::<Option<&str>,_>("school")},
        "first_season":initial.text["first_season"],"latest_season":initial.text["latest_season"],"status":status});
    render(
        &state,
        &user,
        &parts.uri,
        &parts.headers,
        "debater/debater_profile_edit.html",
        context! {form,object,csrf=>token},
        Some(&csrf),
    )
}
