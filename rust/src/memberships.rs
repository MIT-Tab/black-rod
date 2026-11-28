use std::collections::BTreeSet;

use sqlx::{AnyConnection, AssertSqlSafe};

use crate::{ApiError, agent::journal::storage_error};

pub(crate) async fn sync_memberships(
    conn: &mut AnyConnection,
    [table, current, target]: [&str; 3],
    record: i64,
    new: &BTreeSet<i64>,
    related_debaters: bool,
) -> Result<(), ApiError> {
    let statement = format!("SELECT CAST({target} AS BIGINT) FROM {table} WHERE {current}=$1");
    let existing = sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
        .bind(record)
        .fetch_all(&mut *conn)
        .await?
        .into_iter()
        .collect::<BTreeSet<_>>();
    // A Django ManyToMany set() sees only active debaters on the removal side.
    let scope = if related_debaters {
        " AND EXISTS (SELECT 1 FROM core_debater d WHERE d.id=$2 AND NOT d.temporary AND NOT d.synthetic)"
    } else {
        ""
    };
    let delete = format!("DELETE FROM {table} WHERE {current}=$1 AND {target}=$2{scope}");
    for related in existing.difference(new) {
        sqlx::query(AssertSqlSafe(delete.as_str()))
            .bind(record)
            .bind(related)
            .execute(&mut *conn)
            .await?;
    }
    let insert = format!("INSERT INTO {table} ({current},{target}) VALUES ($1,$2)");
    for related in new.difference(&existing) {
        sqlx::query(AssertSqlSafe(insert.clone()))
            .bind(record)
            .bind(related)
            .execute(&mut *conn)
            .await
            .map_err(storage_error)?;
    }
    Ok(())
}
