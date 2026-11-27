use std::{io::Cursor, path::PathBuf};

use axum::{
    Json,
    body::Bytes,
    extract::{FromRequest, Multipart, Path, Request, State},
    http::{HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use chrono::{NaiveDate, Utc};
use image::ImageReader;
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use uuid::Uuid;

use crate::{AppState, login};

const FOLDER: &str = "django-summernote";
const FILE_LIMIT: usize = 1024 * 1024;

fn media_root() -> PathBuf {
    std::env::var_os("BLACK_ROD_MEDIA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_default()
}

fn same_origin(mut response: Response) -> Response {
    response.headers_mut().insert(
        header::X_FRAME_OPTIONS,
        HeaderValue::from_static("SAMEORIGIN"),
    );
    response
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"status":"false","message":message}))).into_response()
}

struct Upload {
    name: String,
    mime: &'static str,
    bytes: Bytes,
}

async fn read_uploads(state: &AppState, request: Request) -> Result<Vec<Upload>, Box<Response>> {
    let headers = request.headers().clone();
    let origin = state.origin(&headers).unwrap_or_default();
    let multipart = headers
        .get(header::CONTENT_TYPE)
        .and_then(|header| header.to_str().ok())
        .is_some_and(|value| value.starts_with("multipart/form-data"));
    if !multipart {
        if !login::csrf_valid(&headers, None, &origin) {
            return Err(Box::new(login::csrf_failure()));
        }
        return Ok(Vec::new());
    }
    let mut multipart = Multipart::from_request(request, &())
        .await
        .map_err(|err| Box::new(err.into_response()))?;
    let mut files = Vec::new();
    let mut csrf = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|err| Box::new(err.into_response()))?
    {
        if field.name() == Some("csrfmiddlewaretoken") {
            csrf = Some(
                field
                    .text()
                    .await
                    .map_err(|err| Box::new(err.into_response()))?,
            );
        } else if field.name() == Some("files")
            && let Some(raw) = field.file_name().map(str::to_owned)
        {
            let name = raw
                .rsplit(['/', '\\'])
                .next()
                .unwrap_or_default()
                .chars()
                .take(255)
                .collect::<String>();
            if name.is_empty() {
                continue;
            }
            let bytes = field
                .bytes()
                .await
                .map_err(|err| Box::new(err.into_response()))?;
            files.push((name, bytes));
        }
    }
    if !login::csrf_valid(&headers, csrf.as_deref(), &origin) {
        return Err(Box::new(login::csrf_failure()));
    }
    tokio::task::spawn_blocking(move|| {
        files.into_iter().map(|(name,bytes)|{
            if bytes.len()>FILE_LIMIT {
                return Err(Box::new(error(StatusCode::BAD_REQUEST,"File size exceeds the limit allowed and cannot be saved")))
            }
            let mime=ImageReader::new(Cursor::new(&bytes)).with_guessed_format().ok().and_then(|reader|{
                let format=reader.format()?;
                reader.decode().ok().map(|_|format.to_mime_type())
            });
            let Some(mime)=mime else{
                return Err(Box::new(error(StatusCode::BAD_REQUEST,
                    "Upload a valid image. The file you uploaded was either not an image or a corrupted image.")))
            };
            Ok(Upload{name,mime,bytes})
        }).collect()
    }).await.map_err(|_|Box::new(error(StatusCode::INTERNAL_SERVER_ERROR,"Failed to read attachment")))?
}

async fn write_files(
    state: &AppState,
    files: Vec<Upload>,
    saved: &mut Vec<PathBuf>,
) -> Result<Vec<Value>, Box<dyn std::error::Error + Send + Sync>> {
    let day = Utc::now().date_naive().to_string();
    let relative = PathBuf::from(FOLDER).join(day);
    let root = media_root();
    tokio::fs::create_dir_all(root.join(&relative)).await?;
    let mut tx = state.pool.begin().await?;
    let mut results = Vec::with_capacity(files.len());
    for Upload { name, mime, bytes } in files {
        let suffix = name.rsplit('.').next().unwrap_or_default();
        let extension = if !suffix.is_empty()
            && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
            && suffix.len() <= 12
        {
            suffix
        } else {
            mime.strip_prefix("image/").unwrap_or("img")
        };
        let filename = relative.join(format!("{}.{}", Uuid::new_v4(), extension));
        let absolute = root.join(&filename);
        let mut file = tokio::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&absolute)
            .await?;
        saved.push(absolute);
        file.write_all(&bytes).await?;
        file.flush().await?;
        let storage_name = filename.to_string_lossy();
        sqlx::query("INSERT INTO django_summernote_attachment (name,file,uploaded) VALUES ($1,$2,CURRENT_TIMESTAMP)")
            .bind(&name).bind(storage_name.as_ref()).execute(&mut *tx).await?;
        let mut url = String::from("/");
        url.push_str(&storage_name);
        results.push(json!({"name":name,"size":bytes.len(),"url":url}));
    }
    tx.commit().await?;
    Ok(results)
}

pub(crate) async fn upload(State(state): State<AppState>, request: Request) -> Response {
    if request.method() != Method::POST {
        return same_origin(error(
            StatusCode::BAD_REQUEST,
            "Only POST method is allowed",
        ));
    }
    let files = match read_uploads(&state, request).await {
        Ok(files) => files,
        Err(response) => return same_origin(*response),
    };
    if files.is_empty() {
        return same_origin(error(StatusCode::BAD_REQUEST, "No files were requested"));
    }
    let mut saved = Vec::new();
    match write_files(&state, files, &mut saved).await {
        Ok(files) => same_origin(Json(json!({"files":files})).into_response()),
        Err(reason) => {
            eprintln!("Could not save Summernote attachment: {reason}");
            for file in saved {
                let _ = tokio::fs::remove_file(file).await;
            }
            same_origin(error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to save attachment",
            ))
        }
    }
}

pub(crate) async fn attachment(Path((day, name)): Path<(String, String)>) -> Response {
    let Some((id, extension)) = name.split_once('.') else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if NaiveDate::parse_from_str(&day, "%Y-%m-%d").is_err()
        || Uuid::parse_str(id).is_err()
        || extension.is_empty()
        || !extension.bytes().all(|byte| byte.is_ascii_alphanumeric())
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let file = media_root().join(FOLDER).join(day).join(name);
    let Ok(bytes) = tokio::fs::read(file).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(format) = image::guess_format(&bytes) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    (
        [
            (header::CONTENT_TYPE, format.to_mime_type()),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        bytes,
    )
        .into_response()
}
