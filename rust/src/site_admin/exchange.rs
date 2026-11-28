mod formats;
mod import;

use axum::{
    body::Body,
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use minijinja::{Value, context};
use serde_json::{Value as Json, json};
use sqlx::{AssertSqlSafe, Row};

use super::{
    AdminRequest, cap_first,
    listing::{self, Parameters, filters::Filter},
    schema::{Exchange, Field, Model, schema},
};
use crate::{ApiError, login};

fn common(model: &Model, exchange: &Exchange, importing: bool) -> Value {
    let app = schema()
        .app(model.app())
        .expect("admin exchange application");
    let choices = if importing {
        &exchange.import_formats
    } else {
        &exchange.export_formats
    };
    context! {
        title=>if importing{"Import"}else{"Export"},importing,model_name=>model.slug(),app_label=>app.label.as_str(),
        app_name=>&app.name,app_url=>app.url(),plural=>cap_first(&model.plural),model_url=>model.url(),
        formats=>choices,fields=>&exchange.fields,media=>exchange.import_media(),
    }
}

fn export_value(field: &Field, raw: Option<String>) -> Json {
    let Some(raw) = raw else {
        return if matches!(
            field.kind.as_str(),
            "TextField" | "CharField" | "EmailField" | "URLField" | "JSONField"
        ) {
            json!("None")
        } else if field.kind == "BooleanField"
            || field.related.is_some()
            || matches!(field.kind.as_str(), "DateField" | "DateTimeField")
        {
            json!("")
        } else {
            Json::Null
        };
    };
    match field.kind.as_str() {
        "BooleanField" => json!(if matches!(raw.as_str(), "true" | "1" | "t") {
            "1"
        } else {
            "0"
        }),
        "AutoField"
        | "BigAutoField"
        | "IntegerField"
        | "SmallIntegerField"
        | "PositiveIntegerField"
        | "PositiveSmallIntegerField"
        | "PositiveBigIntegerField"
        | "BigIntegerField"
        | "ForeignKey"
        | "OneToOneField" => raw
            .parse::<i64>()
            .ok()
            .map_or_else(|| json!(raw), |number| json!(number)),
        "FloatField" => raw
            .parse::<f64>()
            .ok()
            .map_or_else(|| json!(raw), |number| json!(number)),
        "DecimalField" => raw.parse::<f64>().ok().map_or_else(
            || json!(raw),
            |value| {
                json!(format!(
                    "{value:.scale$}",
                    scale = field.scale.unwrap_or_default()
                ))
            },
        ),
        "DateTimeField" => json!(raw.get(..19).unwrap_or(&raw).replace('T', " ")),
        "JSONField" => serde_json::from_str::<Json>(&raw)
            .map_or_else(|_| json!(raw), |value| json!(value.to_string())),
        _ => json!(raw),
    }
}

async fn export_rows(
    admin: &AdminRequest<'_>,
    model: &Model,
    exchange: &Exchange,
    query: &Parameters,
    filter: Filter<'_>,
) -> Result<Vec<Vec<Json>>, ApiError> {
    let resource = exchange.resource(model)?;
    let fields = exchange
        .headers
        .iter()
        .map(|name| resource.fields.get(name).ok_or(ApiError::Serialization))
        .collect::<Result<Vec<_>, _>>()?;
    let Filter {
        mut source,
        condition,
        values,
    } = filter;
    let offset = usize::from(!model.actions.is_empty());
    let order = query
        .get("o")
        .map(|order| {
            order
                .split('.')
                .filter_map(|value| {
                    let index = value.parse::<i32>().ok()?;
                    let column = model
                        .list_display
                        .get((index.unsigned_abs() as usize).checked_sub(offset)?)?;
                    Some(format!(
                        "{}{}",
                        if index < 0 { "-" } else { "" },
                        column.order_by.as_ref()?
                    ))
                })
                .collect::<Vec<_>>()
        })
        .filter(|order| !order.is_empty())
        .unwrap_or_else(|| model.ordering.clone());
    let ordering = listing::ordering_sql(&mut source, &order);
    let mut conn = admin.state.pool.acquire().await?;
    let mut columns = Vec::new();
    for (index, name) in exchange.headers.iter().enumerate() {
        let field = model.fields.get(name);
        let projection = if let Some([table, foreign, member]) =
            field.and_then(|field| field.through.as_ref())
        {
            let aggregate = if conn.backend_name() == "PostgreSQL" {
                "STRING_AGG"
            } else {
                "GROUP_CONCAT"
            };
            let debaters = field.and_then(|field| field.related.as_deref()) == Some("core.debater");
            let relation = if debaters {
                format!(
                    "JOIN core_debater d ON d.id=link.{member} AND NOT d.temporary AND NOT d.synthetic"
                )
            } else {
                String::new()
            };
            format!(
                "(SELECT {aggregate}(CAST(id AS TEXT),',') FROM (
                SELECT link.{member} AS id FROM {table} link {relation}
                WHERE link.{foreign}=t0.id ORDER BY link.{member}
            ) members)"
            )
        } else if let Some(column) = field.and_then(|field| field.column.as_ref()) {
            format!(r#"CAST(t0."{column}" AS TEXT)"#)
        } else {
            "NULL".to_owned()
        };
        columns.push(format!("{projection} AS column_{index}"));
    }
    let statement = format!(
        "SELECT {} FROM {}{condition} ORDER BY {ordering}",
        columns.join(","),
        source.sql()
    );
    let mut query = sqlx::query(AssertSqlSafe(statement));
    for value in values {
        query = query.bind(value);
    }
    let rows = query.fetch_all(&mut *conn).await?;
    rows.iter()
        .map(|row| {
            fields
                .iter()
                .enumerate()
                .map(|(index, field)| {
                    let value = row.try_get::<Option<String>, _>(index)?;
                    if field.through.is_some() {
                        return Ok(json!(value.unwrap_or_default()));
                    }
                    Ok(export_value(field, value))
                })
                .collect()
        })
        .collect()
}

