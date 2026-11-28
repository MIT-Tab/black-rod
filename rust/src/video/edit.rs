use std::collections::BTreeMap;

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyConnection, AnyPool, Row};

use super::{labels, metadata};
use crate::{ApiError, AppState, autocomplete, is_django_integer, login, session, tag_filter, web};

const DIRECTORY: &str = "/core/videos/";
const PARTICIPANTS: [(&str, &str); 5] = [
    ("pm", "PM"),
    ("mg", "MG"),
    ("lo", "LO"),
    ("mo", "MO"),
    ("tournament", "Tournament"),
];
const ROUND_VALUES: [i64; 15] = [0, 1, 2, 3, 4, 5, 14, 6, 7, 8, 9, 10, 11, 12, 13];
const CONTENT_TYPE: &str =
    "SELECT CAST(id AS BIGINT) FROM django_content_type WHERE app_label='core' AND model='video'";
type Errors = BTreeMap<&'static str, String>;

#[derive(Serialize, Default)]
struct Choice {
    value: String,
    label: String,
}

#[derive(Serialize)]
struct Fields {
    people: BTreeMap<&'static str, Choice>,
    round: String,
    permissions: String,
    case: String,
    description: String,
    link: String,
    password: String,
    tags: Vec<String>,
}

impl Default for Fields {
    fn default() -> Self {
        Self {
            people: PARTICIPANTS
                .iter()
                .map(|(role, _)| (*role, Choice::default()))
                .collect(),
            round: "0".to_owned(),
            permissions: "2".to_owned(),
            case: String::new(),
            description: String::new(),
            link: String::new(),
            password: String::new(),
            tags: Vec::new(),
        }
    }
}

impl Fields {
    async fn load(conn: &mut AnyConnection, id: i64) -> Result<Option<(Self, Value)>, ApiError> {
        let record = sqlx::query(
            "SELECT CAST(round AS BIGINT) AS round,CAST(permissions AS BIGINT) AS permissions,
            link,password,description,\"case\" FROM core_video WHERE id=$1",
        )
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?;
        let Some(record) = record else {
            return Ok(None);
        };
        let mut object = metadata(conn, id).await?;
        object["url"] = json!(format!("{DIRECTORY}{id}"));
        let people = PARTICIPANTS
            .iter()
            .map(|(role, _)| {
                (
                    *role,
                    Choice {
                        value: object[role]["id"]
                            .as_i64()
                            .map(|id| id.to_string())
                            .unwrap_or_default(),
                        label: object[role]["name"].as_str().unwrap_or_default().to_owned(),
                    },
                )
            })
            .collect();
        let fields = Self {
            people,
            round: record.try_get::<i64, _>("round")?.to_string(),
            permissions: record.try_get::<i64, _>("permissions")?.to_string(),
            case: record.try_get("case")?,
            description: record.try_get("description")?,
            link: record.try_get("link")?,
            password: record.try_get("password")?,
            tags: object["tags"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|tag| tag["name"].as_str())
                .map(str::to_owned)
                .collect(),
        };
        Ok(Some((fields, object)))
    }

