use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::Duration,
};

use axum::{
    Json,
    body::to_bytes,
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::json;
use sqlx::{AnyConnection, AnyPool, AssertSqlSafe, Row};

use crate::{ApiError, AppState, flash, login, merge_requests, web};

pub(crate) type Cache = moka::future::Cache<(), Arc<Vec<Suggestion>>>;

pub(crate) fn cache(enabled: bool) -> Cache {
    Cache::builder()
        .time_to_live(if enabled {
            Duration::from_secs(300)
        } else {
            Duration::ZERO
        })
        .max_capacity(1)
        .build()
}

const CANDIDATES: &str = "
    SELECT CAST(d.id AS BIGINT) AS id,TRIM(d.first_name || ' ' || d.last_name) AS name,
        LOWER(d.first_name) AS first,LOWER(d.last_name) AS last,
        d.first_season,d.latest_season,CAST(d.school_id AS BIGINT) AS school_id,s.name AS school_name
    FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
    WHERE NOT d.temporary AND NOT d.synthetic AND d.alias_group_id IS NULL
      AND d.first_name<>'' AND d.last_name<>''
      AND NOT EXISTS(SELECT 1 FROM core_mergedebaterrequest r
        WHERE r.status='pending' AND (r.primary_debater_id=d.id OR r.secondary_debater_id=d.id))
    ORDER BY d.id DESC LIMIT 2000
";

#[derive(Clone, Serialize)]
struct School {
    name: String,
}

#[derive(Clone, Serialize)]
struct Person {
    id: i64,
    name: String,
    first_season: Option<String>,
    latest_season: Option<String>,
    school: Option<School>,
    school_id: Option<i64>,
    total_results: i64,
}

#[derive(Serialize)]
pub(crate) struct Suggestion {
    debater_one: Person,
    debater_two: Person,
    same_school: bool,
    percent_match: u8,
    min_results: i64,
    max_results: i64,
}

struct Candidate {
    person: Person,
    first: String,
    last: String,
    search: Vec<char>,
}

async fn candidates(pool: &AnyPool) -> Result<Vec<Candidate>, ApiError> {
    sqlx::query(CANDIDATES)
        .fetch_all(pool)
        .await?
        .iter()
        .map(|row| {
            let name: String = row.try_get("name")?;
            let search = name.to_lowercase().chars().collect();
            let person = Person {
                id: row.try_get("id")?,
                name,
                first_season: row.try_get("first_season")?,
                latest_season: row.try_get("latest_season")?,
                school_id: row.try_get("school_id")?,
                school: row
                    .try_get::<Option<String>, _>("school_name")?
                    .map(|name| School { name }),
                total_results: 0,
            };
            Ok(Candidate {
                person,
                first: row.try_get("first")?,
                last: row.try_get("last")?,
                search,
            })
        })
        .collect::<Result<Vec<_>, sqlx::Error>>()
        .map_err(Into::into)
}

async fn totals(
    conn: &mut AnyConnection,
    ids: &HashSet<i64>,
) -> Result<HashMap<i64, i64>, ApiError> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let ids = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
    let sql = format!("SELECT CAST(d.id AS BIGINT) AS id,
        (SELECT COUNT(*) FROM core_team_debaters m JOIN core_teamresult t ON t.team_id=m.team_id WHERE m.debater_id=d.id)
        +(SELECT COUNT(*) FROM core_speakerresult WHERE debater_id=d.id)
        +(SELECT COUNT(*) FROM core_roundstats WHERE debater_id=d.id)
        +(SELECT COUNT(*) FROM core_video WHERE pm_id=d.id)
        +(SELECT COUNT(*) FROM core_video WHERE mg_id=d.id)
        +(SELECT COUNT(*) FROM core_video WHERE lo_id=d.id)
        +(SELECT COUNT(*) FROM core_video WHERE mo_id=d.id) AS total
        FROM core_debater d WHERE d.id IN ({ids})");
    Ok(sqlx::query_as::<_, (i64, i64)>(AssertSqlSafe(sql))
        .fetch_all(conn)
        .await?
        .into_iter()
        .collect())
}

fn ordered_groups<K: Eq + std::hash::Hash>(keys: impl Iterator<Item = K>) -> Vec<Vec<usize>> {
    let mut groups = Vec::<Vec<usize>>::new();
    let mut positions = HashMap::new();
    for (index, key) in keys.enumerate() {
        let position = *positions.entry(key).or_insert_with(|| {
            let position = groups.len();
            groups.push(Vec::new());
            position
        });
        groups[position].push(index);
    }
    groups
}

fn pairs(group: &[usize]) -> impl Iterator<Item = (usize, usize)> + '_ {
    group.iter().enumerate().flat_map(move |(index, first)| {
        group[index + 1..]
            .iter()
            .map(move |second| (*first, *second))
    })
}

