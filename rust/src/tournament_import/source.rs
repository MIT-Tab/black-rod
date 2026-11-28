use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    io::{Read, Write},
    sync::{Arc, LazyLock},
    time::Duration,
};

use axum::{
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{Days, NaiveDate, Utc};
use flate2::{Compression, read::ZlibDecoder, write::ZlibEncoder};
use minijinja::context;
use ring::digest;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use url::{Url, form_urlencoded};

use super::{Error, as_id, invalid};
use crate::{
    ApiError, AppState, admin,
    agent::journal::TABLES,
    cache::Memcached,
    flash,
    live::snapshots::{self, INDEX, RETENTION, Source},
    login,
    tournament_profile::edit,
    web,
};

const PATH: &str = "/core/tournaments/import/";
const CHUNK: usize = 250_000;
const TTL: u32 = 3600;
const EXPIRED: &str = "This import review expired. Open the tournament import again.";
const RESULT_KEYS: [&str; 7] = [
    "varsity_team_placements",
    "novice_team_placements",
    "non_placing_teams",
    "varsity_speaker_awards",
    "novice_speaker_awards",
    "new_schools",
    "new_debater_data",
];

static CLIENT: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::limited(5))
        .build()
        .expect("results import client")
});

type Snapshots = moka::future::Cache<String, (Source, Arc<Value>)>;
type Reviews = moka::future::Cache<String, (i64, i64, Arc<Value>)>;

#[derive(Clone, Default, Deserialize, Serialize)]
pub(super) struct Mappings {
    pub(super) debaters: BTreeMap<String, i64>,
    pub(super) schools: BTreeMap<String, i64>,
}

#[derive(Clone)]
pub(crate) struct Cache {
    sources: Snapshots,
    reviews: Reviews,
    mappings: moka::future::Cache<String, Mappings>,
}

impl Cache {
    pub(crate) fn new() -> Self {
        Self {
            sources: Snapshots::builder().max_capacity(100).build(),
            mappings: moka::future::Cache::builder()
                .max_capacity(100)
                .time_to_live(Duration::from_secs(TTL.into()))
                .build(),
            reviews: Reviews::builder()
                .max_capacity(100)
                .time_to_live(Duration::from_secs(TTL.into()))
                .build(),
        }
    }

    pub(crate) fn retained_live(&self, active: &HashMap<String, Value>) -> HashMap<String, Value> {
        let now = Utc::now().timestamp() as f64;
        self.sources
            .iter()
            .filter_map(|(_, (source, data))| {
                if active.contains_key(&source.url)
                    || source
                        .missing_since
                        .is_some_and(|missing| now - missing >= RETENTION)
                {
                    return None;
                }
                let mut payload = data
                    .as_object()?
                    .iter()
                    .filter(|(name, _)| !matches!(name.as_str(), "bundle" | "new_debater_data"))
                    .map(|(name, value)| (name.clone(), value.clone()))
                    .collect::<serde_json::Map<_, _>>();
                payload.insert("_cached_only".to_owned(), Value::Bool(true));
                Some((source.url, Value::Object(payload)))
            })
            .collect()
    }
}

pub(super) fn key(url: &str) -> String {
    hex::encode(digest::digest(
        &digest::SHA256,
        url.trim_end_matches('/').as_bytes(),
    ))
}

fn django_key(key: &str) -> String {
    format!(":1:{key}")
}

fn encode(value: &Value) -> Option<Vec<u8>> {
    let serialized = serde_json::to_vec(value).ok()?;
    let mut data = ZlibEncoder::new(Vec::new(), Compression::default());
    data.write_all(&serialized).ok()?;
    data.finish().ok()
}

fn decode(compressed: &[u8]) -> Option<Value> {
    let mut data = Vec::new();
    ZlibDecoder::new(compressed).read_to_end(&mut data).ok()?;
    serde_json::from_slice(&data).ok()
}

fn api_url(input: &str) -> Option<String> {
    let normalized = input.trim().trim_end_matches('/');
    let parsed = Url::parse(normalized).ok()?;
    (matches!(parsed.scheme(), "http" | "https")
        && parsed.host().is_some()
        && parsed.username().is_empty()
        && parsed.password().is_none())
    .then(|| normalized.to_owned())
}

