use std::{
    cmp::Reverse,
    collections::{BTreeMap, BinaryHeap, VecDeque},
    sync::{
        Arc, RwLock,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use axum::{
    body::to_bytes,
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::json;
use tokio::sync::Mutex;

use crate::{ApiError, AppState, flash, login, session, team_profile::read_transaction, web};

use super::{
    PATH, failed,
    graph::{Graph, People, pairs},
    page, selected,
};

mod shared;

const CACHE_TTL: Duration = Duration::from_secs(24 * 60 * 60);

type Cached = (u64, Instant, Arc<str>);

#[derive(Default)]
pub(crate) struct Cache {
    generation: AtomicU64,
    data: RwLock<Option<Cached>>,
    computing: Arc<Mutex<()>>,
}

impl Cache {
    fn current(&self, generation: u64) -> Option<Arc<str>> {
        self.data
            .read()
            .ok()?
            .as_ref()
            .filter(|(tag, expires, _)| *tag == generation && expires.elapsed() < CACHE_TTL)
            .map(|(_, _, html)| html.clone())
    }
}

async fn whole_graph(state: &AppState) -> Result<(People, Graph), ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let people = People::load(&mut tx).await?;
    let mut graph = people
        .groups
        .keys()
        .map(|&id| (id, Default::default()))
        .collect::<Graph>();
    for members in people.rosters(&mut tx).await?.into_values() {
        for (left, right) in pairs(&members) {
            graph.entry(left).or_default().insert(right);
            graph.entry(right).or_default().insert(left);
        }
    }
    tx.commit().await?;
    Ok((people, graph))
}

struct Compact {
    ids: Vec<i64>,
    edges: Vec<Vec<usize>>,
}

impl Compact {
    fn new(graph: &Graph) -> Self {
        let ids = graph.keys().copied().collect::<Vec<_>>();
        let positions = ids
            .iter()
            .enumerate()
            .map(|(index, &id)| (id, index))
            .collect::<BTreeMap<_, _>>();
        let edges = graph
            .values()
            .map(|neighbors| neighbors.iter().map(|id| positions[id]).collect())
            .collect();
        Self { ids, edges }
    }

    fn distances(&self, source: usize) -> Vec<Vec<usize>> {
        let mut seen = vec![false; self.ids.len()];
        seen[source] = true;
        let mut queue = VecDeque::from([(source, 0)]);
        let mut layers: Vec<Vec<usize>> = Vec::new();
        while let Some((vertex, distance)) = queue.pop_front() {
            for &next in &self.edges[vertex] {
                if seen[next] {
                    continue;
                }
                seen[next] = true;
                queue.push_back((next, distance + 1));
                if layers.len() == distance {
                    layers.push(Vec::new());
                }
                layers[distance].push(next);
            }
        }
        layers
    }

    fn path_counts(&self, source: usize, unique: bool) -> [Vec<u64>; 3] {
        let mut counts = std::array::from_fn(|_| vec![0; self.ids.len()]);
        let minimum = if unique { source + 1 } else { 0 };
        let mut visited = [source; 4];
        self.visit(source, 1, &mut visited, minimum, &mut counts);
        counts
    }

    fn visit(
        &self,
        vertex: usize,
        hop: usize,
        visited: &mut [usize; 4],
        minimum: usize,
        counts: &mut [Vec<u64>; 3],
    ) {
        for &next in &self.edges[vertex] {
            if visited[..hop].contains(&next) {
                continue;
            }
            if hop >= 2 && next >= minimum {
                counts[hop - 2][next] += 1;
            }
            if hop < 4 {
                visited[hop] = next;
                self.visit(next, hop + 1, visited, minimum, counts);
            }
        }
    }
}

type Ranked = (Reverse<u64>, i64, i64);

fn rank_leader(ranking: &mut BinaryHeap<Ranked>, count: u64, left: i64, right: i64, limit: usize) {
    if count == 0 {
        return;
    }
    ranking.push((Reverse(count), left, right));
    if ranking.len() > limit {
        ranking.pop();
    }
}

fn calculate(people: People, graph: Graph) -> Result<String, ApiError> {
    let compact = Compact::new(&graph);
    let mut diameter = 0;
    let mut furthest = Vec::new();
    let mut reach: [BinaryHeap<Ranked>; 6] = std::array::from_fn(|_| BinaryHeap::new());
    let mut interconnected: [BinaryHeap<Ranked>; 3] = std::array::from_fn(|_| BinaryHeap::new());
    for (source, &id) in compact.ids.iter().enumerate() {
        let layers = compact.distances(source);
        if let Some(last) = layers.last()
            && layers.len() >= diameter
        {
            if layers.len() > diameter {
                diameter = layers.len();
                furthest.clear();
            }
            let mut last = last
                .iter()
                .map(|&index| compact.ids[index])
                .filter(|end| *end > id)
                .collect::<Vec<_>>();
            last.sort_unstable();
            furthest.extend(
                last.into_iter()
                    .map(|end| (&people.profiles[&id], &people.profiles[&end])),
            );
        }
        let mut count = 0;
        for (distance, ranking) in reach.iter_mut().enumerate() {
            count += layers.get(distance).map_or(0, Vec::len) as u64;
            rank_leader(ranking, count, id, 0, 3);
        }
        for (length, counts) in compact.path_counts(source, true).into_iter().enumerate() {
            for (target, count) in counts.into_iter().enumerate() {
                rank_leader(
                    &mut interconnected[length],
                    count,
                    id,
                    compact.ids[target],
                    5,
                );
            }
        }
    }
    let reach = reach.map(|ranking| {
        ranking
            .into_sorted_vec()
            .into_iter()
            .map(|(Reverse(count), id, _)| (&people.profiles[&id], count))
            .collect::<Vec<_>>()
    });
    let interconnected = interconnected.map(|ranking| {
        ranking
            .into_sorted_vec()
            .into_iter()
            .map(|(Reverse(count), a, b)| (&people.profiles[&a], &people.profiles[&b], count))
            .collect::<Vec<_>>()
    });
    web::render("debaters/_partnership_stats_results.html",context!{
        diameter,furthest_pairs=>furthest,reach_leaders=>reach,interconnection_leaders=>interconnected,
    }).map_err(|error|{
        eprintln!("Partnership statistics template failed: {error}");
        ApiError::Serialization
    })
}

fn uncached(mut response: Response) -> Response {
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("max-age=0, no-cache, no-store, must-revalidate, private"),
    );
    response.headers_mut().insert(
        header::EXPIRES,
        HeaderValue::from_str(&chrono::Utc::now().format("%a, %d %b %Y %T GMT").to_string())
            .unwrap(),
    );
    response
}

fn html(data: Arc<str>) -> Response {
    uncached(Html(data.to_string()).into_response())
}

async fn compute(state: &AppState) -> Result<Arc<str>, ApiError> {
    let (people, graph) = whole_graph(state).await?;
    tokio::task::spawn_blocking(move || calculate(people, graph))
        .await
        .map_err(|_| ApiError::Serialization)?
        .map(Arc::from)
}

pub(crate) async fn results(State(state): State<AppState>) -> Response {
    if let Some(cache) = &state.memcached {
        match shared::results(&state, cache).await {
            shared::Fragment::Ready(Ok(data)) => return html(data),
            shared::Fragment::Ready(Err(error)) => return uncached(failed(error)),
            shared::Fragment::Pending => return uncached(StatusCode::ACCEPTED.into_response()),
            shared::Fragment::Unavailable => (),
        }
    }
    let cache = state.partnership_stats.clone();
    let generation = cache.generation.load(Ordering::Acquire);
    if let Some(data) = cache.current(generation) {
        return html(data);
    }
    let Ok(guard) = cache.computing.clone().try_lock_owned() else {
        return uncached(StatusCode::ACCEPTED.into_response());
    };
    if let Some(data) = cache.current(generation) {
        return html(data);
    }
    let pending = tokio::spawn(async move {
        let _guard = guard;
        let result = compute(&state).await?;
        if let Ok(mut data) = cache.data.write()
            && cache.generation.load(Ordering::Acquire) == generation
        {
            *data = Some((generation, Instant::now(), result.clone()));
        }
        Ok::<_, ApiError>(result)
    });
    match pending.await {
        Ok(Ok(data)) => html(data),
        Ok(Err(error)) => uncached(failed(error)),
        Err(_) => uncached(StatusCode::INTERNAL_SERVER_ERROR.into_response()),
    }
}

async fn dashboard(state: &AppState, uri: &Uri, headers: &HeaderMap) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::messages(headers, secret))
        .unwrap_or_default();
    let csrf = if user.is_superuser {
        login::csrf_tokens(headers).ok()
    } else {
        None
    };
    let mut response = page(
        state,
        uri,
        &user,
        "debaters/partnership_stats.html",
        context! {
            messages,csrf=>csrf.as_ref().map(|(_,token)|token),
        },
    )?;
    let secure = state
        .origin(headers)
        .is_some_and(|url| url.starts_with("https://"));
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    if let Some((csrf, _)) = csrf {
        login::set_csrf_cookie(&mut response, &csrf, secure);
    }
    Ok(response)
}