// Names are at most 65 characters. SequenceMatcher's popular-element heuristic
// only applies to strings of length 200 or greater.
pub(super) fn matching_ratio(left: &[char], right: &[char]) -> f64 {
    if left.is_empty() && right.is_empty() {
        return 1.0;
    }
    let mut matches = 0;
    let mut remaining = vec![(0, left.len(), 0, right.len())];
    let mut runs = vec![0; right.len() + 1];
    let mut previous = runs.clone();
    while let Some((lo, hi, start, end)) = remaining.pop() {
        let (mut a, mut b, mut best) = (lo, start, 0);
        previous[start..=end].fill(0);
        runs[start] = 0;
        for (i, ch) in left.iter().enumerate().take(hi).skip(lo) {
            for (j, other) in right.iter().enumerate().take(end).skip(start) {
                let run = if ch == other { previous[j] + 1 } else { 0 };
                runs[j + 1] = run;
                if run > best {
                    (a, b, best) = (i + 1 - run, j + 1 - run, run);
                }
            }
            std::mem::swap(&mut runs, &mut previous);
        }
        if best > 0 {
            matches += best;
            if lo < a && start < b {
                remaining.push((lo, a, start, b));
            }
            if a + best < hi && b + best < end {
                remaining.push((a + best, hi, b + best, end));
            }
        }
    }
    2.0 * matches as f64 / (left.len() + right.len()) as f64
}

fn candidate_pairs(candidates: &[Candidate]) -> Vec<(usize, usize, f64)> {
    let names = ordered_groups(candidates.iter().map(|item| (&item.first, &item.last)));
    let initials = ordered_groups(candidates.iter().map(|item| item.first.chars().next()));
    let mut eligible = names
        .iter()
        .flat_map(|group| pairs(group).map(|(one, two)| (one, two, 1.0)))
        .collect::<Vec<_>>();
    for (one, two) in initials
        .iter()
        .filter(|group| group.len() <= 100)
        .flat_map(|group| pairs(group))
    {
        let a = &candidates[one];
        let b = &candidates[two];
        if a.first == b.first && a.last == b.last {
            continue;
        }
        let ratio = matching_ratio(&a.search, &b.search);
        if ratio >= 0.75 {
            eligible.push((one, two, ratio));
        }
    }
    eligible
}

async fn build(pool: &AnyPool) -> Result<Arc<Vec<Suggestion>>, ApiError> {
    let mut candidates = candidates(pool).await?;
    let eligible = candidate_pairs(&candidates);
    let ids = eligible
        .iter()
        .flat_map(|(one, two, _)| [candidates[*one].person.id, candidates[*two].person.id])
        .collect::<HashSet<_>>();
    let results = totals(&mut *pool.acquire().await?, &ids).await?;
    for item in &mut candidates {
        item.person.total_results = results.get(&item.person.id).copied().unwrap_or_default();
    }
    let mut matches = eligible
        .into_iter()
        .filter_map(|(one, two, ratio)| {
            let a = &candidates[one].person;
            let b = &candidates[two].person;
            let max = a.total_results.max(b.total_results);
            let min = a.total_results.min(b.total_results);
            let same = a.school_id == b.school_id;
            let mut score = ratio * 100.0 + (a.id.max(b.id) as f64 / 200.0).min(50.0);
            if same {
                score += 30.0
            }
            if max > 0 {
                score += if min <= 5 {
                    (1.0 - min as f64 / (max + 1) as f64) * 40.0
                } else {
                    -20.0
                };
            }
            (score > 0.0).then_some((one, two, ratio, same, min, max, score))
        })
        .collect::<Vec<_>>();
    matches.sort_by(|a, b| b.6.total_cmp(&a.6));
    Ok(Arc::new(
        matches
            .into_iter()
            .take(200)
            .map(|(one, two, ratio, same, min, max, _)| Suggestion {
                debater_one: candidates[one].person.clone(),
                debater_two: candidates[two].person.clone(),
                same_school: same,
                percent_match: (ratio * 100.0).round_ties_even() as u8,
                min_results: min,
                max_results: max,
            })
            .collect(),
    ))
}

