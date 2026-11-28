use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
    time::Duration,
};

use axum::{
    body::{Body, to_bytes},
    extract::{FromRequest, Multipart, Request},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use chrono::{NaiveDate, NaiveDateTime, Utc};
use minijinja::context;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{Acquire, AnyConnection, AssertSqlSafe};

use super::{
    super::{form, model_rules, redirect},
    AdminRequest, ApiError, Exchange, Field, Model, common, formats, login,
};
use crate::flash;

const TTL: Duration = Duration::from_secs(3600);
const LIMIT: usize = 64 * 1024 * 1024;

#[derive(Deserialize, Serialize)]
struct Pending {
    user: i64,
    model: String,
    name: String,
    format: usize,
    rows: Vec<Map<String, Value>>,
}

fn location(id: &str) -> Option<PathBuf> {
    let parsed = uuid::Uuid::parse_str(id).ok()?;
    let mut root = std::env::temp_dir();
    let user = std::env::var("USER").unwrap_or_default();
    let user = user
        .chars()
        .filter(|value| value.is_ascii_alphanumeric())
        .collect::<String>();
    root.push(format!("black-rod-admin-import-{user}"));
    Some(root.join(format!("{parsed}.json")))
}

fn directory(path: &Path) -> std::io::Result<()> {
    let mut builder = std::fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    if let Err(error) = builder.create(path)
        && error.kind() != std::io::ErrorKind::AlreadyExists
    {
        return Err(error);
    }
    let metadata = std::fs::symlink_metadata(path)?;
    let private = metadata.is_dir();
    #[cfg(unix)]
    let private = {
        use std::os::unix::fs::PermissionsExt;
        private && metadata.permissions().mode() & 0o077 == 0
    };
    if !private {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "Admin import storage must be a private directory",
        ));
    }
    Ok(())
}

fn write_file(pending: &Pending) -> Result<String, ApiError> {
    use std::io::Write;

    let token = uuid::Uuid::new_v4().to_string();
    let path = location(&token).expect("generated import path");
    let root = path.parent().expect("private import directory");
    let failed = |error: std::io::Error| {
        eprintln!("Admin import temporary storage failed: {error}");
        ApiError::Serialization
    };
    directory(root).map_err(failed)?;
    if let Ok(files) = std::fs::read_dir(root) {
        for file in files.flatten() {
            if file
                .metadata()
                .and_then(|data| data.modified())
                .ok()
                .and_then(|modified| modified.elapsed().ok())
                .is_some_and(|age| age > TTL)
                && file
                    .path()
                    .extension()
                    .is_some_and(|extension| extension == "json" || extension == "processing")
            {
                let _ = std::fs::remove_file(file.path());
            }
        }
    }
    let mut options = std::fs::OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(failed)?;
    let bytes = serde_json::to_vec(pending)?;
    file.write_all(&bytes).map_err(failed)?;
    Ok(token)
}

