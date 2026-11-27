mod batch;
mod catalog;
pub(crate) mod journal;
pub(crate) mod merge;
pub(crate) mod mutation;

use std::collections::BTreeMap;

use axum::{
    Json,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use ring::digest;
use serde_json::{Value, json};
use sqlx::{Any, AnyConnection, AssertSqlSafe, Row, any::AnyArguments, query::Query};

use crate::{ApiError, AppState, is_django_integer, team_profile::read_transaction};

const PREFIX: &str = "/api/agent/v1/";
const GUIDE: &str = include_str!("../../../docs/agent-corrections.md");

#[derive(Clone, Copy)]
enum Endpoint<'a> {
    Discovery,
    Guide,
    Records(&'a str, Option<i64>),
    History(Option<i64>),
    Undo(i64),
}

impl Endpoint<'_> {
    fn parse(path: &str) -> Option<Endpoint<'_>> {
        let path = path.strip_prefix(PREFIX)?;
        if path.is_empty() {
            return Some(Endpoint::Discovery);
        }
        let parts = path.strip_suffix('/')?.split('/').collect::<Vec<_>>();
        match parts.as_slice() {
            ["guide"] => Some(Endpoint::Guide),
            ["records", resource] => Some(Endpoint::Records(resource, None)),
            ["records", resource, raw] if is_django_integer(raw) => {
                Some(Endpoint::Records(resource, Some(raw.parse().ok()?)))
            }
            ["batches"] => Some(Endpoint::History(None)),
            ["batches", raw] if is_django_integer(raw) => {
                Some(Endpoint::History(Some(raw.parse().ok()?)))
            }
            ["batches", raw, "undo"] if is_django_integer(raw) => {
                Some(Endpoint::Undo(raw.parse().ok()?))
            }
            _ => None,
        }
    }

    fn methods(self) -> &'static str {
        match self {
            Self::Undo(_) => "POST, OPTIONS",
            Self::History(_) => "GET, POST, HEAD, OPTIONS",
            _ => "GET, HEAD, OPTIONS",
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Discovery => "Agent Discovery",
            Self::Guide => "Agent Guide",
            Self::Records(..) => "Agent Records",
            Self::History(_) => "Agent Batches",
            Self::Undo(_) => "Agent Undo",
        }
    }

    fn render(self, status: StatusCode, body: Value) -> Response {
        let mut response = (
            status,
            [
                (header::ALLOW, self.methods()),
                (header::VARY, "Accept"),
                (header::CACHE_CONTROL, "no-store, private"),
            ],
            Json(body),
        )
            .into_response();
        if status == StatusCode::UNAUTHORIZED {
            response
                .headers_mut()
                .insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        }
        response
    }

    fn invalid(self, body: Value) -> Response {
        self.render(StatusCode::BAD_REQUEST, body)
    }

    fn not_found(self) -> Response {
        self.render(StatusCode::NOT_FOUND, json!({"detail":"Record not found."}))
    }
}

async fn credential(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<Option<(i64, bool)>, ApiError> {
    let parts = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .split_ascii_whitespace()
        .collect::<Vec<_>>();
    let [scheme, token] = parts.as_slice() else {
        return Ok(None);
    };
    if !scheme.eq_ignore_ascii_case("bearer") || !token.starts_with("br_agent_") {
        return Ok(None);
    }
    let hash = hex::encode(digest::digest(&digest::SHA256, token.as_bytes()));
    let possible=sqlx::query_scalar::<_,i64>(
        "SELECT CAST(id AS BIGINT) FROM core_agentcredential WHERE token_hash=$1 AND revoked_at IS NULL
            AND expires_at>CURRENT_TIMESTAMP"
    ).bind(&hash).fetch_optional(&state.pool).await?;
    if possible.is_none() {
        return Ok(None);
    }
    let credential=sqlx::query("UPDATE core_agentcredential SET last_used_at=CURRENT_TIMESTAMP
        WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at>CURRENT_TIMESTAMP AND EXISTS (
            SELECT 1 FROM core_user WHERE id=core_agentcredential.created_by_id AND is_active AND is_superuser
        ) RETURNING CAST(id AS BIGINT) AS id, CAST(CASE WHEN can_write THEN 1 ELSE 0 END AS BIGINT) AS writable")
        .bind(hash).fetch_optional(&state.pool).await?;
    Ok(credential
        .map(|row| {
            Ok::<_, sqlx::Error>((row.try_get("id")?, row.try_get::<i64, _>("writable")? != 0))
        })
        .transpose()?)
}

fn well_formed(headers: &HeaderMap) -> bool {
    let mut parts = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .split_ascii_whitespace();
    parts
        .next()
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case("bearer"))
        && parts
            .next()
            .is_some_and(|token| token.starts_with("br_agent_"))
        && parts.next().is_none()
}

enum Param {
    Integer(i64),
    Float(f64),
    Bool(bool),
    Text(String),
}

impl Param {
    fn bind<'a>(self, statement: Query<'a, Any, AnyArguments>) -> Query<'a, Any, AnyArguments> {
        match self {
            Self::Integer(value) => statement.bind(value),
            Self::Float(value) => statement.bind(value),
            Self::Bool(value) => statement.bind(value),
            Self::Text(value) => statement.bind(value),
        }
    }
}

