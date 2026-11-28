use std::collections::{BTreeMap, HashSet};

use axum::{
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyPool, Row, any::AnyRow};

use super::{AUTHORS, DETAIL, TAGS, TYPES};
use crate::{
    ApiError, AppState, autocomplete, flash, is_django_integer, login,
    memberships::sync_memberships,
    session::{self, User},
    tag_filter, web,
};

const LIST: &str = "/core/resources/";
const CLAIM: &str = "/core/claim-debater/request/";
const PROFILE: &str = "SELECT CAST(id AS BIGINT) AS id,
    TRIM(first_name || ' ' || last_name) AS name, CAST(user_id AS BIGINT) AS user_id
    FROM core_debater WHERE NOT synthetic AND NOT temporary";
const CONTENT_TYPE: &str = "SELECT CAST(id AS BIGINT) FROM django_content_type
    WHERE app_label='core' AND model='resource'";
const PERMISSIONS: [(&str, &str); 2] = [
    ("0", "Public (Discoverable by Google search)"),
    ("1", "Requires Login"),
];
type Errors = BTreeMap<&'static str, String>;

fn redirect(path: &str) -> Response {
    (StatusCode::FOUND, [(header::LOCATION, path)]).into_response()
}

fn url(id: i64) -> String {
    format!("{LIST}{id}")
}

fn error(error: ApiError) -> Response {
    eprintln!("Resource edit failed: {error:?}");
    StatusCode::INTERNAL_SERVER_ERROR.into_response()
}

#[derive(Default, Serialize)]
struct Fields {
    title: String,
    resource_type: String,
    content_link: String,
    description: String,
    usage_permissions: String,
    viewing_permission: String,
    authors: Vec<(i64, String)>,
    tags: Vec<String>,
}

impl Fields {
    fn existing(row: &AnyRow, authors: &[AnyRow], tags: &[AnyRow]) -> Result<Self, ApiError> {
        Ok(Self {
            title: row.try_get("title")?,
            resource_type: row.try_get("resource_type")?,
            content_link: row.try_get("content_link")?,
            description: row.try_get("description")?,
            usage_permissions: row.try_get("usage_permissions")?,
            viewing_permission: row.try_get::<i64, _>("permission")?.to_string(),
            authors: authors
                .iter()
                .map(|author| Ok((author.try_get("id")?, author.try_get("name")?)))
                .collect::<Result<_, sqlx::Error>>()?,
            tags: tags
                .iter()
                .map(|tag| tag.try_get("name"))
                .collect::<Result<_, _>>()?,
        })
    }

