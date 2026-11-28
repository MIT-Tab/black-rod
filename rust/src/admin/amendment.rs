mod imports;
pub(super) mod record;
pub(super) mod round;

use axum::{
    extract::{FromRequest, Multipart, Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::AnyConnection;

use crate::{
    ApiError, AppState,
    agent::{
        merge::{self, AuditActor},
        mutation::Failure,
    },
    delete_relations, flash, login, tournament_import,
    upload_file::{self, UploadFile},
};

#[derive(Debug)]
pub(super) enum Error {
    Invalid(String),
    Database(ApiError),
}

impl Error {
    pub(super) fn import(error: tournament_import::Error) -> Self {
        match error {
            tournament_import::Error::Invalid(error) => Self::Invalid(error),
            tournament_import::Error::Database(error) => Self::Database(error),
        }
    }
    pub(super) fn message(&self) -> String {
        match self {
            Self::Invalid(error) => error.clone(),
            Self::Database(ApiError::Constraint) => {
                "A database constraint prevented the amendment.".to_owned()
            }
            Self::Database(error) => {
                eprintln!("Round amendment failed: {error:?}");
                "The database could not apply the amendment.".to_owned()
            }
        }
    }
}

impl From<ApiError> for Error {
    fn from(error: ApiError) -> Self {
        Self::Database(error)
    }
}

impl From<sqlx::Error> for Error {
    fn from(error: sqlx::Error) -> Self {
        Self::Database(crate::agent::journal::storage_error(error))
    }
}

impl From<serde_json::Error> for Error {
    fn from(error: serde_json::Error) -> Self {
        Self::Database(error.into())
    }
}

fn invalid(message: impl Into<String>) -> Error {
    Error::Invalid(message.into())
}

pub(super) fn text(value: &Value) -> String {
    tournament_import::text(value)
}

fn truthy(value: &Value) -> bool {
    tournament_import::truthy(value)
}

pub(super) fn integer(value: &Value) -> Option<i64> {
    tournament_import::as_id(value)
}

fn boolean(value: &Value) -> bool {
    match value {
        Value::String(text) => matches!(
            text.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on" | "y" | "t"
        ),
        value => truthy(value),
    }
}

fn array<'a>(value: &'a Value, error: &str) -> Result<&'a [Value], Error> {
    if !truthy(value) {
        Ok(&[])
    } else {
        value
            .as_array()
            .map(Vec::as_slice)
            .ok_or_else(|| invalid(error))
    }
}

pub(super) async fn erase(
    conn: &mut AnyConnection,
    table: &'static str,
    id: i64,
) -> Result<(), Error> {
    delete_relations::delete(conn, table, id)
        .await
        .map_err(|error| match error {
            delete_relations::DeleteError::Database(error) => error.into(),
            delete_relations::DeleteError::Absent => invalid("Record not found."),
            delete_relations::DeleteError::Protected => {
                invalid("This record still has linked results.")
            }
        })
}

const KINDS: [&str; 6] = [
    "create_round",
    "update_round",
    "delete_round",
    "delete_tournament_import",
    "move_tournament_import",
    "resolve_synthetic",
];

fn explicit(
    values: &Value,
    output: &mut Vec<Value>,
    fallback: &str,
    error: &str,
) -> Result<(), Error> {
    for action in array(values, error)? {
        if !action.is_object() {
            return Err(invalid("Each amendment action must be an object."));
        }
        let mut kind = [text(&action["type"]), text(&action["action"])]
            .into_iter()
            .find(|value| !value.is_empty())
            .unwrap_or_else(|| fallback.to_owned());
        kind = kind.trim().to_owned();
        if kind.is_empty() {
            return Err(invalid("Each amendment action must declare a type."));
        }
        if !KINDS.contains(&kind.as_str()) {
            return Err(invalid(format!(
                "Unsupported amendment action type: {kind}."
            )));
        }
        let mut normalized = action.clone();
        normalized["type"] = json!(kind);
        output.push(normalized);
    }
    Ok(())
}

