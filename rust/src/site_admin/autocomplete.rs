use axum::{
    Json,
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::json;
use sqlx::AssertSqlSafe;

use super::{
    AdminRequest,
    listing::{Source, label, search_condition},
    schema::schema,
};
use crate::{ApiError, login, web};

pub(super) async fn endpoint(admin: &AdminRequest<'_>) -> Result<Response, ApiError> {
    let query = login::form_values(admin.uri.query().unwrap_or_default().as_bytes());
    let input = |name| query.get(name).map_or("", String::as_str);
    let source = schema().model(input("app_label"), input("model_name"));
    let model = source
        .and_then(|model| model.fields.get(input("field_name")))
        .and_then(|field| field.related.as_deref())
        .and_then(|related| related.split_once('.'))
        .and_then(|(app, model)| schema().model(app, model));
    let Some(model) = model else {
        return Ok(web::permission_denied(
            admin.state,
            &admin.viewer.user,
            admin.uri,
        ));
    };
    if !admin.viewer.read(model) {
        return Ok(web::permission_denied(
            admin.state,
            &admin.viewer.user,
            admin.uri,
        ));
    }
    if model.search.is_empty() {
        return Ok(StatusCode::NOT_FOUND.into_response());
    }
    let mut from = Source::new(model);
    let mut values = Vec::new();
    let mut conditions: Vec<_> = search_condition(model, input("term"), &mut values)
        .into_iter()
        .collect();
    if model.excludes_temporary() {
        conditions.push("NOT t0.temporary".to_owned());
    }
    let mut order = Vec::new();
    for key in &model.ordering {
        if let Some((field, _)) = from.resolve(key.trim_start_matches('-')) {
            order.push(format!(
                "{field} {}",
                if key.starts_with('-') { "DESC" } else { "ASC" }
            ));
        }
    }
    order.push("t0.id DESC".to_owned());
    let conditions = if conditions.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", conditions.join(" AND "))
    };
    let id = input("page").parse::<u32>().unwrap_or(1).max(1);
    let name = label(&mut from, model, "");
    let sql = format!(
        "SELECT CAST(t0.id AS TEXT),{name} FROM {}{conditions} ORDER BY {} LIMIT 21 OFFSET {}",
        from.sql(),
        order.join(","),
        20 * u64::from(id - 1)
    );
    let mut query = sqlx::query_as::<_, (String, Option<String>)>(AssertSqlSafe(sql));
    for input in &values {
        query = query.bind(input);
    }
    let mut rows = query.fetch_all(&admin.state.pool).await?;
    let more = rows.len() > 20;
    rows.truncate(20);
    let results = rows
        .into_iter()
        .map(|(id, name)| json!({"id":id,"text":name.unwrap_or_else(||id.clone())}))
        .collect::<Vec<_>>();
    Ok(Json(json!({"results":results,"pagination":{"more":more}})).into_response())
}

pub(super) async fn view_on_site(
    admin: &AdminRequest<'_>,
    content: i64,
    id: i64,
) -> Result<Response, ApiError> {
    let model = sqlx::query_scalar::<_, String>(
        "SELECT model FROM django_content_type WHERE id=$1 AND app_label='core'",
    )
    .bind(content)
    .fetch_optional(&admin.state.pool)
    .await?;
    let Some(model) = model else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let path = match model.as_str() {
        "school" => format!("/core/schools/{id}"),
        "debater" => format!("/core/debaters/{id}"),
        "team" => format!("/core/teams/{id}"),
        "tournament" => format!("/core/tournaments/{id}"),
        "resource" => format!("/core/resources/{id}"),
        "motion" => format!("/core/motions/{id}"),
        "video" => format!("/core/videos/{id}"),
        _ => return Ok(StatusCode::NOT_FOUND.into_response()),
    };
    Ok((StatusCode::FOUND, [(header::LOCATION, path)]).into_response())
}
