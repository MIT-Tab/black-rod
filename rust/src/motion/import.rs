use std::{
    collections::HashMap,
    io::{Cursor, Read},
    sync::LazyLock,
};

use axum::{
    body::to_bytes,
    extract::{FromRequest, Multipart, Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use calamine::{Data, DataType, Reader, Xlsx};
use chrono::NaiveDate;
use minijinja::context;
use regex::Regex;
use serde::{Deserialize, Serialize};
use sqlx::{AnyPool, Row};
use zip::ZipArchive;

use crate::{
    ApiError, AppState, autocomplete, flash, login,
    session::{self, User},
    web,
};

const SIGNING_SALT: &[u8] = b"django.core.signingsigner";
const IMPORT_EXPIRED: &str = "Import preview expired or was changed.";

#[derive(Deserialize, Serialize)]
struct ImportRow {
    row_number: usize,
    text: String,
    background_slide: String,
    date_set: String,
    tournament_id: Option<i64>,
    tournament: String,
    tags: Vec<String>,
    duplicate: bool,
    errors: Vec<String>,
}

fn worksheet_index(data: &[u8]) -> usize {
    static ACTIVE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"\bactiveTab\s*=\s*["'](\d+)["']"#).unwrap());
    let Ok(mut workbook) = ZipArchive::new(Cursor::new(data)) else {
        return 0;
    };
    let Ok(mut reader) = workbook.by_name("xl/workbook.xml") else {
        return 0;
    };
    let mut content = String::new();
    if reader.read_to_string(&mut content).is_err() {
        return 0;
    }
    ACTIVE
        .captures(&content)
        .and_then(|capture| capture[1].parse().ok())
        .unwrap_or_default()
}

fn spreadsheet(bytes: &[u8], path: &str) -> Result<Vec<Vec<String>>, String> {
    let extension = path
        .rsplit('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    let rows = match extension.as_str() {
        "csv" => {
            let text = std::str::from_utf8(bytes).map_err(|_| "The CSV must be UTF-8 encoded.")?;
            csv::ReaderBuilder::new()
                .flexible(true)
                .has_headers(false)
                .from_reader(text.trim_start_matches('\u{feff}').as_bytes())
                .records()
                .map(|row| {
                    row.map(|record| record.iter().map(str::to_owned).collect())
                        .map_err(|error| error.to_string())
                })
                .collect::<Result<Vec<_>, _>>()?
        }
        "xlsx" => {
            let mut workbook = Xlsx::new(Cursor::new(bytes)).map_err(|error| error.to_string())?;
            let range = workbook
                .worksheet_range_at(worksheet_index(bytes))
                .ok_or("The spreadsheet is empty.")?
                .map_err(|error| error.to_string())?;
            let mut rows = Vec::new();
            let (first_row, first_col) = range.start().unwrap_or_default();
            for _ in 0..first_row {
                rows.push(Vec::new());
            }
            for row in range.rows() {
                let mut cells = vec![String::new(); first_col as usize];
                for value in row {
                    cells.push(match value {
                        Data::DateTime(_) => value
                            .as_datetime()
                            .map(|date| date.date().to_string())
                            .unwrap_or_default(),
                        Data::Bool(true) => "True".to_owned(),
                        Data::Bool(false) | Data::Int(0) | Data::Empty => String::new(),
                        _ => value.to_string(),
                    });
                }
                rows.push(cells);
            }
            rows
        }
        _ => return Err("Upload a CSV or XLSX spreadsheet.".to_owned()),
    };
    Ok(rows)
}

fn header(value: &str) -> String {
    let normalized = value.trim().to_lowercase().replace('_', " ");
    match normalized.as_str() {
        "motion" | "text" | "motion text" => "motion_text".to_owned(),
        "background" | "background slide" => "background_slide".to_owned(),
        "date" | "date set" => "date_set".to_owned(),
        "tournament set" => "tournament".to_owned(),
        "topics" | "topic tags" => "tags".to_owned(),
        _ => normalized.replace(' ', "_"),
    }
}

async fn tournament(
    pool: &AnyPool,
    value: &str,
) -> Result<Result<Option<(i64, String)>, String>, ApiError> {
    if value.is_empty() {
        return Ok(Ok(None));
    }
    if is_django_id(value)
        && let Ok(id) = value.parse::<i64>()
        && let Some(name) =
            sqlx::query_scalar::<_, String>("SELECT name FROM core_tournament WHERE id=$1")
                .bind(id)
                .fetch_optional(pool)
                .await?
    {
        return Ok(Ok(Some((id, name))));
    }
    let matches = sqlx::query("SELECT CAST(id AS BIGINT) AS id,name FROM core_tournament WHERE LOWER(name)=LOWER($1) LIMIT 2")
        .bind(value).fetch_all(pool).await?;
    Ok(match matches.as_slice() {
        [] => Err(format!("tournament '{value}' was not found")),
        [row] => Ok(Some((row.try_get("id")?, row.try_get("name")?))),
        _ => Err(format!(
            "tournament '{value}' is ambiguous; use its numeric ID"
        )),
    })
}

fn is_django_id(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit())
}