fn load_file(id: &str) -> Option<Pending> {
    let path = location(id)?;
    directory(path.parent()?).ok()?;
    let meta = std::fs::symlink_metadata(&path).ok()?;
    if !meta.is_file()
        || meta.modified().ok()?.elapsed().ok()? > TTL
        || meta.len() > (LIMIT * 8) as u64
    {
        return None;
    }
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

#[derive(Serialize)]
struct PreviewRow {
    kind: &'static str,
    cells: Vec<Diff>,
}

#[derive(Serialize)]
struct Diff {
    single: bool,
    before: String,
    after: String,
}

#[derive(Serialize)]
struct RowError {
    number: usize,
    message: String,
    values: String,
}

#[derive(Default)]
struct ResultRows {
    records: Vec<PreviewRow>,
    errors: Vec<RowError>,
    new: usize,
    updated: usize,
}

fn number(value: &Value, text: &str) -> Option<i64> {
    use rust_decimal::{Decimal, prelude::ToPrimitive};
    use std::str::FromStr;
    value
        .as_i64()
        .or_else(|| Decimal::from_str(text.trim()).ok()?.trunc().to_i64())
}

fn clean(field: &Field, value: &Value, workbook: bool) -> Result<Value, String> {
    let raw = formats::text(value);
    let text = raw.trim();
    let empty = text.is_empty();
    let parse_integer = || {
        if empty {
            return Ok(Value::Null);
        }
        number(value, text)
            .map(|value| json!(value))
            .ok_or("Enter a whole number.".to_owned())
    };
    if field.related.is_some() {
        if empty || value == &Value::Bool(false) || value.as_f64() == Some(0.0) {
            return Ok(Value::Null);
        }
        let id = text.parse::<i64>().ok().or_else(|| {
            value.as_f64().filter(|number| number.fract() == 0.0)?;
            number(value, text)
        });
        return id
            .map(|id| json!(id))
            .ok_or_else(|| "Enter a whole number.".to_owned());
    }
    let cleaned = match field.kind.as_str() {
        "BooleanField" => match text.to_ascii_lowercase().as_str() {
            "" | "null" | "none" => Value::Null,
            "1" | "true" => json!(true),
            _ => json!(false),
        },
        "AutoField"
        | "BigAutoField"
        | "IntegerField"
        | "SmallIntegerField"
        | "BigIntegerField"
        | "PositiveIntegerField"
        | "PositiveBigIntegerField"
        | "PositiveSmallIntegerField" => return parse_integer(),
        "FloatField" | "DecimalField" => {
            if empty {
                Value::Null
            } else {
                let number = text
                    .parse::<f64>()
                    .ok()
                    .filter(|value| value.is_finite())
                    .ok_or("Enter a number.")?;
                if field.kind == "DecimalField" {
                    json!(text)
                } else {
                    json!(number)
                }
            }
        }
        "DateField" => {
            if empty {
                Value::Null
            } else {
                let parsed = [
                    "%Y-%m-%d",
                    "%m/%d/%Y",
                    "%m/%d/%y",
                    "%b %d %Y",
                    "%b %d, %Y",
                    "%d %b %Y",
                    "%d %b, %Y",
                    "%B %d %Y",
                    "%B %d, %Y",
                    "%d %B %Y",
                    "%d %B, %Y",
                ]
                .into_iter()
                .find_map(|format| NaiveDate::parse_from_str(text, format).ok())
                .or_else(|| {
                    if workbook {
                        return NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M:%S%.f")
                            .or_else(|_| text.parse::<NaiveDateTime>())
                            .ok()
                            .map(|value| value.date())
                            .or_else(|| {
                                chrono::DateTime::parse_from_rfc3339(text)
                                    .ok()
                                    .map(|value| value.date_naive())
                            });
                    }
                    None
                })
                .ok_or("Enter a valid date.")?;
                json!(parsed.to_string())
            }
        }
        "DateTimeField" => {
            if empty {
                Value::Null
            } else {
                let date = chrono::DateTime::parse_from_rfc3339(text)
                    .map(|value| value.with_timezone(&Utc))
                    .ok()
                    .or_else(|| {
                        NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M:%S%.f")
                            .ok()
                            .map(|value| value.and_utc())
                    })
                    .ok_or("Enter a valid date and time.")?;
                json!(date.to_rfc3339())
            }
        }
        "JSONField" => {
            if value.is_object() || value.is_array() {
                value.clone()
            } else if empty {
                Value::Null
            } else {
                serde_json::from_str(text).map_err(|_| "Enter a valid JSON.".to_owned())?
            }
        }
        _ => {
            if field.size.is_some_and(|limit| raw.chars().count() > limit) {
                return Err(format!(
                    "Ensure this value has at most {} characters.",
                    field.size.unwrap_or_default()
                ));
            }
            json!(raw)
        }
    };
    Ok(cleaned)
}

async fn relations(
    conn: &mut AnyConnection,
    model: &Model,
    name: &str,
    id: i64,
) -> Result<String, ApiError> {
    let Some([table, owner, member]) = &model.fields[name].through else {
        return Ok(String::new());
    };
    let sql =
        format!("SELECT CAST({member} AS TEXT) FROM {table} WHERE {owner}=$1 ORDER BY {member}");
    let ids = sqlx::query_scalar::<_, String>(AssertSqlSafe(sql))
        .bind(id)
        .fetch_all(conn)
        .await?;
    Ok(ids.join(","))
}

async fn column_diff(
    conn: &mut AnyConnection,
    model: &Model,
    exchange: &Exchange,
    before: &Value,
    after: &Value,
    id: i64,
    previous: &Map<String, Value>,
) -> Result<Vec<Diff>, ApiError> {
    let mut result = Vec::new();
    for name in &exchange.headers {
        let field = &model.fields[name];
        let (old, new) = if field.through.is_some() {
            (
                previous.get(name).map(formats::text).unwrap_or_default(),
                relations(conn, model, name, id).await?,
            )
        } else if let Some(column) = &field.column {
            let render = |state: &Value| {
                let value = &state[column];
                if value.is_null() {
                    return String::new();
                }
                if field.kind == "BooleanField" {
                    return if value == true { "1" } else { "0" }.to_owned();
                }
                formats::text(value)
            };
            (render(before), render(after))
        } else {
            (String::new(), String::new())
        };
        result.push(Diff {
            single: old == new,
            before: old,
            after: new,
        });
    }
    Ok(result)
}

#[derive(Clone, Copy)]
struct ImportMode {
    actor: Option<i64>,
    workbook: bool,
}

async fn apply_row(
    conn: &mut AnyConnection,
    admin_model: &Model,
    model: &Model,
    exchange: &Exchange,
    current_season: i32,
    row: &Map<String, Value>,
    mode: ImportMode,
) -> Result<Result<PreviewRow, String>, ApiError> {
    let Some(value) = row.get("id") else {
        return Ok(Err("'id'".to_owned()));
    };
    let raw_id = formats::text(value);
    let requested = number(value, &raw_id);
    if !raw_id.trim().is_empty() && requested.is_none() {
        return Ok(Err("Enter a whole number.".to_owned()));
    }
    let before = if let Some(id) = requested {
        model.storage.get(conn, id).await?
    } else {
        Value::Null
    };
    let existing = requested.filter(|_| !before.is_null());
    let mut columns = before.as_object().cloned().unwrap_or_default();
    let mut previous = Map::new();
    if let Some(id) = existing
        && mode.actor.is_none()
    {
        for name in &exchange.headers {
            if model.fields[name].through.is_some() {
                previous.insert(name.clone(), json!(relations(conn, model, name, id).await?));
            }
        }
    }
    for (name, field) in &model.fields {
        let Some(column) = field
            .column
            .as_ref()
            .filter(|_| !field.primary && field.through.is_none())
        else {
            continue;
        };
        if field.auto_now || field.auto_now_add && existing.is_none() {
            columns.insert(column.clone(), form::default(field));
        } else if let Some(input) = row.get(name) {
            let cleaned = match clean(field, input, mode.workbook) {
                Ok(cleaned) => cleaned,
                Err(problem) => return Ok(Err(format!("{name}: {problem}"))),
            };
            if field.related.is_some()
                && let Some(id) = cleaned.as_i64()
                && !form::validation::related_exists(conn, model, field, id).await?
            {
                return Ok(Err(format!(
                    "{name}: The referenced record could not be found."
                )));
            }
            columns.insert(column.clone(), cleaned);
        } else if existing.is_none() {
            columns.insert(column.clone(), form::default(field));
        }
    }
    columns.remove("id");
    if let Some(id) = requested.filter(|_| existing.is_none()) {
        columns.insert("id".to_owned(), json!(id));
    }
    model_rules::before_save(conn, model, current_season, existing, &mut columns).await?;
    let id = match model.storage.write(conn, &columns, existing).await {
        Ok(id) => id,
        Err(ApiError::Constraint) => return Ok(Err(
            "A record with these values already exists, or a referenced record could not be found."
                .to_owned(),
        )),
        Err(reason) => return Err(reason),
    };
    for (name, field) in &model.fields {
        let Some([table, owner, member]) = field.through.as_ref() else {
            continue;
        };
        let Some(value) = row.get(name) else { continue };
        let data = formats::text(value);
        let related = match value {
            Value::Bool(true) => Some(BTreeSet::from([1])),
            Value::Bool(false) => Some(BTreeSet::new()),
            Value::Number(_) => number(value, &data).map(|id| BTreeSet::from([id])),
            _ => data
                .split(',')
                .map(str::trim)
                .filter(|key| !key.is_empty())
                .map(str::parse::<i64>)
                .collect::<Result<BTreeSet<_>, _>>()
                .ok(),
        };
        let Some(related) = related else {
            return Ok(Err(format!("{name}: Enter a whole number.")));
        };
        let Some((app, related_model)) = field
            .related
            .as_deref()
            .and_then(|name| name.split_once('.'))
        else {
            continue;
        };
        let Some(target) = super::super::schema::schema().model(app, related_model) else {
            continue;
        };
        let additional = if related_model == "debater" {
            " AND NOT temporary AND NOT synthetic"
        } else {
            ""
        };
        let mut valid = BTreeSet::new();
        if !related.is_empty() {
            let ids = related
                .iter()
                .map(i64::to_string)
                .collect::<Vec<_>>()
                .join(",");
            let statement = format!(
                "SELECT CAST(id AS BIGINT) FROM {} WHERE id IN ({ids}){additional}",
                target.table
            );
            valid.extend(
                sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
                    .fetch_all(&mut *conn)
                    .await?,
            );
        }
        form::sync_memberships(conn, [table, owner, member], id, &valid).await?;
    }
    if model.slug() == "debater" {
        crate::claims::edit::team_names(conn, id).await?;
    }
    let kind = if existing.is_none() { "new" } else { "update" };
    let cells = if let Some(actor) = mode.actor {
        let name = form::loaded_label(conn, model, id).await?;
        form::log(
            conn,
            admin_model,
            actor,
            id,
            &name,
            if existing.is_none() { 1 } else { 2 },
            json!(format!("{kind} through import_export")),
        )
        .await?;
        Vec::new()
    } else {
        let after = model.storage.get(conn, id).await?;
        column_diff(conn, model, exchange, &before, &after, id, &previous).await?
    };
    Ok(Ok(PreviewRow { kind, cells }))
}

fn has_explicit_id(row: &Map<String, Value>) -> bool {
    row.get("id")
        .is_some_and(|value| number(value, &formats::text(value)).is_some())
}

async fn process(
    admin: &AdminRequest<'_>,
    admin_model: &Model,
    exchange: &Exchange,
    rows: &[Map<String, Value>],
    format: usize,
    confirming: bool,
) -> Result<ResultRows, ApiError> {
    let model = exchange.resource(admin_model)?;
    let kind = exchange
        .import_formats
        .get(format)
        .ok_or(ApiError::Serialization)?;
    let mode = ImportMode {
        actor: confirming.then_some(admin.viewer.user.id).flatten(),
        workbook: matches!(kind.as_str(), "xlsx" | "xls" | "ods"),
    };
    let mut tx = admin.state.pool.begin().await?;
    let sync_sequence =
        confirming && tx.backend_name() == "PostgreSQL" && rows.iter().any(has_explicit_id);
    if sync_sequence {
        // Ordinary INSERTs take a conflicting table lock before allocating an ID.
        let lock = format!(
            r#"LOCK TABLE "{}" IN SHARE ROW EXCLUSIVE MODE"#,
            model.table
        );
        sqlx::query(AssertSqlSafe(lock)).execute(&mut *tx).await?;
    }
    let mut imported_explicit_id = false;
    let mut result = ResultRows::default();
    for (index, row) in rows.iter().enumerate() {
        let mut savepoint = tx.begin().await?;
        match apply_row(
            &mut savepoint,
            admin_model,
            model,
            exchange,
            admin.state.current_season,
            row,
            mode,
        )
        .await?
        {
            Ok(record) => {
                savepoint.commit().await?;
                if record.kind == "new" {
                    result.new += 1;
                    imported_explicit_id |= sync_sequence && has_explicit_id(row);
                } else {
                    result.updated += 1
                }
                result.records.push(record);
            }
            Err(message) => {
                savepoint.rollback().await?;
                result.errors.push(RowError {
                    number: index + 1,
                    message,
                    values: exchange
                        .headers
                        .iter()
                        .filter_map(|name| row.get(name))
                        .map(formats::text)
                        .collect::<Vec<_>>()
                        .join(", "),
                });
            }
        }
    }
    if confirming && result.errors.is_empty() {
        if imported_explicit_id {
            let sql = format!(
                r#"WITH imported AS (SELECT CAST(MAX(id) AS BIGINT) AS last_id FROM "{}"),
                       id_sequence AS (SELECT CAST(pg_get_serial_sequence($1,'id') AS REGCLASS) AS relation)
                   SELECT setval(id_sequence.relation, imported.last_id)
                   FROM imported CROSS JOIN id_sequence
                   WHERE imported.last_id > COALESCE(pg_sequence_last_value(id_sequence.relation), 0)"#,
                model.table
            );
            sqlx::query(AssertSqlSafe(sql))
                .bind(&model.table)
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
        crate::search::invalidate(admin.state).await;
        if model.table == "core_tournament" && (result.new > 0 || result.updated > 0) {
            crate::recent_results::invalidate(admin.state).await;
        }
    } else {
        tx.rollback().await?;
    }
    Ok(result)
}

#[derive(Default)]
struct Upload {
    csrf: Option<String>,
    format: Option<String>,
    file: Option<(String, axum::body::Bytes)>,
}

async fn upload(request: Request) -> Result<Upload, Box<Response>> {
    let mut multipart = Multipart::from_request(request, &())
        .await
        .map_err(|error| Box::new(error.into_response()))?;
    let mut upload = Upload::default();
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|error| Box::new(error.into_response()))?
    {
        match field.name() {
            Some("csrfmiddlewaretoken") => upload.csrf = field.text().await.ok(),
            Some("input_format") => upload.format = field.text().await.ok(),
            Some("import_file") => {
                if let Some(name) = field.file_name().map(|name| {
                    name.rsplit(['\\', '/'])
                        .next()
                        .unwrap_or_default()
                        .to_owned()
                }) {
                    let bytes = field
                        .bytes()
                        .await
                        .map_err(|error| Box::new(error.into_response()))?;
                    if bytes.len() > LIMIT {
                        return Err(Box::new(StatusCode::PAYLOAD_TOO_LARGE.into_response()));
                    }
                    upload.file = Some((name, bytes));
                }
            }
            _ => {}
        }
    }
    Ok(upload)
}

pub(in crate::site_admin) async fn preview(
    admin: &AdminRequest<'_>,
    model: &Model,
    request: Request,
) -> Result<Response, ApiError> {
    let Some(exchange) = &model.exchange else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let standard = common(model, exchange, true);
    if request.method() != Method::POST {
        return admin.view("site_admin/exchange.html", standard);
    }
    let upload = match upload(request).await {
        Ok(upload) => upload,
        Err(response) => return Ok(*response),
    };
    let Some(origin) = admin.origin() else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    if !login::csrf_valid(admin.headers, upload.csrf.as_deref(), &origin) {
        return Ok(login::csrf_failure());
    }
    let choice = upload
        .format
        .as_ref()
        .and_then(|input| input.parse::<usize>().ok())
        .filter(|index| *index < exchange.import_formats.len());
    let file_error = upload
        .file
        .as_ref()
        .filter(|(name, bytes)| !name.is_empty() && !bytes.is_empty())
        .is_none()
        .then_some("This field is required.");
    let error = choice.is_none().then_some("This field is required.");
    if let (Some(format), Some((name, bytes)), None) = (choice, upload.file, file_error) {
        let parse = exchange.import_formats[format].clone();
        let rows = tokio::task::spawn_blocking(move || formats::read(&parse, &bytes))
            .await
            .map_err(|_| ApiError::Serialization)?;
        let rows = match rows {
            Ok(rows) => rows,
            Err(problem) => {
                return admin.view(
                    "site_admin/exchange.html",
                    context! {problem,selected=>format,..standard},
                );
            }
        };
        let result = process(admin, model, exchange, &rows, format, false).await?;
        if !result.errors.is_empty() {
            return admin.view(
                "site_admin/exchange.html",
                context! {row_errors=>result.errors,selected=>format,..standard},
            );
        }
        let user = admin.viewer.user.id.ok_or(ApiError::Serialization)?;
        let pending = Pending {
            user,
            model: model.key.clone(),
            name: name.clone(),
            format,
            rows,
        };
        let token = tokio::task::spawn_blocking(move || write_file(&pending))
            .await
            .map_err(|_| ApiError::Serialization)??;
        return admin.view("site_admin/exchange.html",context!{preview=>result.records,token,original_file_name=>name,selected=>format,..standard});
    }
    admin.view(
        "site_admin/exchange.html",
        context! {error,file_error,selected=>choice,..standard},
    )
}

pub(in crate::site_admin) async fn confirm(
    admin: &AdminRequest<'_>,
    model: &Model,
    method: Method,
    body: Body,
) -> Result<Response, ApiError> {
    let Some(exchange) = &model.exchange else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    if method != Method::POST {
        return Ok(StatusCode::METHOD_NOT_ALLOWED.into_response());
    }
    let Ok(bytes) = to_bytes(body, 128 * 1024).await else {
        return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
    };
    let fields = login::form_values(&bytes);
    let Some(origin) = admin.origin() else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    if !login::csrf_valid(
        admin.headers,
        fields.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return Ok(login::csrf_failure());
    }
    let token = fields.get("import_file_name").cloned().unwrap_or_default();
    let Some(path) = location(&token) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let pending = tokio::task::spawn_blocking(move || load_file(&token))
        .await
        .map_err(|_| ApiError::Serialization)?;
    let verified = pending.filter(|pending| {
        Some(pending.user) == admin.viewer.user.id
            && pending.model == model.key
            && fields.get("original_file_name") == Some(&pending.name)
            && fields
                .get("input_format")
                .and_then(|input| input.parse::<usize>().ok())
                == Some(pending.format)
    });
    let Some(pending) = verified else {
        return admin.view("site_admin/exchange.html",context!{problem=>"The import preview expired. Upload the file again.",..common(model,exchange,true)});
    };
    let claimed = path.with_extension("processing");
    if tokio::fs::rename(&path, &claimed).await.is_err() {
        return Ok(StatusCode::CONFLICT.into_response());
    }
    let outcome = process(admin, model, exchange, &pending.rows, pending.format, true).await;
    let _ = tokio::fs::remove_file(claimed).await;
    let result = outcome?;
    if !result.errors.is_empty() {
        return admin.view(
            "site_admin/exchange.html",
            context! {row_errors=>result.errors,..common(model,exchange,true)},
        );
    }
    let mut response = redirect(&model.url());
    if let Some(secret) = admin.state.secret.as_deref() {
        flash::push(
            &mut response,
            admin.headers,
            secret,
            25,
            &format!(
                "Import finished, with {} new and {} updated {}.",
                result.new, result.updated, model.plural
            ),
            admin.secure(),
        );
    }
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "no-store".parse().expect("cache header"),
    );
    Ok(response)
}