pub(crate) async fn stats(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    dashboard(&state, &uri, &headers)
        .await
        .unwrap_or_else(failed)
}

pub(crate) async fn clear(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let Ok(body) = to_bytes(body, 2_621_440).await else {
        return StatusCode::PAYLOAD_TOO_LARGE.into_response();
    };
    let form = login::form_values(&body);
    let Some(origin) = state.origin(&parts.headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    if !login::csrf_valid(
        &parts.headers,
        form.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return login::csrf_failure();
    }
    let Ok(user) = session::user(&state.pool, &parts.headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_superuser {
        return web::permission_denied(&state, &user, &parts.uri);
    }
    if let Some(cache) = &state.memcached {
        shared::clear(cache).await;
    }
    state
        .partnership_stats
        .generation
        .fetch_add(1, Ordering::AcqRel);
    let mut response = (
        StatusCode::FOUND,
        [(header::LOCATION, format!("{PATH}stats/"))],
    )
        .into_response();
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            &mut response,
            &parts.headers,
            secret,
            25,
            "Partnership statistics cache cleared.",
            origin.starts_with("https://"),
        );
    }
    response
}

async fn person(state: &AppState, uri: &Uri, headers: &HeaderMap) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    let values = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<BTreeMap<_, _>>();
    let mut bindings = context! {fields=>json!({"debater":{}})};
    if !values.is_empty() {
        let (people, graph) = whole_graph(state).await?;
        bindings = match selected(&people, &values, "debater") {
            Ok(chosen) => {
                let id = people.canonical[&chosen.id];
                let compact = Compact::new(&graph);
                let source = compact.ids.binary_search(&id).unwrap();
                let layers = compact.distances(source);
                let mut furthest = layers
                    .last()
                    .into_iter()
                    .flatten()
                    .map(|&index| compact.ids[index])
                    .collect::<Vec<_>>();
                furthest.sort_unstable();
                let furthest = furthest
                    .into_iter()
                    .map(|id| &people.profiles[&id])
                    .collect::<Vec<_>>();
                let rankings = compact.path_counts(source, false).map(|counts| {
                    let mut ranking = BinaryHeap::new();
                    for (target, count) in counts.into_iter().enumerate() {
                        rank_leader(&mut ranking, count, compact.ids[target], 0, 5);
                    }
                    ranking
                        .into_sorted_vec()
                        .into_iter()
                        .map(|(Reverse(count), id, _)| (&people.profiles[&id], count))
                        .collect::<Vec<_>>()
                });
                context! {selected_debater=>chosen, distance=>layers.len(),furthest_debaters=>furthest,
                personal_rankings=>rankings,fields=>json!({"debater":{"value":chosen.id,"label":chosen.name}})}
            }
            Err(error) => context! {fields=>json!({"debater":{"error":error}})},
        };
    }
    page(
        state,
        uri,
        &user,
        "debaters/personal_partnership_stats.html",
        bindings,
    )
}

pub(crate) async fn personal(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    person(&state, &uri, &headers).await.unwrap_or_else(failed)
}
