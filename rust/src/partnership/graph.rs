use std::{
    cmp::Reverse,
    collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap, HashSet, VecDeque},
};

use serde::Serialize;
use sqlx::{AnyConnection, Row};

use crate::ApiError;

type Vertex = i64;
pub(super) type Graph = BTreeMap<Vertex, BTreeSet<Vertex>>;

const PEOPLE: &str =
    "SELECT CAST(d.id AS BIGINT) AS id, CAST(d.alias_group_id AS BIGINT) AS alias_id,
    TRIM(d.first_name || ' ' || d.last_name) AS name, CAST(d.school_id AS BIGINT) AS school_id,
    s.name AS school, d.first_season,d.latest_season
    FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
    WHERE NOT d.synthetic AND NOT d.temporary ORDER BY d.id";
const ROSTERS: &str =
    "SELECT CAST(m.team_id AS BIGINT) AS team_id, CAST(m.debater_id AS BIGINT) AS debater_id
    FROM core_team_debaters m JOIN core_team t ON t.id=m.team_id
    WHERE NOT t.synthetic AND NOT EXISTS (SELECT 1 FROM core_teamresult r
        JOIN core_teamresult_hidden_from_debaters h ON h.teamresult_id=r.id WHERE r.team_id=t.id)
    ORDER BY m.team_id,m.debater_id";

#[derive(Serialize)]
pub(super) struct Person {
    pub(super) id: i64,
    pub(super) name: String,
    pub(super) school_id: Option<i64>,
    pub(super) school: Option<String>,
    pub(super) first_season: Option<String>,
    pub(super) latest_season: Option<String>,
}

pub(super) struct People {
    pub(super) profiles: BTreeMap<i64, Person>,
    pub(super) canonical: HashMap<i64, i64>,
    pub(super) groups: BTreeMap<i64, Vec<i64>>,
}

impl People {
    pub(super) async fn load(conn: &mut AnyConnection) -> Result<Self, ApiError> {
        let mut profiles = BTreeMap::new();
        let mut canonical = HashMap::new();
        let mut groups: BTreeMap<i64, Vec<i64>> = BTreeMap::new();
        let mut aliases = HashMap::new();
        for row in sqlx::query(PEOPLE).fetch_all(conn).await? {
            let id: i64 = row.try_get("id")?;
            let alias: Option<i64> = row.try_get("alias_id")?;
            let canonical_id = alias
                .filter(|alias| *alias != 0)
                .map_or(id, |alias| *aliases.entry(alias).or_insert(id));
            canonical.insert(id, canonical_id);
            groups.entry(canonical_id).or_default().push(id);
            profiles.insert(
                id,
                Person {
                    id,
                    name: row.try_get("name")?,
                    school_id: row.try_get("school_id")?,
                    school: row.try_get("school")?,
                    first_season: row.try_get("first_season")?,
                    latest_season: row.try_get("latest_season")?,
                },
            );
        }
        Ok(Self {
            profiles,
            canonical,
            groups,
        })
    }

    pub(super) async fn rosters(
        &self,
        conn: &mut AnyConnection,
    ) -> Result<BTreeMap<i64, BTreeSet<i64>>, ApiError> {
        let mut rosters: BTreeMap<i64, BTreeSet<i64>> = BTreeMap::new();
        for row in sqlx::query(ROSTERS).fetch_all(conn).await? {
            let person: i64 = row.try_get("debater_id")?;
            if let Some(id) = self.canonical.get(&person) {
                rosters
                    .entry(row.try_get("team_id")?)
                    .or_default()
                    .insert(*id);
            }
        }
        Ok(rosters)
    }
}

pub(super) fn pairs(ids: &BTreeSet<i64>) -> impl Iterator<Item = (i64, i64)> + '_ {
    ids.iter()
        .enumerate()
        .flat_map(|(index, left)| ids.iter().skip(index + 1).map(|right| (*left, *right)))
}

fn shortest(
    graph: &Graph,
    start: i64,
    end: i64,
    blocked: &[i64],
    edges: &HashSet<(i64, i64)>,
) -> Option<Vec<i64>> {
    let mut parents = HashMap::from([(start, None)]);
    let mut queue = VecDeque::from([start]);
    while let Some(current) = queue.pop_front() {
        if current == end {
            let mut path = Vec::new();
            let mut vertex = Some(end);
            while let Some(id) = vertex {
                path.push(id);
                vertex = parents[&id];
            }
            path.reverse();
            return Some(path);
        }
        for &neighbor in graph.get(&current).into_iter().flatten() {
            if !blocked.contains(&neighbor)
                && !edges.contains(&(current, neighbor))
                && let std::collections::hash_map::Entry::Vacant(entry) = parents.entry(neighbor)
            {
                entry.insert(Some(current));
                queue.push_back(neighbor);
            }
        }
    }
    None
}

pub(super) fn shortest_paths(graph: &Graph, first: i64, last: i64) -> Vec<Vec<i64>> {
    let Some(path) = shortest(graph, first, last, &[], &HashSet::new()) else {
        return Vec::new();
    };
    let mut paths = vec![path];
    let mut queued = HashSet::new();
    let mut candidates = BinaryHeap::new();
    while paths.len() < 5 {
        let previous = paths.last().unwrap();
        for (index, &vertex) in previous.iter().take(previous.len() - 1).enumerate() {
            let prefix = &previous[..=index];
            let blocked_edges = paths
                .iter()
                .filter(|path| path.len() > index + 1 && path.starts_with(prefix))
                .map(|path| (vertex, path[index + 1]))
                .collect();
            if let Some(suffix) = shortest(graph, vertex, last, &previous[..index], &blocked_edges)
            {
                let mut candidate = previous[..index].to_vec();
                candidate.extend(suffix);
                if queued.insert(candidate.clone()) {
                    candidates.push(Reverse((candidate.len(), candidate)));
                }
            }
        }
        let Some(Reverse((_, next))) = candidates.pop() else {
            break;
        };
        paths.push(next);
    }
    paths
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_are_shortest_first_and_loopless() {
        let graph = Graph::from([
            (1, BTreeSet::from([2, 3])),
            (2, BTreeSet::from([1, 3, 4])),
            (3, BTreeSet::from([1, 2, 4])),
            (4, BTreeSet::from([2, 3])),
        ]);
        assert_eq!(
            shortest_paths(&graph, 1, 4),
            vec![
                vec![1, 2, 4],
                vec![1, 3, 4],
                vec![1, 2, 3, 4],
                vec![1, 3, 2, 4]
            ]
        );
        assert_eq!(shortest_paths(&graph, 3, 3), vec![vec![3]]);
        assert!(shortest_paths(&graph, 1, 9).is_empty());
    }
}
