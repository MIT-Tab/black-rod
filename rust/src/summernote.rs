use std::{
    fs::File,
    io::{BufReader, Seek},
    path::PathBuf,
    time::Duration,
};

use axum::{
    Json,
    extract::{FromRequest, Multipart, Path, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{NaiveDate, Utc};
use image::{ImageReader, Limits};
use minijinja::context;
use serde_json::{Value, json};
use tokio::{
    io::{self, AsyncRead, AsyncWriteExt},
    sync::Semaphore,
};
use uuid::Uuid;

use crate::{AppState, login, web};

const FOLDER: &str = "django-summernote";
const FILE_LIMIT: usize = 1024 * 1024;
const FILE_COUNT_LIMIT: usize = 100;
const UPLOAD_READ_TIMEOUT: Duration = Duration::from_secs(120);
pub(crate) const REQUEST_LIMIT: usize = (FILE_COUNT_LIMIT + 1) * FILE_LIMIT;
static IMAGE_UPLOADS: Semaphore = Semaphore::const_new(2);

fn media_root(state: &AppState) -> PathBuf {
    std::env::var_os("BLACK_ROD_MEDIA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| state.release_root.to_path_buf())
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

struct StagedUpload {
    name: String,
    file: File,
    size: usize,
}

struct Upload {
    name: String,
    mime: &'static str,
    file: File,
    size: usize,
}

fn read_error() -> Box<Response> {
    Box::new(error(
        StatusCode::INTERNAL_SERVER_ERROR,
        "Failed to read attachment",
    ))
}

async fn read_uploads(
    state: &AppState,
    request: Request,
) -> Result<Vec<StagedUpload>, Box<Response>> {
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
    if login::csrf_request_secret(&headers, &origin).is_err() {
        return Err(Box::new(login::csrf_failure()));
    }
    let mut multipart = Multipart::from_request(request, &())
        .await
        .map_err(|err| Box::new(err.into_response()))?;
    let mut files = Vec::new();
    let mut csrf = None;
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|err| Box::new(err.into_response()))?
    {
        if field.name() == Some("csrfmiddlewaretoken") {
            let mut token = Vec::new();
            while let Some(chunk) = field
                .chunk()
                .await
                .map_err(|err| Box::new(err.into_response()))?
            {
                if token.len() + chunk.len() > 128 {
                    return Err(Box::new(login::csrf_failure()));
                }
                token.extend_from_slice(&chunk);
            }
            csrf = String::from_utf8(token).ok();
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
            if files.len() == FILE_COUNT_LIMIT {
                return Err(Box::new(error(
                    StatusCode::BAD_REQUEST,
                    "Too many files were requested",
                )));
            }
            let temporary = tokio::task::spawn_blocking(tempfile::tempfile)
                .await
                .map_err(|_| read_error())?
                .map_err(|_| read_error())?;
            let mut file = tokio::fs::File::from_std(temporary);
            let mut size = 0;
            while let Some(chunk) = field
                .chunk()
                .await
                .map_err(|err| Box::new(err.into_response()))?
            {
                size += chunk.len();
                if size > FILE_LIMIT {
                    return Err(Box::new(error(
                        StatusCode::BAD_REQUEST,
                        "File size exceeds the limit allowed and cannot be saved",
                    )));
                }
                file.write_all(&chunk).await.map_err(|_| read_error())?;
            }
            file.flush().await.map_err(|_| read_error())?;
            files.push(StagedUpload {
                name,
                file: file.into_std().await,
                size,
            });
        }
    }
    if !login::csrf_valid(&headers, csrf.as_deref(), &origin) {
        return Err(Box::new(login::csrf_failure()));
    }
    Ok(files)
}

async fn validate_uploads(files: Vec<StagedUpload>) -> Result<Vec<Upload>, Box<Response>> {
    let permit = IMAGE_UPLOADS.acquire().await.map_err(|_| read_error())?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        files
            .into_iter()
            .map(|StagedUpload { name, mut file, size }| {
                file.rewind().map_err(|_| read_error())?;
                let mime = ImageReader::new(BufReader::new(&mut file))
                    .with_guessed_format()
                    .ok()
                    .and_then(|mut reader| {
                        let format = reader.format()?;
                        let mut limits = Limits::default();
                        limits.max_alloc = Some(64 * 1024 * 1024);
                        limits.max_image_width = Some(16_384);
                        limits.max_image_height = Some(16_384);
                        reader.limits(limits);
                        reader.decode().ok().map(|_| format.to_mime_type())
                    })
                    .ok_or_else(|| Box::new(error(StatusCode::BAD_REQUEST,
                        "Upload a valid image. The file you uploaded was either not an image or a corrupted image.")))?;
                file.rewind().map_err(|_| read_error())?;
                Ok(Upload { name, mime, file, size })
            })
            .collect()
    })
    .await
    .map_err(|_| read_error())?
}

pub(crate) async fn write_attachment(
    state: &AppState,
    name: &str,
    mime: Option<&str>,
    bytes: &[u8],
) -> std::io::Result<(String, PathBuf)> {
    write_attachment_from(state, name, mime, bytes).await
}

async fn write_attachment_from(
    state: &AppState,
    name: &str,
    mime: Option<&str>,
    mut contents: impl AsyncRead + Unpin,
) -> io::Result<(String, PathBuf)> {
    let day = Utc::now().date_naive().to_string();
    let relative = PathBuf::from(FOLDER).join(day);
    let root = media_root(state);
    tokio::fs::create_dir_all(root.join(&relative)).await?;
    let suffix = name.rsplit('.').next().unwrap_or_default();
    let extension = if let Some(mime) = mime {
        match image::ImageFormat::from_path(name) {
            Ok(format) if format.to_mime_type() == mime => suffix,
            _ => mime.strip_prefix("image/").unwrap_or("bin"),
        }
    } else if !suffix.is_empty()
        && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
        && suffix.len() <= 12
    {
        suffix
    } else {
        "bin"
    };
    let filename = relative.join(format!("{}.{}", Uuid::new_v4(), extension));
    let absolute = root.join(&filename);
    let mut file = tokio::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&absolute)
        .await?;
    let result = async {
        io::copy(&mut contents, &mut file).await?;
        file.flush().await
    }
    .await;
    if let Err(error) = result {
        let _ = tokio::fs::remove_file(&absolute).await;
        return Err(error);
    }
    Ok((filename.to_string_lossy().into_owned(), absolute))
}

