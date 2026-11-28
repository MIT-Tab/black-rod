mod actions;
mod matching;

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::Duration,
};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::{Deserialize, Serialize};
use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};

use crate::{ApiError, AppState, flash, login, web};

const VERSION: &str = ":1:synthetic_resolution_suggestions_version";
pub(super) const PEOPLE: &str = "
    SELECT CAST(d.id AS BIGINT) AS id,d.first_name,d.last_name,d.first_season,d.latest_season,
      CAST(d.school_id AS BIGINT) AS school_id,s.name AS school_name,
      CASE WHEN d.temporary THEN 1 ELSE 0 END AS temporary,
      CASE WHEN d.synthetic THEN 1 ELSE 0 END AS synthetic
    FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
";

#[derive(Clone, Deserialize, Serialize)]
pub(super) struct School {
    id: i64,
    name: String,
    short_name: String,
    display_name: String,
    temporary: bool,
    synthetic: bool,
}

#[derive(Clone, Deserialize, Serialize)]
pub(super) struct Affiliation {
    name: String,
}

#[derive(Clone, Deserialize, Serialize)]
pub(super) struct Person {
    id: i64,
    first: String,
    last: String,
    name: String,
    display_name: String,
    temporary: bool,
    synthetic: bool,
    first_season: Option<String>,
    latest_season: Option<String>,
    school_id: Option<i64>,
    school: Option<Affiliation>,
    total_results: i64,
}

impl Person {
    pub(super) fn read(row: &AnyRow) -> Result<Self, sqlx::Error> {
        let first: String = row.try_get("first_name")?;
        let last: String = row.try_get("last_name")?;
        let name = format!("{first} {last}").trim().to_owned();
        let temporary = row.try_get::<i32, _>("temporary")? == 1;
        Ok(Self {
            id: row.try_get("id")?,
            first,
            last,
            display_name: display(&name, temporary),
            name,
            temporary,
            synthetic: row.try_get::<i32, _>("synthetic")? == 1,
            first_season: row.try_get("first_season")?,
            latest_season: row.try_get("latest_season")?,
            school_id: row.try_get("school_id")?,
            school: row
                .try_get::<Option<String>, _>("school_name")?
                .map(|name| Affiliation { name }),
            total_results: 0,
        })
    }
}

fn display(name: &str, temporary: bool) -> String {
    if temporary {
        format!("{name} (New)")
    } else {
        name.to_owned()
    }
}

fn sql_ids(ids: &[i64]) -> String {
    if ids.is_empty() {
        return "NULL".to_owned();
    }
    ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",")
}

