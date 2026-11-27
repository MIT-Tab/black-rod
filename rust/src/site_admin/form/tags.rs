use std::collections::BTreeSet;

use sqlx::{AnyConnection, AssertSqlSafe};

use crate::{ApiError, autocomplete, tag_filter};

use super::{AdminRequest, Field, Model, related_model};

pub(super) async fn display(
    admin: &AdminRequest<'_>,
    model: &Model,
    field: &Field,
    id: i64,
) -> Result<String, ApiError> {
    let (Some([through, owner, target]), Some(tags)) = (&field.through, related_model(field))
    else {
        return Ok(String::new());
    };
    let generic = field.generic_through.unwrap_or_default();
    let content = if generic {
        " AND link.content_type_id=(SELECT id FROM django_content_type WHERE app_label=$2 AND model=$3)"
    } else {
        ""
    };
    let sql = format!(
        "SELECT tag.name FROM {} tag JOIN {through} link ON tag.id=link.{target}
         WHERE link.{owner}=$1{content}",
        tags.table
    );
    let mut query = sqlx::query_scalar::<_, String>(AssertSqlSafe(sql)).bind(id);
    if generic {
        query = query.bind(model.app()).bind(model.slug());
    }
    let mut names = query
        .fetch_all(&admin.state.pool)
        .await?
        .into_iter()
        .map(|name| {
            if name.contains([',', ' ']) {
                format!("\"{name}\"")
            } else {
                name
            }
        })
        .collect::<Vec<_>>();
    names.sort_unstable();
    Ok(names.join(", "))
}

pub(super) async fn save(
    conn: &mut AnyConnection,
    model: &Model,
    field: &Field,
    id: i64,
    input: &str,
) -> Result<(), ApiError> {
    let (Some([through, owner, target]), Some(tags)) = (&field.through, related_model(field))
    else {
        return Ok(());
    };
    let content = if field.generic_through.unwrap_or_default() {
        Some(sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM django_content_type WHERE app_label=$1 AND model=$2",
        ).bind(model.app()).bind(model.slug()).fetch_one(&mut *conn).await?)
    } else {
        None
    };
    let extra = if content.is_some() {
        " AND content_type_id=$2"
    } else {
        ""
    };
    let query = format!("SELECT CAST({target} AS BIGINT) FROM {through} WHERE {owner}=$1{extra}");
    let mut current = sqlx::query_scalar::<_, i64>(AssertSqlSafe(query)).bind(id);
    if let Some(content) = content {
        current = current.bind(content);
    }
    let current = current
        .fetch_all(&mut *conn)
        .await?
        .into_iter()
        .collect::<BTreeSet<_>>();
    let mut selected = BTreeSet::new();
    for name in tag_filter::parse([input]) {
        selected.insert(autocomplete::insert_tag(conn, &tags.table, &name).await?);
    }
    let remove = format!(
        "DELETE FROM {through} WHERE {owner}=$1{extra} AND {target}=${}",
        if content.is_some() { 3 } else { 2 }
    );
    let insert = if content.is_some() {
        format!(
            "INSERT INTO {through} ({owner},{target},content_type_id) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING"
        )
    } else {
        format!("INSERT INTO {through} ({owner},{target}) VALUES ($1,$2) ON CONFLICT DO NOTHING")
    };
    for tag in current.difference(&selected) {
        let mut query = sqlx::query(AssertSqlSafe(remove.as_str())).bind(id);
        if let Some(content) = content {
            query = query.bind(content);
        }
        query.bind(tag).execute(&mut *conn).await?;
    }
    for tag in selected.difference(&current) {
        let mut query = sqlx::query(AssertSqlSafe(insert.as_str()))
            .bind(id)
            .bind(tag);
        if let Some(content) = content {
            query = query.bind(content);
        }
        query.execute(&mut *conn).await?;
    }
    Ok(())
}