pub(super) fn can_import(admin: &AdminRequest<'_>, model: &Model) -> bool {
    model.import_export
        && model
            .exchange
            .as_ref()
            .and_then(|exchange| exchange.resource(model).ok())
            .is_some_and(|resource| {
                [model, resource].iter().all(|model| {
                    admin.viewer.allows(model, "add") && admin.viewer.allows(model, "change")
                })
            })
}

pub(super) fn can_export(admin: &AdminRequest<'_>, model: &Model) -> bool {
    model.import_export
        && model
            .exchange
            .as_ref()
            .and_then(|exchange| exchange.resource(model).ok())
            .is_some_and(|resource| admin.viewer.read(model) && admin.viewer.read(resource))
}

pub(super) async fn export_page(
    admin: &AdminRequest<'_>,
    model: &Model,
    method: Method,
    body: Body,
) -> Result<Response, ApiError> {
    let Some(exchange) = &model.exchange else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    if !can_export(admin, model) {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let query = url::form_urlencoded::parse(admin.uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect();
    let sqlite = admin.state.pool.connect_options().database_url.scheme() == "sqlite";
    let Some(filter) = Filter::new(model, &query, sqlite) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let mut error = None;
    if method == Method::POST {
        let values = match login::parse_form(admin.headers, body, 128 * 1024).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let Some(origin) = admin.origin() else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            admin.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        let selected = values.get("file_format").map_or("", String::as_str);
        if let Some(format) = selected
            .parse::<usize>()
            .ok()
            .and_then(|index| exchange.export_formats.get(index))
        {
            let rows = export_rows(admin, model, exchange, &query, filter).await?;
            let extension = format.clone();
            let headers = exchange.headers.clone();
            let written =
                tokio::task::spawn_blocking(move || formats::write(&extension, &headers, &rows))
                    .await
                    .map_err(|_| ApiError::Serialization)?;
            match written {
                Ok(data) => {
                    let attachment = format!(
                        "attachment; filename=\"{}-{}.{format}\"",
                        model.object_name,
                        chrono::Utc::now().date_naive()
                    );
                    return Ok((
                        [(header::CONTENT_TYPE, formats::content_type(format))],
                        [(header::CONTENT_DISPOSITION, attachment)],
                        data,
                    )
                        .into_response());
                }
                Err(problem) => error = Some(problem),
            }
        } else {
            error = Some(
                if selected.is_empty() {
                    "This field is required."
                } else {
                    "Select a valid choice. That choice is not one of the available choices."
                }
                .to_owned(),
            );
        }
    }
    let base = common(model, exchange, false);
    admin.view("site_admin/exchange.html", context! {error,..base})
}

pub(super) use import::{confirm, preview};