async fn validate(
    pool: &AnyPool,
    rows: &[Vec<String>],
) -> Result<Result<Vec<ImportRow>, String>, ApiError> {
    let Some(first) = rows.first() else {
        return Ok(Err("The spreadsheet is empty.".to_owned()));
    };
    let headers = first
        .iter()
        .enumerate()
        .map(|(index, name)| (header(name), index))
        .collect::<HashMap<_, _>>();
    if !headers.contains_key("motion_text") {
        return Ok(Err(
            "The spreadsheet needs a 'motion_text' column.".to_owned()
        ));
    }
    let mut validated = Vec::new();
    for (index, fields) in rows.iter().enumerate().skip(1) {
        if headers
            .values()
            .all(|index| fields.get(*index).is_none_or(String::is_empty))
        {
            continue;
        }
        let read = |name| {
            headers
                .get(name)
                .and_then(|index| fields.get(*index))
                .map_or("", |value| value.trim())
        };
        let text = read("motion_text");
        let mut errors = Vec::new();
        if text.is_empty() {
            errors.push("motion text is required".to_owned());
        }
        let date = read("date_set");
        let date_set = if date.is_empty() {
            String::new()
        } else {
            ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]
                .iter()
                .find_map(|format| NaiveDate::parse_from_str(date, format).ok())
                .map(|date| date.to_string())
                .unwrap_or_else(|| {
                    errors.push("invalid date (use YYYY-MM-DD or MM/DD/YYYY)".to_owned());
                    String::new()
                })
        };
        let (tournament_id, tournament_name) = match tournament(pool, read("tournament")).await? {
            Ok(Some((id, name))) => (Some(id), name),
            Ok(None) => (None, String::new()),
            Err(error) => {
                errors.push(error);
                (None, String::new())
            }
        };
        let tags = read("tags")
            .split([',', ';'])
            .map(str::trim)
            .filter(|tag| !tag.is_empty())
            .map(str::to_owned)
            .collect();
        let duplicate = !text.is_empty()
            && sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM core_motion WHERE LOWER(text)=LOWER($1) LIMIT 1",
            )
            .bind(text)
            .fetch_optional(pool)
            .await?
            .is_some();
        validated.push(ImportRow {
            row_number: index + 1,
            text: text.to_owned(),
            background_slide: read("background_slide").to_owned(),
            date_set,
            tournament_id,
            tournament: tournament_name,
            tags,
            duplicate,
            errors,
        });
    }
    Ok(Ok(validated))
}

