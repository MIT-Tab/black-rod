mod account;
mod admin;
mod agent;
mod autocomplete;
mod cache;
mod commands;
pub use commands::HELP as COMMAND_HELP;
mod claims;
mod debater_profile;
mod delete_relations;
mod directory;
mod discovery;
mod django_json;
mod elo;
mod entity_delete;
mod entity_forms;
mod exports;
mod flash;
mod generated_code;
mod immaculate;
mod live;
mod login;
mod merge_requests;
mod motion;
pub use live::Live;
mod doty;
mod drf;
mod oauth;
mod outreach;
mod partnership;
mod password;
mod scheduling;
pub use oauth::OAuth;
mod points;
mod private_api;
mod recent_results;
mod replay;
mod resource;
mod round_contract;
mod routing;
pub use private_api::PrivateTokens;
pub use routing::app;
mod schedule;
mod school_admin;
mod school_profile;
mod schools;
mod search;
mod session;
mod site_admin;
mod standings;
mod stats;
mod summernote;
mod tag_filter;
mod team_profile;
mod tournament_import;
mod tournament_profile;
mod video;
mod web;

use std::{net::SocketAddr, path::Path, sync::Arc, time::Duration};

use axum::{
    Json,
    body::Bytes,
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
};
use chrono::{DateTime, Utc};
use moka::future::Cache;
use serde_json::json;
use sqlx::AnyPool;