fn filter_value(field: &catalog::Field, raw: &str) -> Result<Param, String> {
    match field.kind() {
        "BooleanField" => match raw {
            "t" | "1" => Ok(Param::Bool(true)),
            "f" | "0" => Ok(Param::Bool(false)),
            _ if raw.eq_ignore_ascii_case("true") => Ok(Param::Bool(true)),
            _ if raw.eq_ignore_ascii_case("false") => Ok(Param::Bool(false)),
            _ => Err(format!("['“{raw}” value must be either True or False.']")),
        },
        "AutoField"
        | "BigAutoField"
        | "ForeignKey"
        | "OneToOneField"
        | "IntegerField"
        | "BigIntegerField"
        | "SmallIntegerField"
        | "PositiveIntegerField"
        | "PositiveSmallIntegerField"
        | "PositiveBigIntegerField" => raw
            .trim()
            .parse()
            .map(Param::Integer)
            .map_err(|_| format!("['“{raw}” value must be an integer.']")),
        "FloatField" | "DecimalField" => raw
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .map(Param::Float)
            .ok_or_else(|| format!("['“{raw}” value must be a number.']")),
        _ => Ok(Param::Text(raw.to_owned())),
    }
}

fn placeholder(value: Param, args: &mut Vec<Param>, cast: Option<&str>) -> String {
    args.push(value);
    let position = args.len();
    cast.map_or_else(
        || format!("${position}"),
        |kind| format!("CAST(CAST(${position} AS TEXT) AS {kind})"),
    )
}

fn filter_cast(field: &catalog::Field, sqlite: bool) -> Option<&'static str> {
    if sqlite {
        return None;
    }
    match field.kind() {
        "DateField" => Some("DATE"),
        "DateTimeField" => Some("TIMESTAMPTZ"),
        _ => None,
    }
}

fn params(uri: &Uri) -> BTreeMap<String, String> {
    url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect()
}

fn int(params: &BTreeMap<String, String>, key: &str, initial: i64) -> Result<i64, String> {
    params.get(key).map_or(Ok(initial), |value| {
        value
            .trim()
            .parse()
            .map_err(|_| format!("invalid literal for int() with base 10: '{value}'"))
    })
}