async fn index(state: &AppState) -> HashMap<String, Source> {
    if let Some(cache) = &state.memcached {
        return snapshots::source_index(cache).await.unwrap_or_default();
    }
    let now = Utc::now().timestamp() as f64;
    state
        .import_cache
        .sources
        .iter()
        .filter_map(|(key, (row, _))| {
            row.missing_since
                .is_none_or(|date| now - date < RETENTION)
                .then(|| (key.as_ref().clone(), row))
        })
        .collect()
}

async fn discard(cache: &Memcached, keys: &[String]) {
    for item in keys {
        cache.delete_raw(&django_key(item)).await;
    }
}

async fn chunks(cache: &Memcached, stem: &str, bytes: &[u8], ttl: u32) -> Option<Vec<String>> {
    let mut keys = Vec::new();
    for (index, bytes) in bytes.chunks(CHUNK).enumerate() {
        let key = format!("{stem}:{}", index * CHUNK);
        if !cache
            .write_raw(&django_key(&key), 0, bytes, ttl, true)
            .await
        {
            discard(cache, &keys).await;
            return None;
        }
        keys.push(key);
    }
    Some(keys)
}

async fn guard_index(cache: &Memcached) -> bool {
    for _ in 0..100 {
        if cache
            .write_raw(&format!("{INDEX}:lock"), 2, b"1", 30, false)
            .await
        {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    false
}

async fn publish(state: &AppState, url: &str, data: Arc<Value>) -> Result<(), Error> {
    let id = key(url);
    let mut row = Source {
        url: url.to_owned(),
        name: data["tournament"]["name"]
            .as_str()
            .unwrap_or(url)
            .to_owned(),
        updated_at: Some(Utc::now().to_rfc3339()),
        missing_since: None,
        chunks: Vec::new(),
    };
    let Some(cache) = &state.memcached else {
        state.import_cache.sources.insert(id, (row, data)).await;
        return Ok(());
    };
    let bytes =
        encode(&data).ok_or_else(|| invalid("Could not cache the complete tournament snapshot"))?;
    let generation = uuid::Uuid::new_v4().simple();
    let stored = chunks(
        cache,
        &format!("mittab-import:{id}:{generation}"),
        &bytes,
        0,
    )
    .await
    .ok_or_else(|| invalid("Could not cache the complete tournament snapshot"))?;
    row.chunks = stored.clone();
    if !guard_index(cache).await {
        discard(cache, &stored).await;
        return Err(invalid(
            "Another worker is updating the tournament cache; try again.",
        ));
    }
    let mut before = match snapshots::source_index(cache).await {
        Ok(index) => index,
        Err(error) => {
            cache.delete_raw(&format!("{INDEX}:lock")).await;
            discard(cache, &stored).await;
            eprintln!("Could not read the tournament snapshot index: {error}");
            return Err(invalid("Could not publish the tournament snapshot"));
        }
    };
    let previous = before.insert(id.clone(), row);
    let published = match serde_pickle::to_vec(&before, Default::default()) {
        Ok(bytes) => match cache.update_raw_checked(INDEX, 1, &bytes, 0).await {
            Ok(stored) => Some(stored),
            Err(_) => {
                // A lost acknowledgement can leave an index that references these chunks.
                snapshots::source_index(cache)
                    .await
                    .ok()
                    .filter(|index| index.get(&id).is_some_and(|source| source.chunks == stored))
                    .map(|_| true)
            }
        },
        Err(_) => Some(false),
    };
    cache.delete_raw(&format!("{INDEX}:lock")).await;
    if published != Some(true) {
        if published == Some(false) {
            discard(cache, &stored).await;
        }
        return Err(invalid("Could not publish the tournament snapshot"));
    }
    if let Some(old) = previous {
        discard(cache, &old.chunks).await;
    }
    Ok(())
}

async fn fetch_payload(url: &str) -> Result<(String, Arc<Value>), Error> {
    let url = api_url(url).ok_or_else(|| invalid("Enter a valid URL."))?;
    let response = CLIENT
        .get(format!("{url}/api/import-results"))
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .map_err(|_| invalid("The tournament import source is unavailable."))?;
    let mut payload: Value = response
        .json()
        .await
        .map_err(|_| invalid("Unsupported tournament import snapshot"))?;
    if !payload.is_object() || as_id(&payload["schema_version"]) != Some(1) {
        return Err(invalid("Unsupported tournament import snapshot"));
    }
    if payload["tournament"]["name"]
        .as_str()
        .is_none_or(str::is_empty)
    {
        return Err(invalid("Missing tournament metadata in import snapshot"));
    }
    if !payload["import_ready"].is_boolean()
        || !RESULT_KEYS.iter().all(|key| payload[key].is_array())
        || !payload["debater_counts"].is_object()
        || (payload["import_ready"] == true && !payload["bundle"].is_object())
    {
        return Err(invalid("Incomplete tournament import snapshot"));
    }
    payload["source_url"] = json!(url);
    Ok((url, Arc::new(payload)))
}

pub(super) async fn fresh_or_cached(state: &AppState, url: &str) -> Result<Arc<Value>, Error> {
    match fetch_payload(url).await {
        Ok((url, payload)) => {
            publish(state, &url, payload.clone()).await?;
            Ok(payload)
        }
        Err(error) => {
            let id = key(url);
            let Some(row) = index(state).await.remove(&id) else {
                return Err(error);
            };
            let value = if let Some(cache) = &state.memcached {
                snapshots::cached_source(cache, &id, &row.chunks)
                    .await
                    .map(Arc::new)
            } else {
                state
                    .import_cache
                    .sources
                    .get(&id)
                    .await
                    .map(|(_, data)| data)
            };
            value.ok_or_else(|| {
                invalid("The tournament is unavailable and its snapshot has expired.")
            })
        }
    }
}

pub(super) async fn save_review(
    state: &AppState,
    id: i64,
    user: i64,
    data: Arc<Value>,
) -> Result<String, Error> {
    let token = uuid::Uuid::new_v4().simple().to_string();
    let Some(cache) = &state.memcached else {
        state
            .import_cache
            .reviews
            .insert(token.clone(), (id, user, data))
            .await;
        return Ok(token);
    };
    let bytes = encode(&data)
        .ok_or_else(|| invalid("The review snapshot could not be saved; try again."))?;
    let stem = format!("mittab-review:{token}");
    let stored = chunks(cache, &stem, &bytes, TTL)
        .await
        .ok_or_else(|| invalid("The review snapshot could not be saved; try again."))?;
    let record = serde_pickle::to_vec(&(&stored, id, user), Default::default())
        .map_err(|_| invalid("The review snapshot could not be saved; try again."))?;
    if !cache
        .write_raw(&django_key(&stem), 1, &record, TTL, true)
        .await
    {
        discard(cache, &stored).await;
        return Err(invalid(
            "The review snapshot could not be saved; try again.",
        ));
    }
    Ok(token)
}

pub(super) async fn load_review(
    state: &AppState,
    token: &str,
    id: i64,
    user: i64,
) -> Result<Arc<Value>, Error> {
    if token.len() != 32 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(invalid(EXPIRED));
    }
    let Some(cache) = &state.memcached else {
        return state
            .import_cache
            .reviews
            .get(token)
            .await
            .filter(|(event, actor, _)| (*event, *actor) == (id, user))
            .map(|(_, _, data)| data)
            .ok_or_else(|| invalid(EXPIRED));
    };
    let stem = format!("mittab-review:{token}");
    let Some((1, meta)) = cache.read_raw(&django_key(&stem)).await else {
        return Err(invalid(EXPIRED));
    };
    let (parts, event, actor): (Vec<String>, i64, i64) =
        serde_pickle::from_slice(&meta, Default::default()).map_err(|_| invalid(EXPIRED))?;
    if (event, actor) != (id, user) {
        return Err(invalid(EXPIRED));
    }
    let mut data = Vec::new();
    for part in parts {
        if !part.starts_with(&format!("{stem}:")) {
            return Err(invalid(EXPIRED));
        }
        let Some((0, bytes)) = cache.read_raw(&django_key(&part)).await else {
            return Err(invalid(EXPIRED));
        };
        data.extend(bytes);
    }
    decode(&data).map(Arc::new).ok_or_else(|| invalid(EXPIRED))
}

pub(super) async fn review_mappings(state: &AppState, token: &str) -> Mappings {
    if let Some(cache) = &state.memcached {
        let key = django_key(&format!("mittab-review:{token}:mappings"));
        if let Some((1, data)) = cache.read_raw(&key).await {
            return serde_pickle::from_slice(&data, Default::default()).unwrap_or_default();
        }
        return Mappings::default();
    }
    state
        .import_cache
        .mappings
        .get(token)
        .await
        .unwrap_or_default()
}

pub(super) async fn save_mappings(
    state: &AppState,
    token: &str,
    mappings: Mappings,
) -> Result<(), Error> {
    if let Some(cache) = &state.memcached {
        let key = django_key(&format!("mittab-review:{token}:mappings"));
        let data = serde_pickle::to_vec(&mappings, Default::default())
            .map_err(|_| ApiError::Serialization)?;
        if !cache.write_raw(&key, 1, &data, TTL, true).await {
            return Err(invalid(
                "The review snapshot could not be saved; try again.",
            ));
        }
    } else {
        state
            .import_cache
            .mappings
            .insert(token.to_owned(), mappings)
            .await;
    }
    Ok(())
}

pub(super) fn review_url(id: i64, token: &str, url: &str) -> String {
    let mut params = form_urlencoded::Serializer::new(String::new());
    params
        .append_pair("tournament", &id.to_string())
        .append_pair("review", token)
        .append_pair("api_url", url);
    format!("/core/tournaments/data_entry?{}", params.finish())
}

async fn match_event(state: &AppState, data: &Value) -> Result<i64, Error> {
    let meta = &data["tournament"];
    let date = meta["date"]
        .as_str()
        .and_then(|date| NaiveDate::parse_from_str(date, "%Y-%m-%d").ok())
        .ok_or_else(|| invalid("The tournament import has an invalid date."))?;
    let start = date
        .checked_sub_days(Days::new(7))
        .ok_or(ApiError::InvalidStoredDate)?
        .to_string();
    let end = date
        .checked_add_days(Days::new(7))
        .ok_or(ApiError::InvalidStoredDate)?
        .to_string();
    let season = super::text(&meta["season"]);
    let kind = as_id(&meta["tournament_type"])
        .ok_or_else(|| invalid("The tournament import has an invalid type."))?;
    let host = as_id(&meta["host_school"]["apda_id"]).ok_or_else(|| {
        invalid("Set this tournament's APDA host school in mit-tab before importing.")
    })?;
    let mut tx = state.pool.begin().await?;
    let id = sqlx::query_scalar::<_, i64>(
        "UPDATE core_school SET id=id WHERE id=$1 AND NOT temporary RETURNING CAST(id AS BIGINT)",
    )
    .bind(host)
    .fetch_optional(&mut *tx)
    .await?;
    if id.is_none() {
        return Err(invalid(
            "Set this tournament's APDA host school in mit-tab before importing.",
        ));
    }
    let query = if tx.backend_name() == "SQLite" {
        "SELECT CAST(id AS BIGINT) FROM core_tournament WHERE host_id=$1 AND season=$2 AND qual_type=$3
            AND date BETWEEN $4 AND $5 ORDER BY date,id LIMIT 2"
    } else {
        "SELECT CAST(id AS BIGINT) FROM core_tournament WHERE host_id=$1 AND season=$2 AND qual_type=$3
            AND date BETWEEN CAST(CAST($4 AS TEXT) AS DATE) AND CAST(CAST($5 AS TEXT) AS DATE) ORDER BY date,id LIMIT 2"
    };
    let matches = sqlx::query_scalar::<_, i64>(query)
        .bind(host)
        .bind(&season)
        .bind(kind)
        .bind(start)
        .bind(end)
        .fetch_all(&mut *tx)
        .await?;
    if let [id] = matches.as_slice() {
        tx.commit().await?;
        return Ok(*id);
    }
    if !matches.is_empty() {
        return Err(invalid(
            "Multiple tournaments match this source. Correct their dates before importing.",
        ));
    }
    let day = date.to_string();
    let (name, short_name) = edit::names(
        &mut tx,
        host,
        kind,
        None,
        edit::Naming {
            season: &season,
            date: &day,
            name_suffix: "0",
            manual_name: "",
            short_name: "",
        },
    )
    .await?;
    let scoring = edit::scoring(kind, &season);
    let fields = Map::from_iter([
        ("name".into(), json!(name)),
        ("short_name".into(), json!(short_name)),
        ("season".into(), json!(season)),
        ("num_rounds".into(), json!(5)),
        ("host_id".into(), json!(host)),
        ("num_teams".into(), json!(-1)),
        ("num_novice_teams".into(), json!(-1)),
        ("num_debaters".into(), json!(-1)),
        ("num_novice_debaters".into(), json!(-1)),
        ("date".into(), json!(day)),
        ("name_suffix".into(), json!(0)),
        ("manual_name".into(), json!("")),
        ("qual_type".into(), json!(kind)),
        (
            "online_qual_points".into(),
            json!(scoring.online.unwrap_or_default()),
        ),
        ("qual".into(), json!(scoring.qual)),
        ("noty".into(), json!(false)),
        ("soty".into(), json!(scoring.soty)),
        ("toty".into(), json!(scoring.toty)),
        (
            "autoqual_bar".into(),
            json!(scoring.bar.unwrap_or_default()),
        ),
    ]);
    let id = TABLES["core.Tournament"]
        .write(&mut tx, &fields, None)
        .await?;
    tx.commit().await?;
    Ok(id)
}

async fn sources(state: &AppState) -> BTreeMap<String, Source> {
    let mut found = index(state).await.into_iter().collect::<BTreeMap<_, _>>();
    if let Some(live) = &state.live {
        for url in live.directory_urls().await {
            found.entry(key(&url)).or_insert_with(|| Source {
                url: url.clone(),
                name: url,
                updated_at: None,
                missing_since: None,
                chunks: Vec::new(),
            });
        }
    }
    found
}

fn redirect(headers: &HeaderMap, state: &AppState, origin: &str, message: &str) -> Response {
    let mut response = (StatusCode::FOUND, [(header::LOCATION, PATH)]).into_response();
    if let Some(secret) = state.secret.as_deref() {
        flash::push(
            &mut response,
            headers,
            secret,
            40,
            message,
            origin.starts_with("https://"),
        );
    }
    response
}

async fn request(state: &AppState, request: Request) -> Result<Response, Error> {
    let uri = request.uri().clone();
    let user = match admin::require(state, &uri, request.headers()).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    let Some(origin) = state.origin(request.headers()) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let secure = origin.starts_with("https://");
    let user_id = user.id.ok_or(ApiError::Serialization)?;
    if request.method() == Method::POST {
        let (parts, body) = request.into_parts();
        let Ok(bytes) = to_bytes(body, 2_621_440).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        let fields = login::form_values(&bytes);
        if !login::csrf_valid(
            &parts.headers,
            fields.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        let rows = sources(state).await;
        let Some(row) = fields.get("source").and_then(|source| rows.get(source)) else {
            return Ok(redirect(
                &parts.headers,
                state,
                &origin,
                "This tournament is no longer available.",
            ));
        };
        let result=async {
            let payload=fresh_or_cached(state,&row.url).await?;
            if payload["import_ready"]!=true || !super::truthy(&payload["bundle"]) {
                return Err(invalid("Results are still embargoed. Try after outrounds are posted or three hours after the final inround."));
            }
            let id=match_event(state,&payload).await?;
            let token=save_review(state,id,user_id,payload).await?;
            Ok(review_url(id,&token,&row.url))
        }.await;
        return match result {
            Ok(target) => Ok((StatusCode::FOUND, [(header::LOCATION, target)]).into_response()),
            Err(Error::Invalid(reason)) => Ok(redirect(&parts.headers, state, &origin, &reason)),
            Err(error) => Err(error),
        };
    }
    let headers = request.headers();
    let rows = sources(state).await;
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::messages(headers, secret))
        .unwrap_or_default();
    let (cookie, csrf) = login::csrf_tokens(headers).map_err(|_| ApiError::Serialization)?;
    let common = web::base_context(state, &user, &uri);
    let html = web::render(
        "tournaments/import_sources.html",
        context! {sources=>rows,messages,csrf,..common},
    )
    .map_err(|error| {
        eprintln!("Import source template failed: {error}");
        ApiError::Serialization
    })?;
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &cookie, secure);
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    Ok(response)
}

