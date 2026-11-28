mod inputs;

use std::collections::{BTreeMap, HashMap};

use axum::{
    Json,
    body::to_bytes,
    extract::{FromRequest, Multipart, OriginalUri, Path, Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use chrono::NaiveDateTime;
use minijinja::context;
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};

use crate::{ApiError, AppState, admin, flash, is_django_integer, login, session::User, web};

const DASHBOARD: &str = "/core/admin/scheduling/";
const WORKSPACE:&str="
    SELECT CAST(w.id AS BIGINT) AS id, CAST(w.version AS BIGINT) AS version,
        w.schools_csv,w.dates_csv,w.schools_filename,w.dates_filename,
        CAST(w.settings AS TEXT) AS settings,CAST(w.updated_at AS TEXT) AS updated_at,u.username
    FROM core_schedulerworkspace w LEFT JOIN core_user u ON u.id=w.updated_by_id WHERE w.name='default'
";
const RUNS: &str = "
    SELECT CAST(r.id AS BIGINT) AS id,r.status,CAST(r.result AS TEXT) AS result,
        r.output_text,r.error_text,CAST(r.created_at AS TEXT) AS created_at,u.username
    FROM core_schedulingrun r LEFT JOIN core_user u ON u.id=r.created_by_id WHERE r.workspace_id=$1
";
const SETTINGS: &[(&str, &str, i64)] = &[
    ("max_workers", "Parallel workers", 4),
    (
        "already_scheduled_penalty",
        "Already scheduled score",
        1_000_000_000,
    ),
    ("rank_1_penalty", "Rank 1 score", 10),
    ("rank_2_penalty", "Rank 2 score", 0),
    ("rank_3_penalty", "Rank 3 score", -10),
    ("impossible_penalty", "Impossible score", -1_000),
    (
        "missing_unopposed_host_penalty",
        "Missing unopposed host score",
        -1_000,
    ),
    (
        "missing_requested_unopposed_penalty",
        "Missing requested unopposed score",
        -100,
    ),
    ("missing_tag_penalty", "Missing tag score", -1_000),
    ("tag_bonus", "Tag match bonus", 10_000),
    (
        "north_to_south_penalty",
        "North school in South slot",
        -10_000,
    ),
    (
        "north_to_central_penalty",
        "North school in Central slot",
        -1_000,
    ),
    (
        "south_to_central_penalty",
        "South school in Central slot",
        -100,
    ),
    (
        "central_to_north_penalty",
        "Central school in North slot",
        -1_000,
    ),
    (
        "central_to_south_penalty",
        "Central school in South slot",
        -100,
    ),
    (
        "central_on_two_tournament_weekend_penalty",
        "Central school on 2-tournament weekend",
        0,
    ),
    (
        "south_to_north_penalty",
        "South school in North slot",
        -10_000,
    ),
];

#[derive(Serialize)]
struct Workspace {
    id: i64,
    version: i64,
    schools_csv: String,
    dates_csv: String,
    schools_filename: String,
    dates_filename: String,
    settings: Value,
    updated_at: String,
    updated_by: Option<Value>,
}

impl Workspace {
    fn row(row: AnyRow) -> Result<Self, ApiError> {
        let updated_by = row
            .try_get::<Option<String>, _>("username")?
            .map(|name| json!({"username":name}));
        Ok(Self {
            id: row.try_get("id")?,
            version: row.try_get("version")?,
            schools_csv: row.try_get("schools_csv")?,
            dates_csv: row.try_get("dates_csv")?,
            schools_filename: row.try_get("schools_filename")?,
            dates_filename: row.try_get("dates_filename")?,
            settings: serde_json::from_str(row.try_get("settings")?)?,
            updated_at: row.try_get("updated_at")?,
            updated_by,
        })
    }

    fn has_inputs(&self) -> bool {
        !self.schools_csv.trim().is_empty() && !self.dates_csv.trim().is_empty()
    }

    fn summary(&self) -> Result<Option<inputs::Summary>, String> {
        if self.has_inputs() {
            inputs::summarize(&self.schools_csv, &self.dates_csv).map(Some)
        } else {
            Ok(None)
        }
    }

    fn settings(&self) -> Value {
        let mut settings = serde_json::Map::new();
        for (key, _, initial) in SETTINGS {
            let default = if *key == "max_workers" {
                std::thread::available_parallelism().map_or(4, |workers| workers.get().min(8))
                    as i64
            } else {
                *initial
            };
            let value = self
                .settings
                .get(key)
                .filter(|value| !value.is_null() && value.as_str() != Some(""));
            settings.insert(
                (*key).to_owned(),
                value.cloned().unwrap_or_else(|| json!(default)),
            );
        }
        Value::Object(settings)
    }
}

fn sqlite(pool: &AnyPool) -> bool {
    pool.connect_options().database_url.scheme() == "sqlite"
}

async fn workspace(pool: &AnyPool) -> Result<Workspace, ApiError> {
    if let Some(row) = sqlx::query(WORKSPACE).fetch_optional(pool).await? {
        return Workspace::row(row);
    }
    let json = if sqlite(pool) {
        "'{}'"
    } else {
        "CAST('{}' AS JSONB)"
    };
    let query=format!("INSERT INTO core_schedulerworkspace (name,schools_csv,dates_csv,schools_filename,dates_filename,
        settings,version,created_at,updated_at) VALUES ('default','','','','',{json},1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        ON CONFLICT (name) DO NOTHING");
    sqlx::query(AssertSqlSafe(query)).execute(pool).await?;
    Workspace::row(sqlx::query(WORKSPACE).fetch_one(pool).await?)
}

fn timestamp(value: &str) -> Option<NaiveDateTime> {
    let raw = value.split('+').next().unwrap_or(value);
    NaiveDateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S%.f").ok()
}

fn iso(value: &str) -> String {
    timestamp(value).map_or_else(
        || value.to_owned(),
        |time| {
            time.and_utc()
                .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, false)
        },
    )
}

fn display_time(value: &str) -> String {
    use chrono::Timelike;
    let Some(time) = timestamp(value) else {
        return value.to_owned();
    };
    let date = web::django_date(&time.date().to_string());
    if time.hour() == 0 && time.minute() == 0 {
        return format!("{date}, midnight");
    }
    if time.hour() == 12 && time.minute() == 0 {
        return format!("{date}, noon");
    }
    let suffix = if time.hour() < 12 { "a.m." } else { "p.m." };
    let hour = time.format("%-I");
    if time.minute() == 0 {
        format!("{date}, {hour} {suffix}")
    } else {
        format!("{date}, {} {suffix}", time.format("%-I:%M"))
    }
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"success":false,"error":message}))).into_response()
}

