use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    sync::LazyLock,
};

use sqlx::{AnyConnection, AssertSqlSafe};

const SCHEMA: &str = include_str!("../schema/delete_relations.tsv");

#[derive(Clone, Copy, PartialEq)]
enum Rule {
    Cascade,
    SetNull,
    Protect,
}

struct Relation {
    table: &'static str,
    foreign_key: &'static str,
    rule: Rule,
    order: &'static str,
    generic: Option<(&'static str, &'static str, &'static str)>,
}

static RELATIONS: LazyLock<BTreeMap<&str, Vec<Relation>>> = LazyLock::new(|| {
    let mut relations: BTreeMap<&str, Vec<Relation>> = BTreeMap::new();
    for line in SCHEMA.lines() {
        let fields = line.split('\t').collect::<Vec<_>>();
        let [parent, table, foreign_key, action, generic, order] = fields.as_slice() else {
            unreachable!("Invalid model deletion schema")
        };
        let rule = match *action {
            "CASCADE" => Rule::Cascade,
            "SET_NULL" => Rule::SetNull,
            "PROTECT" => Rule::Protect,
            _ => unreachable!("Invalid model foreign-key rule"),
        };
        relations.entry(parent).or_default().push(Relation {
            table,
            foreign_key,
            rule,
            order,
            generic: generic.split_once(':').and_then(|(column, label)| {
                let (app, model) = label.split_once('.')?;
                Some((column, app, model))
            }),
        });
    }
    relations
});

#[derive(Debug)]
pub(crate) enum DeleteError {
    Absent,
    Protected,
    Database(sqlx::Error),
}

impl From<sqlx::Error> for DeleteError {
    fn from(error: sqlx::Error) -> Self {
        Self::Database(error)
    }
}

fn placeholders(len: usize) -> String {
    (1..=len)
        .map(|index| format!("${index}"))
        .collect::<Vec<_>>()
        .join(",")
}

async fn children(
    conn: &mut AnyConnection,
    edge: &Relation,
    ids: &[i64],
    preview: bool,
) -> Result<Vec<(i64, i64)>, sqlx::Error> {
    let mut selected = Vec::new();
    let lock = !preview && edge.rule != Rule::Protect && conn.backend_name() == "PostgreSQL";
    for chunk in ids.chunks(200) {
        let table = edge.table;
        let field = edge.foreign_key;
        let marks = placeholders(chunk.len());
        let content = edge.generic.map_or_else(String::new, |(column, app, model)| format!(
            " AND {column}=(SELECT id FROM django_content_type WHERE app_label='{app}' AND model='{model}')"
        ));
        let order = if lock {
            // Recheck the relationship after waiting on a concurrent child update.
            // Keep the child stable until the delete and any audit snapshots finish.
            " ORDER BY id FOR UPDATE".to_owned()
        } else if edge.table == "taggit_taggeditem" && edge.generic.is_some() {
            " ORDER BY object_id,tag_id".to_owned()
        } else if edge.order == "-" {
            String::new()
        } else {
            format!(" ORDER BY {}", edge.order)
        };
        let sql = format!(
            "SELECT CAST({field} AS BIGINT),CAST(id AS BIGINT) FROM {table}
            WHERE {field} IN ({marks}){content}{order}"
        );
        let mut query = sqlx::query_as::<_, (i64, i64)>(AssertSqlSafe(sql));
        for id in chunk {
            query = query.bind(id);
        }
        selected.extend(query.fetch_all(&mut *conn).await?);
    }
    Ok(selected)
}

fn delete_order(
    table: &'static str,
    seen: &mut HashSet<&'static str>,
    sorted: &mut Vec<&'static str>,
) {
    if !seen.insert(table) {
        return;
    }
    for edge in RELATIONS.get(table).into_iter().flatten() {
        if edge.rule == Rule::Cascade {
            delete_order(edge.table, seen, sorted);
        }
    }
    sorted.push(table);
}

pub(crate) type Record = (&'static str, i64);

#[derive(Default)]
pub(crate) struct DeleteDisplay {
    pub(crate) edges: BTreeMap<Option<Record>, Vec<Record>>,
    pub(crate) tables: Vec<&'static str>,
    pub(crate) protected: BTreeSet<Record>,
}

impl DeleteDisplay {
    fn link(&mut self, source: Option<Record>, table: &'static str, target: i64) {
        self.edges.entry(source).or_default().push((table, target));
        if !self.tables.contains(&table) {
            self.tables.push(table);
        }
    }
}

pub(crate) struct Deletion {
    root: &'static str,
    selected: BTreeMap<&'static str, BTreeSet<i64>>,
    nulls: BTreeMap<(&'static str, &'static str), BTreeSet<i64>>,
    pub(crate) display: DeleteDisplay,
}

pub(crate) async fn plan(
    conn: &mut AnyConnection,
    root: &'static str,
    id: i64,
) -> Result<Deletion, DeleteError> {
    plan_many(conn, root, &[id]).await
}

async fn plan_many(
    conn: &mut AnyConnection,
    root: &'static str,
    ids: &[i64],
) -> Result<Deletion, DeleteError> {
    lock_records(conn, root, ids).await?;
    collect(conn, root, ids, false).await
}

pub(crate) async fn lock_records(
    conn: &mut AnyConnection,
    root: &'static str,
    ids: &[i64],
) -> Result<(), DeleteError> {
    let postgres = conn.backend_name() == "PostgreSQL";
    for chunk in ids.chunks(200) {
        let marks = placeholders(chunk.len());
        let lock = if postgres {
            format!(
                "SELECT CAST(id AS BIGINT) FROM {root} WHERE id IN ({marks}) ORDER BY id FOR UPDATE"
            )
        } else {
            format!("UPDATE {root} SET id=id WHERE id IN ({marks})")
        };
        let mut query = sqlx::query(AssertSqlSafe(lock));
        for id in chunk {
            query = query.bind(id);
        }
        let locked = if postgres {
            query.fetch_all(&mut *conn).await?.len() as u64
        } else {
            query.execute(&mut *conn).await?.rows_affected()
        };
        if locked != chunk.len() as u64 {
            return Err(DeleteError::Absent);
        }
    }
    Ok(())
}

pub(crate) async fn preview(
    conn: &mut AnyConnection,
    root: &'static str,
    id: i64,
) -> Result<Deletion, DeleteError> {
    let sql = format!("SELECT CAST(id AS BIGINT) FROM {root} WHERE id=$1");
    let exists = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?;
    if exists.is_none() {
        return Err(DeleteError::Absent);
    }
    collect(conn, root, &[id], true).await
}

async fn collect(
    conn: &mut AnyConnection,
    root: &'static str,
    ids: &[i64],
    preview: bool,
) -> Result<Deletion, DeleteError> {
    let mut deletion = Deletion {
        root,
        selected: BTreeMap::from([(root, ids.iter().copied().collect())]),
        nulls: BTreeMap::new(),
        display: DeleteDisplay::default(),
    };
    if preview {
        for id in ids {
            deletion.display.link(None, root, *id);
        }
    }
    collect_edges(conn, &mut deletion, root, ids, preview).await?;
    Ok(deletion)
}

async fn collect_edges(
    conn: &mut AnyConnection,
    deletion: &mut Deletion,
    table: &'static str,
    parents: &[i64],
    preview: bool,
) -> Result<(), DeleteError> {
    let mut protected = false;
    for edge in RELATIONS.get(table).into_iter().flatten() {
        if protected && edge.generic.is_some() {
            // Django stops before visiting generic relations on a protected model.
            break;
        }
        let rows = children(conn, edge, parents, preview).await?;
        if rows.is_empty() {
            continue;
        }
        match edge.rule {
            Rule::Protect if preview => {
                protected = true;
                deletion
                    .display
                    .protected
                    .extend(rows.into_iter().map(|(_, id)| (edge.table, id)));
            }
            Rule::Protect => return Err(DeleteError::Protected),
            Rule::SetNull => deletion
                .nulls
                .entry((edge.table, edge.foreign_key))
                .or_default()
                .extend(rows.into_iter().map(|(_, id)| id)),
            Rule::Cascade => {
                let unique = deletion.selected.entry(edge.table).or_default();
                let mut added = Vec::new();
                for (source, id) in rows {
                    if preview {
                        let parent = edge.generic.is_none().then_some((table, source));
                        deletion.display.link(parent, edge.table, id);
                    }
                    if unique.insert(id) {
                        added.push(id);
                    }
                }
                if !added.is_empty() {
                    Box::pin(collect_edges(conn, deletion, edge.table, &added, preview)).await?;
                }
            }
        }
    }
    Ok(())
}

impl Deletion {
    pub(crate) fn removed(&self) -> impl Iterator<Item = (&'static str, i64)> + '_ {
        self.selected
            .iter()
            .flat_map(|(&table, rows)| rows.iter().map(move |&id| (table, id)))
    }

    pub(crate) fn affected(&self) -> impl Iterator<Item = (&'static str, i64)> + '_ {
        self.selected
            .iter()
            .map(|(&table, rows)| (table, rows))
            .chain(self.nulls.iter().map(|(&(table, _), rows)| (table, rows)))
            .flat_map(|(table, rows)| rows.iter().map(move |&id| (table, id)))
    }

    pub(crate) async fn apply(self, conn: &mut AnyConnection) -> Result<(), DeleteError> {
        let Self {
            root,
            mut selected,
            nulls,
            display: _,
        } = self;
        for ((table, field), rows) in nulls {
            let ids = rows
                .into_iter()
                .filter(|id| {
                    selected
                        .get(table)
                        .is_none_or(|records| !records.contains(id))
                })
                .collect::<Vec<_>>();
            for chunk in ids.chunks(200) {
                let marks = placeholders(chunk.len());
                let sql = format!("UPDATE {table} SET {field}=NULL WHERE id IN ({marks})");
                let mut query = sqlx::query(AssertSqlSafe(sql));
                for id in chunk {
                    query = query.bind(id);
                }
                query.execute(&mut *conn).await?;
            }
        }
        let mut ordered = Vec::new();
        delete_order(root, &mut HashSet::new(), &mut ordered);
        for table in ordered {
            let Some(ids) = selected.remove(table) else {
                continue;
            };
            let ids = ids.into_iter().collect::<Vec<_>>();
            for chunk in ids.chunks(200) {
                let marks = placeholders(chunk.len());
                let sql = format!("DELETE FROM {table} WHERE id IN ({marks})");
                let mut query = sqlx::query(AssertSqlSafe(sql));
                for id in chunk {
                    query = query.bind(id);
                }
                query.execute(&mut *conn).await?;
            }
        }
        Ok(())
    }
}

/// Apply the model deletion rules inside the caller's transaction.
pub(crate) async fn delete(
    conn: &mut AnyConnection,
    root: &'static str,
    id: i64,
) -> Result<(), DeleteError> {
    plan(conn, root, id).await?.apply(conn).await
}

/// Apply model deletion rules after the caller locks and rechecks the root record.
pub(crate) async fn delete_locked(
    conn: &mut AnyConnection,
    root: &'static str,
    id: i64,
) -> Result<(), DeleteError> {
    collect(conn, root, &[id], false).await?.apply(conn).await
}

/// Apply the same rules to unique roots in batches inside the caller's transaction.
pub(crate) async fn delete_many(
    conn: &mut AnyConnection,
    root: &'static str,
    ids: &[i64],
) -> Result<(), DeleteError> {
    if ids.is_empty() {
        return Ok(());
    }
    plan_many(conn, root, ids).await?.apply(conn).await
}