pub(super) fn actions(document: &Value) -> Result<Vec<Value>, Error> {
    if !document.is_object() {
        return Err(invalid(
            "Amendment JSON must be an object at the top level.",
        ));
    }
    let mut result = Vec::new();
    explicit(
        &document["actions"],
        &mut result,
        "",
        "The 'actions' field must be a list.",
    )?;
    explicit(
        &document["synthetic_resolutions"],
        &mut result,
        "resolve_synthetic",
        "The 'synthetic_resolutions' field must be a list.",
    )?;
    for (section, groups) in [
        (
            "rounds",
            &[
                ("create", "create_round"),
                ("update", "update_round"),
                ("delete", "delete_round"),
            ][..],
        ),
        (
            "tournament_imports",
            &[
                ("delete", "delete_tournament_import"),
                ("move", "move_tournament_import"),
            ][..],
        ),
    ] {
        let group = &document[section];
        if truthy(group) && !group.is_object() {
            return Err(invalid(format!("The '{section}' field must be an object.")));
        }
        for &(key, kind) in groups {
            explicit(
                &group[key],
                &mut result,
                kind,
                &format!("The '{section}.{key}' field must be a list."),
            )?;
        }
    }
    if result.is_empty() {
        return Err(invalid("No amendment actions were found in the JSON file."));
    }
    Ok(result)
}

#[derive(Default)]
pub(super) struct Summary {
    actions_applied: usize,
    synthetic_resolutions: usize,
    pub(super) rounds_created: usize,
    pub(super) rounds_updated: usize,
    pub(super) rounds_deleted: usize,
    pub(super) rounds_moved: usize,
    pub(super) tournament_imports_deleted: usize,
    pub(super) tournament_imports_moved: usize,
    pub(super) linked_source_imports_moved: usize,
}

impl Summary {
    fn message(&self) -> String {
        let mut message = format!("Applied {} amendment actions.", self.actions_applied);
        let details = [
            (self.synthetic_resolutions, "synthetic resolutions"),
            (self.rounds_created, "rounds created"),
            (self.rounds_updated, "rounds updated"),
            (self.rounds_deleted, "rounds deleted"),
            (self.rounds_moved, "rounds moved"),
            (
                self.tournament_imports_deleted,
                "tournament imports deleted",
            ),
            (
                self.tournament_imports_moved,
                "tournament import move actions",
            ),
            (
                self.linked_source_imports_moved,
                "linked source imports moved",
            ),
        ]
        .into_iter()
        .filter(|(count, _)| *count > 0)
        .map(|(count, label)| format!("{count} {label}"))
        .collect::<Vec<_>>();
        if !details.is_empty() {
            message.push_str(&format!(" {}.", details.join("; ")));
        }
        message
    }
}

pub(super) async fn execute(
    conn: &mut AnyConnection,
    actions: &[Value],
    season: i32,
    actor: Option<i64>,
    context: &Value,
) -> Result<Summary, Error> {
    let mut summary = Summary {
        actions_applied: actions.len(),
        ..Default::default()
    };
    for (index, action) in actions.iter().enumerate() {
        let kind = action["type"].as_str().unwrap_or_default();
        let id = |field| {
            integer(&action[field]).ok_or_else(|| invalid(format!("'{field}' is required.")))
        };
        let imported = || {
            integer(&action["tournament_import_id"])
                .or_else(|| integer(&action["id"]))
                .ok_or_else(|| {
                    invalid("Tournament import actions require 'id' or 'tournament_import_id'.")
                })
        };
        match kind {
            "create_round" | "update_round" => {
                round::save(conn, action, kind == "create_round", season, &mut summary).await?;
            }
            "delete_round" => {
                let id = round::resolve(conn, action).await?;
                erase(conn, "core_round", id).await?;
                summary.rounds_deleted += 1;
            }
            "delete_tournament_import" => imports::delete(conn, imported()?, &mut summary).await?,
            "move_tournament_import" => {
                imports::move_to(conn, imported()?, id("target_tournament_id")?, &mut summary)
                    .await?
            }
            "resolve_synthetic" => {
                let entity = text(&action["entity_type"]).trim().to_ascii_lowercase();
                let mut metadata = context.clone();
                metadata["action_type"] = json!(kind);
                metadata["action_index"] = json!(index + 1);
                merge::resolve(
                    conn,
                    &entity,
                    id("synthetic_id")?,
                    id("target_id")?,
                    AuditActor {
                        user: actor,
                        reason: &text(&action["reason"]),
                        context: metadata,
                    },
                )
                .await
                .map_err(|error| match error {
                    Failure::Invalid(message) => invalid(message),
                    Failure::Stale(detail) => invalid(text(&detail["detail"])),
                    Failure::Database(error) => error.into(),
                })?;
                summary.synthetic_resolutions += 1;
            }
            _ => {
                return Err(invalid(format!(
                    "Unsupported amendment action type: {kind}."
                )));
            }
        }
    }
    Ok(summary)
}

