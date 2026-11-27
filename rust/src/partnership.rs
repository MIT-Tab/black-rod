mod graph;
pub(crate) mod network;
pub(crate) mod stats;

use std::collections::BTreeMap;

use axum::{
    Json,
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};

use crate::{ApiError, AppState, is_django_integer, session, team_profile::read_transaction, web};

use graph::{Graph, People, Person, pairs, shortest_paths};

const PATH: &str = "/core/debaters/partnership-paths/";

fn selected<'a>(
    people: &'a People,
    values: &BTreeMap<String, String>,
    key: &str,
) -> Result<&'a Person, &'static str> {
    let value = values.get(key).map(String::as_str).unwrap_or_default();
    if value.is_empty() {
        return Err("This field is required.");
    }
    if !is_django_integer(value) {
        return Err("Select a valid choice. That choice is not one of the available choices.");
    }
    value
        .parse::<i64>()
        .ok()
        .and_then(|id| people.profiles.get(&id))
        .ok_or("Select a valid choice. That choice is not one of the available choices.")
}

async fn paths_page(
    state: &AppState,
    uri: &Uri,
    headers: &HeaderMap,
) -> Result<Response, ApiError> {
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    let values = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<BTreeMap<_, _>>();
    let mut paths = Vec::new();
    let mut fields = BTreeMap::from([("first_debater", json!({})), ("second_debater", json!({}))]);
    let mut searched = false;
    if !values.is_empty() {
        let mut tx = read_transaction(&state.pool).await?;
        let people = People::load(&mut tx).await?;
        let [first, last] = ["first_debater", "second_debater"].map(|key| {
            let result = selected(&people, &values, key);
            fields.insert(
                key,
                match result {
                    Ok(person) => json!({"value":person.id,"label":person.name}),
                    Err(error) => json!({"error":error}),
                },
            );
            result
        });
        if let (Ok(first), Ok(last)) = (first, last) {
            searched = true;
            let begin = people.canonical[&first.id];
            let end = people.canonical[&last.id];
            if begin == end {
                paths.push(vec![json!(first)]);
            } else {
                let mut graph = Graph::new();
                for members in people.rosters(&mut tx).await?.into_values() {
                    for (left, right) in pairs(&members) {
                        graph.entry(left).or_default().insert(right);
                        graph.entry(right).or_default().insert(left);
                    }
                }
                for path in shortest_paths(&graph, begin, end) {
                    paths.push(
                        path.into_iter()
                            .map(|id| {
                                let person = if id == begin {
                                    first
                                } else if id == end {
                                    last
                                } else {
                                    &people.profiles[&id]
                                };
                                json!(person)
                            })
                            .collect(),
                    );
                }
            }
        }
        tx.commit().await?;
    }
    page(
        state,
        uri,
        &user,
        "debaters/partnership_paths.html",
        context! {paths,fields,searched},
    )
}

fn page(
    state: &AppState,
    uri: &Uri,
    user: &session::User,
    template: &str,
    bindings: minijinja::Value,
) -> Result<Response, ApiError> {
    let common = web::base_context(state, user, uri);
    match web::render(template, context! {..bindings,..common}) {
        Ok(html) => Ok(([(header::VARY, "Cookie")], Html(html)).into_response()),
        Err(error) => {
            eprintln!("Partnership template failed: {error}");
            Err(ApiError::Serialization)
        }
    }
}

pub(crate) async fn paths(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    paths_page(&state, &uri, &headers)
        .await
        .unwrap_or_else(failed)
}

fn failed(error: ApiError) -> Response {
    eprintln!("Partnership request failed: {error:?}");
    StatusCode::INTERNAL_SERVER_ERROR.into_response()
}

pub(crate) async fn autocomplete(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
) -> Response {
    let values = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<BTreeMap<_, _>>();
    let words = values
        .get("q")
        .map_or("", String::as_str)
        .split_whitespace()
        .map(str::to_lowercase)
        .collect::<Vec<_>>();
    if words.is_empty() {
        return Json(json!({"results":[],"pagination":{"more":false}})).into_response();
    }
    let mut conn = match state.pool.acquire().await {
        Ok(conn) => conn,
        Err(error) => return failed(error.into()),
    };
    let people = match People::load(&mut conn).await {
        Ok(people) => people,
        Err(error) => return failed(error),
    };
    let query = words.join(" ");
    let mut matches = people
        .profiles
        .values()
        .filter_map(|person| {
            let normalized = person.name.to_lowercase();
            if words.iter().all(|word| normalized.contains(word)) {
                let rank = if normalized == query {
                    0
                } else if normalized.starts_with(&query) {
                    1
                } else {
                    2
                };
                Some((rank, normalized, person.id, person))
            } else {
                None
            }
        })
        .collect::<Vec<_>>();
    matches.sort_by(|left, right| (&left.0, &left.1, &left.2).cmp(&(&right.0, &right.1, &right.2)));
    let page = values
        .get("page")
        .and_then(|page| page.parse::<usize>().ok())
        .unwrap_or(1)
        .max(1);
    let offset = (page - 1).saturating_mul(10);
    let results = matches
        .iter()
        .skip(offset)
        .take(10)
        .map(|(_, _, _, person)| {
            let label = person.label();
            json!({"id":person.id.to_string(),"text":label,"selected_text":label})
        })
        .collect::<Vec<Value>>();
    Json(json!({"results":results,"pagination":{"more":matches.len().saturating_sub(offset)>10}}))
        .into_response()
}
