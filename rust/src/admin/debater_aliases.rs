use std::{cmp::Reverse, collections::HashMap};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use sqlx::{Any, AssertSqlSafe, Row, Transaction, any::AnyRow};

use crate::{ApiError, AppState, flash, login, web};

const LIST: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name,
        LOWER(d.first_name) AS first, LOWER(d.last_name) AS last,
        d.first_season, d.latest_season, CAST(d.alias_group_id AS BIGINT) AS alias_id,
        CAST(d.school_id AS BIGINT) AS school_id, s.name AS school_name,
        CASE WHEN s.temporary THEN 1 ELSE 0 END AS school_temporary
    FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
    WHERE NOT d.temporary AND NOT d.synthetic
";

#[derive(Serialize)]
struct School {
    name: String,
    temporary: bool,
}

#[derive(Serialize)]
struct Debater {
    id: i64,
    name: String,
    first_season: Option<String>,
    latest_season: Option<String>,
    school: Option<School>,
    #[serde(skip)]
    school_id: Option<i64>,
    #[serde(skip)]
    alias_id: Option<i64>,
}

impl Debater {
    fn read(row: &AnyRow) -> Result<Self, sqlx::Error> {
        let school = row.try_get::<Option<String>, _>("school_name")?;
        let temporary = row.try_get::<i32, _>("school_temporary")? == 1;
        Ok(Self {
            id: row.try_get("id")?,
            name: row.try_get("name")?,
            first_season: row.try_get("first_season")?,
            latest_season: row.try_get("latest_season")?,
            school_id: row.try_get("school_id")?,
            alias_id: row.try_get("alias_id")?,
            school: school.map(|name| School { name, temporary }),
        })
    }

    fn seasons(&self) -> impl Iterator<Item = i32> + '_ {
        [&self.first_season, &self.latest_season]
            .into_iter()
            .filter_map(|value| value.as_ref()?.trim().parse().ok())
    }

    fn label(&self) -> String {
        let school = self.school.as_ref().map_or_else(
            || "None".to_owned(),
            |school| {
                format!(
                    "{}{}",
                    school.name,
                    if school.temporary { " (New)" } else { "" }
                )
            },
        );
        format!("{} ({school})", self.name)
    }
}

#[derive(Serialize)]
struct Suggestion<'a> {
    debater_one: &'a Debater,
    debater_two: &'a Debater,
    season_gap: Option<u32>,
}

fn suggestions<'a>(people: &'a [(String, String, Debater)]) -> Vec<Suggestion<'a>> {
    let mut groups = Vec::<Vec<&Debater>>::new();
    let mut positions = HashMap::new();
    for (first, last, debater) in people {
        let offset = *positions.entry((first, last)).or_insert_with(|| {
            let offset = groups.len();
            groups.push(Vec::new());
            offset
        });
        groups[offset].push(debater);
    }
    let mut suggestions = Vec::new();
    for group in groups {
        for (index, first) in group.iter().enumerate() {
            for second in &group[index + 1..] {
                if first.school_id == second.school_id
                    || (first.alias_id.is_some() && first.alias_id == second.alias_id)
                {
                    continue;
                }
                let gap = first
                    .seasons()
                    .flat_map(|a| second.seasons().map(move |b| a.abs_diff(b)))
                    .min();
                if gap.is_some_and(|gap| gap > 3) {
                    continue;
                }
                suggestions.push(Suggestion {
                    debater_one: first,
                    debater_two: second,
                    season_gap: gap,
                });
            }
        }
    }
    suggestions.sort_by_key(|pair| {
        (
            pair.season_gap.unwrap_or(u32::MAX),
            Reverse(pair.debater_one.id),
        )
    });
    suggestions.truncate(200);
    suggestions
}