async fn write_files(
    state: &AppState,
    files: Vec<Upload>,
    saved: &mut Vec<PathBuf>,
) -> Result<Vec<Value>, Box<dyn std::error::Error + Send + Sync>> {
    let mut tx = state.pool.begin().await?;
    let mut results = Vec::with_capacity(files.len());
    for Upload {
        name,
        mime,
        file,
        size,
    } in files
    {
        let file = tokio::fs::File::from_std(file);
        let (storage_name, absolute) =
            write_attachment_from(state, &name, Some(mime), file).await?;
        saved.push(absolute);
        sqlx::query("INSERT INTO django_summernote_attachment (name,file,uploaded) VALUES ($1,$2,CURRENT_TIMESTAMP)")
            .bind(&name).bind(&storage_name).execute(&mut *tx).await?;
        results.push(json!({"name":name,"size":size,"url":format!("/{storage_name}")}));
    }
    tx.commit().await?;
    Ok(results)
}

pub(crate) async fn editor(
    State(state): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let id_safe = id.replace('-', "_");
    if !id_safe
        .bytes()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == b'_')
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Ok((csrf, token)) = login::csrf_tokens(&headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let css = [
        "/static/summernote/summernote-lite.css",
        "/static/summernote/django_summernote.css",
    ];
    let js = [
        "//code.jquery.com/jquery-3.6.0.min.js",
        "/static/summernote/jquery.ui.widget.js",
        "/static/summernote/jquery.iframe-transport.js",
        "/static/summernote/jquery.fileupload.js",
        "/static/summernote/summernote-lite.min.js",
        "/static/summernote/ResizeSensor.js",
    ];
    let html = web::render(
        "django_summernote/widget_iframe_editor.html",
        context! {
            id_safe, css, js, iframe=>true, csrf_token=>token,
            config=>context!{jquery=>"$",disable_attachment=>false},
        },
    );
    let Ok(html) = html else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let mut response = same_origin(([(header::VARY, "Cookie")], Html(html)).into_response());
    let secure = state
        .origin(&headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    login::set_csrf_cookie(&mut response, &csrf, secure);
    response
}

pub(crate) async fn upload(State(state): State<AppState>, request: Request) -> Response {
    if request.method() != Method::POST {
        return same_origin(error(
            StatusCode::BAD_REQUEST,
            "Only POST method is allowed",
        ));
    }
    let staged =
        match tokio::time::timeout(UPLOAD_READ_TIMEOUT, read_uploads(&state, request)).await {
            Ok(Ok(files)) => files,
            Ok(Err(response)) => return same_origin(*response),
            Err(_) => {
                return same_origin(error(
                    StatusCode::REQUEST_TIMEOUT,
                    "Attachment upload timed out",
                ));
            }
        };
    if staged.is_empty() {
        return same_origin(error(StatusCode::BAD_REQUEST, "No files were requested"));
    }
    let files = match validate_uploads(staged).await {
        Ok(files) => files,
        Err(response) => return same_origin(*response),
    };
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

pub(crate) async fn attachment(
    State(state): State<AppState>,
    Path((day, name)): Path<(String, String)>,
) -> Response {
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
    let file = media_root(&state).join(FOLDER).join(day).join(name);
    let Ok(bytes) = tokio::fs::read(file).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let mime = image::guess_format(&bytes)
        .map_or("application/octet-stream", |format| format.to_mime_type());
    (
        [
            (header::CONTENT_TYPE, mime),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        bytes,
    )
        .into_response()
}
