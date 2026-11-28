mod cache_format;
mod ingestion;
mod profiles;
mod rating;

use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{Datelike, Local};
use minijinja::context;
use moka::future::Cache as LocalCache;
use ring::digest::{SHA1_FOR_LEGACY_USE_ONLY, SHA256, digest};
use serde::Serialize;
use serde_json::json;
use tokio::sync::Mutex;

use crate::{ApiError, AppState, flash, login, session, team_profile::read_transaction, web};

const NAMESPACE: &str = ":1:elo_dashboard_state:namespace_version";
const DASHBOARD_TTL: Duration = Duration::from_secs(6 * 60 * 60);
const PAGE_LENGTH: usize = 50;

#[derive(Clone)]
pub(crate) struct Cache {
    generation: Arc<AtomicU64>,
    pending_invalidation: Arc<Mutex<bool>>,
    results: LocalCache<String, Arc<profiles::Dashboard>>,
    ingestion: LocalCache<String, Arc<ingestion::Ingested>>,
    ratings: LocalCache<String, Arc<rating::Computed>>,
}

impl Cache {
    pub(crate) fn new(enabled: bool) -> Self {
        let ttl = if enabled {
            DASHBOARD_TTL
        } else {
            Duration::ZERO
        };
        Self {
            generation: Arc::new(AtomicU64::new(1)),
            pending_invalidation: Arc::new(Mutex::new(false)),
            results: LocalCache::builder()
                .max_capacity(100)
                .time_to_live(ttl)
                .build(),
            ingestion: runtime_cache(enabled),
            ratings: runtime_cache(enabled),
        }
    }
}

fn runtime_cache<T: Send + Sync + 'static>(enabled: bool) -> LocalCache<String, Arc<T>> {
    let ttl = if enabled {
        Duration::from_secs(600)
    } else {
        Duration::ZERO
    };
    LocalCache::builder()
        .max_capacity(2)
        .time_to_live(ttl)
        .build()
}

async fn shared_version(cache: &crate::cache::Memcached) -> Option<u64> {
    let entry = match cache.read_raw_checked(NAMESPACE).await.ok()? {
        Some(entry) => entry,
        None if cache.write_raw(NAMESPACE, 2, b"1", 0, false).await => return Some(1),
        None => cache.read_raw(NAMESPACE).await?,
    };
    let (2, bytes) = entry else {
        return None;
    };
    std::str::from_utf8(&bytes).ok()?.parse().ok()
}

async fn publish_invalidation(cache: &crate::cache::Memcached, pending: &mut bool) -> Option<u64> {
    let before = shared_version(cache).await?;
    if !cache.advance_counter(NAMESPACE, 0).await {
        return None;
    }
    let after = shared_version(cache)
        .await
        .filter(|value| *value > before)?;
    *pending = false;
    Some(after)
}

fn retry_invalidation(cache: crate::cache::Memcached, pending: Arc<Mutex<bool>>) {
    tokio::spawn(async move {
        let mut delay = Duration::from_secs(1);
        loop {
            tokio::time::sleep(delay).await;
            let mut pending = pending.lock().await;
            if !*pending || publish_invalidation(&cache, &mut pending).await.is_some() {
                break;
            }
            delay = (delay * 2).min(Duration::from_secs(30));
        }
    });
}

async fn version(state: &AppState) -> Option<u64> {
    let Some(cache) = &state.memcached else {
        return Some(state.elo.generation.load(Ordering::Relaxed));
    };
    let mut pending = state.elo.pending_invalidation.lock().await;
    if *pending {
        return publish_invalidation(cache, &mut pending).await;
    }
    drop(pending);
    shared_version(cache).await
}

