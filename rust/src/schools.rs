use axum::{
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AssertSqlSafe, Row, any::AnyRow};

use crate::{ApiError, AppState, Endpoint, cached_json, is_django_integer, json_error, options};

const ACTIVE: &str = "
    SELECT CAST(s.id AS BIGINT) AS id, s.name,
           CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included_in_oty
    FROM core_school s
    JOIN core_debater d ON d.school_id = s.id AND d.latest_season >= $1
    WHERE NOT s.temporary
    GROUP BY s.id, s.name, s.included_in_oty
    ORDER BY COUNT(d.id) DESC, s.name
    LIMIT 25
";

const ALL: &str = "
    SELECT CAST(s.id AS BIGINT) AS id, s.name,
           CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included_in_oty
    FROM core_school s
    LEFT JOIN core_debater d ON d.school_id = s.id
    LEFT JOIN core_team_debaters td ON td.debater_id = d.id
    LEFT JOIN core_teamresult tr ON tr.team_id = td.team_id
    LEFT JOIN core_tournament t ON t.id = tr.tournament_id
    WHERE NOT s.temporary
    GROUP BY s.id, s.name, s.included_in_oty
    ORDER BY MAX(t.date) DESC NULLS LAST, MAX(d.latest_season) DESC NULLS LAST, s.name
";

pub(crate) const SCHOOL: &str = "
    SELECT CAST(id AS BIGINT) AS id, name,
           CAST(CASE WHEN included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included_in_oty
    FROM core_school
    WHERE id = $1 AND NOT temporary
";

pub(crate) const DEBATER_COLUMNS: &str = "CAST(d.id AS BIGINT) AS id, d.first_name, d.last_name,
           CAST(d.status AS BIGINT) AS status, CAST(d.region AS TEXT) AS region,
           CAST(CASE WHEN d.dino_to_contact_opt_in OR d.dino_judge_contact_opt_in
                THEN 1 ELSE 0 END AS BIGINT) AS show_region";

const DEBATERS: &str = "
    SELECT {debater_columns}
    FROM core_debater d
    LEFT JOIN core_team_debaters td ON td.debater_id = d.id
    LEFT JOIN core_teamresult tr ON tr.team_id = td.team_id
    LEFT JOIN core_tournament t ON t.id = tr.tournament_id
    WHERE d.school_id = $1 AND d.latest_season >= $2 AND NOT d.temporary AND NOT d.synthetic
    GROUP BY d.id, d.first_name, d.last_name, d.status, d.region,
             d.dino_to_contact_opt_in, d.dino_judge_contact_opt_in, d.latest_season
    ORDER BY MAX(t.date) DESC NULLS LAST, d.latest_season DESC, d.last_name, d.first_name
";

const ACTIVE_DOC: &str = "API endpoint to list schools with recent activity.\n\nGET /api/schools/\nReturns: Top 25 schools by number of active debaters in last 2 years\nCached for 5 minutes.";
const ALL_DOC: &str = "API endpoint to list all schools.\n\nGET /api/schools/all/\nReturns: List of all schools in the database\nCached for 5 minutes.";
const DEBATERS_DOC: &str = "API endpoint to list debaters from a specific school.\n\nGET /api/debaters/<school_id>/\nReturns: List of debaters active in the last 5 years for the specified school\nCached for 5 minutes per school.";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum Directory {
    Active,
    All,
    Debaters(i64),
}

#[derive(Serialize)]
pub(crate) struct School {
    pub(crate) id: i64,
    pub(crate) name: String,
    included_in_oty: bool,
    url: String,
    api_url: String,
}

impl School {
    pub(crate) fn from_row(row: &AnyRow, origin: &str) -> Result<Self, sqlx::Error> {
        Ok(Self::new(
            row.try_get("id")?,
            row.try_get("name")?,
            row.try_get::<i64, _>("included_in_oty")? != 0,
            origin,
        ))
    }

    pub(crate) fn new(id: i64, name: String, included_in_oty: bool, origin: &str) -> Self {
        Self {
            id,
            name,
            included_in_oty,
            url: format!("{origin}/core/schools/{id}"),
            api_url: format!("{origin}/api/schools/{id}/detail/"),
        }
    }
}

#[derive(Serialize)]
pub(crate) struct Debater<'a> {
    id: i64,
    name: String,
    first_name: String,
    last_name: String,
    status: Value,
    status_code: i64,
    school_id: Option<i64>,
    school_name: Option<&'a str>,
    school: Option<&'a School>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    regions: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    region_display: Option<String>,
    url: String,
    api_url: String,
}