async fn records(
    state: &AppState,
    endpoint: Endpoint<'_>,
    uri: &Uri,
    resource: &str,
    id: Option<i64>,
) -> Result<Response, ApiError> {
    let Some(model) = catalog::MODELS.get(resource) else {
        return Ok(endpoint.invalid(json!({"resource":"Unknown resource."})));
    };
    let mut tx = read_transaction(&state.pool).await?;
    let mut statement = format!(
        "SELECT {} FROM \"{}\" AS row_source WHERE ",
        model.projection(),
        model.table
    );
    if let Some(id) = id {
        statement.push_str("id=$1");
        let row = sqlx::query(AssertSqlSafe(statement))
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?;
        let Some(row) = row else {
            return Ok(endpoint.not_found());
        };
        let result = model.row(&mut tx, &row).await?;
        tx.commit().await?;
        return Ok(endpoint.render(StatusCode::OK, result));
    }
    let query = params(uri);
    let limit = match int(&query, "limit", 100) {
        Ok(value) => value,
        Err(error) => return Ok(endpoint.invalid(json!([error]))),
    };
    let after = match int(&query, "after", 0) {
        Ok(value) => value,
        Err(error) => return Ok(endpoint.invalid(json!([error]))),
    };
    if !(1..=500).contains(&limit) || after < 0 {
        return Ok(endpoint.invalid(json!([
            "limit must be 1–500 and after must be nonnegative."
        ])));
    }
    let sqlite = state.pool.connect_options().database_url.scheme() == "sqlite";
    let mut args = vec![Param::Integer(after)];
    statement.push_str("id>$1");
    for (name, value) in &query {
        if matches!(name.as_str(), "after" | "limit" | "q") {
            continue;
        }
        let key = name.strip_suffix("__in").unwrap_or(name);
        let Some(field) = model
            .fields
            .get(key)
            .filter(|field| field.exposed && !matches!(field.kind(), "JSONField" | "TextField"))
        else {
            return Ok(endpoint.invalid(json!({(name):"Unknown or unsupported filter."})));
        };
        let column = &field.column;
        let cast = filter_cast(field, sqlite);
        if name.ends_with("__in") {
            let values = value.split(',').collect::<Vec<_>>();
            if values.len() > 200 {
                return Ok(endpoint.invalid(json!(["At most 200 filter values."])));
            }
            let mut markers = Vec::new();
            for raw in values {
                match filter_value(field, raw) {
                    Ok(value) => markers.push(placeholder(value, &mut args, cast)),
                    Err(error) => return Ok(endpoint.invalid(json!([error]))),
                }
            }
            statement.push_str(&format!(" AND \"{column}\" IN ({})", markers.join(",")));
        } else if value == "null" {
            statement.push_str(&format!(" AND \"{column}\" IS NULL"));
        } else {
            let parsed = match filter_value(field, value) {
                Ok(value) => value,
                Err(error) => return Ok(endpoint.invalid(json!([error]))),
            };
            let mark = placeholder(parsed, &mut args, cast);
            statement.push_str(&format!(" AND \"{column}\"={mark}"));
        }
    }
    if let Some(term) = query.get("q").filter(|value| !value.is_empty()) {
        let columns = model
            .fields
            .values()
            .filter(|field| field.exposed && matches!(field.kind(), "CharField" | "TextField"))
            .map(|field| field.column.as_str())
            .collect::<Vec<_>>();
        if columns.is_empty() {
            statement.push_str(" AND FALSE");
        } else {
            let escaped = term
                .chars()
                .take(200)
                .collect::<String>()
                .replace('!', "!!")
                .replace('%', "!%")
                .replace('_', "!_");
            let marker = placeholder(Param::Text(format!("%{escaped}%")), &mut args, None);
            let conditions = columns
                .into_iter()
                .map(|column| format!("LOWER(\"{column}\") LIKE LOWER({marker}) ESCAPE '!'"))
                .collect::<Vec<_>>()
                .join(" OR ");
            statement.push_str(&format!(" AND ({conditions})"));
        }
    }
    let marker = placeholder(Param::Integer(limit + 1), &mut args, None);
    statement.push_str(&format!(" ORDER BY row_source.id LIMIT {marker}"));
    let mut statement = sqlx::query(AssertSqlSafe(statement));
    for arg in args {
        statement = arg.bind(statement);
    }
    let rows = statement.fetch_all(&mut *tx).await?;
    let mut results = Vec::new();
    for row in rows.iter().take(limit as usize) {
        results.push(model.row(&mut tx, row).await?);
    }
    tx.commit().await?;
    let after = results
        .last()
        .filter(|_| rows.len() > limit as usize)
        .and_then(|row| row["id"].as_i64());
    Ok(endpoint.render(
        StatusCode::OK,
        json!({"results":results,"next_after":after}),
    ))
}

const BATCH: &str = "SELECT CAST(b.id AS BIGINT) AS id,CAST(b.request_id AS TEXT) AS request_id,
    CAST(b.credential_id AS BIGINT) AS credential_id,b.reason, CAST(b.created_at AS TEXT) AS created_at,
    CAST(b.undoes_id AS BIGINT) AS undoes, CAST(b.changes AS TEXT) AS changes, CAST(b.results AS TEXT) AS results,
    (SELECT CAST(id AS BIGINT) FROM core_agentchangeset WHERE undoes_id=b.id) AS reversed_by
    FROM core_agentchangeset b";

fn changes(row: &sqlx::any::AnyRow) -> Result<Value, ApiError> {
    Ok(serde_json::from_str(row.try_get("changes")?)?)
}

fn history_row(row: &sqlx::any::AnyRow) -> Result<Value, sqlx::Error> {
    Ok(
        json!({"id":row.try_get::<i64,_>("id")?,"reason":row.try_get::<&str,_>("reason")?,
        "credential_id":row.try_get::<i64,_>("credential_id")?,
        "created_at":catalog::datetime(row.try_get("created_at")?,false),"undoes":row.try_get::<Option<i64>,_>("undoes")?}),
    )
}

