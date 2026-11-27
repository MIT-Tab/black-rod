mod bundle;
mod identity;
pub(crate) mod results;

use axum::{
    extract::{FromRequest, Multipart, Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use ring::digest;
use serde_json::{Value, json};
use sqlx::AnyConnection;

use crate::{ApiError, AppState, admin, flash, login, session::User, web};

#[derive(Debug)]
enum Error {
    Invalid(String),
    Database(ApiError),
}

impl From<sqlx::Error> for Error {
    fn from(error: sqlx::Error) -> Self {
        Self::Database(crate::agent::journal::storage_error(error))
    }
}

impl From<ApiError> for Error {
    fn from(error: ApiError) -> Self {
        Self::Database(error)
    }
}

fn invalid(message: impl Into<String>) -> Error {
    Error::Invalid(message.into())
}

fn as_id(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str()?.trim().parse().ok())
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
    }
}

fn text(value: &Value) -> String {
    if !truthy(value) {
        String::new()
    } else {
        value
            .as_str()
            .map_or_else(|| value.to_string(), str::to_owned)
    }
}

async fn ready(conn: &mut AnyConnection, id: i64) -> Result<bool, ApiError> {
    let value=sqlx::query_scalar::<_,i64>("SELECT CAST(CASE WHEN EXISTS(SELECT 1 FROM core_teamresult WHERE tournament_id=$1)
        OR EXISTS(SELECT 1 FROM core_speakerresult WHERE tournament_id=$1) THEN 1 ELSE 0 END AS BIGINT)")
        .bind(id).fetch_one(conn).await?;
    Ok(value == 1)
}

async fn page(
    state: &AppState,
    user: &User,
    uri: &Uri,
    headers: &HeaderMap,
    id: i64,
    error: Option<&str>,
) -> Result<Response, Error> {
    let mut conn = state.pool.acquire().await?;
    let Some(name) =
        sqlx::query_scalar::<_, String>("SELECT name FROM core_tournament WHERE id=$1")
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?
    else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let results_ready = ready(&mut conn, id).await?;
    let (csrf, token) = login::csrf_tokens(headers).map_err(|_| ApiError::Serialization)?;
    let common = web::base_context(state, user, uri);
    let settings =
        context! {tournament=>json!({"id":id,"name":name}),results_ready, error,csrf=>token};
    let markup = web::render(
        "tournaments/mittab_bundle_upload.html",
        context! {..settings,..common},
    )
    .map_err(|error| {
        eprintln!("Mit-Tab upload template failed: {error}");
        ApiError::Serialization
    })?;
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    let mut response = ([(header::VARY, "Cookie")], Html(markup)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

#[derive(Default)]
struct Upload {
    token: Option<String>,
    tournament: Option<i64>,
    filename: String,
    content: Vec<u8>,
}

impl Upload {
    async fn read(request: Request, state: &AppState) -> Result<Self, StatusCode> {
        let mut multipart = Multipart::from_request(request, state)
            .await
            .map_err(|error| error.status())?;
        let mut upload = Self::default();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|error| error.status())?
        {
            match field.name() {
                Some("csrfmiddlewaretoken") => {
                    upload.token = Some(field.text().await.map_err(|error| error.status())?)
                }
                Some("tournament") => {
                    upload.tournament = field
                        .text()
                        .await
                        .ok()
                        .and_then(|id| id.trim().parse().ok())
                }
                Some("bundle_file") => {
                    upload.filename = field
                        .file_name()
                        .map_or("", |name| {
                            name.rsplit(['/', '\\']).next().unwrap_or_default()
                        })
                        .to_owned();
                    upload.content = field
                        .bytes()
                        .await
                        .map_err(|error| error.status())?
                        .to_vec();
                }
                _ => (),
            }
        }
        Ok(upload)
    }

    fn document(&self) -> Result<Value, Error> {
        if self.filename.is_empty() {
            return Err(invalid("This field is required."));
        }
        if self.content.is_empty() {
            return Err(invalid("The submitted file is empty."));
        }
        if !self.filename.to_ascii_lowercase().ends_with(".json") {
            return Err(invalid("Please upload a .json Mit-Tab bundle file."));
        }
        let text = std::str::from_utf8(&self.content)
            .map_err(|_| invalid("Mit-Tab bundle files must be valid UTF-8 JSON."))?;
        serde_json::from_str(text.trim_start_matches('\u{feff}')).map_err(|error| {
            let detail = error.to_string();
            let detail = detail
                .split_once(" at line ")
                .map_or(detail.as_str(), |(head, _)| head);
            invalid(format!("Invalid JSON: {detail}."))
        })
    }
}

async fn request(state: &AppState, request: Request) -> Result<Response, Error> {
    let uri = request.uri().clone();
    let user = match admin::require(state, &uri, request.headers()).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    let id = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(key, _)| key == "tournament")
        .last()
        .and_then(|(_, value)| value.parse().ok());
    if request.method() != Method::POST {
        return match id {
            Some(id) => page(state, &user, &uri, request.headers(), id, None).await,
            None => Ok(StatusCode::BAD_REQUEST.into_response()),
        };
    }
    let headers = request.headers().clone();
    let upload = match Upload::read(request, state).await {
        Ok(upload) => upload,
        Err(status) => return Ok(status.into_response()),
    };
    let Some(origin) = state.origin(&headers) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    if !login::csrf_valid(&headers, upload.token.as_deref(), &origin) {
        return Ok(login::csrf_failure());
    }
    let Some(id) = id.or(upload.tournament) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let result = match upload.document() {
        Ok(document) => {
            let hash = hex::encode(digest::digest(&digest::SHA256, &upload.content));
            bundle::apply(state, id, &document, &upload.filename, &hash).await
        }
        Err(error) => Err(error),
    };
    let summary = match result {
        Ok(summary) => summary,
        Err(Error::Invalid(error)) => {
            return page(state, &user, &uri, &headers, id, Some(&error)).await;
        }
        Err(Error::Database(ApiError::Constraint)) => {
            return page(
                state,
                &user,
                &uri,
                &headers,
                id,
                Some("A database constraint prevented this bundle from being imported."),
            )
            .await;
        }
        Err(error) => return Err(error),
    };
    let (level, message) = summary.flash();
    let mut response = (
        StatusCode::FOUND,
        [(header::LOCATION, format!("/core/tournaments/{id}"))],
    )
        .into_response();
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            &mut response,
            &headers,
            secret,
            level,
            &message,
            origin.starts_with("https://"),
        );
    }
    Ok(response)
}

pub(crate) async fn upload(State(state): State<AppState>, http: Request) -> Response {
    match request(&state, http).await {
        Ok(response) => response,
        Err(Error::Invalid(error)) => {
            eprintln!("Mit-Tab upload parameters failed: {error}");
            StatusCode::BAD_REQUEST.into_response()
        }
        Err(Error::Database(error)) => {
            eprintln!("Mit-Tab upload failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