const OTY_GUIDE: &str = include_str!("../../api/content/oty_guide.md");
const API_HEADERS: [(header::HeaderName, &str); 2] = [
    (header::VARY, "Accept, Cookie"),
    (header::ALLOW, "GET, HEAD, OPTIONS"),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Endpoint {
    Directory(schools::Directory),
    Schedule(i32),
}

struct WatchedWebpack {
    path: std::path::PathBuf,
    bundles: std::sync::RwLock<Arc<Vec<String>>>,
}

#[derive(Clone)]
pub struct AppState {
    pool: AnyPool,
    secret: Option<Arc<[u8]>>,
    private_tokens: Arc<PrivateTokens>,
    oauth: Option<Arc<OAuth>>,
    live: Option<Arc<Live>>,
    bundles: Arc<Vec<String>>,
    watched_webpack: Option<Arc<WatchedWebpack>>,
    current_season: i32,
    debater_tab_cards: bool,
    tournament_tab_card_search: bool,
    debater_partner_chart: bool,
    public_origin: Option<String>,
    trusted_proxy: Option<std::net::IpAddr>,
    allowed_hosts: Option<Arc<[String]>>,
    guide_modified: Option<String>,
    public_json: Cache<(String, Endpoint), cache::CachedBody>,
    memcached: Option<cache::Memcached>,
    elo: elo::Cache,
    merge_suggestions: admin::merge_suggestions::Cache,
    synthetic_suggestions: admin::synthetic::suggestions::Cache,
    cache_lifetime: Duration,
    import_cache: tournament_import::source::Cache,
    standings: Cache<(uuid::Uuid, String, i32), Arc<standings::Boards>>,
    replay: Cache<(uuid::Uuid, String, i32), Arc<replay::Replay>>,
    home: Cache<(uuid::Uuid, i32), Arc<standings::Boards>>,
    stats: Cache<(), Arc<serde_json::Value>>,
    recent_results: Cache<(), Arc<String>>,
    search: Cache<u32, Arc<search::SearchIndex>>,
    partnership_stats: Arc<partnership::stats::Cache>,
    claim_limits: claims::Limits,
    account_limits: account::limits::Limits,
    school_admin_limits: school_admin::debater::Limits,
    snapshots: Cache<(uuid::Uuid, String, i32, String), Arc<standings::Boards>>,
}

impl AppState {
    pub fn new(pool: AnyPool, current_season: i32) -> Self {
        Self::with_cache_lifetime(pool, current_season, Duration::from_secs(300))
    }

    pub fn with_cache_lifetime(pool: AnyPool, current_season: i32, lifetime: Duration) -> Self {
        let guide_modified = Path::new("api/content/oty_guide.md")
            .metadata()
            .and_then(|metadata| metadata.modified())
            .ok()
            .map(|time| DateTime::<Utc>::from(time).date_naive().to_string());
        Self {
            pool,
            secret: None,
            private_tokens: Arc::default(),
            oauth: None,
            live: None,
            bundles: Arc::default(),
            watched_webpack: None,
            current_season,
            debater_tab_cards: false,
            tournament_tab_card_search: false,
            debater_partner_chart: false,
            public_origin: None,
            trusted_proxy: None,
            allowed_hosts: None,
            guide_modified,
            snapshots: Cache::builder()
                .time_to_live(lifetime)
                .max_capacity(12)
                .build(),
            stats: Cache::builder()
                .time_to_live(if lifetime.is_zero() {
                    Duration::ZERO
                } else {
                    Duration::from_secs(3600)
                })
                .max_capacity(1)
                .build(),
            search: Cache::builder()
                .time_to_live(lifetime.min(Duration::from_secs(60)))
                .max_capacity(1)
                .build(),
            recent_results: Cache::builder()
                .time_to_live(if lifetime.is_zero() {
                    Duration::ZERO
                } else {
                    Duration::from_secs(86_400)
                })
                .max_capacity(1)
                .build(),
            home: Cache::builder()
                .time_to_live(lifetime)
                .max_capacity(24)
                .build(),
            replay: Cache::builder()
                .time_to_live(lifetime)
                .max_capacity(12)
                .build(),
            standings: Cache::builder()
                .time_to_live(lifetime)
                .max_capacity(24)
                .build(),
            public_json: Cache::builder()
                .expire_after(cache::BodyExpiry)
                .weigher(|_, body: &cache::CachedBody| {
                    u32::try_from(body.bytes.len()).unwrap_or(u32::MAX)
                })
                .max_capacity(16 * 1024 * 1024)
                .build(),
            memcached: None,
            elo: elo::Cache::new(!lifetime.is_zero()),
            merge_suggestions: admin::merge_suggestions::cache(!lifetime.is_zero()),
            synthetic_suggestions: admin::synthetic::suggestions::Cache::new(!lifetime.is_zero()),
            partnership_stats: Arc::default(),
            account_limits: account::limits::cache(),
            claim_limits: Cache::builder()
                .time_to_live(Duration::from_secs(86_400))
                .max_capacity(4096)
                .build(),
            school_admin_limits: Cache::builder()
                .time_to_live(Duration::from_secs(86_400))
                .max_capacity(4096)
                .build(),
            cache_lifetime: lifetime,
            import_cache: tournament_import::source::Cache::new(),
        }
    }

    pub fn with_debater_profile_features(mut self, tab_cards: bool, partner_chart: bool) -> Self {
        self.debater_tab_cards = tab_cards;
        self.debater_partner_chart = partner_chart;
        self
    }

    pub fn with_tournament_tab_card_search(mut self, enabled: bool) -> Self {
        self.tournament_tab_card_search = enabled;
        self
    }

    pub fn with_memcached(mut self, address: SocketAddr) -> Self {
        self.memcached = Some(cache::Memcached::new(address));
        self
    }

    pub fn with_session_secret(mut self, secret: String) -> Self {
        self.secret = Some(Arc::from(secret.into_bytes()));
        self
    }

    pub fn with_live(mut self, live: Live) -> Self {
        self.live = Some(Arc::new(live));
        self
    }

    pub fn with_oauth(mut self, oauth: OAuth) -> Self {
        self.oauth = Some(Arc::new(oauth));
        self
    }

    pub fn with_private_tokens(mut self, tokens: PrivateTokens) -> Self {
        self.private_tokens = Arc::new(tokens);
        self
    }

    pub fn with_webpack_stats(mut self, path: &Path) -> Result<Self, Box<dyn std::error::Error>> {
        self.bundles = Arc::new(web::bundles(path, false)?);
        Ok(self)
    }

    pub fn with_production_webpack_stats(
        mut self,
        path: &Path,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let bundles = web::bundles(path, true)
            .map_err(|error| format!("Could not load production webpack stats: {error}"))?;
        if bundles.is_empty() {
            return Err("Production webpack stats has no main JavaScript assets.".into());
        }
        self.bundles = Arc::new(bundles);
        Ok(self)
    }

    pub fn watch_webpack_stats(mut self, path: &Path) -> Self {
        self.watched_webpack = Some(Arc::new(WatchedWebpack {
            path: path.to_owned(),
            bundles: std::sync::RwLock::new(Arc::new(
                web::bundles(path, false).unwrap_or_default(),
            )),
        }));
        self
    }

    fn bundles(&self) -> Arc<Vec<String>> {
        if let Some(watch) = &self.watched_webpack {
            if let Ok(bundles) = web::bundles(&watch.path, false)
                && let Ok(mut current) = watch.bundles.write()
            {
                if **current != bundles {
                    *current = Arc::new(bundles);
                }
                return current.clone();
            }
            if let Ok(current) = watch.bundles.read() {
                return current.clone();
            }
        }
        self.bundles.clone()
    }

    pub async fn refresh_tournament_sources(&self) -> Result<usize, &'static str> {
        tournament_import::refresh_sources(self).await
    }

    pub fn with_trusted_proxy(mut self, address: std::net::IpAddr) -> Self {
        self.trusted_proxy = Some(address);
        self
    }

    pub fn with_public_origin(mut self, origin: &str) -> Result<Self, &'static str> {
        let url = url::Url::parse(origin).map_err(|_| "Invalid Black Rod public origin")?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host().is_none()
            || url.path() != "/"
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err("Black Rod public origin must be an HTTP or HTTPS origin");
        }
        self.public_origin = Some(url.origin().ascii_serialization());
        Ok(self)
    }

    pub fn with_allowed_hosts(mut self, hosts: &str) -> Self {
        self.allowed_hosts = Some(
            hosts
                .split(',')
                .map(str::trim)
                .filter(|host| !host.is_empty())
                .map(str::to_ascii_lowercase)
                .collect::<Arc<[_]>>(),
        );
        self
    }

    fn host_allowed(&self, headers: &HeaderMap) -> bool {
        let Some(allowed) = &self.allowed_hosts else {
            return true;
        };
        let Some(host) = headers
            .get(header::HOST)
            .and_then(|value| value.to_str().ok())
        else {
            return false;
        };
        if host.contains('@') {
            return false;
        }
        let Ok(authority) = host.parse::<axum::http::uri::Authority>() else {
            return false;
        };
        let host = authority.host().trim_end_matches('.').to_ascii_lowercase();
        allowed.iter().any(|rule| {
            rule == "*"
                || *rule == host
                || rule
                    .strip_prefix('.')
                    .is_some_and(|domain| host == domain || host.ends_with(rule))
        })
    }

    fn origin(&self, headers: &HeaderMap) -> Option<String> {
        if self.public_origin.is_some() {
            return self.public_origin.clone();
        }
        let host = headers.get(header::HOST)?.to_str().ok()?;
        let authority = host.parse::<axum::http::uri::Authority>().ok()?;
        (!host.contains('@') && !authority.host().is_empty()).then(|| format!("http://{host}"))
    }
}

