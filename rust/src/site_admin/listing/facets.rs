use std::collections::HashMap;

use sqlx::{AssertSqlSafe, Row};

use super::{AdminRequest, Filter, Parameters, add_values, filters::predicate};
use crate::ApiError;
use crate::site_admin::schema::{Field, Model};

type Counts = HashMap<Option<String>, i64>;

fn sqlite(admin: &AdminRequest<'_>) -> bool {
    admin.state.pool.connect_options().database_url.scheme() == "sqlite"
}

pub(super) fn label(base: &str, counts: &Option<Counts>, value: Option<&str>) -> String {
    match counts {
        Some(counts) => {
            let count = counts.get(&value.map(str::to_owned)).copied().unwrap_or(0);
            format!("{base} ({count})")
        }
        None => base.to_owned(),
    }
}

pub(super) async fn grouped(
    admin: &AdminRequest<'_>,
    model: &Model,
    path: &str,
    params: &Parameters,
) -> Result<Option<Counts>, ApiError> {
    if !params.contains_key("_facets") {
        return Ok(None);
    }
    let Filter {
        mut source,
        condition,
        mut values,
    } = Filter::new(model, params, sqlite(admin)).ok_or(ApiError::Serialization)?;
    let mut join = String::new();
    let value = if path == "profile_visibility" && model.slug() == "teamresult" {
        "CASE WHEN EXISTS (SELECT 1 FROM core_teamresult_hidden_from_debaters hidden
            WHERE hidden.teamresult_id=t0.id) THEN 'hidden' ELSE 'visible' END"
            .to_owned()
    } else if let Some(field) = model.fields.get(path)
        && let Some([table, owner, target]) = field.through.as_ref()
    {
        join = format!(" LEFT JOIN {table} facet ON facet.{owner}=t0.id");
        if field.generic_through.unwrap_or_default() {
            let app = add_values(&mut values, model.app());
            let model = add_values(&mut values, model.slug());
            join.push_str(&format!(
                " AND facet.content_type_id=(SELECT id FROM django_content_type WHERE app_label={app} AND model={model})"
            ));
        }
        format!("CAST(facet.{target} AS TEXT)")
    } else {
        let (column, field) = source.resolve(path).ok_or(ApiError::Serialization)?;
        if field.kind == "BooleanField" {
            format!("CASE WHEN {column} THEN '1' WHEN {column} IS NULL THEN NULL ELSE '0' END")
        } else {
            format!("CAST({column} AS TEXT)")
        }
    };
    let sql = format!(
        "SELECT {value}, COUNT(DISTINCT t0.id) FROM {}{join}{condition} GROUP BY 1",
        source.sql()
    );
    let mut query = sqlx::query_as::<_, (Option<String>, i64)>(AssertSqlSafe(sql));
    for value in values {
        query = query.bind(value);
    }
    Ok(Some(
        query
            .fetch_all(&admin.state.pool)
            .await?
            .into_iter()
            .collect(),
    ))
}

pub(super) async fn date_ranges(
    admin: &AdminRequest<'_>,
    model: &Model,
    path: &str,
    field: &Field,
    params: &Parameters,
    choices: &[(&str, Parameters)],
) -> Result<Option<Vec<i64>>, ApiError> {
    if !params.contains_key("_facets") {
        return Ok(None);
    }
    let sqlite = sqlite(admin);
    let Filter {
        mut source,
        condition,
        mut values,
    } = Filter::new(model, params, sqlite).ok_or(ApiError::Serialization)?;
    let (column, _) = source.resolve(path).ok_or(ApiError::Serialization)?;
    let mut projection = Vec::new();
    for (_, options) in choices {
        let predicates = options
            .iter()
            .map(|(key, value)| {
                let (_, lookup) = key.rsplit_once("__")?;
                predicate(&column, field, lookup, value, sqlite, &mut values)
            })
            .collect::<Option<Vec<_>>>()
            .ok_or(ApiError::Serialization)?;
        projection.push(if predicates.is_empty() {
            "COUNT(DISTINCT t0.id)".to_owned()
        } else {
            format!(
                "COUNT(DISTINCT CASE WHEN {} THEN t0.id END)",
                predicates.join(" AND ")
            )
        });
    }
    let sql = format!(
        "SELECT {} FROM {}{condition}",
        projection.join(","),
        source.sql()
    );
    let mut query = sqlx::query(AssertSqlSafe(sql));
    for value in values {
        query = query.bind(value);
    }
    let row = query.fetch_one(&admin.state.pool).await?;
    Ok(Some(
        (0..choices.len())
            .map(|index| row.try_get(index))
            .collect::<Result<Vec<_>, _>>()?,
    ))
}
