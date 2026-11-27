mod data;
mod distribution;

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;

use crate::{ApiError, AppState, team_profile::read_transaction, web};

async fn compute(state: &AppState) -> Result<Vec<serde_json::Value>, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let identities = data::Identities::load(&mut tx).await?;
    let mut summaries = Vec::new();
    for season in data::seasons(&mut tx).await? {
        let mut samples = distribution::Samples::default();
        let novices = identities.novices(&season);
        for tournament in data::tournaments(&mut tx, &season, &identities)
            .await?
            .into_values()
        {
            samples.push(tournament, novices.clone());
        }
        if let Some(summary) = samples.finish(&season, summaries.len()) {
            summaries.push(summary);
        }
    }
    tx.commit().await?;
    Ok(summaries)
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let user = match super::require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let seasons = match compute(&state).await {
        Ok(seasons) => seasons,
        Err(error) => {
            eprintln!("Speaks statistics data failed: {error:?}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let common = web::base_context(&state, &user, &uri);
    match web::render("admin/speaks_statistics.html", context! {seasons,..common}) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Speaks statistics template failed: {error:#}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