pub(super) async fn invalidate(state: &AppState) {
    crate::search::invalidate(state).await;
    crate::standings::invalidate(
        state,
        &state.current_season.to_string(),
        crate::standings::STORED_BOARDS,
    )
    .await;
    crate::elo::invalidate(state).await;
}

fn document(bytes: &[u8]) -> Result<Value, Error> {
    let decoded = std::str::from_utf8(bytes)
        .map_err(|_| invalid("Amendment files must be valid UTF-8 JSON."))?;
    serde_json::from_str(decoded.trim_start_matches('\u{feff}')).map_err(|error| {
        let detail = error.to_string();
        let detail = detail
            .split_once(" at line ")
            .map_or(detail.as_str(), |(head, _)| head);
        invalid(format!("Invalid JSON: {detail}."))
    })
}

pub(crate) async fn upload(State(state): State<AppState>, request: Request) -> Response {
    let uri = request.uri().clone();
    let headers = request.headers().clone();
    let user = match super::require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    if request.method() != Method::POST {
        return super::render_tools(&state, &uri, &headers, &user, &[]).await;
    }
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    if login::csrf_request_secret(&headers, &origin).is_err() {
        return login::csrf_failure();
    }
    let mut multipart = match Multipart::from_request(request, &state).await {
        Ok(multipart) => multipart,
        Err(error) => return error.status().into_response(),
    };
    let mut token = None;
    let mut filename = String::new();
    let mut content = None;
    loop {
        let field = match multipart.next_field().await {
            Ok(Some(field)) => field,
            Ok(None) => break,
            Err(error) => return error.status().into_response(),
        };
        match field.name() {
            Some("csrfmiddlewaretoken") => match upload_file::text(field).await {
                Ok(value) => token = Some(value),
                Err(status) => return status.into_response(),
            },
            Some("amendment_file") => {
                filename = field
                    .file_name()
                    .unwrap_or_default()
                    .rsplit(['/', '\\'])
                    .next()
                    .unwrap_or_default()
                    .to_owned();
                content = match UploadFile::stage(field).await {
                    Ok(content) => Some(content),
                    Err(status) => return status.into_response(),
                };
            }
            _ => (),
        }
    }
    if !login::csrf_valid(&headers, token.as_deref(), &origin) {
        return login::csrf_failure();
    }
    let result = async {
        let Some(content) = content.filter(|file| !file.empty) else {
            return Err(invalid("Please upload a valid amendment JSON file."));
        };
        if !filename.to_ascii_lowercase().ends_with(".json") {
            return Err(invalid("Please upload a .json amendment file."));
        }
        let document = content
            .parse(document)
            .await
            .map_err(|_| Error::Database(ApiError::Serialization))??;
        let actions = actions(&document)?;
        let mut tx = state.pool.begin().await?;
        let summary = execute(
            &mut tx,
            &actions,
            state.current_season,
            user.id,
            &json!({
                "source":"admin_tools_round_amendment_upload","file_name":filename,
            }),
        )
        .await?;
        tx.commit().await?;
        Ok(summary)
    }
    .await;
    match result {
        Ok(summary) => {
            invalidate(&state).await;
            let mut response = (
                StatusCode::FOUND,
                [(header::LOCATION, "/core/admin-tools/")],
            )
                .into_response();
            if let Some(secret) = state.secret.as_deref() {
                flash::push(
                    &mut response,
                    &headers,
                    secret,
                    25,
                    &summary.message(),
                    origin.starts_with("https://"),
                );
            }
            response
        }
        Err(error) => {
            super::render_tools(
                &state,
                &uri,
                &headers,
                &user,
                &[format!(
                    "Round amendment upload failed: {}",
                    error.message()
                )],
            )
            .await
        }
    }
}