    async fn submitted(
        pool: &AnyPool,
        user: &User,
        owned: &[(i64, String)],
        body: &[(String, String)],
    ) -> Result<(Self, Errors), ApiError> {
        let values = body
            .iter()
            .cloned()
            .collect::<std::collections::HashMap<_, _>>();
        let field = |name| values.get(name).map_or("", |value| value.trim());
        let mut errors = Errors::new();
        let mut fields = Self {
            title: field("title").to_owned(),
            resource_type: field("resource_type").to_owned(),
            content_link: field("content_link").to_owned(),
            description: field("description").to_owned(),
            usage_permissions: field("usage_permissions").to_owned(),
            viewing_permission: field("viewing_permission").to_owned(),
            authors: Vec::new(),
            tags: tag_filter::parse(
                body.iter()
                    .filter(|(name, _)| name == "tags")
                    .map(|(_, value)| value),
            ),
        };
        for (key, value, max) in [
            ("title", &fields.title, 255),
            ("content_link", &fields.content_link, 4096),
        ] {
            if value.is_empty() {
                errors.insert(key, "This field is required.".to_owned());
            } else if value.chars().count() > max {
                errors.insert(
                    key,
                    format!(
                        "Ensure this value has at most {max} characters (it has {}).",
                        value.chars().count()
                    ),
                );
            }
        }
        for (key, value) in [
            ("title", &fields.title),
            ("description", &fields.description),
            ("usage_permissions", &fields.usage_permissions),
        ] {
            if value.contains('\0') {
                errors.insert(key, "Null characters are not allowed.".to_owned());
            }
        }
        if !fields.content_link.is_empty() && !errors.contains_key("content_link") {
            if !fields.content_link.contains("://") {
                fields.content_link = format!("http://{}", fields.content_link);
            }
            let valid = !fields.content_link.chars().any(char::is_whitespace)
                && url::Url::parse(&fields.content_link).is_ok_and(|url| {
                    matches!(url.scheme(), "http" | "https" | "ftp" | "ftps")
                        && url.host_str().is_some_and(|host| {
                            host == "localhost" || host.contains('.') || host.starts_with('[')
                        })
                });
            if !valid {
                errors.insert("content_link", "Enter a valid URL.".to_owned());
            }
        }
        for (name, value, choices) in [
            ("resource_type", fields.resource_type.as_str(), TYPES),
            (
                "viewing_permission",
                fields.viewing_permission.as_str(),
                &PERMISSIONS[..],
            ),
        ] {
            if value.is_empty() {
                errors.insert(name, "This field is required.".to_owned());
            } else if !choices.iter().any(|(key, _)| *key == value) {
                errors.insert(
                    name,
                    format!("Select a valid choice. {value} is not one of the available choices."),
                );
            }
        }
        if fields
            .tags
            .iter()
            .any(|tag| tag.chars().count() > 100 || tag.contains('\0'))
        {
            errors.insert(
                "tags",
                "Ensure each tag has at most 100 characters.".to_owned(),
            );
        }
        let mut seen = HashSet::new();
        for (_, submitted) in body.iter().filter(|(name, _)| name == "authors") {
            if !seen.insert(submitted.clone()) {
                continue;
            }
            let Ok(id) = submitted.parse::<i64>() else {
                errors.insert("authors", format!("“{submitted}” is not a valid value."));
                continue;
            };
            let query = format!("{PROFILE} AND id=$1");
            if let Some(row) = sqlx::query(sqlx::AssertSqlSafe(query))
                .bind(id)
                .fetch_optional(pool)
                .await?
            {
                fields.authors.push((id, row.try_get("name")?));
            } else {
                errors.insert(
                    "authors",
                    format!(
                        "Select a valid choice. {submitted} is not one of the available choices."
                    ),
                );
            }
        }
        if !errors.contains_key("authors") {
            if fields.authors.is_empty() {
                errors.insert("authors", "This field is required.".to_owned());
            } else if !user.is_superuser
                && !owned.is_empty()
                && !fields
                    .authors
                    .iter()
                    .any(|(id, _)| owned.iter().any(|(own, _)| own == id))
            {
                errors.insert("authors","You must include yourself as an author. Please select one of your claimed debater profiles.".to_owned());
            }
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
    fn from_uri(uri: &Uri) -> Option<Self> {
        let path = uri.path().strip_prefix(LIST)?;
        if path == "create" {
            return Some(Self::Create);
        }
        let (raw, action) = path.split_once('/')?;
        if !is_django_integer(raw) {
            return None;
        }
        let id = raw.parse().ok()?;
        match action {
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

enum Write {
    Saved(i64),
    Deleted,
    Missing,
    Denied,
}

async fn save(
    pool: &AnyPool,
    action: Action,
    user: &User,
    fields: &Fields,
) -> Result<Write, ApiError> {
    let mut tx = pool.begin().await?;
    if let Some(id) = action.id() {
        // Lock the resource before checking ownership and replacing its relations.
        let locked = sqlx::query("UPDATE core_resource SET id=id WHERE id=$1")
            .bind(id)
            .execute(&mut *tx)
            .await?;
        if locked.rows_affected() == 0 {
            return Ok(Write::Missing);
        }
        let current = sqlx::query(DETAIL)
            .bind(id)
            .bind(user.id)
            .fetch_one(&mut *tx)
            .await?;
        if !user.is_superuser && current.try_get::<i64, _>("author")? == 0 {
            return Ok(Write::Denied);
        }
    }
    let content_type = sqlx::query_scalar::<_, i64>(CONTENT_TYPE)
        .fetch_one(&mut *tx)
        .await?;
    if let Action::Delete(id) = action {
        sqlx::query("DELETE FROM core_taggedresource WHERE content_type_id=$1 AND object_id=$2")
            .bind(content_type)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        sqlx::query("DELETE FROM core_resource_authors WHERE resource_id=$1")
            .bind(id)
            .execute(&mut *tx)
            .await?;
        sqlx::query("DELETE FROM core_resource WHERE id=$1")
            .bind(id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        return Ok(Write::Deleted);
    }
    let query = match action {
        Action::Create => "INSERT INTO core_resource (title,resource_type,content_link,description,
            usage_permissions,viewing_permission,created_by_id,created_at,updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING CAST(id AS BIGINT)",
        _ => "UPDATE core_resource SET title=$1,resource_type=$2,content_link=$3,description=$4,
            usage_permissions=$5,viewing_permission=$6,updated_at=CURRENT_TIMESTAMP WHERE id=$7
            RETURNING CAST(id AS BIGINT)",
    };
    let id = sqlx::query_scalar::<_, i64>(query)
        .bind(&fields.title)
        .bind(&fields.resource_type)
        .bind(&fields.content_link)
        .bind(&fields.description)
        .bind(&fields.usage_permissions)
        .bind(fields.viewing_permission.parse::<i64>().unwrap_or_default())
        .bind(action.id().or(user.id))
        .fetch_one(&mut *tx)
        .await?;
    let authors = fields.authors.iter().map(|(author, _)| *author).collect();
    sync_memberships(
        &mut tx,
        ["core_resource_authors", "resource_id", "debater_id"],
        id,
        &authors,
        true,
    )
    .await?;
    let existing = sqlx::query_scalar::<_,i64>("SELECT CAST(tag_id AS BIGINT) FROM core_taggedresource WHERE content_type_id=$1 AND object_id=$2")
        .bind(content_type).bind(id).fetch_all(&mut *tx).await?;
    let mut tags = Vec::new();
    for tag in &fields.tags {
        tags.push(autocomplete::insert_tag(&mut tx, "core_resourcetag", tag).await?);
    }
    for tag in &existing {
        if !tags.contains(tag) {
            sqlx::query("DELETE FROM core_taggedresource WHERE content_type_id=$1 AND object_id=$2 AND tag_id=$3")
                .bind(content_type).bind(id).bind(tag).execute(&mut *tx).await?;
        }
    }
    for tag in tags {
        if !existing.contains(&tag) {
            sqlx::query("INSERT INTO core_taggedresource (content_type_id,object_id,tag_id) VALUES ($1,$2,$3)")
                .bind(content_type).bind(id).bind(tag).execute(&mut *tx).await?;
        }
    }
    tx.commit().await?;
    Ok(Write::Saved(id))
}

async fn page(state: &AppState, request: Request, action: Action) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    let owned = if user.is_superuser {
        Vec::new()
    } else {
        let query = format!("{PROFILE} AND user_id=$1 ORDER BY id");
        sqlx::query(sqlx::AssertSqlSafe(query))
            .bind(user.id)
            .fetch_all(&state.pool)
            .await?
            .iter()
            .map(|row| Ok((row.try_get("id")?, row.try_get("name")?)))
            .collect::<Result<Vec<_>, sqlx::Error>>()?
    };
    let mut obj = Value::Null;
    let mut fields = if let Some(id) = action.id() {
        let Some(record) = sqlx::query(DETAIL)
            .bind(id)
            .bind(user.id)
            .fetch_optional(&state.pool)
            .await?
        else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        let permission = if matches!(action, Action::Edit(_)) {
            "change_resource"
        } else {
            "delete_resource"
        };
        if !user.is_superuser
            && (record.try_get::<i64, _>("author")? == 0
                || !session::has_permission(&state.pool, &user, permission).await?)
        {
            return Ok(web::permission_denied(state, &user, &uri));
        }
        let authors = sqlx::query(AUTHORS).bind(id).fetch_all(&state.pool).await?;
        let tags = sqlx::query(TAGS).bind(id).fetch_all(&state.pool).await?;
        obj = super::object(&record,id,
            authors.iter().map(|author| json!({"name":author.get::<&str,_>("name"),
                "url":format!("/core/debaters/{}",author.get::<i64,_>("id"))})).collect(),
            tags.iter().map(|tag| json!({"name":tag.get::<&str,_>("name"), "slug":tag.get::<&str,_>("slug")})).collect());
        obj["url"] = json!(url(id));
        Fields::existing(&record, &authors, &tags)?
    } else {
        if !user.is_superuser && owned.is_empty() {
            let mut response = redirect(CLAIM);
            if let Some(secret) = state.secret.as_deref() {
                flash::push(
                    &mut response,
                    request.headers(),
                    secret,
                    40,
                    "You must first claim a debater profile to post a resource.",
                    secure(state, request.headers()),
                );
            }
            return Ok(response);
        }
        Fields {
            resource_type: "other".to_owned(),
            viewing_permission: "0".to_owned(),
            authors: owned.clone(),
            ..Fields::default()
        }
    };

    let method = request.method().clone();
    let (parts, body) = request.into_parts();
    let mut errors = Errors::new();
    if method == Method::POST {
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
            (fields, errors) = Fields::submitted(&state.pool, &user, &owned, &body).await?;
        }
        if errors.is_empty() {
            return Ok(match save(&state.pool, action, &user, &fields).await? {
                Write::Saved(id) => redirect(&url(id)),
                Write::Deleted => redirect(LIST),
                Write::Missing => StatusCode::NOT_FOUND.into_response(),
                Write::Denied => web::permission_denied(state, &user, &uri),
            });
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let template = match action {
        Action::Create => "resources/create.html",
        Action::Edit(_) => "resources/update.html",
        Action::Delete(_) => "resources/delete.html",
    };
    let common = web::base_context(state, &user, &uri);
    let bindings = context! {object=>obj,fields,errors,csrf=>token, resource_types=>TYPES, resource_permissions=>PERMISSIONS,
    resource_tag_url=>if user.is_superuser{"/core/resources/tags"}else{"/core/resources/tags_no_create"},
    resource_can_create_tags=>user.is_superuser};
    let html = match web::render(template, context! {..bindings,..common}) {
        Ok(html) => html,
        Err(reason) => {
            eprintln!("Resource form template failed: {reason}");
            return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure(state, &parts.headers));
    Ok(response)
}

fn secure(state: &AppState, headers: &HeaderMap) -> bool {
    state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"))
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let Some(action) = Action::from_uri(request.uri()) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    page(&state, request, action).await.unwrap_or_else(error)
}
