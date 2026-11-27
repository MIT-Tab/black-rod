use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::{
    AppState, flash, login,
    session::{self, StoredSession},
};

use super::{
    Board, CELLS, Guess, catalog, clues_visible, game_day, generate, pick_guess,
    redirect_with_message, render, scoring, uncached,
};

const SESSION_KEY: &str = "immaculate_grid_practice";
const URL: &str = "/immaculate-grid/infinite/";

#[derive(Serialize, Deserialize)]
pub(super) struct Play {
    pub(super) board: Board,
    pub(super) guesses: Vec<Guess>,
}

pub(super) fn play(stored: &StoredSession) -> Option<Play> {
    let data = stored.values.get(SESSION_KEY)?;
    let play = serde_json::from_str::<Play>(data.get()).ok()?;
    play.board.valid_shape().then_some(play)
}

fn seed() -> Result<u128, ring::error::Unspecified> {
    let mut bytes = [0_u8; 16];
    SystemRandom::new().fill(&mut bytes)?;
    bytes[6] = (bytes[6] & 0xf) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(u128::from_be_bytes(bytes))
}

async fn generate(state: &AppState) -> Result<Option<Board>, crate::ApiError> {
    let id = seed().map_err(|_| crate::ApiError::Serialization)?;
    let day = game_day();
    let mut conn = state.pool.acquire().await?;
    let catalog = catalog::load(&mut conn, day, &[]).await?;
    drop(conn);
    tokio::task::spawn_blocking(move || generate::build(day, catalog, &[], Some(id)))
        .await
        .map_err(|_| crate::ApiError::Serialization)
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    let headers = request.headers();
    let Ok(user) = session::user(&state.pool, headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_authenticated {
        return uncached(login::redirect_to_login(request.uri()));
    }
    let Some(secret) = state.secret.as_deref() else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(Some(mut stored)) = StoredSession::read(&state.pool, headers, secret).await else {
        return uncached(login::redirect_to_login(request.uri()));
    };
    let Some(origin) = state.origin(headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let secure = origin.starts_with("https://");
    let post = request.method() == Method::POST;
    let (parts, body) = request.into_parts();
    let mut status = StatusCode::OK;
    let mut data = json!({"day":game_day().to_string(), "infinite":true});
    if post {
        let Ok(body) = to_bytes(body, 128 * 1024).await else {
            return StatusCode::PAYLOAD_TOO_LARGE.into_response();
        };
        let fields = login::form_values(&body);
        if !login::csrf_valid(
            &parts.headers,
            fields.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return login::csrf_failure();
        }
        match generate(&state).await {
            Ok(Some(board)) if clues_visible(&state.pool, &board).await.unwrap_or(false) => {
                let Ok(value) = serde_json::value::to_raw_value(&Play {
                    board,
                    guesses: Vec::new(),
                }) else {
                    return StatusCode::INTERNAL_SERVER_ERROR.into_response();
                };
                stored.values.insert(SESSION_KEY.to_owned(), value);
                match stored.save(&state.pool, secret).await {
                    Ok(true) => {
                        return redirect_with_message(URL, None, &parts.headers, secret, secure);
                    }
                    Ok(false) => {
                        return redirect_with_message(
                            URL,
                            Some("This grid has changed. Choose a square on the current board."),
                            &parts.headers,
                            secret,
                            secure,
                        );
                    }
                    Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
                }
            }
            Ok(_) => status = StatusCode::SERVICE_UNAVAILABLE,
            Err(error) => {
                eprintln!("Practice board generation failed: {error:?}");
                status = StatusCode::INTERNAL_SERVER_ERROR;
            }
        }
    } else if let Some(play) = play(&stored)
        && clues_visible(&state.pool, &play.board)
            .await
            .unwrap_or(false)
    {
        data = scoring::grid(&play.board, &play.guesses, true);
    }
    let messages = flash::messages(&parts.headers, secret);
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    data["csrf"] = json!(token);
    data["messages"] = json!(messages);
    data["practice_post_failed"] = json!(post);
    let mut response = render(
        &state,
        &user,
        &parts.uri,
        "immaculate_grid/grid.html",
        data,
        status,
    );
    login::set_csrf_cookie(&mut response, &csrf, secure);
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    response
}

pub(crate) async fn guess(State(state): State<AppState>, request: Request) -> Response {
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_authenticated {
        return uncached(login::redirect_to_login(request.uri()));
    }
    let Some(secret) = state.secret.as_deref() else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let (parts, body) = request.into_parts();
    let Ok(body) = to_bytes(body, 128 * 1024).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let form = login::form_values(&body);
    let field = |name| form.get(name).map(String::as_str).unwrap_or_default();
    if !login::csrf_valid(
        &parts.headers,
        form.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return login::csrf_failure();
    }
    let redirect = |message| {
        redirect_with_message(
            URL,
            message,
            &parts.headers,
            secret,
            origin.starts_with("https://"),
        )
    };
    let (Ok(day), Ok(board_id), Ok(cell), Ok(attempts), Ok(debater)) = (
        field("day").parse(),
        field("board").parse::<u128>(),
        field("cell").parse::<usize>(),
        field("attempts").parse::<usize>(),
        field("debater").parse::<i64>(),
    ) else {
        return redirect(Some(
            "Choose a square and select a debater from the search results.",
        ));
    };
    if cell >= CELLS || board_id == 0 {
        return redirect(Some(
            "Choose a square and select a debater from the search results.",
        ));
    }
    let changed = "This grid has changed. Choose a square on the current board.";
    let Ok(Some(mut stored)) = StoredSession::read(&state.pool, &parts.headers, secret).await
    else {
        return redirect(Some(changed));
    };
    let Some(mut current) = play(&stored) else {
        return redirect(Some(changed));
    };
    if current.board.id != board_id
        || current.board.date != day
        || !clues_visible(&state.pool, &current.board)
            .await
            .unwrap_or(false)
    {
        return redirect(Some(changed));
    }
    match pick_guess(
        &state.pool,
        &current.board,
        &current.guesses,
        debater,
        cell,
        attempts,
    )
    .await
    {
        Ok(Ok(guess)) => current.guesses.push(guess),
        Ok(Err(issue)) => return redirect(Some(issue)),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
    let Ok(value) = serde_json::value::to_raw_value(&current) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    stored.values.insert(SESSION_KEY.to_owned(), value);
    match stored.save(&state.pool, secret).await {
        Ok(true) => redirect(None),
        Ok(false) => redirect(Some("Your grid was updated. Choose a square to continue.")),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}
