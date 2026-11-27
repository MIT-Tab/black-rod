use std::collections::HashMap;

use axum::{
    Json,
    body::to_bytes,
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::{AnyConnection, AnyPool, AssertSqlSafe, Row};

use crate::{ApiError, AppState, login, schedule::season_display, session};

#[derive(Clone, Copy)]
enum Kind {
    School { claim: bool },
    Debater,
    Team,
    Tournament { all: bool },
    Alias,
    Topic { create: bool },
    VideoTag { create: bool },
    ResourceTag { create: bool },
}

impl Kind {
    fn path(path: &str) -> Self {
        match path {
            "/core/schools/autocomplete/claim" => Self::School { claim: true },
            "/core/schools/autocomplete" => Self::School { claim: false },
            "/core/debaters/autocomplete" => Self::Debater,
            "/core/debater-alias-groups/autocomplete" => Self::Alias,
            "/core/tournaments/all_autocomplete" => Self::Tournament { all: true },
            "/core/tournaments/autocomplete" => Self::Tournament { all: false },
            "/core/motions/topics" => Self::Topic { create: true },
            "/core/motions/topics_no_create" => Self::Topic { create: false },
            "/core/videos/tags" => Self::VideoTag { create: true },
            "/core/videos/tags_no_create" => Self::VideoTag { create: false },
            "/core/resources/tags" => Self::ResourceTag { create: true },
            "/core/resources/tags_no_create" => Self::ResourceTag { create: false },
            _ => Self::Team,
        }
    }

    fn tag_permission(self) -> Option<&'static str> {
        match self {
            Self::Topic { create: true } => Some("add_motiontopic"),
            Self::VideoTag { create: true } => Some("taggit.add_tag"),
            Self::ResourceTag { create: true } => Some("add_resourcetag"),
            _ => None,
        }
    }

    fn tag_table(self) -> Option<&'static str> {
        match self {
            Self::Topic { .. } => Some("core_motiontopic"),
            Self::VideoTag { .. } => Some("taggit_tag"),
            Self::ResourceTag { .. } => Some("core_resourcetag"),
            _ => None,
        }
    }

    fn is_tag(self) -> bool {
        matches!(
            self,
            Self::Topic { .. } | Self::VideoTag { .. } | Self::ResourceTag { .. }
        )
    }

    fn fields(
        self,
    ) -> (
        &'static str,
        &'static str,
        &'static str,
        &'static [&'static str],
    ) {
        match self {
            Self::Topic { .. } => ("x.name", "core_motiontopic x", "x.id", &["x.name"]),
            Self::VideoTag { .. } => ("x.name", "taggit_tag x", "x.id", &["x.name"]),
            Self::ResourceTag { .. } => ("x.name", "core_resourcetag x", "x.id", &["x.name"]),
            Self::School {..} => (
                "x.name, CAST(CASE WHEN x.temporary THEN 1 ELSE 0 END AS BIGINT) AS temporary,
                 CAST(CASE WHEN x.synthetic THEN 1 ELSE 0 END AS BIGINT) AS synthetic",
                "core_school x", "x.name, x.id", &["x.name", "x.short_name"],
            ),
            Self::Debater => (
                "x.first_name, x.last_name, COALESCE(s.name, 'Unaffiliated') AS school,
                 CAST(CASE WHEN x.temporary THEN 1 ELSE 0 END AS BIGINT) AS temporary,
                 CAST(CASE WHEN x.synthetic THEN 1 ELSE 0 END AS BIGINT) AS synthetic",
                "core_debater x LEFT JOIN core_school s ON s.id = x.school_id",
                "x.id DESC", &["x.first_name", "x.last_name", "s.name"],
            ),
            Self::Team => (
                "x.name, CAST(CASE WHEN x.synthetic THEN 1 ELSE 0 END AS BIGINT) AS synthetic",
                "core_team x", "x.name, x.id", &["x.name"],
            ),
            Self::Tournament {..} => ("x.name, x.season", "core_tournament x", "x.date", &["x.name"]),
            Self::Alias => (
                "x.label AS name, CAST((SELECT COUNT(*) FROM core_debater d
                   WHERE d.alias_group_id = x.id AND NOT d.temporary AND NOT d.synthetic) AS BIGINT) AS count",
                "core_debateraliasgroup x", "x.label, x.id", &["x.label"],
            ),
        }
    }
}

fn synthetic(value: &str) -> Option<bool> {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" => Some(true),
        "false" | "0" | "no" => Some(false),
        _ => None,
    }
}