fn is_django_integer(raw: &str) -> bool {
    !raw.is_empty() && raw.bytes().all(|byte| byte.is_ascii_digit())
}

async fn robots() -> &'static str {
    "User-agent: *\nAllow: /"
}

async fn oty_guide() -> &'static str {
    OTY_GUIDE.trim()
}

async fn oty_guide_options() -> Response {
    options(
        "Oty Guide Api",
        "Expose the OTY scoring explainer in a machine-readable format.",
    )
}

async fn oty_guide_api(
    State(state): State<AppState>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
) -> Response {
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let path = uri.path_and_query().map(|p| p.as_str()).unwrap_or_default();
    let season = state.current_season;
    let api_path = "/api/oty-guide/";
    api_response(Json(json!({
        "title": "APDA OTY Scoring Guide",
        "season": season.to_string(),
        "season_display": schedule::season_display(season),
        "format": "markdown",
        "body": OTY_GUIDE.trim(),
        "last_modified": state.guide_modified,
        "links": {
            "plain_text": format!("{origin}/llm/oty-guide/"),
            "llm_proxy": schedule::proxy_url(&origin, api_path),
            "self": schedule::proxy_url(&origin, path),
        },
    })))
}

fn api_response(body: impl IntoResponse) -> Response {
    (API_HEADERS, body).into_response()
}

fn json_error(status: StatusCode, detail: &str) -> Response {
    api_response((status, Json(json!({ "detail": detail }))))
}

fn options(name: &str, description: &str) -> Response {
    api_response(Json(json!({
        "name": name,
        "description": description,
        "renders": ["application/json", "text/html"],
        "parses": ["application/json", "application/x-www-form-urlencoded", "multipart/form-data"],
    })))
}

#[derive(Debug)]
enum ApiError {
    NotFound(&'static str),
    Database,
    Constraint,
    Serialization,
    InvalidStoredDate,
}

impl From<sqlx::Error> for ApiError {
    fn from(error: sqlx::Error) -> Self {
        eprintln!("API query failed: {error}");
        Self::Database
    }
}

impl From<serde_json::Error> for ApiError {
    fn from(error: serde_json::Error) -> Self {
        eprintln!("API response encoding failed: {error}");
        Self::Serialization
    }
}

async fn cached_json(state: AppState, headers: HeaderMap, item: Endpoint, path: &str) -> Response {
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let output = state
        .public_json
        .try_get_with((origin.clone(), item), async {
            let remote = state
                .memcached
                .as_ref()
                .filter(|_| !state.cache_lifetime.is_zero())
                .map(|cache| (cache, cache::Memcached::key("directory", &origin, item)));
            if let Some((cache, key)) = &remote
                && let Some(body) = cache.get(key).await
            {
                return Ok(body);
            }
            let body = match item {
                Endpoint::Directory(directory) => directory.json(&state, &origin).await,
                Endpoint::Schedule(season) => schedule::json(&state, &origin, season, path).await,
            };
            let body = Bytes::from(body?);
            if let Some((cache, key)) = &remote {
                cache.set(key, &body, state.cache_lifetime).await;
            }
            Ok(cache::CachedBody {
                bytes: body,
                lifetime: state.cache_lifetime,
            })
        })
        .await;
    match output {
        Ok(body) => api_response(([(header::CONTENT_TYPE, "application/json")], body.bytes)),
        Err(error) => match *error {
            ApiError::NotFound(detail) => json_error(StatusCode::NOT_FOUND, detail),
            _ => json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error."),
        },
    }
}