pub(crate) async fn invalidate(state: &AppState) -> Option<u64> {
    let previous = state
        .elo
        .generation
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            Some(value.saturating_add(1))
        })
        .expect("local generation always advances or saturates");
    state.elo.ingestion.invalidate_all();
    state.elo.ratings.invalidate_all();
    state.elo.results.invalidate_all();
    let Some(cache) = &state.memcached else {
        return Some(previous.saturating_add(1));
    };
    let mut pending = state.elo.pending_invalidation.lock().await;
    let retry_scheduled = *pending;
    *pending = true;
    let published = publish_invalidation(cache, &mut pending).await;
    if published.is_none() && !retry_scheduled {
        eprintln!("Shared ELO cache invalidation failed and will be retried");
        retry_invalidation(cache.clone(), Arc::clone(&state.elo.pending_invalidation));
    }
    published
}

fn signature(data: &impl Serialize) -> String {
    let bytes = serde_json::to_vec(data).expect("serializable ELO settings");
    hex::encode(digest(&SHA256, &bytes))
}

async fn result(state: &AppState, key: Option<&str>) -> Option<Arc<profiles::Dashboard>> {
    let key = key?;
    if state.cache_lifetime.is_zero() {
        return None;
    }
    if let Some(cache) = &state.memcached {
        let (1, bytes) = cache.read_raw(key).await? else {
            return None;
        };
        return cache_format::decode(&bytes).map(Arc::new);
    }
    state.elo.results.get(key).await
}

async fn save(
    state: &AppState,
    latest: String,
    key: String,
    signatures: (&str, &str),
    result: Arc<profiles::Dashboard>,
) {
    if state.cache_lifetime.is_zero() {
        return;
    }
    if let Some(cache) = &state.memcached {
        for (entry, settings) in [(&latest, Some(signatures)), (&key, None)] {
            if let Some(bytes) = cache_format::encode(&result, settings) {
                cache
                    .write_raw(entry, 1, &bytes, DASHBOARD_TTL.as_secs() as u32, true)
                    .await;
            }
        }
    } else {
        state.elo.results.insert(latest, Arc::clone(&result)).await;
        state.elo.results.insert(key, result).await;
    }
}