fn failed(error: ApiError) -> Response {
    eprintln!("Scheduler request failed: {error:?}");
    StatusCode::INTERNAL_SERVER_ERROR.into_response()
}

pub(crate) async fn data(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = admin::require(&state, &uri, &headers).await {
        return *response;
    }
    let workspace = match workspace(&state.pool).await {
        Ok(workspace) => workspace,
        Err(error) => return failed(error),
    };
    let counts = match workspace.summary() {
        Ok(Some(summary)) => summary.counts(),
        Ok(None) => {
            return error(
                StatusCode::BAD_REQUEST,
                "Upload the schools and dates CSVs first.",
            );
        }
        Err(reason) => return error(StatusCode::BAD_REQUEST, &reason),
    };
    Json(json!({"success":true,"workspace":{
        "version":workspace.version,"updated_at":iso(&workspace.updated_at),
        "schools_filename":workspace.schools_filename,"dates_filename":workspace.dates_filename,
        "schools_csv":workspace.schools_csv,"dates_csv":workspace.dates_csv,"settings":workspace.settings(),"summary":counts,
    }})).into_response()
}

fn fallback<'a>(input: &'a Value, default: &'a Value) -> &'a Value {
    if input.is_null()
        || input == false
        || input.as_str() == Some("")
        || input.as_f64() == Some(0.0)
        || input.as_object().is_some_and(|obj| obj.is_empty())
        || input.as_array().is_some_and(|arr| arr.is_empty())
    {
        default
    } else {
        input
    }
}