async fn import(pool: &AnyPool, rows: Vec<ImportRow>) -> Result<(usize, usize), ApiError> {
    let mut tx = pool.begin().await?;
    let (mut created, mut skipped) = (0, 0);
    let query = if pool.connect_options().database_url.scheme() == "sqlite" {
        "INSERT INTO core_motion (text,background_slide,date_set,tournament_id,created_at,updated_at)
        VALUES ($1,$2,$3,$4,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        ON CONFLICT (text) DO NOTHING RETURNING CAST(id AS BIGINT)"
    } else {
        "INSERT INTO core_motion (text,background_slide,date_set,tournament_id,created_at,updated_at)
        VALUES ($1,$2,CAST(CAST($3 AS TEXT) AS DATE),$4,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        ON CONFLICT (text) DO NOTHING RETURNING CAST(id AS BIGINT)"
    };
    for row in rows {
        if !row.errors.is_empty() || row.duplicate {
            skipped += 1;
            continue;
        }
        let Some(id) = sqlx::query_scalar::<_, i64>(query)
            .bind(&row.text)
            .bind(&row.background_slide)
            .bind((!row.date_set.is_empty()).then_some(&row.date_set))
            .bind(row.tournament_id)
            .fetch_optional(&mut *tx)
            .await?
        else {
            skipped += 1;
            continue;
        };
        for name in row.tags {
            let tag = autocomplete::insert_tag(&mut tx, "core_motiontopic", &name).await?;
            sqlx::query("INSERT INTO core_taggedmotion (content_object_id,tag_id) VALUES ($1,$2) ON CONFLICT DO NOTHING")
                .bind(id).bind(tag).execute(&mut *tx).await?;
        }
        created += 1;
    }
    tx.commit().await?;
    Ok((created, skipped))
}

async fn page(state: &AppState, request: Request, user: User) -> Result<Response, ApiError> {
    let origin = state.origin(request.headers()).unwrap_or_default();
    let secure = origin.starts_with("https://");
    let uri = request.uri().clone();
    let method = request.method().clone();
    let headers = request.headers().clone();
    let mut rows = Vec::new();
    let mut signed_rows = String::new();
    let mut warning = None;
    if method == Method::POST {
        let Some(secret) = state.secret.as_deref() else {
            return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
        };
        let multipart = headers
            .get(header::CONTENT_TYPE)
            .and_then(|header| header.to_str().ok())
            .is_some_and(|value| value.starts_with("multipart/form-data"));
        if multipart {
            let mut form = match Multipart::from_request(request, &()).await {
                Ok(form) => form,
                Err(err) => return Ok(err.into_response()),
            };
            let (mut csrf, mut file) = (None, None);
            loop {
                let field = match form.next_field().await {
                    Ok(Some(field)) => field,
                    Ok(None) => break,
                    Err(error) => return Ok(error.into_response()),
                };
                if field.name() == Some("csrfmiddlewaretoken") {
                    csrf = match field.text().await {
                        Ok(text) => Some(text),
                        Err(err) => return Ok(err.into_response()),
                    };
                } else if field.name() == Some("spreadsheet")
                    && let Some(name) = field
                        .file_name()
                        .filter(|name| !name.is_empty())
                        .map(str::to_owned)
                {
                    let bytes = match field.bytes().await {
                        Ok(bytes) => bytes,
                        Err(err) => return Ok(err.into_response()),
                    };
                    file = Some((name, bytes));
                }
            }
            if !login::csrf_valid(&headers, csrf.as_deref(), &origin) {
                return Ok(login::csrf_failure());
            }
            if let Some((name, body)) = file {
                match spreadsheet(&body, &name) {
                    Ok(values) => match validate(&state.pool, &values).await? {
                        Ok(validated) => {
                            let payload =
                                crate::django_json::ascii(&serde_json::to_string(&validated)?);
                            let Ok(signed) =
                                session::encode_compressed(&payload, secret, SIGNING_SALT)
                            else {
                                return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
                            };
                            signed_rows = signed;
                            rows = validated;
                        }
                        Err(error) => warning = Some(error),
                    },
                    Err(error) => warning = Some(error),
                }
            } else {
                warning = Some("This field is required.".to_owned());
            }
        } else {
            let Ok(body) = to_bytes(request.into_body(), 2_621_440).await else {
                return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
            };
            let fields = login::form_values(&body);
            if !login::csrf_valid(
                &headers,
                fields.get("csrfmiddlewaretoken").map(String::as_str),
                &origin,
            ) {
                return Ok(login::csrf_failure());
            }
            if fields.get("confirm").is_some_and(|value| !value.is_empty()) {
                let input = fields.get("rows").map_or("", String::as_str);
                let Some(rows) = session::decode_timed(input, secret, SIGNING_SALT, 3600) else {
                    return Ok((StatusCode::BAD_REQUEST, IMPORT_EXPIRED).into_response());
                };
                let (created, skipped) = import(&state.pool, rows).await?;
                let mut response =
                    (StatusCode::FOUND, [(header::LOCATION, "/core/motions/")]).into_response();
                flash::push(
                    &mut response,
                    &headers,
                    secret,
                    25,
                    &format!(
                        "Imported {created} motions; skipped {skipped} invalid or duplicate rows."
                    ),
                    secure,
                );
                return Ok(response);
            }
            warning = Some("This field is required.".to_owned());
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let common = web::base_context(state, &user, &uri);
    let bindings = context! {rows,signed_rows, warning, csrf=>token};
    let html = match web::render("motions/import.html", context! {..common,..bindings}) {
        Ok(html) => html,
        Err(err) => {
            eprintln!("Motion import template failed: {err}");
            return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let user = match session::user(&state.pool, request.headers(), state.secret.as_deref()).await {
        Ok(user) => user,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    if !user.is_authenticated {
        return login::redirect_to_login(request.uri());
    }
    if !session::has_permission(&state.pool, &user, "add_motion")
        .await
        .unwrap_or(false)
    {
        return web::permission_denied(&state, &user, request.uri());
    }
    page(&state, request, user).await.unwrap_or_else(|err| {
        eprintln!("Motion import failed: {err:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