pub(crate) async fn page(State(state): State<AppState>, request_body: Request) -> Response {
    match request(&state, request_body).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Tournament source request failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn refresh_presence(
    cache: &Memcached,
    active: &HashSet<String>,
    outcomes: &HashSet<String>,
    confirmed: bool,
    now: f64,
) -> Result<usize, &'static str> {
    let mut cached = snapshots::source_index(cache)
        .await
        .map_err(|_| "Could not read the tournament snapshot index")?;
    for row in cached.values_mut() {
        if active.contains(&row.url) {
            if row.missing_since.is_some() {
                for key in &row.chunks {
                    if !cache.touch_raw(&django_key(key), 0).await {
                        return Err("Could not extend an active tournament snapshot");
                    }
                }
                row.missing_since = None;
            }
        } else if confirmed && row.missing_since.is_none() && !outcomes.contains(&row.url) {
            row.missing_since = Some(now);
        }
    }
    let bytes = serde_pickle::to_vec(&cached, Default::default())
        .map_err(|_| "Could not encode the tournament snapshot index")?;
    if !matches!(
        cache.update_raw_checked(INDEX, 1, &bytes, 0).await,
        Ok(true)
    ) {
        return Err("Could not save tournament snapshot retention");
    }
    // Shorten chunk lifetimes only after the index records when they disappeared.
    let mut complete = true;
    for row in cached.values() {
        if let Some(missing) = row.missing_since {
            let remaining = ((RETENTION + missing - now).ceil() as u32).clamp(1, RETENTION as u32);
            for key in &row.chunks {
                complete &= cache.touch_raw(&django_key(key), remaining).await;
            }
        }
    }
    complete
        .then_some(cached.len())
        .ok_or("Could not update all retained snapshot lifetimes")
}

