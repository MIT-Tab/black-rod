use std::{
    collections::{BTreeMap, BTreeSet, HashSet, VecDeque},
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
}

static RELATIONS: LazyLock<BTreeMap<&str, Vec<Relation>>> = LazyLock::new(|| {
    let mut relations: BTreeMap<&str, Vec<Relation>> = BTreeMap::new();
    for line in SCHEMA.lines() {
        let fields = line.split('\t').collect::<Vec<_>>();
        let [parent, table, foreign_key, action] = fields.as_slice() else {
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
) -> Result<Vec<i64>, sqlx::Error> {
    let mut selected = Vec::new();
    for chunk in ids.chunks(200) {
        let table = edge.table;
        let field = edge.foreign_key;
        let marks = placeholders(chunk.len());
        let sql = format!("SELECT CAST(id AS BIGINT) FROM {table} WHERE {field} IN ({marks})");
        let mut query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql));
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

async fn delete_tags(
    conn: &mut AnyConnection,
    table: &'static str,
    ids: &[i64],
) -> Result<(), sqlx::Error> {
    let (tagging, model) = match table {
        "core_video" => ("taggit_taggeditem", "video"),
        "core_resource" => ("core_taggedresource", "resource"),
        _ => return Ok(()),
    };
    for chunk in ids.chunks(200) {
        let marks = placeholders(chunk.len());
        let sql = format!(
            "DELETE FROM {tagging} WHERE object_id IN ({marks}) AND content_type_id IN
            (SELECT id FROM django_content_type WHERE app_label='core' AND model='{model}')"
        );
        let mut query = sqlx::query(AssertSqlSafe(sql));
        for id in chunk {
            query = query.bind(id);
        }
        query.execute(&mut *conn).await?;
    }
    Ok(())
}

pub(crate) struct Deletion {
    root: &'static str,
    selected: BTreeMap<&'static str, BTreeSet<i64>>,
    nulls: BTreeMap<(&'static str, &'static str), BTreeSet<i64>>,
}

pub(crate) async fn plan(
    conn: &mut AnyConnection,
    root: &'static str,
    id: i64,
) -> Result<Deletion, DeleteError> {
    let lock = format!("UPDATE {root} SET id=id WHERE id=$1");
    let selected = sqlx::query(AssertSqlSafe(lock))
        .bind(id)
        .execute(&mut *conn)
        .await?;
    if selected.rows_affected() == 0 {
        return Err(DeleteError::Absent);
    }
    let mut selected: BTreeMap<&str, BTreeSet<i64>> =
        BTreeMap::from([(root, BTreeSet::from([id]))]);
    let mut todo = VecDeque::from([(root, vec![id])]);
    let mut nulls: BTreeMap<(&str, &str), BTreeSet<i64>> = BTreeMap::new();
    while let Some((table, parents)) = todo.pop_front() {
        for edge in RELATIONS.get(table).into_iter().flatten() {
            let rows = children(conn, edge, &parents).await?;
            if rows.is_empty() {
                continue;
            }
            match edge.rule {
                Rule::Protect => return Err(DeleteError::Protected),
                Rule::SetNull => nulls
                    .entry((edge.table, edge.foreign_key))
                    .or_default()
                    .extend(rows),
                Rule::Cascade => {
                    let unique = selected.entry(edge.table).or_default();
                    let added = rows
                        .into_iter()
                        .filter(|&id| unique.insert(id))
                        .collect::<Vec<_>>();
                    if !added.is_empty() {
                        todo.push_back((edge.table, added));
                    }
                }
            }
        }
    }
    Ok(Deletion {
        root,
        selected,
        nulls,
    })
}

impl Deletion {
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
            delete_tags(conn, table, &ids).await?;
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
