use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use axum::{
    Json,
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::json;
use sqlx::{AnyConnection, Row};

use crate::{ApiError, AppState, session, team_profile::read_transaction};

use super::{
    PATH, failed,
    graph::{Graph, People, pairs, shortest_paths},
    page, selected,
};

const AXES: &[(&str, &str)] = &[
    ("", "None"),
    ("toty_points", "TOTY points"),
    ("qual_points", "Qual points"),
    ("first_year", "First year in league"),
    ("final_year", "Final year in league"),
    ("tournaments", "Tournaments attended"),
    ("partner_count", "Partners"),
    ("school", "School"),
    ("qualification_count", "Nationals-qualified seasons"),
];
const EVENTS: &str = "
    SELECT CAST(r.team_id AS BIGINT) AS team_id, CAST(r.tournament_id AS BIGINT) AS tournament_id
    FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
    WHERE ($1='' OR t.season >= $1) AND ($2='' OR t.season <= $2)
    UNION
    SELECT CAST(r.gov_id AS BIGINT),CAST(r.tournament_id AS BIGINT)
    FROM core_round r JOIN core_tournament t ON t.id=r.tournament_id
    WHERE ($1='' OR t.season >= $1) AND ($2='' OR t.season <= $2)
    UNION
    SELECT CAST(r.opp_id AS BIGINT),CAST(r.tournament_id AS BIGINT)
    FROM core_round r JOIN core_tournament t ON t.id=r.tournament_id
    WHERE ($1='' OR t.season >= $1) AND ($2='' OR t.season <= $2)";
const SPEAKER_EVENTS: &str = "
    SELECT CAST(r.debater_id AS BIGINT) AS person_id,CAST(r.tournament_id AS BIGINT) AS tournament_id
    FROM core_speakerresult r JOIN core_tournament t ON t.id=r.tournament_id WHERE NOT r.hidden_from_profile
    AND ($1='' OR t.season >= $1) AND ($2='' OR t.season <= $2)";
const TOTY: &str = "
    SELECT CAST(team_id AS BIGINT) AS id, CAST(points AS DOUBLE PRECISION) AS points FROM core_toty
    WHERE points>=0 AND ($1='' OR season >= $1) AND ($2='' OR season <= $2)";
const QUAL_POINTS: &str = "
    SELECT CAST(debater_id AS BIGINT) AS id, CAST(points AS DOUBLE PRECISION) AS points FROM core_qualpoints
    WHERE ($1='' OR season >= $1) AND ($2='' OR season <= $2)";
const QUALS: &str = "
    SELECT CAST(debater_id AS BIGINT) AS id, season FROM core_qual
    WHERE ($1='' OR season >= $1) AND ($2='' OR season <= $2)";

type Errors = BTreeMap<&'static str, Vec<String>>;

#[derive(Serialize)]
struct Filters {
    x_axis: String,
    y_axis: String,
    from_season: String,
    to_season: String,
    schools: Vec<(i64, String)>,
    qualified: bool,
    min_tournaments: String,
    minimum: usize,
}

impl Filters {
    async fn read(
        conn: &mut AnyConnection,
        uri: &Uri,
    ) -> Result<(Self, Vec<String>, Errors), ApiError> {
        let params = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
            .collect::<Vec<_>>();
        let value = |key| {
            params
                .iter()
                .rfind(|(name, _)| name == key)
                .map_or("", |(_, value)| value.as_ref())
        };
        let seasons = sqlx::query_scalar::<_, String>(
            "SELECT DISTINCT season FROM core_tournament ORDER BY season",
        )
        .fetch_all(&mut *conn)
        .await?;
        let mut errors = Errors::new();
        for key in ["x_axis", "y_axis", "from_season", "to_season"] {
            let raw = value(key);
            let valid = raw.is_empty()
                || match key {
                    "x_axis" | "y_axis" => AXES.iter().any(|(choice, _)| *choice == raw),
                    _ => seasons.iter().any(|season| season == raw),
                };
            if !valid {
                errors.insert(
                    key,
                    vec![format!(
                        "Select a valid choice. {raw} is not one of the available choices."
                    )],
                );
            }
        }
        let from = value("from_season");
        let to = value("to_season");
        if !from.is_empty()
            && !to.is_empty()
            && from > to
            && !errors.contains_key("from_season")
            && !errors.contains_key("to_season")
        {
            errors.insert(
                "to_season",
                vec!["Choose a season on or after the starting season.".to_owned()],
            );
        }
        let mut schools = Vec::new();
        let mut seen = HashSet::new();
        for (_, raw) in params.iter().filter(|(name, _)| name == "schools") {
            if let Ok(id) = raw.parse::<i64>() {
                if seen.insert(id) {
                    if let Some(name) = sqlx::query_scalar::<_, String>(
                        "SELECT name FROM core_school WHERE id=$1 AND NOT temporary",
                    )
                    .bind(id)
                    .fetch_optional(&mut *conn)
                    .await?
                    {
                        schools.push((id, name));
                    } else {
                        errors.insert(
                            "schools",
                            vec![format!(
                                "Select a valid choice. {raw} is not one of the available choices."
                            )],
                        );
                    }
                }
            } else {
                errors.insert("schools", vec![format!("“{raw}” is not a valid value.")]);
            }
        }
        schools.sort_by(|a, b| a.1.cmp(&b.1));
        let raw_minimum = value("min_tournaments").trim();
        let minimum = if raw_minimum.is_empty() {
            0
        } else {
            match raw_minimum
                .strip_suffix(".0")
                .unwrap_or(raw_minimum)
                .parse::<i64>()
            {
                Ok(number) if number >= 0 => number as usize,
                Ok(_) => {
                    errors.insert(
                        "min_tournaments",
                        vec!["Ensure this value is greater than or equal to 0.".to_owned()],
                    );
                    0
                }
                Err(_) => {
                    errors.insert("min_tournaments", vec!["Enter a whole number.".to_owned()]);
                    0
                }
            }
        };
        Ok((
            Self {
                x_axis: value("x_axis").to_owned(),
                y_axis: value("y_axis").to_owned(),
                from_season: from.to_owned(),
                to_season: to.to_owned(),
                schools,
                qualified: !value("qualified").is_empty()
                    && !value("qualified").eq_ignore_ascii_case("false"),
                min_tournaments: raw_minimum.to_owned(),
                minimum,
            },
            seasons,
            errors,
        ))
    }

    async fn rows(
        &self,
        conn: &mut AnyConnection,
        sql: &'static str,
    ) -> Result<Vec<sqlx::any::AnyRow>, ApiError> {
        Ok(sqlx::query(sql)
            .bind(&self.from_season)
            .bind(&self.to_season)
            .fetch_all(conn)
            .await?)
    }
}

#[derive(Serialize)]
struct Node<'a> {
    id: i64,
    label: &'a str,
    schools: BTreeSet<&'a str>,
    school_id: i64,
    tournaments: usize,
    qualified: bool,
    url: String,
    toty_points: f64,
    qual_points: f64,
    qualification_count: usize,
    partner_count: usize,
    first_year: Option<i32>,
    final_year: Option<i32>,
}