async fn batch(
    conn: &mut AnyConnection,
    endpoint: Endpoint<'_>,
    id: i64,
) -> Result<Response, ApiError> {
    let sql = format!("{BATCH} WHERE b.id=$1");
    let Some(row) = sqlx::query(AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(conn)
        .await?
    else {
        return Ok(endpoint.not_found());
    };
    let mut data = history_row(&row)?;
    let changes = changes(&row)?;
    let requests: &str = row.try_get("request_id")?;
    data["request_id"] = json!(
        uuid::Uuid::parse_str(requests)
            .ok()
            .map(|uuid| uuid.to_string())
    );
    data["reversed_by"] = json!(row.try_get::<Option<i64>, _>("reversed_by")?);
    data["results"] = serde_json::from_str(row.try_get("results")?)?;
    data["change_hash"] = json!(catalog::hash(&changes));
    data["changes"] = json!(
        changes
            .as_array()
            .into_iter()
            .flatten()
            .map(catalog::change_summary)
            .collect::<Vec<_>>()
    );
    Ok(endpoint.render(StatusCode::OK, data))
}

async fn history(
    state: &AppState,
    endpoint: Endpoint<'_>,
    uri: &Uri,
    id: Option<i64>,
) -> Result<Response, ApiError> {
    if let Some(id) = id {
        let mut conn = state.pool.acquire().await?;
        return batch(&mut conn, endpoint, id).await;
    }
    let params = params(uri);
    let (limit, before) = match (int(&params, "limit", 50), int(&params, "before", i64::MAX)) {
        (Ok(a), Ok(b)) => (a, b),
        (Err(error), _) | (_, Err(error)) => return Ok(endpoint.invalid(json!([error]))),
    };
    if !(1..=200).contains(&limit) || before < 1 {
        return Ok(endpoint.invalid(json!(["Invalid history pagination."])));
    }
    let sql = format!("{BATCH} WHERE b.id<$1 ORDER BY b.id DESC LIMIT $2");
    let rows = sqlx::query(AssertSqlSafe(sql))
        .bind(before)
        .bind(limit + 1)
        .fetch_all(&state.pool)
        .await?;
    let mut results = Vec::new();
    for row in rows.iter().take(limit as usize) {
        let mut result = history_row(row)?;
        result["changed_rows"] = json!(changes(row)?.as_array().map_or(0, Vec::len));
        results.push(result);
    }
    let next = results
        .last()
        .filter(|_| rows.len() > limit as usize)
        .and_then(|row| row["id"].as_i64());
    Ok(endpoint.render(
        StatusCode::OK,
        json!({"results":results,"next_before":next}),
    ))
}

async fn view(
    state: &AppState,
    endpoint: Endpoint<'_>,
    request: Request,
) -> Result<Response, ApiError> {
    let (uri, method, headers) = (request.uri(), request.method(), request.headers());
    let Some((credential_id, can_write)) = credential(state, headers).await? else {
        let detail = if well_formed(headers) {
            "Invalid, expired, or revoked agent credential."
        } else {
            "A valid agent bearer credential is required."
        };
        return Ok(endpoint.render(StatusCode::UNAUTHORIZED, json!({"detail":detail})));
    };
    if !can_write && !matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS) {
        return Ok(endpoint.render(
            StatusCode::FORBIDDEN,
            json!({"detail":"You do not have permission to perform this action."}),
        ));
    }
    if method == Method::POST
        && let Endpoint::History(None) | Endpoint::Undo(_) = endpoint
    {
        let undo = match endpoint {
            Endpoint::Undo(id) => Some(id),
            _ => None,
        };
        return batch::post(state, endpoint, credential_id, undo, request).await;
    }
    if method == Method::OPTIONS {
        return Ok(endpoint.render(StatusCode::OK,json!({
            "name":endpoint.name(),"description":"","renders":["application/json","text/html"],"parses":["application/json"],
        })));
    }
    if matches!(*method, Method::GET | Method::HEAD) {
        match endpoint {
            Endpoint::Discovery => {
                return Ok(endpoint.render(StatusCode::OK, catalog::discovery(can_write)));
            }
            Endpoint::Guide => return Ok(endpoint.render(StatusCode::OK, json!({"guide":GUIDE}))),
            Endpoint::Records(resource, id) => {
                return records(state, endpoint, uri, resource, id).await;
            }
            Endpoint::History(id) => return history(state, endpoint, uri, id).await,
            Endpoint::Undo(_) => (),
        }
    }
    Ok(endpoint.render(
        StatusCode::METHOD_NOT_ALLOWED,
        json!({"detail":format!("Method \"{method}\" not allowed.")}),
    ))
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    let path = request.uri().path().to_owned();
    let Some(endpoint) = Endpoint::parse(&path) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    view(&state, endpoint, request).await.unwrap_or_else(|error| {
        if matches!(error, ApiError::Constraint) {
            return endpoint.invalid(json!(["A database constraint prevents this change. The entire request was rolled back."]));
        }
        eprintln!("Agent data request failed: {error:?}");
        endpoint.render(StatusCode::CONFLICT, json!({"detail":"The database could not safely complete the change. The request was rolled back; retry with the same request_id."}))
    })
}
