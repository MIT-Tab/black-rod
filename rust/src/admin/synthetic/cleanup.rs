use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    sync::LazyLock,
};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use sqlx::{AnyConnection, AssertSqlSafe, Row};

use crate::{AppState, admin::amendment, login, web};

const CLEANUP: &str = "/core/admin/synthetic-cleanup/";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum Entity {
    Team,
    Debater,
    School,
}

impl Entity {
    const DISPLAY: [(Self, &'static str); 3] = [
        (Self::School, "Schools"),
        (Self::Debater, "Debaters"),
        (Self::Team, "Teams"),
    ];

    fn parse(value: &str) -> Option<Self> {
        match value {
            "school" => Some(Self::School),
            "debater" => Some(Self::Debater),
            "team" => Some(Self::Team),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::School => "school",
            Self::Debater => "debater",
            Self::Team => "team",
        }
    }

    fn table(self) -> &'static str {
        match self {
            Self::School => "core_school",
            Self::Debater => "core_debater",
            Self::Team => "core_team",
        }
    }
}

type Key = (Entity, i64);
type References = BTreeMap<&'static str, usize>;

fn token((entity, id): Key) -> String {
    format!("{}:{id}", entity.name())
}

fn selection(value: &str) -> Option<Key> {
    let (entity, id) = value.trim().split_once(':')?;
    Some((
        Entity::parse(&entity.trim().to_ascii_lowercase())?,
        id.trim().parse().ok()?,
    ))
}

struct Relation {
    entity: Entity,
    label: &'static str,
    table: &'static str,
    current: &'static str,
    reference: &'static str,
    peer: Option<Entity>,
    outbound: bool,
}

static RELATIONS: LazyLock<Vec<Relation>> = LazyLock::new(|| {
    include_str!("../../../schema/synthetic_cleanup.tsv")
        .lines()
        .map(|line| {
            let values = line.split('\t').collect::<Vec<_>>();
            let [entity, label, table, current, reference, peer, direction] = values.as_slice()
            else {
                unreachable!("invalid synthetic cleanup schema");
            };
            Relation {
                entity: Entity::parse(entity).expect("synthetic entity"),
                label,
                table,
                current,
                reference,
                peer: Entity::parse(peer),
                outbound: *direction == "outbound",
            }
        })
        .collect()
});

#[derive(Clone, Serialize)]
struct Record {
    entity_type: &'static str,
    object_id: i64,
    display_name: String,
    secondary_text: String,
    selection_token: String,
}

#[derive(Default)]
struct Node {
    inbound: Vec<(&'static str, Key)>,
    neighbors: Vec<Key>,
    external: References,
    isolation: References,
}

impl Node {
    fn unreferenced(&self) -> bool {
        self.inbound.is_empty() && self.external.is_empty()
    }

    fn component_clear(&self) -> bool {
        self.external.is_empty() && self.isolation.is_empty()
    }
}

#[derive(Default)]
struct Analysis {
    records: BTreeMap<Key, Record>,
    nodes: HashMap<Key, Node>,
    component: HashMap<Key, usize>,
    blockers: Vec<References>,
}

impl Analysis {
    fn group(&mut self) {
        for &key in self.nodes.keys() {
            if self.component.contains_key(&key) {
                continue;
            }
            let group = self.blockers.len();
            let mut counts = References::new();
            let mut pending = vec![key];
            while let Some(key) = pending.pop() {
                let std::collections::hash_map::Entry::Vacant(entry) = self.component.entry(key)
                else {
                    continue;
                };
                entry.insert(group);
                let node = &self.nodes[&key];
                pending.extend(
                    node.neighbors
                        .iter()
                        .copied()
                        .filter(|peer| !self.component.contains_key(peer)),
                );
                if !node.component_clear() {
                    for (&label, &count) in node.external.iter().chain(&node.isolation) {
                        *counts.entry(label).or_default() += count;
                    }
                }
            }
            self.blockers.push(counts);
        }
    }