pub(crate) fn quote_pattern(value: &str) -> String {
    let value = value
        .replace('!', "!!")
        .replace('%', "!%")
        .replace('_', "!_");
    format!("%{value}%")
}

fn forwarded_id(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| value.as_str()?.parse().ok())
}

async fn lookup(
    pool: &AnyPool,
    resource: Kind,
    include_temporary: bool,
    remote: Option<bool>,
    forwarded: &Value,
    query: &str,
    page: i64,
) -> Result<Option<Value>, ApiError> {
    let (projection, source, order, columns) = resource.fields();
    let mut restriction = Vec::new();
    match resource {
        Kind::School { claim } => {
            if !include_temporary {
                restriction.push("NOT x.temporary".to_owned());
            }
            if let Some(synthetic) = remote.or(claim.then_some(false)) {
                restriction.push(
                    if synthetic {
                        "x.synthetic"
                    } else {
                        "NOT x.synthetic"
                    }
                    .to_owned(),
                );
            }
        }
        Kind::Debater => {
            if !include_temporary {
                restriction.push("NOT x.temporary".to_owned());
            }
            restriction.push(
                if remote == Some(true) {
                    "x.synthetic"
                } else {
                    "NOT x.synthetic"
                }
                .to_owned(),
            );
        }
        Kind::Team => {
            restriction.push(
                if remote == Some(true) {
                    "x.synthetic"
                } else {
                    "NOT x.synthetic"
                }
                .to_owned(),
            );
        }
        Kind::Tournament { all: false } => restriction.push(
            "NOT EXISTS (
            SELECT 1 FROM core_teamresult r WHERE r.tournament_id = x.id
        ) AND NOT EXISTS (SELECT 1 FROM core_speakerresult r WHERE r.tournament_id = x.id)"
                .to_owned(),
        ),
        _ => (),
    }

    let mut id = None;
    let mut school = None;
    let mut patterns = Vec::new();
    let whole_words =
        !resource.is_tag() && !matches!(resource, Kind::Alias | Kind::Tournament { .. });
    let query = if whole_words { query.trim() } else { query };
    if whole_words && !query.is_empty() && query.bytes().all(|byte| byte.is_ascii_digit()) {
        let Ok(value) = query.parse::<i64>() else {
            return Ok(Some(json!({"results":[],"pagination":{"more":false}})));
        };
        id = Some(value);
        restriction.push("x.id = $1".into());
    } else if !query.is_empty() {
        let pattern = quote_pattern(query);
        patterns.push(if resource.is_tag() {
            pattern[1..].to_owned()
        } else {
            pattern
        });
        if whole_words && query.split_whitespace().count() > 1 {
            patterns.extend(query.split_whitespace().map(quote_pattern));
            patterns.truncate(64);
        }
        let mut alternatives = Vec::new();
        for index in 1..=patterns.len() {
            let term = format!("${index}");
            alternatives.extend(
                columns
                    .iter()
                    .map(|column| format!("LOWER({column}) LIKE LOWER({term}) ESCAPE '!'")),
            );
            if matches!(resource, Kind::Team) {
                alternatives.push(format!("EXISTS (
                    SELECT 1 FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id
                    LEFT JOIN core_school s ON s.id = d.school_id WHERE td.team_id = x.id AND
                    (LOWER(d.first_name) LIKE LOWER({term}) ESCAPE '!' OR LOWER(d.last_name) LIKE LOWER({term}) ESCAPE '!'
                     OR LOWER(s.name) LIKE LOWER({term}) ESCAPE '!')
                )"));
            }
        }
        restriction.push(format!("({})", alternatives.join(" OR ")));
    }
    let mut args = patterns.len() + usize::from(id.is_some());
    if matches!(resource, Kind::Debater)
        && let Some(value) = forwarded_id(&forwarded["school"]).filter(|id| *id != 0)
    {
        args += 1;
        school = Some(value);
        restriction.push(format!("x.school_id = ${args}"));
    }
    let limit = args + 1;
    let offset = args + 2;
    let clause = if restriction.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", restriction.join(" AND "))
    };
    let sql = format!(
        "SELECT CAST(x.id AS BIGINT) AS id, {projection} FROM {source}{clause} ORDER BY {order} LIMIT ${limit} OFFSET ${offset}"
    );
    let mut statement = sqlx::query(AssertSqlSafe(sql));
    if let Some(id) = id {
        statement = statement.bind(id);
    }
    for pattern in patterns {
        statement = statement.bind(pattern);
    }
    if let Some(school) = school {
        statement = statement.bind(school);
    }
    let offset = (page - 1).saturating_mul(10);
    let rows = statement.bind(11_i64).bind(offset).fetch_all(pool).await?;
    if rows.is_empty() && page > 1 {
        return Ok(None);
    }
    let more = rows.len() > 10;
    let mut entries = Vec::new();
    for row in rows.iter().take(10) {
        let id: i64 = row.try_get("id")?;
        let name: String;
        let label = match resource {
            Kind::School { .. } => {
                name = row.try_get("name")?;
                let new = if row.try_get::<i64, _>("temporary")? != 0 {
                    " (New)"
                } else {
                    ""
                };
                let synthetic = if row.try_get::<i64, _>("synthetic")? != 0 {
                    " [Synthetic]"
                } else {
                    ""
                };
                format!("<{id}> {name}{new}{synthetic}")
            }
            Kind::Debater => {
                let first: &str = row.try_get("first_name")?;
                let last: &str = row.try_get("last_name")?;
                name = format!("{first} {last}");
                let school: &str = row.try_get("school")?;
                let new = if row.try_get::<i64, _>("temporary")? != 0 {
                    " (New)"
                } else {
                    ""
                };
                let synthetic = if row.try_get::<i64, _>("synthetic")? != 0 {
                    " [Synthetic]"
                } else {
                    ""
                };
                format!("<{id}> {}{new} ({school}){synthetic}", name.trim())
            }
            Kind::Team => {
                name = row.try_get("name")?;
                let members = sqlx::query(
                    "
                    SELECT d.first_name, d.last_name FROM core_team_debaters td
                    JOIN core_debater d ON d.id = td.debater_id
                    WHERE td.team_id = $1 AND NOT d.temporary AND NOT d.synthetic
                    ORDER BY d.id
                ",
                )
                .bind(id)
                .fetch_all(pool)
                .await?;
                let parts = members
                    .iter()
                    .map(|row| {
                        let first: &str = row.try_get("first_name")?;
                        let last: &str = row.try_get("last_name")?;
                        Ok(format!("{first} {last}").trim().to_owned())
                    })
                    .collect::<Result<Vec<_>, sqlx::Error>>()?;
                let members = if parts.is_empty() {
                    String::new()
                } else {
                    format!(" ({})", parts.join(", "))
                };
                let synthetic = if row.try_get::<i64, _>("synthetic")? != 0 {
                    " [Synthetic]"
                } else {
                    ""
                };
                format!("<{id}> {name}{members}{synthetic}")
            }
            Kind::Tournament { .. } => {
                name = row.try_get("name")?;
                let season: &str = row.try_get("season")?;
                let display = season
                    .parse()
                    .map_or_else(|_| season.to_owned(), season_display);
                format!("<{id}> {name} ({display})")
            }
            Kind::Topic { .. } | Kind::VideoTag { .. } | Kind::ResourceTag { .. } => {
                row.try_get("name")?
            }
            Kind::Alias => {
                name = row.try_get("name")?;
                let name = if name.is_empty() {
                    format!("Alias Group #{id}")
                } else {
                    name
                };
                let count: i64 = row.try_get("count")?;
                if count == 0 {
                    name
                } else {
                    let plural = if count == 1 { "" } else { "s" };
                    format!("{name} ({count} linked debater{plural})")
                }
            }
        };
        entries.push(json!({"id":id.to_string(), "text":label, "selected_text":label}));
    }
    Ok(Some(json!({"results":entries, "pagination":{"more":more}})))
}

