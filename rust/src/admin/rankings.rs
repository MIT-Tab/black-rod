use axum::{
    Json,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::json;

use crate::{ApiError, AppState, login, tournament_import::results::standings, web};

const TYPES: [(&str, &str); 4] = [
    ("toty", "TOTY"),
    ("coty", "COTY / Quals"),
    ("soty", "SOTY"),
    ("noty", "NOTY"),
];

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    let uri = request.uri().clone();
    let headers = request.headers().clone();
    let user = match super::require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    if request.method() == Method::POST {
        let Some(origin) = state.origin(&headers) else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        let fields = match login::parse_form(&headers, request.into_body(), 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return status.into_response(),
        };
        if !login::csrf_valid(
            &headers,
            fields.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return login::csrf_failure();
        }
        let selected = fields.get("season").map_or("", String::as_str);
        let ranking = fields.get("ranking_type").map_or("", String::as_str);
        let error = |value| Json(json!({"success":false,"error":value})).into_response();
        if selected.is_empty() || ranking.is_empty() {
            return error("Season and ranking type are required");
        }
        if !TYPES.iter().any(|(key, _)| *key == ranking) {
            return error("Unknown ranking type");
        }
        let valid = selected.len() <= 16
            && selected.split('-').all(|segment| {
                !segment.is_empty() && segment.bytes().all(|digit| digit.is_ascii_digit())
            })
            && selected
                .split('-')
                .next()
                .is_some_and(|year| year.parse::<i32>().is_ok());
        if !valid {
            return error("Invalid season");
        }
        let result = async {
            let mut tx = state.pool.begin().await?;
            standings::recompute_rankings(&mut tx, selected, ranking, state.current_season).await?;
            tx.commit().await?;
            Ok::<_, ApiError>(())
        }
        .await;
        if let Err(reason) = result {
            eprintln!("Rankings recomputation failed: {reason:?}");
            return error("The database could not recompute the rankings.");
        }
        crate::search::invalidate(&state).await;
        let boards = if ranking == "coty" {
            &["coty", "online_quals"][..]
        } else {
            &[ranking][..]
        };
        crate::standings::invalidate(&state, selected, boards).await;
        return Json(json!({"success":true,"message":format!("Successfully recomputed {} rankings for season {selected}", ranking.to_uppercase())})).into_response();
    }
    let Ok((secret, token)) = login::csrf_tokens(&headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let bindings = web::base_context(&state, &user, &uri);
    let html = match web::render(
        "admin/rankings_recompute.html",
        context! {
            seasons=>web::seasons(state.current_season), ranking_types=>TYPES, csrf=>token, ..bindings
        },
    ) {
        Ok(html) => html,
        Err(reason) => {
            eprintln!("Rankings recompute template failed: {reason}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    let secure = state
        .origin(&headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    login::set_csrf_cookie(&mut response, &secret, secure);
    response
}