impl<'a> Debater<'a> {
    pub(crate) fn from_row(
        row: &AnyRow,
        origin: &str,
        school: Option<&'a School>,
    ) -> Result<Self, sqlx::Error> {
        let id = row.try_get("id")?;
        let first_name: String = row.try_get("first_name")?;
        let last_name: String = row.try_get("last_name")?;
        let status_code = row.try_get("status")?;
        let status = match status_code {
            0 => json!("Novice"),
            1 => json!("Varsity"),
            2 => json!("Dino"),
            unknown => json!(unknown),
        };
        let regions = if row.try_get::<i64, _>("show_region")? != 0 {
            region_list(&row.try_get::<String, _>("region")?)
        } else {
            Vec::new()
        };
        let region_display = (!regions.is_empty()).then(|| {
            regions
                .iter()
                .map(|code| region_label(code))
                .collect::<Vec<_>>()
                .join(", ")
        });
        Ok(Self {
            id,
            name: format!("{first_name} {last_name}").trim().to_owned(),
            first_name,
            last_name,
            status,
            status_code,
            school_id: school.map(|school| school.id),
            school_name: school.map(|school| school.name.as_str()),
            school,
            regions,
            region_display,
            url: format!("{origin}/core/debaters/{id}"),
            api_url: format!("{origin}/api/debaters/{id}/detail/"),
        })
    }
}

pub(crate) fn region_list(raw_json: &str) -> Vec<String> {
    match serde_json::from_str(raw_json) {
        Ok(Value::String(encoded)) => serde_json::from_str(&encoded).unwrap_or_else(|_| {
            if encoded.is_empty() {
                Vec::new()
            } else {
                vec![encoded]
            }
        }),
        Ok(Value::Array(values)) => values
            .into_iter()
            .filter_map(|value| value.as_str().map(str::to_owned))
            .collect(),
        Ok(Value::Object(values)) => values.into_iter().map(|(key, _)| key).collect(),
        _ => Vec::new(),
    }
}

pub(crate) fn region_label(code: &str) -> &str {
    match code {
        "north" => "North",
        "south" => "South",
        "central" => "Central",
        "midwest" => "Midwest",
        "west" => "West",
        "online" => "Online",
        "other" => "Other",
        unknown => unknown,
    }
}

#[derive(Serialize)]
struct SchoolList {
    count: usize,
    schools: Vec<School>,
}

#[derive(Serialize)]
struct SchoolDebaters<'a> {
    school: &'a School,
    count: usize,
    debaters: Vec<Debater<'a>>,
}

impl Directory {
    pub(crate) async fn json(self, state: &AppState, origin: &str) -> Result<Vec<u8>, ApiError> {
        match self {
            Self::Active | Self::All => {
                let rows = match self {
                    Self::Active => {
                        sqlx::query(ACTIVE)
                            .bind((state.current_season - 2).to_string())
                            .fetch_all(&state.pool)
                            .await?
                    }
                    _ => sqlx::query(ALL).fetch_all(&state.pool).await?,
                };
                let schools = rows
                    .iter()
                    .map(|row| School::from_row(row, origin))
                    .collect::<Result<Vec<_>, _>>()?;
                Ok(serde_json::to_vec(&SchoolList {
                    count: schools.len(),
                    schools,
                })?)
            }
            Self::Debaters(school_id) => {
                let school = sqlx::query(SCHOOL)
                    .bind(school_id)
                    .fetch_optional(&state.pool)
                    .await?
                    .ok_or(ApiError::NotFound("School not found"))?;
                let school = School::from_row(&school, origin)?;
                let rows = sqlx::query(AssertSqlSafe(
                    DEBATERS.replace("{debater_columns}", DEBATER_COLUMNS),
                ))
                .bind(school_id)
                .bind((state.current_season - 5).to_string())
                .fetch_all(&state.pool)
                .await?;
                let debaters = rows
                    .iter()
                    .map(|row| Debater::from_row(row, origin, Some(&school)))
                    .collect::<Result<Vec<_>, _>>()?;
                Ok(serde_json::to_vec(&SchoolDebaters {
                    school: &school,
                    count: debaters.len(),
                    debaters,
                })?)
            }
        }
    }
}

pub(crate) async fn active(State(state): State<AppState>, headers: HeaderMap) -> Response {
    cached_json(state, headers, Endpoint::Directory(Directory::Active), "").await
}

pub(crate) async fn all(State(state): State<AppState>, headers: HeaderMap) -> Response {
    cached_json(state, headers, Endpoint::Directory(Directory::All), "").await
}

pub(crate) async fn debaters(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    if !uri
        .path()
        .strip_prefix("/api/debaters/")
        .and_then(|path| path.strip_suffix('/'))
        .is_some_and(is_django_integer)
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    match id.parse() {
        Ok(school) => {
            cached_json(
                state,
                headers,
                Endpoint::Directory(Directory::Debaters(school)),
                "",
            )
            .await
        }
        Err(_) => json_error(StatusCode::NOT_FOUND, "School not found"),
    }
}

pub(crate) async fn active_options() -> Response {
    options("Active School List Api", ACTIVE_DOC)
}

pub(crate) async fn all_options() -> Response {
    options("All School List Api", ALL_DOC)
}

pub(crate) async fn debaters_options(Path(id): Path<String>) -> Response {
    if !is_django_integer(&id) {
        return StatusCode::NOT_FOUND.into_response();
    }
    options("School Debaters Api", DEBATERS_DOC)
}