    async fn submitted(
        pool: &AnyPool,
        body: &[(String, String)],
    ) -> Result<(Self, Errors), ApiError> {
        let values = body
            .iter()
            .cloned()
            .collect::<std::collections::HashMap<_, _>>();
        let raw = |name| values.get(name).map_or("", String::as_str).trim();
        let mut errors = Errors::new();
        let mut fields = Self {
            round: raw("round").to_owned(),
            permissions: raw("permissions").to_owned(),
            case: raw("case").to_owned(),
            description: raw("description").to_owned(),
            link: raw("link").to_owned(),
            password: raw("password").to_owned(),
            tags: tag_filter::parse(
                body.iter()
                    .filter(|(name, _)| name == "tags")
                    .map(|(_, value)| value),
            ),
            ..Self::default()
        };
        for (role, _) in PARTICIPANTS {
            let input = raw(role);
            if input.is_empty() {
                errors.insert(role, "This field is required.".to_owned());
                continue;
            }
            let (id, name) = if let Ok(id) = input.parse::<i64>() {
                let sql = if role == "tournament" {
                    "SELECT name FROM core_tournament WHERE id=$1"
                } else {
                    "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1 AND NOT synthetic AND NOT temporary"
                };
                (
                    id,
                    sqlx::query_scalar::<_, String>(sql)
                        .bind(id)
                        .fetch_optional(pool)
                        .await?,
                )
            } else {
                (0, None)
            };
            if let Some(name) = name {
                fields.people.insert(
                    role,
                    Choice {
                        value: id.to_string(),
                        label: name,
                    },
                );
            } else {
                errors.insert(
                    role,
                    "Select a valid choice. That choice is not one of the available choices."
                        .to_owned(),
                );
            }
        }
        for (name, value, choices) in [
            ("round", fields.round.as_str(), ROUND_VALUES.as_slice()),
            (
                "permissions",
                fields.permissions.as_str(),
                &[0_i64, 1, 2][..],
            ),
        ] {
            if value.is_empty() {
                errors.insert(name, "This field is required.".to_owned());
            } else if !choices.iter().any(|choice| choice.to_string() == value) {
                errors.insert(
                    name,
                    format!("Select a valid choice. {value} is not one of the available choices."),
                );
            }
        }
        if fields.link.is_empty() {
            errors.insert("link", "This field is required.".to_owned());
        }
        for (name, value, max) in [
            ("link", &fields.link, 4096),
            ("password", &fields.password, 1024),
            ("case", &fields.case, usize::MAX),
            ("description", &fields.description, usize::MAX),
        ] {
            if value.contains('\0') {
                errors.insert(name, "Null characters are not allowed.".to_owned());
            } else if value.chars().count() > max {
                errors.insert(
                    name,
                    format!(
                        "Ensure this value has at most {max} characters (it has {}).",
                        value.chars().count()
                    ),
                );
            }
        }
        if fields
            .tags
            .iter()
            .any(|tag| tag.contains('\0') || tag.chars().count() > 100)
        {
            errors.insert(
                "tags",
                "Ensure each tag has at most 100 characters.".to_owned(),
            );
        }
        Ok((fields, errors))
    }
}

#[derive(Clone, Copy)]
enum Action {
    Create,
    Edit(i64),
    Delete(i64),
}

impl Action {
    fn read(uri: &Uri) -> Option<Self> {
        let path = uri.path().strip_prefix(DIRECTORY)?;
        if path == "create" {
            return Some(Self::Create);
        }
        let (raw, mode) = path.split_once('/')?;
        if !is_django_integer(raw) {
            return None;
        }
        let id = raw.parse().ok()?;
        match mode {
            "edit" => Some(Self::Edit(id)),
            "delete" => Some(Self::Delete(id)),
            _ => None,
        }
    }

    fn id(self) -> Option<i64> {
        match self {
            Self::Create => None,
            Self::Edit(id) | Self::Delete(id) => Some(id),
        }
    }
}