    fn blockers(&self, key: Key, ignored: &HashSet<String>) -> References {
        let node = &self.nodes[&key];
        if node.unreferenced() {
            return References::new();
        }
        let mut counts = node.external.clone();
        for (&label, &count) in &node.isolation {
            *counts.entry(label).or_default() += count;
        }
        for &(label, peer) in &node.inbound {
            if !ignored.contains(&token(peer)) {
                *counts.entry(label).or_default() += 1;
            }
        }
        if counts.is_empty() {
            self.blockers[self.component[&key]].clone()
        } else {
            counts
        }
    }
}

async fn analyze(conn: &mut AnyConnection) -> Result<Analysis, sqlx::Error> {
    let mut data = Analysis::default();
    let records = [
        (Entity::School, "SELECT CAST(id AS BIGINT) AS id, name || CASE WHEN temporary THEN ' (New)' ELSE '' END AS display,
            CASE WHEN TRIM(COALESCE(short_name,''))='' THEN 'No short name' ELSE 'Short name: ' || TRIM(short_name) END AS detail
            FROM core_school WHERE synthetic"),
        (Entity::Debater, "SELECT CAST(d.id AS BIGINT) AS id,
            TRIM(d.first_name || ' ' || d.last_name) || CASE WHEN d.temporary THEN ' (New)' ELSE '' END AS display,
            'School: ' || COALESCE(s.name,'Unaffiliated') AS detail
            FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id WHERE d.synthetic"),
        (Entity::Team, "SELECT CAST(id AS BIGINT) AS id, name AS display,
            'Short name: ' || COALESCE(NULLIF(TRIM(short_name),''),'None') AS detail FROM core_team WHERE synthetic"),
    ];
    for (kind, query) in records {
        for row in sqlx::query(query).fetch_all(&mut *conn).await? {
            let id = row.try_get("id")?;
            data.nodes.insert((kind, id), Node::default());
            data.records.insert(
                (kind, id),
                Record {
                    entity_type: kind.name(),
                    object_id: id,
                    display_name: row.try_get("display")?,
                    secondary_text: row.try_get("detail")?,
                    selection_token: token((kind, id)),
                },
            );
        }
    }
    for relation in RELATIONS.iter().filter(|relation| {
        data.records
            .keys()
            .any(|(kind, _)| *kind == relation.entity)
    }) {
        let Relation {
            entity,
            label,
            table,
            current,
            reference,
            peer,
            outbound,
        } = *relation;
        let entities = entity.table();
        let query = format!("SELECT CAST(r.{current} AS BIGINT),CAST(r.{reference} AS BIGINT)
            FROM {table} r JOIN {entities} n ON n.id=r.{current} WHERE n.synthetic AND r.{reference} IS NOT NULL");
        let edges = sqlx::query_as::<_, (i64, i64)>(AssertSqlSafe(query))
            .fetch_all(&mut *conn)
            .await?;
        for (id, other) in edges {
            let key = (entity, id);
            let internal = peer
                .map(|kind| (kind, other))
                .filter(|other| data.nodes.contains_key(other));
            let node = data.nodes.get_mut(&key).expect("existing synthetic record");
            match (outbound, internal) {
                (false, Some(internal)) => {
                    node.inbound.push((label, internal));
                    node.neighbors.push(internal);
                    data.nodes
                        .get_mut(&internal)
                        .expect("existing synthetic peer")
                        .neighbors
                        .push(key);
                }
                (false, None) => *node.external.entry(label).or_default() += 1,
                (true, None) => *node.isolation.entry(label).or_default() += 1,
                (true, Some(_)) => {}
            }
        }
    }
    data.group();
    Ok(data)
}

async fn submit(state: &AppState, request: Request) -> Result<Response, amendment::Error> {
    let (_, headers, body) = match super::form(state, request).await {
        Ok(form) => form,
        Err(response) => return Ok(*response),
    };
    let selections = body
        .iter()
        .filter(|(key, _)| key == "selected_ids")
        .map(|(_, value)| value.clone())
        .collect::<Vec<_>>();
    if selections.is_empty() {
        return Ok(super::notices(
            state,
            &headers,
            CLEANUP,
            &[(
                20,
                "Select at least one synthetic entity to delete.".to_owned(),
            )],
        ));
    }

    let mut tx = state.pool.begin().await?;
    if tx.backend_name() == "SQLite" {
        sqlx::query("UPDATE core_school SET synthetic=synthetic WHERE FALSE")
            .execute(&mut *tx)
            .await?;
    } else {
        sqlx::query("SET LOCAL lock_timeout='10s'")
            .execute(&mut *tx)
            .await?;
        let tables = RELATIONS
            .iter()
            .map(|relation| relation.table)
            .chain(Entity::DISPLAY.map(|(kind, _)| kind.table()))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>()
            .join(", ");
        sqlx::query(AssertSqlSafe(format!(
            "LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE"
        )))
        .execute(&mut *tx)
        .await?;
    }
    let analysis = analyze(&mut tx).await?;
    let ignored = selections.iter().cloned().collect::<HashSet<_>>();
    let mut selected = BTreeSet::new();
    let mut skipped = Vec::new();
    for value in selections {
        let Some(key) = selection(&value).filter(|key| analysis.records.contains_key(key)) else {
            skipped.push(format!("{value} (missing)"));
            continue;
        };
        let blocked = analysis.blockers(key, &ignored);
        if blocked.is_empty() {
            selected.insert(key);
        } else {
            let labels = blocked
                .into_iter()
                .map(|(label, count)| format!("{label} ({count})"))
                .collect::<Vec<_>>();
            skipped.push(format!("{} ({})", token(key), labels.join(", ")));
        }
    }
    let count = selected.len();
    for (kind, id) in selected {
        amendment::erase(&mut tx, kind.table(), id).await?;
    }
    tx.commit().await?;
    let mut messages = Vec::new();
    if count > 0 {
        let suffix = if count == 1 { "y" } else { "ies" };
        messages.push((
            25,
            format!("Deleted {count} synthetic cleanup entit{suffix}."),
        ));
        amendment::invalidate(state).await;
    }
    if !skipped.is_empty() {
        let suffix = if skipped.len() == 1 { "" } else { "s" };
        messages.push((
            30,
            format!(
                "Skipped {} selection{suffix} because they were missing or are now referenced: {}",
                skipped.len(),
                skipped
                    .iter()
                    .take(5)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("; ")
            ),
        ));
    }
    Ok(super::notices(state, &headers, CLEANUP, &messages))
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    let user = match crate::admin::require(&state, request.uri(), request.headers()).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    if request.method() == Method::POST {
        return match submit(&state, request).await {
            Ok(response) => response,
            Err(error) => {
                eprintln!("Synthetic cleanup failed: {}", error.message());
                StatusCode::INTERNAL_SERVER_ERROR.into_response()
            }
        };
    }
    let Ok(mut conn) = crate::team_profile::read_transaction(&state.pool).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let data = match analyze(&mut conn).await {
        Ok(data) => data,
        Err(error) => {
            eprintln!("Synthetic cleanup query failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let mut total = 0;
    let sections = Entity::DISPLAY.map(|(kind, label)| {
        let rows = data
            .records
            .iter()
            .rev()
            .filter(|&(key, _)| {
                key.0 == kind
                    && (data.nodes[key].unreferenced()
                        || data.blockers[data.component[key]].is_empty())
            })
            .map(|(_, row)| row)
            .collect::<Vec<_>>();
        let count = rows.len();
        total += count;
        context! {label, count, rows, entity_type=>kind.name()}
    });
    let Ok((csrf, token)) = login::csrf_tokens(request.headers()) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let secure = state
        .origin(request.headers())
        .is_some_and(|url| url.starts_with("https://"));
    let base = web::base_context(&state, &user, request.uri());
    let mut response = match web::render(
        "admin/synthetic_cleanup.html",
        context! {
            sections, total_count=>total, csrf=>token, ..base
        },
    ) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Synthetic cleanup template failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    login::set_csrf_cookie(&mut response, &csrf, secure);
    response
}