#[derive(Serialize)]
struct Edge {
    from: i64,
    to: i64,
    tournaments: usize,
}

#[derive(Serialize)]
struct Network<'a> {
    nodes: Vec<Node<'a>>,
    edges: Vec<Edge>,
}

fn season_year(value: Option<&str>) -> Option<i32> {
    let season = value?.trim().split('-').next()?;
    (!season.is_empty() && season.bytes().all(|digit| digit.is_ascii_digit()))
        .then(|| season.parse().ok())
        .flatten()
}

async fn build<'a>(
    conn: &mut AnyConnection,
    filters: &Filters,
    people: &'a People,
) -> Result<Network<'a>, ApiError> {
    let memberships = people.rosters(conn).await?;
    let mut team_events: HashMap<i64, BTreeSet<i64>> = HashMap::new();
    for row in filters.rows(conn, EVENTS).await? {
        let team = row.try_get("team_id")?;
        if memberships.contains_key(&team) {
            team_events
                .entry(team)
                .or_default()
                .insert(row.try_get("tournament_id")?);
        }
    }
    let restricted = !filters.from_season.is_empty() || !filters.to_season.is_empty();
    let mut tournaments: HashMap<i64, BTreeSet<i64>> = HashMap::new();
    let mut partnerships: BTreeMap<(i64, i64), BTreeSet<i64>> = BTreeMap::new();
    let empty = BTreeSet::new();
    for (team, members) in &memberships {
        let events = team_events.get(team).unwrap_or(&empty);
        if restricted && events.is_empty() {
            continue;
        }
        for person in members {
            tournaments.entry(*person).or_default().extend(events);
        }
        for pair in pairs(members) {
            partnerships.entry(pair).or_default().extend(events);
        }
    }
    for row in filters.rows(conn, SPEAKER_EVENTS).await? {
        let id: i64 = row.try_get("person_id")?;
        if let Some(identity) = people.canonical.get(&id) {
            tournaments
                .entry(*identity)
                .or_default()
                .insert(row.try_get("tournament_id")?);
        }
    }
    let mut toty = HashMap::new();
    for row in filters.rows(conn, TOTY).await? {
        let id: i64 = row.try_get("id")?;
        let points: f64 = row.try_get("points")?;
        for member in memberships.get(&id).into_iter().flatten() {
            *toty.entry(*member).or_insert(0.0) += points;
        }
    }
    let mut qual_points = HashMap::new();
    for row in filters.rows(conn, QUAL_POINTS).await? {
        let id: i64 = row.try_get("id")?;
        if let Some(identity) = people.canonical.get(&id) {
            *qual_points.entry(*identity).or_insert(0.0) += row.try_get::<f64, _>("points")?;
        }
    }
    let mut quals: HashMap<i64, HashSet<String>> = HashMap::new();
    for row in filters.rows(conn, QUALS).await? {
        let id: i64 = row.try_get("id")?;
        if let Some(identity) = people.canonical.get(&id) {
            quals
                .entry(*identity)
                .or_default()
                .insert(row.try_get("season")?);
        }
    }
    let schools: HashSet<i64> = filters.schools.iter().map(|(id, _)| *id).collect();
    let eligible = people
        .groups
        .iter()
        .filter_map(|(identity, members)| {
            let attends_school = schools.is_empty()
                || members.iter().any(|member| {
                    people.profiles[member]
                        .school_id
                        .is_some_and(|id| schools.contains(&id))
                });
            (attends_school
                && (!filters.qualified || quals.contains_key(identity))
                && tournaments.get(identity).map_or(0, BTreeSet::len) >= filters.minimum)
                .then_some(*identity)
        })
        .collect::<HashSet<_>>();
    let edges = partnerships
        .into_iter()
        .filter_map(|((from, to), events)| {
            (eligible.contains(&from) && eligible.contains(&to)).then_some(Edge {
                from,
                to,
                tournaments: events.len(),
            })
        })
        .collect::<Vec<_>>();
    let mut partner_counts = BTreeMap::new();
    for edge in &edges {
        *partner_counts.entry(edge.from).or_insert(0) += 1;
        *partner_counts.entry(edge.to).or_insert(0) += 1;
    }
    let mut nodes = Vec::new();
    for (id, partner_count) in partner_counts {
        let person = &people.profiles[&id];
        let profiles = people.groups[&id].iter().map(|id| &people.profiles[id]);
        nodes.push(Node {
            id,
            label: &person.name,
            schools: profiles
                .clone()
                .filter_map(|person| person.school.as_deref())
                .collect(),
            school_id: person.school_id.unwrap_or_default(),
            tournaments: tournaments.get(&id).map_or(0, BTreeSet::len),
            qualified: quals.contains_key(&id),
            url: format!("/core/debaters/{id}"),
            toty_points: toty.get(&id).copied().unwrap_or_default(),
            qual_points: qual_points.get(&id).copied().unwrap_or_default(),
            qualification_count: quals.get(&id).map_or(0, HashSet::len),
            partner_count,
            first_year: profiles
                .clone()
                .filter_map(|person| season_year(person.first_season.as_deref()))
                .min(),
            final_year: profiles
                .filter_map(|person| season_year(person.latest_season.as_deref()))
                .max(),
        });
    }
    Ok(Network { nodes, edges })
}