async fn save(pool: &AnyPool, action: Action, fields: &Fields) -> Result<Option<i64>, ApiError> {
    let mut tx = pool.begin().await?;
    let content_type = sqlx::query_scalar::<_, i64>(CONTENT_TYPE)
        .fetch_one(&mut *tx)
        .await?;
    if let Action::Delete(id) = action {
        sqlx::query("DELETE FROM taggit_taggeditem WHERE content_type_id=$1 AND object_id=$2")
            .bind(content_type)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        let rows = sqlx::query("DELETE FROM core_video WHERE id=$1")
            .bind(id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        return Ok((rows.rows_affected() > 0).then_some(id));
    }
    let query = if action.id().is_some() {
        "UPDATE core_video SET pm_id=$1,mg_id=$2,lo_id=$3,mo_id=$4,tournament_id=$5,
        round=$6,permissions=$7,\"case\"=$8,description=$9,link=$10,password=$11 WHERE id=$12
        RETURNING CAST(id AS BIGINT)"
    } else {
        "INSERT INTO core_video (pm_id,mg_id,lo_id,mo_id,tournament_id,round,permissions,\"case\",description,link,password)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING CAST(id AS BIGINT)"
    };
    let mut update = sqlx::query_scalar::<_, i64>(query);
    for (role, _) in PARTICIPANTS {
        update = update.bind(fields.people[role].value.parse::<i64>().ok());
    }
    update = update
        .bind(fields.round.parse::<i64>().ok())
        .bind(fields.permissions.parse::<i64>().ok())
        .bind(&fields.case)
        .bind(&fields.description)
        .bind(&fields.link)
        .bind(&fields.password);
    if let Some(id) = action.id() {
        update = update.bind(id);
    }
    let Some(id) = update.fetch_optional(&mut *tx).await? else {
        return Ok(None);
    };
    let existing=sqlx::query_scalar::<_,i64>("SELECT CAST(tag_id AS BIGINT) FROM taggit_taggeditem WHERE content_type_id=$1 AND object_id=$2")
        .bind(content_type).bind(id).fetch_all(&mut *tx).await?;
    let mut next = Vec::new();
    for tag in &fields.tags {
        next.push(autocomplete::insert_tag(&mut tx, "taggit_tag", tag).await?);
    }
    for tag in existing.iter().filter(|tag| !next.contains(tag)) {
        sqlx::query(
            "DELETE FROM taggit_taggeditem WHERE content_type_id=$1 AND object_id=$2 AND tag_id=$3",
        )
        .bind(content_type)
        .bind(id)
        .bind(tag)
        .execute(&mut *tx)
        .await?;
    }
    for tag in next.iter().filter(|tag| !existing.contains(tag)) {
        sqlx::query("INSERT INTO taggit_taggeditem (content_type_id,object_id,tag_id) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING")
            .bind(content_type).bind(id).bind(tag).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(Some(id))
}

async fn view(state: &AppState, request: Request, action: Action) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    let perm = match action {
        Action::Create => "add_video",
        Action::Edit(_) => "change_video",
        Action::Delete(_) => "delete_video",
    };
    if !session::has_permission(&state.pool, &user, perm).await? {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let (mut fields, object) = if let Some(id) = action.id() {
        let Some(values) = Fields::load(&mut *state.pool.acquire().await?, id).await? else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        values
    } else {
        (Fields::default(), Value::Null)
    };
    let (parts, body) = request.into_parts();
    let mut errors = Errors::new();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|url| url.starts_with("https://"));
    if parts.method == Method::POST {
        let body = match login::form_fields(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let values = body
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
        if !matches!(action, Action::Delete(_)) {
            (fields, errors) = Fields::submitted(&state.pool, &body).await?;
        }
        if errors.is_empty() {
            return Ok(match save(&state.pool, action, &fields).await? {
                None => StatusCode::NOT_FOUND.into_response(),
                Some(id) => {
                    let location = if matches!(action, Action::Delete(_)) {
                        DIRECTORY.to_owned()
                    } else {
                        format!("{DIRECTORY}{id}")
                    };
                    (StatusCode::FOUND, [(header::LOCATION, location)]).into_response()
                }
            });
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let common = web::base_context(state, &user, &uri);
    let options = ROUND_VALUES
        .into_iter()
        .map(|value| (value.to_string(), labels(value, 0).0))
        .collect::<Vec<_>>();
    let permissions = (0..3)
        .map(|value| (value.to_string(), labels(0, value).1))
        .collect::<Vec<_>>();
    let creating = matches!(action, Action::Create);
    let template = match action {
        Action::Create => "videos/create.html",
        Action::Edit(_) => "videos/update.html",
        Action::Delete(_) => "videos/delete.html",
    };
    let embed_link = super::embed_link(
        object["link"].as_str().unwrap_or_default(),
        object["password"].as_str().unwrap_or_default(),
    );
    let bindings = context! {object, fields, errors, options, permissions, creating, csrf=>token, embed=>embed_link.is_some(),embed_link};
    let html = web::render(template, context! {..common,..bindings}).map_err(|error| {
        eprintln!("Video editor template failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let Some(action) = Action::read(request.uri()) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    view(&state, request, action).await.unwrap_or_else(|error| {
        eprintln!("Video editor failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
