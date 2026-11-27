use std::{io::Cursor, path::PathBuf};

use axum::{
    Json,
    body::Bytes,
    extract::{FromRequest, Multipart, Path, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{NaiveDate, Utc};
use image::{ImageReader, Limits};
use minijinja::context;
use serde_json::{Value, json};
use tokio::{io::AsyncWriteExt, sync::Semaphore};
use uuid::Uuid;

use crate::{AppState, login, web};

const FOLDER: &str = "django-summernote";
const FILE_LIMIT: usize = 1024 * 1024;
static IMAGE_VALIDATORS: Semaphore = Semaphore::const_new(2);

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
    if files.is_empty() {
        return Ok(Vec::new());
    }
    let failed = || {
        Box::new(error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Failed to read attachment",
        ))
    };
    let permit = IMAGE_VALIDATORS.acquire().await.map_err(|_| failed())?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        files.into_iter().map(|(name, bytes)| {
            if bytes.len() > FILE_LIMIT {
                return Err(Box::new(error(StatusCode::BAD_REQUEST,
                    "File size exceeds the limit allowed and cannot be saved")));
            }
            let mime = ImageReader::new(Cursor::new(&bytes))
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
                });
            let Some(mime) = mime else {
                return Err(Box::new(error(StatusCode::BAD_REQUEST,
                    "Upload a valid image. The file you uploaded was either not an image or a corrupted image.")));
            };
            Ok(Upload {name, mime, bytes})
        }).collect()
    }).await.map_err(|_| failed())?
}

pub(crate) async fn write_attachment(
    name: &str,
    mime: Option<&str>,
    bytes: &[u8],
) -> std::io::Result<(String, PathBuf)> {
    let day = Utc::now().date_naive().to_string();
    let relative = PathBuf::from(FOLDER).join(day);
    let root = media_root();
    tokio::fs::create_dir_all(root.join(&relative)).await?;
    let suffix = name.rsplit('.').next().unwrap_or_default();
    let extension = if !suffix.is_empty()
        && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
        && suffix.len() <= 12
    {
        suffix
    } else {
        mime.and_then(|mime| mime.strip_prefix("image/"))
            .unwrap_or("bin")
    };
    let filename = relative.join(format!("{}.{}", Uuid::new_v4(), extension));
    let absolute = root.join(&filename);
    let mut file = tokio::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&absolute)
        .await?;
    let result = async {
        file.write_all(bytes).await?;
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
    for Upload { name, mime, bytes } in files {
        let (storage_name, absolute) = write_attachment(&name, Some(mime), &bytes).await?;
        saved.push(absolute);
        sqlx::query("INSERT INTO django_summernote_attachment (name,file,uploaded) VALUES ($1,$2,CURRENT_TIMESTAMP)")
            .bind(&name).bind(&storage_name).execute(&mut *tx).await?;
        results.push(json!({"name":name,"size":bytes.len(),"url":format!("/{storage_name}")}));
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