async fn result(state: &AppState, uri: &Uri, want_paths: bool) -> Result<Response, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let (filters, _, mut errors) = Filters::read(&mut tx, uri).await?;
    let people = People::load(&mut tx).await?;
    let endpoints = if want_paths {
        let values = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
            .into_owned()
            .collect();
        ["first_debater", "second_debater"].map(|key| match selected(&people, &values, key) {
            Ok(person) => Some(people.canonical[&person.id]),
            Err(error) => {
                errors.insert(key, vec![error.to_owned()]);
                None
            }
        })
    } else {
        [None, None]
    };
    if !errors.is_empty() {
        return Ok((StatusCode::BAD_REQUEST, Json(json!({"errors":errors}))).into_response());
    }
    let data = build(&mut tx, &filters, &people).await?;
    tx.commit().await?;
    let value = if let [Some(a), Some(b)] = endpoints {
        let nodes = data
            .nodes
            .iter()
            .map(|node| (node.id, node))
            .collect::<BTreeMap<_, _>>();
        let paths = if nodes.contains_key(&a) && nodes.contains_key(&b) {
            let mut graph = Graph::new();
            for edge in data.edges {
                graph.entry(edge.from).or_default().insert(edge.to);
                graph.entry(edge.to).or_default().insert(edge.from);
            }
            shortest_paths(&graph, a, b)
                .into_iter()
                .map(|path| path.into_iter().map(|id| nodes[&id]).collect::<Vec<_>>())
                .collect::<Vec<_>>()
        } else {
            Vec::new()
        };
        json!({"paths":paths})
    } else {
        serde_json::to_value(data)?
    };
    Ok(Json(value).into_response())
}

pub(crate) async fn data(State(state): State<AppState>, OriginalUri(uri): OriginalUri) -> Response {
    result(&state, &uri, uri.path().ends_with("/paths/"))
        .await
        .unwrap_or_else(failed)
}

async fn render(state: &AppState, uri: &Uri, headers: &HeaderMap) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    let mut conn = state.pool.acquire().await?;
    let (filters, seasons, errors) = Filters::read(&mut conn, uri).await?;
    let query =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect::<Vec<_>>();
    let mut encoded = url::form_urlencoded::Serializer::new(String::new());
    encoded.extend_pairs(
        query
            .iter()
            .map(|(name, value)| (name.as_ref(), value.as_ref())),
    );
    let encoded = encoded.finish();
    let network_url = errors
        .is_empty()
        .then(|| format!("{PATH}network/data/?{encoded}"));
    let fields = context! {filters,errors,axis_choices=>AXES,network_url, season_choices=>seasons};
    page(
        state,
        uri,
        &user,
        "debaters/partnership_network.html",
        fields,
    )
}

pub(crate) async fn network(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    render(&state, &uri, &headers).await.unwrap_or_else(failed)
}