async fn submit(state: &AppState, user: i64, request: Request) -> Result<Response, ApiError> {
    let (headers, body) = {
        let (parts, body) = request.into_parts();
        (parts.headers, body)
    };
    let Ok(body) = to_bytes(body, 2_621_440).await else {
        return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
    };
    let values = login::form_values(&body);
    let Some(origin) = state.origin(&headers) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    if !login::csrf_valid(
        &headers,
        values.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return Ok(login::csrf_failure());
    }
    let failure = |message: &str| Json(json!({"success":false,"error":message})).into_response();
    let (Some(one), Some(two)) = (values.get("debater_one"), values.get("debater_two")) else {
        return Ok(failure("Missing debater selection."));
    };
    if one.is_empty() || two.is_empty() {
        return Ok(failure("Missing debater selection."));
    }
    let (Ok(one), Ok(two)) = (one.parse::<i64>(), two.parse::<i64>()) else {
        return Ok(failure("Unable to find one of the selected debaters."));
    };
    let mut tx = state.pool.begin().await?;
    let (Some(first), Some(second)) = (
        merge_requests::person(&mut tx, one, true).await?,
        merge_requests::person(&mut tx, two, true).await?,
    ) else {
        return Ok(failure("Unable to find one of the selected debaters."));
    };
    let totals = totals(&mut tx, &HashSet::from([one, two])).await?;
    let (primary, secondary) = if totals.get(&two) > totals.get(&one) {
        (second, first)
    } else {
        (first, second)
    };
    if !merge_requests::queue(&mut tx, user, &primary, &secondary).await? {
        return Ok(failure(
            "A pending merge request already exists for these debaters.",
        ));
    }
    tx.commit().await?;
    state.merge_suggestions.invalidate(&()).await;
    if let Some(cache) = &state.memcached {
        let _ = cache.delete_raw(":1:merge_suggestions_list").await;
    }
    Ok(Json(json!({"success":true,"message":format!("Merge request created for {} and {}.",primary.name,secondary.name)})).into_response())
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let user = match super::require(state, request.uri(), request.headers()).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    if request.method() == Method::POST {
        return submit(state, user.id.ok_or(ApiError::Serialization)?, request).await;
    }
    let uri = request.uri();
    let headers = request.headers();
    let refresh = login::form_values(uri.query().unwrap_or_default().as_bytes())
        .get("refresh")
        .is_some_and(|value| value == "1");
    let current = if refresh {
        None
    } else {
        state.merge_suggestions.get(&()).await
    };
    let cached = current.is_some();
    let suggestions = if let Some(current) = current {
        current
    } else {
        if refresh {
            state.merge_suggestions.invalidate(&()).await;
        }
        state
            .merge_suggestions
            .try_get_with((), build(&state.pool))
            .await
            .map_err(|_| ApiError::Serialization)?
    };
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(headers, secret))
        .unwrap_or_default();
    let Ok((csrf, _)) = login::csrf_tokens(headers) else {
        return Err(ApiError::Serialization);
    };
    let frame = web::base_context(state, &user, uri);
    let html = web::render(
        "admin/merge_suggestions.html",
        context! {suggestions=>suggestions.as_ref(),cached,messages=>&messages,..frame},
    )
    .map_err(|error| {
        eprintln!("Merge suggestions template failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Merge suggestions failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