pub(super) async fn schools(
    pool: &AnyPool,
    synthetic: bool,
    selected: &[i64],
    limit: bool,
) -> Result<Vec<School>, ApiError> {
    let flag = if synthetic {
        "synthetic"
    } else {
        "NOT synthetic AND NOT temporary"
    };
    let where_ids = if selected.is_empty() {
        String::new()
    } else {
        format!(" AND id IN ({})", sql_ids(selected))
    };
    let order = if synthetic { "id DESC" } else { "name,id" };
    let limit = if selected.is_empty() && limit {
        " LIMIT 300"
    } else {
        ""
    };
    let sql=format!("SELECT CAST(id AS BIGINT) AS id,name,short_name,
        CASE WHEN temporary THEN 1 ELSE 0 END AS temporary, CASE WHEN synthetic THEN 1 ELSE 0 END AS synthetic
        FROM core_school WHERE {flag}{where_ids} ORDER BY {order}{limit}");
    Ok(sqlx::query(AssertSqlSafe(sql))
        .fetch_all(pool)
        .await?
        .iter()
        .map(|row| {
            let name: String = row.try_get("name")?;
            let temporary = row.try_get::<i32, _>("temporary")? == 1;
            Ok(School {
                id: row.try_get("id")?,
                short_name: row
                    .try_get::<Option<String>, _>("short_name")?
                    .unwrap_or_default(),
                display_name: display(&name, temporary),
                name,
                temporary,
                synthetic: row.try_get::<i32, _>("synthetic")? == 1,
            })
        })
        .collect::<Result<_, sqlx::Error>>()?)
}

#[derive(Debug, Default, Clone, PartialEq, Eq, Hash)]
struct Selection {
    debaters: Vec<i64>,
    schools: Vec<i64>,
}

impl Selection {
    fn is_empty(&self) -> bool {
        self.debaters.is_empty() && self.schools.is_empty()
    }

    async fn selected(&self, pool: &AnyPool) -> Result<(Vec<Person>, Vec<School>), ApiError> {
        let mut people = HashMap::new();
        if !self.debaters.is_empty() {
            let ids = sql_ids(&self.debaters);
            let sql = format!("{PEOPLE} WHERE d.synthetic AND d.id IN ({ids})");
            for row in sqlx::query(AssertSqlSafe(sql)).fetch_all(pool).await? {
                let person = Person::read(&row)?;
                people.insert(person.id, person);
            }
        }
        let mut schools = if self.schools.is_empty() {
            HashMap::new()
        } else {
            schools(pool, true, &self.schools, false)
                .await?
                .into_iter()
                .map(|school| (school.id, school))
                .collect()
        };
        Ok((
            self.debaters
                .iter()
                .filter_map(|id| people.remove(id))
                .collect(),
            self.schools
                .iter()
                .filter_map(|id| schools.remove(id))
                .collect(),
        ))
    }

    fn refresh_query(&self) -> String {
        url::form_urlencoded::Serializer::new(String::new())
            .append_pair("run", "1")
            .append_pair("refresh", "1")
            .extend_pairs(
                self.debaters
                    .iter()
                    .map(|id| ("synthetic_debaters", id.to_string())),
            )
            .extend_pairs(
                self.schools
                    .iter()
                    .map(|id| ("synthetic_schools", id.to_string())),
            )
            .finish()
    }

    fn parse(query: &str) -> (Self, bool, bool) {
        let mut scope = Self::default();
        let (mut ran, mut refresh) = (false, false);
        let mut people = HashSet::new();
        let mut schools = HashSet::new();
        for (field, value) in url::form_urlencoded::parse(query.as_bytes()) {
            match field.as_ref() {
                "run" => ran = value == "1",
                "refresh" => refresh = value == "1",
                "synthetic_debaters" => {
                    if let Ok(id) = value.trim().parse::<i64>()
                        && people.insert(id)
                    {
                        scope.debaters.push(id);
                    }
                }
                "synthetic_schools" => {
                    if let Ok(id) = value.trim().parse::<i64>()
                        && schools.insert(id)
                    {
                        scope.schools.push(id);
                    }
                }
                _ => {}
            }
        }
        (scope, ran, refresh)
    }
}

#[derive(Clone)]
pub(crate) struct Cache {
    entries: moka::future::Cache<(u64, Selection), Arc<matching::Suggestions>>,
    enabled: bool,
}

impl Cache {
    pub(crate) fn new(enabled: bool) -> Self {
        Self {
            entries: moka::future::Cache::builder()
                .max_capacity(32)
                .time_to_live(Duration::from_secs(300))
                .build(),
            enabled,
        }
    }

    async fn version(state: &AppState) -> u64 {
        if let Some(cache) = &state.memcached
            && let Some((2, version)) = cache.read_raw(VERSION).await
            && let Ok(text) = std::str::from_utf8(&version)
            && let Ok(version) = text.parse()
        {
            return version;
        }
        1
    }

    pub(super) async fn invalidate(state: &AppState) {
        if let Some(cache) = &state.memcached {
            let version = Self::version(state).await.saturating_add(1);
            let _ = cache
                .write_raw(VERSION, 2, version.to_string().as_bytes(), 0, true)
                .await;
        }
        state.synthetic_suggestions.entries.invalidate_all();
    }

    async fn suggestions(
        &self,
        state: &AppState,
        scope: Selection,
        refresh: bool,
    ) -> Result<(Arc<matching::Suggestions>, bool), ApiError> {
        if !self.enabled {
            return Ok((Arc::new(matching::build(&state.pool, &scope).await?), false));
        }
        let version = Self::version(state).await;
        let key = (version, scope);
        let shared = crate::cache::Memcached::key("synthetic-suggestions", "", &key);
        if !refresh {
            if let Some(existing) = self.entries.get(&key).await {
                return Ok((existing, true));
            }
            if let Some(cache) = &state.memcached
                && let Some((0, bytes)) = cache.read_raw(&shared).await
                && let Ok(data) = serde_json::from_slice::<matching::Suggestions>(&bytes)
            {
                let data = Arc::new(data);
                self.entries.insert(key, data.clone()).await;
                return Ok((data, true));
            }
        } else {
            self.entries.invalidate(&key).await;
        }
        let data = self
            .entries
            .try_get_with(key.clone(), async {
                let data = matching::build(&state.pool, &key.1).await?;
                if let Some(cache) = &state.memcached
                    && let Ok(bytes) = serde_json::to_vec(&data)
                {
                    let _ = cache.write_raw(&shared, 0, &bytes, 300, true).await;
                }
                Ok::<_, ApiError>(Arc::new(data))
            })
            .await
            .map_err(|_| ApiError::Serialization)?;
        Ok((data, false))
    }
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let user = match super::super::require(state, request.uri(), request.headers()).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    if request.method() == Method::POST {
        return actions::apply(state, user.id, request).await;
    }
    let uri = request.uri();
    let headers = request.headers();
    let (scope, ran, refresh) = Selection::parse(uri.query().unwrap_or_default());
    let (selected_debaters, selected_schools) = scope.selected(&state.pool).await?;
    let refresh_query = scope.refresh_query();
    let (results, cached) = if ran {
        state
            .synthetic_suggestions
            .suggestions(state, scope, refresh)
            .await?
    } else {
        (Arc::new(matching::Suggestions::default()), false)
    };
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(headers, secret))
        .unwrap_or_default();
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    let (csrf, token) = login::csrf_tokens(headers).map_err(|_| ApiError::Serialization)?;
    let frame = web::base_context(state, &user, uri);
    let html=web::render("admin/synthetic_resolution_suggestions.html",context!{
        ran,cached,selected_debaters,selected_schools,refresh_query,messages=>&messages,csrf=>token,
        suggestion_total=>results.debaters.len()+results.schools.len(),debater_suggestions=>&results.debaters,
        school_suggestions=>&results.schools,..frame
    }).map_err(|error|{eprintln!("Synthetic suggestions template failed: {error}");ApiError::Serialization})?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Synthetic suggestions failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