fn cache_signatures(config: Compute, display: profiles::Settings) -> (String, String) {
    let seasons = |(first, last)| {
        (first..=last)
            .map(|year: i32| year.to_string())
            .collect::<Vec<_>>()
    };
    let rating = config.rating;
    let mut data = json!({
        "seasons": seasons(config.seasons),
        "higher_elo_win_share": (rating.win_share * 100.0).round() as i32,
        "higher_elo_loss_share": (rating.loss_share * 100.0).round() as i32,
        "k_max": rating.k_max as i32,
        "k_min": rating.k_min as i32,
        "k_decay_scale": rating.decay,
        "initial_rating": rating.start as i32,
        "exclude_proam_partnerships": rating.exclude_proam,
    })
    .to_string();
    if let Some((_, tail)) = data.split_once(r#""k_decay_scale":"#)
        && let Some((number, _)) = tail.split_once(',')
        && let Some((significand, exponent)) = number.split_once('e')
        && let Ok(exponent) = exponent.parse::<i32>()
    {
        data = data.replace(
            &format!(r#""k_decay_scale":{number},"#),
            &format!(r#""k_decay_scale":{significand}e{exponent:+03},"#),
        );
    }
    let display = json!({
        "active_seasons": seasons(display.active),
        "min_rounds": display.min_rounds,
        "min_outrounds": display.min_out,
        "exclude_dino_rounds": display.exclude_dino,
    })
    .to_string();
    let hash = |data: &str| hex::encode(digest(&SHA1_FOR_LEGACY_USE_ONLY, data.as_bytes()));
    (hash(&data), hash(&display))
}

#[derive(Clone, Copy, Serialize)]
struct Compute {
    seasons: (i32, i32),
    rating: rating::Settings,
}

async fn compute(
    state: &AppState,
    generation: Option<u64>,
    config: Compute,
    display: profiles::Settings,
) -> Result<Result<profiles::Dashboard, &'static str>, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let people = ingestion::People::load(&mut tx).await?;
    let fingerprint = ingestion::fingerprint(&mut tx, &people).await?;
    let key = signature(&(generation, &fingerprint, &config));
    let ratings = if let Some(value) = state.elo.ratings.get(&key).await {
        value
    } else {
        let ingestion_key = signature(&(generation, &fingerprint, config.seasons));
        let input = match state.elo.ingestion.get(&ingestion_key).await {
            Some(value) => value,
            None => {
                let seasons = (config.seasons.0..=config.seasons.1)
                    .map(|year| year.to_string())
                    .collect();
                let value = Arc::new(ingestion::ingest(&mut tx, &people, &seasons).await?);
                state
                    .elo
                    .ingestion
                    .insert(ingestion_key, Arc::clone(&value))
                    .await;
                value
            }
        };
        if input.tournaments == 0 {
            return Ok(Err("No imported rounds found for the selected filters."));
        }
        let value = Arc::new(rating::compute(
            input.tournaments,
            &input.debates,
            config.rating,
        ));
        if value.prelims + value.outrounds == 0 {
            return Ok(Err(
                "No rated debates found in imported rounds for the selected filters.",
            ));
        }
        state.elo.ratings.insert(key, Arc::clone(&value)).await;
        value
    };
    let result = profiles::dashboard(&mut tx, &people, &ratings, display).await?;
    tx.commit().await?;
    Ok(Ok(result))
}

#[derive(Serialize)]
struct Control {
    id: String,
    name: String,
    label: &'static str,
    help: &'static str,
    value: String,
    minimum: Option<i32>,
    maximum: Option<i32>,
    step: Option<&'static str>,
    kind: &'static str,
    classes: &'static str,
    role: &'static str,
    output: &'static str,
    error: Option<String>,
}

impl Control {
    fn number(name: &str, label: &'static str, initial: impl ToString) -> Self {
        let initial = initial.to_string();
        Self {
            id: format!("id_elo-{name}"),
            name: format!("elo-{name}"),
            label,
            help: "",
            value: initial.clone(),
            minimum: None,
            maximum: None,
            step: None,
            kind: "number",
            classes: "form-control form-control-sm",
            role: "",
            output: "",
            error: None,
        }
    }

    fn validate(&mut self, post: &HashMap<String, String>) -> Option<f64> {
        self.value = post.get(&self.name).cloned().unwrap_or_default();
        if self.kind == "checkbox" {
            let enabled = post.contains_key(&self.name)
                && !matches!(self.value.to_ascii_lowercase().as_str(), "false" | "0");
            self.value = if enabled { "on" } else { "" }.to_owned();
            return Some(f64::from(enabled));
        }
        let raw = self.value.trim();
        let decimal = self.step == Some("any");
        let parsed = if decimal {
            raw.parse::<f64>().ok().filter(|value| value.is_finite())
        } else {
            let whole = raw
                .split_once('.')
                .filter(|(_, fraction)| {
                    !fraction.is_empty() && fraction.bytes().all(|digit| digit == b'0')
                })
                .map_or(raw, |(whole, _)| whole);
            whole.parse::<i64>().ok().map(|value| value as f64)
        };
        self.error = if raw.is_empty() {
            Some("This field is required.".to_owned())
        } else if let Some(value) = parsed {
            if let Some(min) = self.minimum.filter(|min| value < f64::from(*min)) {
                Some(format!(
                    "Ensure this value is greater than or equal to {min}."
                ))
            } else if let Some(max) = self.maximum.filter(|max| value > f64::from(*max)) {
                Some(format!("Ensure this value is less than or equal to {max}."))
            } else {
                self.step
                    .and_then(|step| step.parse::<f64>().ok())
                    .filter(|step| value % step != 0.0)
                    .map(|step| {
                        let label = self.label.strip_suffix(" (%)").unwrap_or(self.label);
                        format!("{label} must use {step}-point increments.")
                    })
            }
        } else {
            Some(
                if decimal {
                    "Enter a number."
                } else {
                    "Enter a whole number."
                }
                .to_owned(),
            )
        };
        parsed.filter(|_| self.error.is_none())
    }
}

fn controls(min: i32, max: i32) -> HashMap<&'static str, Control> {
    let mut controls = HashMap::new();
    for (name, label, role, initial) in [
        (
            "season_start",
            "Compute Seasons Start",
            "compute-start",
            min,
        ),
        ("season_end", "Compute Seasons End", "compute-end", max),
        (
            "active_season_start",
            "Active Seasons Start",
            "active-start",
            2018.clamp(min, max),
        ),
        ("active_season_end", "Active Seasons End", "active-end", max),
    ] {
        let mut control = Control::number(name, label, initial);
        control.kind = "range";
        control.minimum = Some(min);
        control.maximum = Some(max);
        control.step = Some("1");
        control.classes = "form-range js-season-bound-slider";
        control.role = role;
        controls.insert(name, control);
    }
    for (name, label, initial, minimum, maximum, step) in [
        ("min_rounds", "Minimum Inrounds", "0", Some(0), None, None),
        (
            "min_outrounds",
            "Minimum Outrounds",
            "0",
            Some(0),
            None,
            None,
        ),
        ("k_max", "K Max", "40", Some(5), Some(100), Some("5")),
        ("k_min", "K Min", "10", Some(5), Some(100), Some("5")),
        (
            "k_decay_scale",
            "K Decay Scale",
            "75.0",
            None,
            None,
            Some("any"),
        ),
        (
            "initial_rating",
            "Initial Rating",
            "1500",
            Some(500),
            Some(2000),
            Some("100"),
        ),
    ] {
        let mut control = Control::number(name, label, initial);
        control.minimum = minimum;
        control.maximum = maximum;
        control.step = step;
        controls.insert(name, control);
    }
    for (name, label, action, output) in [
        (
            "higher_elo_win_share",
            "Higher-ELO Win Credit (%)",
            "win",
            "higher-elo-win-share-output",
        ),
        (
            "higher_elo_loss_share",
            "Higher-ELO Loss Burden (%)",
            "loss",
            "higher-elo-loss-share-output",
        ),
    ] {
        let mut control = Control::number(name, label, 50);
        control.kind = "range";
        control.minimum = Some(0);
        control.maximum = Some(100);
        control.step = Some("5");
        control.classes = "form-control form-control-sm form-range js-elo-share-slider";
        control.help = if action == "win" {
            "Percent of team win delta assigned to the higher pre-round ELO partner."
        } else {
            "Percent of team loss delta assigned to the higher pre-round ELO partner."
        };
        control.output = output;
        controls.insert(name, control);
    }
    for (name, label) in [
        ("exclude_proam_partnerships", "Exclude ProAm Partnerships"),
        ("exclude_dino_rounds", "Exclude Dino Rounds"),
    ] {
        let mut control = Control::number(name, label, "");
        control.kind = "checkbox";
        control.classes = "form-check-input";
        controls.insert(name, control);
    }
    controls
}

async fn bounds(state: &AppState) -> (i32, i32) {
    let seasons = sqlx::query_scalar::<_, String>("SELECT DISTINCT season FROM core_tournament")
        .fetch_all(&state.pool)
        .await
        .unwrap_or_default();
    let mut years = seasons
        .iter()
        .filter_map(|year| ingestion::season_year(year));
    let Some(first) = years.next() else {
        return (2017, Local::now().year().max(2017));
    };
    years.fold((first.max(2017), first.max(2017)), |(min, max), year| {
        (min.min(year).max(2017), max.max(year))
    })
}

fn season_label(year: i32) -> String {
    format!("{year}-{}", year + 1)
}

fn range_label(first: i32, last: i32) -> String {
    if first == last {
        season_label(first)
    } else {
        format!("{} through {}", season_label(first), season_label(last))
    }
}

fn submit(
    controls: &mut HashMap<&str, Control>,
    values: &HashMap<String, String>,
) -> Option<(Compute, profiles::Settings)> {
    let parsed = controls
        .iter_mut()
        .map(|(&name, field)| field.validate(values).map(|value| (name, value)))
        .collect::<Vec<_>>()
        .into_iter()
        .collect::<Option<HashMap<_, _>>>()?;
    let get = |name| parsed[name];
    let range = |first, last| {
        let first = get(first) as i32;
        let last = get(last) as i32;
        (first.min(last), first.max(last))
    };
    let compute = Compute {
        seasons: range("season_start", "season_end"),
        rating: rating::Settings {
            start: get("initial_rating"),
            k_max: get("k_max"),
            k_min: get("k_min"),
            decay: get("k_decay_scale"),
            win_share: get("higher_elo_win_share") / 100.0,
            loss_share: get("higher_elo_loss_share") / 100.0,
            exclude_proam: get("exclude_proam_partnerships") != 0.0,
        },
    };
    let display = profiles::Settings {
        active: range("active_season_start", "active_season_end"),
        min_rounds: get("min_rounds") as usize,
        min_out: get("min_outrounds") as usize,
        exclude_dino: get("exclude_dino_rounds") != 0.0,
    };
    Some((compute, display))
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    match response(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("ELO dashboard failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn response(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let (parts, body) = request.into_parts();
    let headers = &parts.headers;
    let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&parts.uri));
    }
    if !user.has_exclusive_pre_access && !user.is_superuser {
        return Ok(web::permission_denied(state, &user, &parts.uri));
    }
    let (min, max) = bounds(state).await;
    let mut controls = controls(min, max);
    let generation = version(state).await;
    let latest = generation.map(|generation| {
        format!(
            ":1:elo_dashboard_state:v{generation}:user:{}",
            user.id.unwrap_or_default()
        )
    });
    let mut found = result(state, latest.as_deref()).await;
    let mut errors = Vec::new();
    if parts.method == Method::POST {
        let values = match login::parse_form(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let Some(origin) = state.origin(headers) else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        if let Some((config, display)) = submit(&mut controls, &values) {
            let (compute_key, display_key) = cache_signatures(config, display);
            let key = latest
                .as_ref()
                .map(|latest| format!("{latest}:compute:{compute_key}:display:{display_key}"));
            found = result(state, key.as_deref()).await;
            if found.is_none() {
                match compute(state, generation, config, display).await? {
                    Ok(computed) => {
                        let computed = Arc::new(computed);
                        if let (Some(latest), Some(key)) = (latest, key) {
                            save(
                                state,
                                latest,
                                key,
                                (&compute_key, &display_key),
                                Arc::clone(&computed),
                            )
                            .await;
                        }
                        found = Some(computed);
                    }
                    Err(error) => errors.push(error),
                }
            }
        }
    }
    let rows = found
        .as_ref()
        .map_or(&[][..], |result| result.ranking_rows.as_slice());
    let requested = url::form_urlencoded::parse(parts.uri.query().unwrap_or_default().as_bytes())
        .filter(|(key, _)| key == "page")
        .map(|(_, value)| value)
        .last()
        .and_then(|value| value.trim().parse::<i64>().ok())
        .unwrap_or(1);
    let page_count = rows.len().div_ceil(PAGE_LENGTH).max(1);
    let page = usize::try_from(requested)
        .ok()
        .filter(|&number| (1..=page_count).contains(&number))
        .unwrap_or(page_count);
    let start = (page - 1) * PAGE_LENGTH;
    let end = (start + PAGE_LENGTH).min(rows.len());
    let page_obj = (!rows.is_empty()).then(|| {
        context! {
            start_index => start + 1,
            end_index => end,
            paginator => context! {count => rows.len()},
            has_other_pages => page_count > 1,
            has_previous => page > 1,
            previous_page_number => page - 1,
            has_next => page < page_count,
            next_page_number => page + 1,
        }
    });
    let chosen = |name: &str, fallback| {
        controls[name]
            .value
            .trim()
            .parse::<i32>()
            .unwrap_or(fallback)
    };
    let (cs, ce) = (chosen("season_start", min), chosen("season_end", max));
    let (as_, ae) = (
        chosen("active_season_start", min),
        chosen("active_season_end", max),
    );
    let common = web::base_context(state, &user, &parts.uri);
    let (csrf, token) = login::csrf_tokens(headers).map_err(|_| ApiError::Serialization)?;
    let secure = state
        .origin(headers)
        .is_some_and(|origin| origin.starts_with("https://"));
    let html = web::render("elo/dashboard.html", context! {
        form => controls, csrf => token, errors, result => found.as_deref(), table_rows => &rows[start..end],
        page_obj, rankings_per_page => PAGE_LENGTH, season_min => min, season_max => max,
        season_min_label => season_label(min), season_max_label => season_label(max),
        compute_season_start_label => season_label(cs), compute_season_end_label => season_label(ce),
        compute_season_range_label => range_label(cs, ce),
        active_season_start_label => season_label(as_), active_season_end_label => season_label(ae),
        active_season_range_label => range_label(as_, ae), ..common
    }).map_err(|error| {
        eprintln!("ELO template failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn invalidate_view(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    if let Err(response) = crate::admin::require(&state, &parts.uri, &parts.headers).await {
        return *response;
    }
    let values = match login::parse_form(&parts.headers, body, 2_621_440).await {
        Ok(fields) => fields,
        Err(status) => return status.into_response(),
    };
    let Some(origin) = state.origin(&parts.headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    if !login::csrf_valid(
        &parts.headers,
        values.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return login::csrf_failure();
    }
    let version = invalidate(&state).await;
    let mut response = (
        StatusCode::FOUND,
        [(header::LOCATION, "/core/admin-tools/")],
    )
        .into_response();
    if let Some(secret) = state.secret.as_deref() {
        let (level, message) = if let Some(version) = version {
            (
                25,
                format!("ELO cache invalidated successfully. Active namespace version: {version}."),
            )
        } else {
            (
                30,
                "The shared ELO cache is unavailable. Standings will retry the invalidation."
                    .to_owned(),
            )
        };
        flash::push(
            &mut response,
            &parts.headers,
            secret,
            level,
            &message,
            origin.starts_with("https://"),
        );
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{
        io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
        net::TcpListener,
    };

    #[tokio::test]
    async fn retries_pending_invalidation_before_reusing_shared_dashboard_data() {
        sqlx::any::install_default_drivers();
        let pool = sqlx::any::AnyPoolOptions::new()
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let state =
            AppState::new(pool.clone(), 2026).with_memcached(listener.local_addr().unwrap());
        let other_worker = AppState::new(pool, 2026).with_memcached(listener.local_addr().unwrap());
        // The cache listener deliberately does not respond until after the failed invalidation.
        assert_eq!(invalidate(&state).await, None);
        let service = tokio::spawn(async move {
            let mut generation = 7;
            while let Ok((socket, _)) = listener.accept().await {
                let mut socket = BufReader::new(socket);
                let mut command = String::new();
                if socket.read_line(&mut command).await.is_err() {
                    continue;
                }
                let reply = match command.split_whitespace().next() {
                    Some("get") => format!("VALUE {NAMESPACE} 2 1\r\n{generation}\r\nEND\r\n"),
                    Some("add") => "NOT_STORED\r\n".into(),
                    Some("incr") => {
                        generation += 1;
                        format!("{generation}\r\n")
                    }
                    Some("touch") => "TOUCHED\r\n".into(),
                    _ => continue,
                };
                let _ = socket.get_mut().write_all(reply.as_bytes()).await;
            }
        });
        assert_eq!(version(&other_worker).await, Some(7));
        assert_eq!(version(&state).await, Some(8));
        assert_eq!(version(&other_worker).await, Some(8));
        assert_eq!(version(&state).await, Some(8));
        service.abort();
    }
}