async fn link(
    tx: &mut Transaction<'_, Any>,
    one: i64,
    two: i64,
) -> Result<Option<String>, ApiError> {
    for id in [one.min(two), one.max(two)] {
        sqlx::query("UPDATE core_debater SET id=id WHERE id=$1")
            .bind(id)
            .execute(&mut **tx)
            .await?;
    }
    let sql = format!("{LIST} AND d.id IN ($1,$2)");
    let people = sqlx::query(AssertSqlSafe(sql))
        .bind(one)
        .bind(two)
        .fetch_all(&mut **tx)
        .await?
        .iter()
        .map(Debater::read)
        .collect::<Result<Vec<_>, _>>()?;
    let (Some(first), Some(second)) = (
        people.iter().find(|person| person.id == one),
        people.iter().find(|person| person.id == two),
    ) else {
        return Ok(None);
    };
    let group = if let Some(group) = first.alias_id.or(second.alias_id) {
        if let Some(old) = second.alias_id.filter(|other| *other != group) {
            sqlx::query("UPDATE core_debater SET alias_group_id=$1 WHERE alias_group_id=$2")
                .bind(group)
                .bind(old)
                .execute(&mut **tx)
                .await?;
            sqlx::query("DELETE FROM core_debateraliasgroup WHERE id=$1")
                .bind(old)
                .execute(&mut **tx)
                .await?;
        }
        group
    } else {
        let label = if first.name.is_empty() {
            &second.name
        } else {
            &first.name
        };
        sqlx::query_scalar::<_, i64>(
            "INSERT INTO core_debateraliasgroup (label) VALUES ($1) RETURNING CAST(id AS BIGINT)",
        )
        .bind(label)
        .fetch_one(&mut **tx)
        .await?
    };
    sqlx::query("UPDATE core_debater SET alias_group_id=$1 WHERE id IN ($2,$3)")
        .bind(group)
        .bind(one)
        .bind(two)
        .execute(&mut **tx)
        .await?;
    Ok(Some(format!(
        "Linked {} and {}.",
        first.label(),
        second.label()
    )))
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let (parts, body) = request.into_parts();
    let user = match super::require(state, &parts.uri, &parts.headers).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    let origin = state.origin(&parts.headers);
    let secure = origin
        .as_ref()
        .is_some_and(|origin| origin.starts_with("https://"));
    if parts.method == Method::POST {
        let form = match login::parse_form(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let Some(origin) = origin else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            &parts.headers,
            form.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        let selected = |key| form.get(key).filter(|input| !input.is_empty());
        let notice =
            if let (Some(one), Some(two)) = (selected("debater_one"), selected("debater_two")) {
                let mut tx = state.pool.begin().await?;
                let updated = match (one.parse(), two.parse()) {
                    (Ok(one), Ok(two)) => link(&mut tx, one, two).await?,
                    _ => None,
                };
                if let Some(message) = updated {
                    tx.commit().await?;
                    crate::elo::invalidate(state).await;
                    (25, message)
                } else {
                    (
                        40,
                        "Unable to find one of the selected debaters.".to_owned(),
                    )
                }
            } else {
                (40, "Missing debater selection.".to_owned())
            };
        let mut response =
            (StatusCode::FOUND, [(header::LOCATION, parts.uri.path())]).into_response();
        if let Some(secret) = state.secret.as_deref() {
            flash::push(
                &mut response,
                &parts.headers,
                secret,
                notice.0,
                &notice.1,
                secure,
            );
        }
        return Ok(response);
    }
    let sql = format!("{LIST} AND d.first_name<>'' AND d.last_name<>'' ORDER BY d.id DESC");
    let rows = sqlx::query(AssertSqlSafe(sql))
        .fetch_all(&state.pool)
        .await?;
    let people = rows
        .iter()
        .map(|row| {
            Ok((
                row.try_get("first")?,
                row.try_get("last")?,
                Debater::read(row)?,
            ))
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()?;
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(&parts.headers, secret))
        .unwrap_or_default();
    let common = web::base_context(state, &user, &parts.uri);
    let bindings = context! {csrf=>token,messages=>&messages,suggestions=>suggestions(&people)};
    let Ok(html) = web::render(
        "admin/debater_alias_suggestions.html",
        context! {..bindings,..common},
    ) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Debater alias suggestions failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