async fn register_run(
    pool: &AnyPool,
    user: i64,
    version: i64,
    status: &str,
    payload: &Value,
) -> Result<Option<i64>, ApiError> {
    let mut tx = pool.begin().await?;
    let found = sqlx::query(
        "UPDATE core_schedulerworkspace SET version=version
        WHERE name='default' AND version=$1",
    )
    .bind(version)
    .execute(&mut *tx)
    .await?;
    if found.rows_affected() == 0 {
        return Ok(None);
    }
    let settings = serde_json::to_string(fallback(&payload["settings_snapshot"], &json!({})))?;
    let result = serde_json::to_string(fallback(&payload["result"], &json!({})))?;
    let (settings_type, result_type) = if sqlite(pool) {
        ("$4", "$5")
    } else {
        (
            "CAST(CAST($4 AS TEXT) AS JSONB)",
            "CAST(CAST($5 AS TEXT) AS JSONB)",
        )
    };
    let query=format!("INSERT INTO core_schedulingrun (workspace_id,created_by_id,workspace_version,status,
        settings_snapshot,result,output_text,error_text,created_at,completed_at)
        SELECT id,$1,$2,$3,{settings_type},{result_type},$6,$7,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
        FROM core_schedulerworkspace WHERE name='default' AND version=$2 RETURNING CAST(id AS BIGINT)");
    let id = sqlx::query_scalar::<_, i64>(AssertSqlSafe(query))
        .bind(user)
        .bind(version)
        .bind(status)
        .bind(settings)
        .bind(result)
        .bind(payload["output_text"].as_str().unwrap_or_default())
        .bind(payload["error_text"].as_str().unwrap_or_default())
        .fetch_optional(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(id)
}

pub(crate) async fn save_run(State(state): State<AppState>, request: Request) -> Response {
    let user = match admin::require(&state, request.uri(), request.headers()).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    if !login::csrf_valid(request.headers(), None, &origin) {
        return login::csrf_failure();
    }
    let Ok(body) = to_bytes(request.into_body(), 2_621_440).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let Ok(payload) = serde_json::from_slice::<Value>(&body) else {
        return error(StatusCode::BAD_REQUEST, "Invalid scheduling run payload.");
    };
    if !payload.is_object() {
        return error(StatusCode::BAD_REQUEST, "Invalid scheduling run payload.");
    }
    let workspace = match workspace(&state.pool).await {
        Ok(workspace) => workspace,
        Err(error) => return failed(error),
    };
    let conflict = "The shared scheduler workspace changed before this run could be saved. Reload the page and run it again.";
    if payload["workspace_version"].as_i64() != Some(workspace.version) {
        return error(StatusCode::CONFLICT, conflict);
    }
    let completed = json!("completed");
    let Some(status @ ("completed" | "failed")) = fallback(&payload["status"], &completed).as_str()
    else {
        return error(
            StatusCode::BAD_REQUEST,
            "Unsupported scheduling run status.",
        );
    };
    match register_run(
        &state.pool,
        user.id.unwrap_or_default(),
        workspace.version,
        status,
        &payload,
    )
    .await
    {
        Ok(Some(id)) => Json(json!({"success":true,"run_id":id})).into_response(),
        Ok(None) => error(StatusCode::CONFLICT, conflict),
        Err(error) => failed(error),
    }
}

fn attachment(body: String, filename: &str, content: &'static str) -> Response {
    if body.is_empty() {
        return StatusCode::NOT_FOUND.into_response();
    }
    let filename = filename.replace(['\r', '\n', '"'], "");
    (
        [
            (header::CONTENT_TYPE, content.to_owned()),
            (
                header::CONTENT_DISPOSITION,
                format!(r#"attachment; filename="{filename}""#),
            ),
        ],
        body,
    )
        .into_response()
}

pub(crate) async fn csv(
    State(state): State<AppState>,
    Path(kind): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = admin::require(&state, &uri, &headers).await {
        return *response;
    }
    let workspace = match workspace(&state.pool).await {
        Ok(workspace) => workspace,
        Err(error) => return failed(error),
    };
    let (filename, body, fallback) = match kind.as_str() {
        "schools" => (
            workspace.schools_filename,
            workspace.schools_csv,
            "schools.csv",
        ),
        "dates" => (workspace.dates_filename, workspace.dates_csv, "dates.csv"),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    let name = if filename.is_empty() {
        fallback
    } else {
        &filename
    };
    attachment(body, name, "text/csv; charset=utf-8")
}

pub(crate) async fn output(
    State(state): State<AppState>,
    Path(raw): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = admin::require(&state, &uri, &headers).await {
        return *response;
    }
    let Some(id) = raw.parse::<i64>().ok().filter(|_| is_django_integer(&raw)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    match sqlx::query_scalar::<_, String>("SELECT output_text FROM core_schedulingrun WHERE id=$1")
        .bind(id)
        .fetch_optional(&state.pool)
        .await
    {
        Ok(Some(output)) => attachment(
            output,
            &format!("scheduling-run-{id}.txt"),
            "text/plain; charset=utf-8",
        ),
        Ok(None) => StatusCode::NOT_FOUND.into_response(),
        Err(error) => failed(error.into()),
    }
}

fn run(row: &AnyRow) -> Result<Value, ApiError> {
    let id: i64 = row.try_get("id")?;
    let result: Value = serde_json::from_str(row.try_get("result")?)?;
    let creator = row
        .try_get::<Option<&str>, _>("username")?
        .map(|name| json!({"username":name}));
    Ok(json!({
        "id":id,"status":row.try_get::<&str,_>("status")?,"result":result,"created_by":creator,
        "created_at":display_time(row.try_get("created_at")?),
        "output_text":row.try_get::<&str,_>("output_text")?,"error_text":row.try_get::<&str,_>("error_text")?,
    }))
}

async fn runs(pool: &AnyPool, workspace: i64) -> Result<(Vec<Value>, Option<Value>), ApiError> {
    let recent = sqlx::query(AssertSqlSafe(format!(
        "{RUNS} ORDER BY r.created_at DESC LIMIT 5"
    )))
    .bind(workspace)
    .fetch_all(pool)
    .await?
    .iter()
    .map(run)
    .collect::<Result<Vec<_>, _>>()?;
    let latest = sqlx::query(AssertSqlSafe(format!(
        "{RUNS} AND r.status='completed' ORDER BY r.created_at DESC LIMIT 1"
    )))
    .bind(workspace)
    .fetch_optional(pool)
    .await?
    .as_ref()
    .map(run)
    .transpose()?;
    Ok((recent, latest))
}

fn notice(state: &AppState, headers: &HeaderMap, level: i32, message: &str) -> Response {
    let mut response = (StatusCode::FOUND, [(header::LOCATION, DASHBOARD)]).into_response();
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            &mut response,
            headers,
            secret,
            level,
            message,
            state
                .origin(headers)
                .is_some_and(|origin| origin.starts_with("https://")),
        );
    }
    response
}

fn conflict(workspace: &Workspace) -> String {
    let date = timestamp(&workspace.updated_at).map_or_else(
        || workspace.updated_at.clone(),
        |time| time.format("%Y-%m-%d %H:%M:%S UTC").to_string(),
    );
    let name = workspace
        .updated_by
        .as_ref()
        .and_then(|user| user["username"].as_str())
        .unwrap_or("another admin");
    format!(
        "This scheduler page changed in the meantime. Latest save: {date} by {name}. Reload and try again."
    )
}

#[derive(Default)]
struct Form {
    fields: HashMap<String, String>,
    errors: BTreeMap<String, Vec<String>>,
    files: HashMap<String, (String, String)>,
    inline_notice: Option<&'static str>,
}

impl Form {
    async fn read(request: Request) -> Result<(HeaderMap, Self), StatusCode> {
        let headers = request.headers().clone();
        let multipart = headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|content| content.starts_with("multipart/form-data"));
        let mut form = Self::default();
        if multipart {
            let mut payload = Multipart::from_request(request, &())
                .await
                .map_err(|error| error.status())?;
            while let Some(field) = payload.next_field().await.map_err(|error| error.status())? {
                let name = field.name().unwrap_or_default().to_owned();
                let filename = field.file_name().map(str::to_owned);
                if let Some(filename) = filename {
                    if filename.is_empty() || !matches!(name.as_str(), "schools_csv" | "dates_csv")
                    {
                        continue;
                    }
                    let filename = filename
                        .rsplit(['/', '\\'])
                        .next()
                        .unwrap_or_default()
                        .chars()
                        .take(255)
                        .collect();
                    let bytes = field.bytes().await.map_err(|error| error.status())?;
                    let Ok(value) = std::str::from_utf8(&bytes) else {
                        form.add_error(&name, "Upload a UTF-8 encoded CSV file.");
                        continue;
                    };
                    let value = value.strip_prefix('\u{feff}').unwrap_or(value).to_owned();
                    if value.is_empty() {
                        form.add_error(&name, "The submitted file is empty.");
                    } else {
                        form.files.insert(name, (filename, value));
                    }
                } else {
                    let text = field.text().await.map_err(|error| error.status())?;
                    form.fields.insert(name, text);
                }
            }
        } else {
            let body = to_bytes(request.into_body(), 2_621_440)
                .await
                .map_err(|_| StatusCode::PAYLOAD_TOO_LARGE)?;
            form.fields = login::form_values(&body);
        }
        Ok((headers, form))
    }

    fn add_error(&mut self, key: &str, error: &str) {
        self.errors
            .entry(key.to_owned())
            .or_default()
            .push(error.to_owned());
    }

    fn integer(&mut self, key: &str) -> Option<i64> {
        let raw = self
            .fields
            .get(key)
            .map(|value| value.trim())
            .unwrap_or_default();
        if raw.is_empty() {
            self.add_error(key, "This field is required.");
            return None;
        }
        let parsed = raw.strip_suffix(".0").unwrap_or(raw).parse().ok();
        if parsed.is_none() {
            self.add_error(key, "Enter a whole number.");
        }
        parsed
    }
}

async fn update_settings(
    pool: &AnyPool,
    workspace: &Workspace,
    owner: i64,
    settings: &Value,
) -> Result<bool, ApiError> {
    let binding = if sqlite(pool) {
        "$3"
    } else {
        "CAST(CAST($3 AS TEXT) AS JSONB)"
    };
    let sql = format!(
        "UPDATE core_schedulerworkspace SET settings={binding},updated_by_id=$2,version=version+1,
        updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND version=$4"
    );
    let count = sqlx::query(AssertSqlSafe(sql))
        .bind(workspace.id)
        .bind(owner)
        .bind(serde_json::to_string(settings)?)
        .bind(workspace.version)
        .execute(pool)
        .await?
        .rows_affected();
    Ok(count == 1)
}

async fn update_files(pool: &AnyPool, w: &Workspace, owner: i64) -> Result<bool, ApiError> {
    let count=sqlx::query("UPDATE core_schedulerworkspace SET schools_csv=$3,dates_csv=$4,
        schools_filename=$5,dates_filename=$6,updated_by_id=$2,version=version+1,updated_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND version=$7")
        .bind(w.id).bind(owner).bind(&w.schools_csv).bind(&w.dates_csv).bind(&w.schools_filename).bind(&w.dates_filename)
        .bind(w.version).execute(pool).await?.rows_affected();
    Ok(count == 1)
}

async fn bootstrap(state: &AppState, w: &mut Workspace) -> Result<(), ApiError> {
    if w.has_inputs() {
        return Ok(());
    }
    let (schools, dates) = tokio::join!(
        tokio::fs::read_to_string("/home/joey/scheduling/schools.csv"),
        tokio::fs::read_to_string("/home/joey/scheduling/dates.csv")
    );
    if let (Ok(schools), Ok(dates)) = (schools, dates) {
        sqlx::query(
            "UPDATE core_schedulerworkspace SET schools_csv=$2,dates_csv=$3,
            schools_filename='schools.csv',dates_filename='dates.csv',updated_at=CURRENT_TIMESTAMP
            WHERE id=$1 AND version=$4",
        )
        .bind(w.id)
        .bind(&schools)
        .bind(&dates)
        .bind(w.version)
        .execute(&state.pool)
        .await?;
        *w = workspace(&state.pool).await?;
    }
    Ok(())
}

async fn template(
    state: &AppState,
    user: &User,
    uri: &Uri,
    headers: &HeaderMap,
    mut workspace: Workspace,
    form: Form,
) -> Response {
    let Ok((recent, latest)) = runs(&state.pool, workspace.id).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let (summary, warning) = match workspace.summary() {
        Ok(summary) => (summary, None),
        Err(warning) => (None, Some(warning)),
    };
    let settings = workspace.settings();
    let mut settings_fields = serde_json::Map::new();
    let mut errors = Vec::new();
    let settings_edit = form
        .fields
        .get("action")
        .is_some_and(|action| action == "save_settings");
    for (key, label, _) in SETTINGS {
        let entered = form
            .fields
            .get(*key)
            .map_or_else(|| settings[key].to_string(), Clone::clone);
        settings_fields.insert((*key).to_owned(),json!({"name":key,"id_for_label":format!("id_{key}"),
            "label":label,"value":entered,"help_text":if *key=="max_workers"{"How many scenario checks to run at the same time."}else{""}}));
        if settings_edit && let Some(error) = form.errors.get(*key) {
            errors.push(json!({"label":label,"errors":error}));
        }
    }
    let notices = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(headers, secret))
        .unwrap_or_default();
    let Ok((csrf, token)) = login::csrf_tokens(headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    let version = workspace.version;
    workspace.updated_at = display_time(&workspace.updated_at);
    let common = web::base_context(state, user, uri);
    let bindings = context! {workspace,summary,summary_error=>warning,version, settings=>Value::Object(settings_fields),
        settings_errors=>errors, inline_notice=>form.inline_notice, notices,
        upload_errors=>form.errors,csrf=>token,recent_runs=>recent,latest_completed_run=>latest,
        workspace_data_url=>"/core/admin/scheduling/workspace-data/",
        save_run_url=>"/core/admin/scheduling/save-browser-run/",
    };
    match web::render(
        "admin/scheduling_dashboard.html",
        context! {..bindings,..common},
    ) {
        Ok(html) => {
            let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
            login::set_csrf_cookie(&mut response, &csrf, secure);
            if !notices.is_empty() {
                flash::clear(&mut response, secure);
            }
            response
        }
        Err(error) => {
            eprintln!("Scheduler template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn dashboard(State(state): State<AppState>, request: Request) -> Response {
    let uri = request.uri().clone();
    let user = match admin::require(&state, &uri, request.headers()).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let mut w = match workspace(&state.pool).await {
        Ok(w) => w,
        Err(error) => return failed(error),
    };
    if let Err(error) = bootstrap(&state, &mut w).await {
        return failed(error);
    }
    let method = request.method().clone();
    if method != Method::POST {
        return template(&state, &user, &uri, request.headers(), w, Form::default()).await;
    }
    let (headers, mut form) = match Form::read(request).await {
        Ok(content) => content,
        Err(status) => return status.into_response(),
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    if !login::csrf_valid(
        &headers,
        form.fields.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return login::csrf_failure();
    }
    let user_id = user.id.unwrap_or_default();
    let version = form.integer("workspace_version");
    match form.fields.get("action").map(String::as_str) {
        Some("save_settings") => {
            let mut settings = serde_json::Map::new();
            for (key, _, _) in SETTINGS {
                if let Some(value) = form.integer(key) {
                    if *key == "max_workers" && !(1..=64).contains(&value) {
                        form.add_error(
                            key,
                            if value < 1 {
                                "Ensure this value is greater than or equal to 1."
                            } else {
                                "Ensure this value is less than or equal to 64."
                            },
                        );
                    }
                    settings.insert((*key).to_owned(), json!(value));
                }
            }
            if !form.errors.is_empty() {
                form.inline_notice = Some("Please fix the scheduler settings below.");
                return template(&state, &user, &uri, &headers, w, form).await;
            }
            if version != Some(w.version) {
                return notice(&state, &headers, 40, &conflict(&w));
            }
            match update_settings(&state.pool, &w, user_id, &Value::Object(settings)).await {
                Ok(true) => notice(&state, &headers, 25, "Scheduler settings saved."),
                Ok(false) => match workspace(&state.pool).await {
                    Ok(current) => notice(&state, &headers, 40, &conflict(&current)),
                    Err(error) => failed(error),
                },
                Err(error) => failed(error),
            }
        }
        Some("upload_csv") => {
            if form.files.is_empty() && form.errors.is_empty() {
                form.add_error("non_field_errors", "Upload at least one CSV file.");
            }
            if !form.errors.is_empty() {
                form.inline_notice = Some("Please upload at least one CSV file.");
                return template(&state, &user, &uri, &headers, w, form).await;
            }
            if version != Some(w.version) {
                return notice(&state, &headers, 40, &conflict(&w));
            }
            let has_schools = form.files.contains_key("schools_csv");
            if let Some((name, body)) = form.files.remove("schools_csv") {
                w.schools_filename = name;
                w.schools_csv = body;
            }
            if let Some((name, body)) = form.files.remove("dates_csv") {
                w.dates_filename = name;
                w.dates_csv = body;
            }
            let summary = match w.summary() {
                Ok(summary) => summary,
                Err(error) => {
                    form.add_error("non_field_errors", &error);
                    form.inline_notice = Some("The uploaded CSV files could not be validated.");
                    return template(&state, &user, &uri, &headers, w, form).await;
                }
            };
            match update_files(&state.pool, &w, user_id).await {
                Ok(true) => {
                    let message = if let Some(summary) = summary {
                        format!(
                            "Scheduler CSVs saved. {} schools and {} dates are ready.",
                            summary.school_count, summary.date_count
                        )
                    } else {
                        let waiting = if w.dates_csv.trim().is_empty() {
                            "dates"
                        } else {
                            "schools"
                        };
                        let label = if has_schools { "Schools" } else { "Dates" };
                        format!("{label} CSV saved. Upload the {waiting} CSV to finish validation.")
                    };
                    notice(&state, &headers, 25, &message)
                }
                Ok(false) => match workspace(&state.pool).await {
                    Ok(current) => notice(&state, &headers, 40, &conflict(&current)),
                    Err(error) => failed(error),
                },
                Err(error) => failed(error),
            }
        }
        _ => notice(&state, &headers, 40, "Unknown scheduling action."),
    }
}