pub(super) async fn refresh(state: &AppState) -> Result<usize, &'static str> {
    let Some(live) = &state.live else {
        return Ok(0);
    };
    let (urls, confirmed) = live.directory_state().await;
    let active = urls.into_iter().collect::<HashSet<_>>();
    let previous = if let Some(cache) = &state.memcached {
        snapshots::source_index(cache)
            .await
            .map_err(|_| "Could not read the tournament snapshot index")?
    } else {
        index(state).await
    };
    let mut urls = active
        .iter()
        .cloned()
        .chain(previous.values().map(|row| row.url.clone()))
        .collect::<BTreeSet<_>>()
        .into_iter();
    let mut outcomes = HashSet::new();
    let mut workers = tokio::task::JoinSet::new();
    let start = |workers: &mut tokio::task::JoinSet<_>, url: String| {
        let state = state.clone();
        workers.spawn(async move {
            match fetch_payload(&url).await {
                Ok((url, payload)) => publish(&state, &url, payload).await.map(|_| Some(url)),
                Err(_) => {
                    eprintln!("Could not fetch a tournament import snapshot during refresh");
                    Ok(None)
                }
            }
        });
    };
    for url in urls.by_ref().take(8) {
        start(&mut workers, url);
    }
    let mut published = true;
    while let Some(result) = workers.join_next().await {
        match result {
            Ok(Ok(Some(url))) => {
                outcomes.insert(url);
            }
            Ok(Ok(None)) => (),
            _ => published = false,
        }
        if let Some(url) = urls.next() {
            start(&mut workers, url);
        }
    }
    if !published {
        return Err("Could not publish all refreshed tournament snapshots");
    }
    let now = Utc::now().timestamp() as f64;
    if let Some(cache) = &state.memcached {
        if !guard_index(cache).await {
            return Err("Could not lock the tournament snapshot index");
        }
        let result = refresh_presence(cache, &active, &outcomes, confirmed, now).await;
        cache.delete_raw(&format!("{INDEX}:lock")).await;
        return result;
    } else {
        for (key, (mut row, value)) in state.import_cache.sources.iter() {
            if active.contains(&row.url) {
                row.missing_since = None;
            } else if confirmed && row.missing_since.is_none() && !outcomes.contains(&row.url) {
                row.missing_since = Some(now);
            }
            state
                .import_cache
                .sources
                .insert(key.as_ref().clone(), (row, value))
                .await;
        }
    }
    Ok(index(state).await.len())
}