pub(crate) async fn endpoint(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let params: HashMap<_, _> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let get = |key| {
        params
            .get(key)
            .map(|value| value.as_ref())
            .unwrap_or_default()
    };
    let forwarded = match serde_json::from_str::<Value>(
        params
            .get("forward")
            .map(|value| value.as_ref())
            .unwrap_or("{}"),
    ) {
        Ok(value) if value.is_object() => value,
        Ok(_) => {
            return (
                StatusCode::BAD_REQUEST,
                [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
                "Not a JSON object",
            )
                .into_response();
        }
        Err(_) => {
            return (
                StatusCode::BAD_REQUEST,
                [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
                "Invalid JSON data",
            )
                .into_response();
        }
    };
    let resource = Kind::path(uri.path());
    let check_temporary = matches!(resource, Kind::Debater | Kind::School { .. });
    let check_user = check_temporary || resource.tag_permission().is_some();
    let (include_temporary, can_create) = if check_user {
        let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        let permission = resource.tag_permission().unwrap_or("change_tournament");
        let permitted = session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false);
        (
            check_temporary && permitted,
            resource.tag_permission().is_some() && permitted,
        )
    } else {
        (false, false)
    };
    let page = get("page");
    let page = if page.is_empty() {
        1
    } else if page == "last" {
        // Select2 sends numeric pages. Invalid page markers use Django's 404 response.
        return StatusCode::NOT_FOUND.into_response();
    } else {
        match page.parse::<i64>().ok().filter(|page| *page > 0) {
            Some(page) => page,
            None => return StatusCode::NOT_FOUND.into_response(),
        }
    };
    match lookup(
        &state.pool,
        resource,
        include_temporary,
        synthetic(get("synthetic")),
        &forwarded,
        get("q"),
        page,
    )
    .await
    {
        Ok(Some(mut value)) => {
            let query = get("q");
            if can_create
                && page == 1
                && !query.is_empty()
                && let Some(results) = value["results"].as_array_mut()
                && !results.iter().any(|entry| {
                    entry["text"]
                        .as_str()
                        .is_some_and(|label| label.to_lowercase() == query.to_lowercase())
                })
            {
                results.push(
                    json!({"id": query, "text": format!("Create \"{query}\""), "create_id": true}),
                );
            }
            if check_user {
                ([(header::VARY, "Cookie")], Json(value)).into_response()
            } else {
                Json(value).into_response()
            }
        }
        Ok(None) => StatusCode::NOT_FOUND.into_response(),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

fn tag_slug(value: &str) -> String {
    let normalized = deunicode::deunicode_with_tofu(value, "").to_ascii_lowercase();
    let allowed: String = normalized
        .chars()
        .filter(|ch| {
            ch.is_ascii_alphanumeric() || ch.is_ascii_whitespace() || matches!(ch, '_' | '-')
        })
        .collect();
    allowed
        .split(|ch: char| ch.is_ascii_whitespace() || ch == '-')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("-")
        .trim_matches(['-', '_'])
        .to_owned()
}

pub(crate) async fn insert_tag(
    conn: &mut AnyConnection,
    table: &'static str,
    name: &str,
) -> Result<i64, sqlx::Error> {
    let lookup = format!("SELECT CAST(id AS BIGINT) FROM {table} WHERE name = $1");
    let insert = format!(
        "INSERT INTO {table} (name, slug) VALUES ($1, $2)
        ON CONFLICT DO NOTHING RETURNING CAST(id AS BIGINT)"
    );
    let base = tag_slug(name);
    for collision in 0_u64.. {
        if let Some(id) = sqlx::query_scalar::<_, i64>(AssertSqlSafe(lookup.as_str()))
            .bind(name)
            .fetch_optional(&mut *conn)
            .await?
        {
            return Ok(id);
        }
        let suffix = if collision == 0 {
            String::new()
        } else {
            format!("_{collision}")
        };
        let available = 100 - suffix.len();
        let slug = format!("{}{suffix}", &base[..base.len().min(available)]);
        if let Some(id) = sqlx::query_scalar::<_, i64>(AssertSqlSafe(insert.as_str()))
            .bind(name)
            .bind(slug)
            .fetch_optional(&mut *conn)
            .await?
        {
            return Ok(id);
        }
    }
    unreachable!("tag slug counter exhausted")
}

pub(crate) async fn create_tag(State(state): State<AppState>, request: Request) -> Response {
    let kind = Kind::path(request.uri().path());
    let (Some(table), Some(permission)) = (kind.tag_table(), kind.tag_permission()) else {
        return StatusCode::METHOD_NOT_ALLOWED.into_response();
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
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
    let Ok(user) = session::user(&state.pool, &parts.headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !session::has_permission(&state.pool, &user, permission)
        .await
        .unwrap_or(false)
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    let Some(name) = form
        .get("text")
        .filter(|name| name.chars().count() <= 100 && !name.contains('\0'))
    else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let Ok(mut conn) = state.pool.acquire().await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    match insert_tag(&mut conn, table, name).await {
        Ok(id) => (
            [(header::VARY, "Cookie")],
            Json(json!({"id": id.to_string(), "text": name})),
        )
            .into_response(),
        Err(error) => {
            eprintln!("Tag creation failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
