mod account;
mod admin;
mod agent;
mod autocomplete;
mod cache;
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
pub use private_api::PrivateTokens;
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
    Json, Router,
    body::Bytes,
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::get,
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

#[derive(Clone)]
pub struct AppState {
    pool: AnyPool,
    secret: Option<Arc<[u8]>>,
    private_tokens: Arc<PrivateTokens>,
    oauth: Option<Arc<OAuth>>,
    live: Option<Arc<Live>>,
    bundles: Arc<Vec<String>>,
    current_season: i32,
    debater_tab_cards: bool,
    debater_partner_chart: bool,
    public_origin: Option<String>,
    guide_modified: Option<String>,
    public_json: Cache<(String, Endpoint), cache::CachedBody>,
    memcached: Option<cache::Memcached>,
    elo: elo::Cache,
    merge_suggestions: admin::merge_suggestions::Cache,
    cache_lifetime: Duration,
    import_cache: tournament_import::source::Cache,
    standings: Cache<(String, i32), Arc<standings::Boards>>,
    replay: Cache<(String, i32), Arc<replay::Replay>>,
    home: Cache<i32, Arc<standings::Boards>>,
    stats: Cache<(), Arc<serde_json::Value>>,
    recent_results: Cache<(), Arc<String>>,
    search: Cache<(), Arc<search::SearchIndex>>,
    partnership_stats: Arc<partnership::stats::Cache>,
    school_admin_limits: school_admin::debater::Limits,
    snapshots: Cache<(String, i32, String), Arc<standings::Boards>>,
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
            current_season,
            debater_tab_cards: false,
            debater_partner_chart: false,
            public_origin: None,
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
            partnership_stats: Arc::default(),
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
        self.bundles = Arc::new(web::bundles(path)?);
        Ok(self)
    }

    pub async fn refresh_tournament_sources(&self) -> usize {
        tournament_import::refresh_sources(self).await
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

    fn origin(&self, headers: &HeaderMap) -> Option<String> {
        if self.public_origin.is_some() {
            return self.public_origin.clone();
        }
        let host = headers.get(header::HOST)?.to_str().ok()?;
        let authority = host.parse::<axum::http::uri::Authority>().ok()?;
        (!host.contains('@') && !authority.host().is_empty()).then(|| format!("http://{host}"))
    }
}

fn api_routes() -> Router<AppState> {
    Router::new()
        .route("/admin", get(append_slash))
        .route("/admin/", get(site_admin::dispatch))
        .route(
            "/admin/login/",
            get(site_admin::sign_in).post(site_admin::sign_in),
        )
        .route("/admin/logout/", axum::routing::post(site_admin::logout))
        .route(
            "/admin/{*path}",
            get(site_admin::dispatch).post(site_admin::dispatch),
        )
        .route(
            "/api/schools/",
            get(schools::active).options(schools::active_options),
        )
        .route(
            "/api/schools/{school}/detail/",
            get(school_profile::detail).options(school_profile::detail_options),
        )
        .route(
            "/api/teams/{team}/detail/",
            get(team_profile::detail).options(team_profile::detail_options),
        )
        .route(
            "/api/tournaments/{tournament}/detail/",
            get(tournament_profile::detail).options(tournament_profile::detail_options),
        )
        .route(
            "/api/debaters/{debater}/detail/",
            get(debater_profile::detail).options(debater_profile::detail_options),
        )
        .route(
            "/api/schools/all/",
            get(schools::all).options(schools::all_options),
        )
        .route(
            "/api/debaters/{school}/",
            get(schools::debaters).options(schools::debaters_options),
        )
        .route(
            "/api/schedule/",
            get(schedule::schedule).options(schedule::schedule_options),
        )
        .route(
            "/api/oty-guide/",
            get(oty_guide_api).options(oty_guide_options),
        )
        .route(
            "/api/standings/",
            get(standings::standings).options(standings::standings_options),
        )
        .route(
            "/api/standings/replay/",
            get(replay::handle).options(replay::replay_options),
        )
        .route(
            "/api/standings/through-date/",
            get(replay::handle).options(replay::through_options),
        )
        .route("/.well-known/ai-plugin.json", get(discovery::manifest))
        .route("/.well-known/openapi.json", get(discovery::schema))
}

fn local_api(state: AppState) -> Router {
    api_routes()
        .layer(axum::middleware::from_fn(api_protocol))
        .with_state(state)
}

pub fn app(state: AppState) -> Router {
    api_routes()
        .route("/api/standings", get(append_slash))
        .route("/api/standings/replay", get(append_slash))
        .route("/api/standings/through-date", get(append_slash))
        .route("/api/schools", get(append_slash))
        .route("/api/schools/all", get(append_slash))
        .route("/api/schools/{school}/detail", get(append_slash))
        .route("/api/teams/{team}/detail", get(append_slash))
        .route("/api/tournaments/{tournament}/detail", get(append_slash))
        .route("/api/debaters/{school}", get(append_slash))
        .route("/api/debaters/{debater}/detail", get(append_slash))
        .route("/api/schedule", get(append_slash))
        .route("/api/oty-guide", get(append_slash))
        .route("/", get(web::show))
        .route("/live", get(live::page))
        .route("/core/my-profile/", get(claims::profile))
        .route(
            "/core/debater/{debater}/edit-profile/",
            get(claims::edit::endpoint).post(claims::edit::endpoint),
        )
        .route(
            "/core/claim-debater/request/",
            get(claims::create).post(claims::create),
        )
        .route(
            "/core/claim-debater/review/",
            get(claims::review).post(claims::review),
        )
        .route(
            "/core/merge-debaters/request/",
            get(merge_requests::page).post(merge_requests::page),
        )
        .route(
            "/core/merge-debaters/review/",
            get(merge_requests::page).post(merge_requests::page),
        )
        .route("/core/merge-debaters/request", get(append_slash))
        .route("/core/merge-debaters/review", get(append_slash))
        .route(
            "/core/debaters/autocomplete/merge",
            get(merge_requests::autocomplete),
        )
        .route("/search/", get(search::page))
        .route("/search", get(append_slash))
        .route("/stats/", get(stats::page))
        .route("/stats", get(append_slash))
        .route("/auth/login/", get(login::view).post(login::view))
        .route("/accounts/apdaonline/login/callback/", get(oauth::callback))
        .route(
            "/accounts/password/change/",
            get(account::change).post(account::change),
        )
        .route(
            "/accounts/password/set/",
            get(account::change).post(account::change),
        )
        .route("/accounts/password/change", get(append_slash))
        .route("/accounts/password/set", get(append_slash))
        .route(
            "/accounts/password/reset/",
            get(account::reset).post(account::reset),
        )
        .route("/accounts/password/reset/done/", get(account::reset))
        .route("/accounts/password/reset/key/done/", get(account::reset))
        .route(
            "/accounts/password/reset/key/{encoded}/",
            get(account::reset).post(account::reset),
        )
        .route("/auth/login", get(append_slash))
        .route("/auth/logout/", axum::routing::post(login::logout))
        .route("/auth/logout", get(append_slash))
        .route("/standings/replay/", get(web::show))
        .route("/standings/replay", get(append_slash))
        .route("/games/", get(web::show))
        .route("/immaculate-grid/", get(immaculate::daily))
        .route("/immaculate-grid", get(append_slash))
        .route(
            "/immaculate-grid/guess/",
            axum::routing::post(immaculate::guess),
        )
        .route("/immaculate-grid/answers/", get(immaculate::answers))
        .route(
            "/immaculate-grid/infinite/",
            get(immaculate::practice::page).post(immaculate::practice::page),
        )
        .route(
            "/immaculate-grid/infinite/guess/",
            axum::routing::post(immaculate::practice::guess),
        )
        .route("/immaculate-grid/search/", get(immaculate::search))
        .route(
            "/immaculate-grid/leaderboard/",
            get(immaculate::leaderboard),
        )
        .route(
            "/immaculate-grid/leaderboard/{player}/",
            get(immaculate::player_board),
        )
        .route("/core/schedules/", get(schedule::page))
        .route("/core/schedules", get(append_slash))
        .route("/core/schools/autocomplete", get(autocomplete::endpoint))
        .route(
            "/core/schools/autocomplete/claim",
            get(autocomplete::endpoint),
        )
        .route("/core/debaters/autocomplete", get(autocomplete::endpoint))
        .route(
            "/core/debater-alias-groups/autocomplete",
            get(autocomplete::endpoint),
        )
        .route("/core/teams/autocomplete", get(autocomplete::endpoint))
        .route(
            "/core/tournaments/autocomplete",
            get(autocomplete::endpoint),
        )
        .route(
            "/core/tournaments/all_autocomplete",
            get(autocomplete::endpoint),
        )
        .route("/core/schools/{school}", get(school_profile::page))
        .route("/core/schools/", get(directory::page))
        .route(
            "/core/schools/create",
            get(entity_forms::endpoint).post(entity_forms::endpoint),
        )
        .route(
            "/core/debaters/create",
            get(entity_forms::endpoint).post(entity_forms::endpoint),
        )
        .route("/core/debaters/", get(directory::page))
        .route(
            "/core/admin/scheduling/",
            get(scheduling::dashboard).post(scheduling::dashboard),
        )
        .route(
            "/core/admin/scheduling/workspace-data/",
            get(scheduling::data),
        )
        .route(
            "/core/admin/scheduling/save-browser-run/",
            axum::routing::post(scheduling::save_run),
        )
        .route(
            "/core/admin/scheduling/download/{kind}/",
            get(scheduling::csv),
        )
        .route(
            "/core/admin/scheduling/runs/{id}/output/",
            get(scheduling::output),
        )
        .route(
            "/core/generated-codes/",
            get(generated_code::endpoint).post(generated_code::endpoint),
        )
        .route("/core/generated-codes", get(append_slash))
        .route("/core/debaters/partnership-paths/", get(partnership::paths))
        .route(
            "/core/debaters/partnership-paths/autocomplete/",
            get(partnership::autocomplete),
        )
        .route(
            "/core/debaters/partnership-paths/network/",
            get(partnership::network::network),
        )
        .route(
            "/core/debaters/partnership-paths/network/data/",
            get(partnership::network::data),
        )
        .route(
            "/core/debaters/partnership-paths/network/paths/",
            get(partnership::network::data),
        )
        .route(
            "/core/debaters/partnership-paths/person/",
            get(partnership::stats::personal),
        )
        .route(
            "/core/debaters/partnership-paths/stats/",
            get(partnership::stats::stats),
        )
        .route(
            "/core/debaters/partnership-paths/stats/results/",
            get(partnership::stats::results),
        )
        .route(
            "/core/debaters/partnership-paths/stats/clear-cache/",
            axum::routing::post(partnership::stats::clear),
        )
        .route("/core/school-admin/", get(school_admin::dashboard))
        .route(
            "/core/school-admin/{school}/debater/create/",
            get(school_admin::debater::endpoint).post(school_admin::debater::endpoint),
        )
        .route(
            "/core/school-admin/debater/{debater}/edit/",
            get(school_admin::debater::endpoint).post(school_admin::debater::endpoint),
        )
        .route("/core/manage-school-admins/", get(school_admin::dashboard))
        .route(
            "/core/manage-school-admins/add/",
            axum::routing::post(school_admin::roles::action),
        )
        .route(
            "/core/manage-school-admins/remove/",
            axum::routing::post(school_admin::roles::action),
        )
        .route(
            "/core/manage-school-admins/primary/",
            axum::routing::post(school_admin::roles::action),
        )
        .route(
            "/core/user-autocomplete/",
            get(school_admin::roles::autocomplete),
        )
        .route("/core/elo/", get(elo::page).post(elo::page))
        .route("/core/elo", get(append_slash))
        .route(
            "/core/admin/elo-cache/invalidate/",
            get(admin::tools).post(elo::invalidate_view),
        )
        .route("/core/admin/elo-cache/invalidate", get(append_slash))
        .route("/core/admin-tools/", get(admin::tools))
        .route(
            "/core/admin/debater-aliases/",
            get(admin::debater_aliases::endpoint).post(admin::debater_aliases::endpoint),
        )
        .route("/core/admin/debater-aliases", get(append_slash))
        .route(
            "/core/admin/merge-suggestions/",
            get(admin::merge_suggestions::endpoint).post(admin::merge_suggestions::endpoint),
        )
        .route("/core/admin/merge-suggestions", get(append_slash))
        .route("/core/mittab-dashboard/", get(admin::mittab::page))
        .route(
            "/core/rankings-recompute/",
            get(admin::rankings::page).post(admin::rankings::page),
        )
        .route("/core/rankings-recompute", get(append_slash))
        .route("/core/mittab-dashboard", get(append_slash))
        .route(
            "/core/admin/synthetic-cleanup/",
            get(admin::synthetic::cleanup_page).post(admin::synthetic::cleanup_page),
        )
        .route("/core/admin/synthetic-cleanup", get(append_slash))
        .route(
            "/core/admin/synthetic-resolve/",
            get(admin::tools).post(admin::synthetic::resolve),
        )
        .route("/core/admin/synthetic-resolve", get(append_slash))
        .route(
            "/core/admin/round-amendments/",
            get(admin::amendment::upload)
                .post(admin::amendment::upload)
                .layer(axum::extract::DefaultBodyLimit::max(64 * 1024 * 1024)),
        )
        .route("/core/admin/round-amendments", get(append_slash))
        .route(
            "/core/admin/tournament-pipeline-audit/",
            get(admin::tournament_audit::overview),
        )
        .route("/core/admin/tournament-pipeline-audit", get(append_slash))
        .route(
            "/core/admin/tournament-audit/{id}/",
            get(admin::tournament_audit::event),
        )
        .route("/core/admin/tournament-audit/{id}", get(append_slash))
        .route(
            "/core/admin/tournament-audit/{id}/round/new/",
            get(admin::tournament_audit::edit::page).post(admin::tournament_audit::edit::page),
        )
        .route(
            "/core/admin/tournament-audit/{id}/round/{round}/",
            get(admin::tournament_audit::edit::page).post(admin::tournament_audit::edit::page),
        )
        .route(
            "/core/admin/tournament-audit/{id}/round/{round}/delete/",
            axum::routing::post(admin::tournament_audit::edit::change),
        )
        .route(
            "/core/admin/tournament-audit/{id}/imports/{source}/delete/",
            axum::routing::post(admin::tournament_audit::edit::change),
        )
        .route(
            "/core/admin/tournament-audit/{id}/imports/{source}/move/",
            axum::routing::post(admin::tournament_audit::edit::change),
        )
        .route("/core/admin/speaks-stats/", get(admin::speaks::page))
        .route("/core/admin/speaks-stats", get(append_slash))
        .route("/core/admin-tools", get(append_slash))
        .route(
            "/core/admin/school-short-names/",
            get(admin::short_names::endpoint).post(admin::short_names::endpoint),
        )
        .route(
            "/core/schools/{school}/delete",
            get(entity_delete::endpoint).post(entity_delete::endpoint),
        )
        .route(
            "/core/debaters/{debater}/delete",
            get(entity_delete::endpoint).post(entity_delete::endpoint),
        )
        .route(
            "/core/teams/{team}/delete",
            get(entity_delete::endpoint).post(entity_delete::endpoint),
        )
        .route(
            "/core/tournaments/{tournament}/delete",
            get(entity_delete::endpoint).post(entity_delete::endpoint),
        )
        .route(
            "/core/schools/{school}/edit",
            get(entity_forms::endpoint).post(entity_forms::endpoint),
        )
        .route(
            "/core/debaters/{debater}/edit",
            get(entity_forms::endpoint).post(entity_forms::endpoint),
        )
        .route("/core/dino-judges/", get(directory::page))
        .route("/core/dino-judges", get(append_slash))
        .route("/core/dino-tos/", get(directory::page))
        .route("/core/dino-tos", get(append_slash))
        .route("/core/debaters/{debater}", get(debater_profile::page))
        .route(
            "/core/debaters/{debater}/tab-cards-csv",
            get(exports::career_tab_cards),
        )
        .route("/core/teams/", get(directory::page))
        .route(
            "/core/teams/{team}/edit",
            get(team_profile::edit::endpoint).post(team_profile::edit::endpoint),
        )
        .route("/core/tournaments/", get(directory::page))
        .route(
            "/core/tournaments/import/",
            get(tournament_import::source::page).post(tournament_import::source::page),
        )
        .route("/core/tournaments/import", get(append_slash))
        .route(
            "/core/tournaments/data_entry/setup",
            get(tournament_import::results::page).post(tournament_import::results::page),
        )
        .route(
            "/core/tournaments/data_entry",
            get(tournament_import::results::page).post(tournament_import::results::page),
        )
        .route(
            "/core/tournaments/get_new_form",
            axum::routing::any(tournament_import::results::new_form),
        )
        .route(
            "/core/tournaments/mittab_bundle/upload",
            get(tournament_import::upload)
                .post(tournament_import::upload)
                .layer(axum::extract::DefaultBodyLimit::max(64 * 1024 * 1024)),
        )
        .route(
            "/core/tournaments/create",
            get(tournament_profile::edit::endpoint).post(tournament_profile::edit::endpoint),
        )
        .route(
            "/core/tournaments/{tournament}/edit",
            get(tournament_profile::edit::endpoint).post(tournament_profile::edit::endpoint),
        )
        .route("/core/tournaments", get(append_slash))
        .route("/core/widgets/recent-results/", get(recent_results::page))
        .route("/core/widgets/recent-results", get(append_slash))
        .route("/core/teams/{team}", get(team_profile::page))
        .route(
            "/core/tournaments/{tournament}",
            get(tournament_profile::page),
        )
        .route("/core/schools", get(append_slash))
        .route("/core/debaters", get(append_slash))
        .route("/core/teams", get(append_slash))
        .route("/games", get(append_slash))
        .nest_service(
            "/static",
            tower_http::services::ServeDir::new("apda/assets"),
        )
        .route(
            "/api/private/forum-profiles/",
            axum::routing::any(private_api::forum),
        )
        .route(
            "/api/private/debater-emails/",
            axum::routing::any(private_api::mittab),
        )
        .route(
            "/core/resources/tags_no_create",
            get(autocomplete::endpoint),
        )
        .route(
            "/core/resources/tags",
            get(autocomplete::endpoint).post(autocomplete::create_tag),
        )
        .route("/core/videos/tags_no_create", get(autocomplete::endpoint))
        .route(
            "/core/videos/tags",
            get(autocomplete::endpoint).post(autocomplete::create_tag),
        )
        .route(
            "/core/motions/topics_no_create",
            get(autocomplete::endpoint),
        )
        .route(
            "/core/motions/topics",
            get(autocomplete::endpoint).post(autocomplete::create_tag),
        )
        .route("/core/motions/", get(directory::page))
        .route("/core/motions", get(append_slash))
        .route("/core/motions/random", get(directory::page))
        .route(
            "/core/motions/import",
            get(motion::import::endpoint).post(motion::import::endpoint),
        )
        .route("/core/motions/{motion}", get(motion::detail))
        .route(
            "/core/motions/create",
            get(motion::edit::endpoint).post(motion::edit::endpoint),
        )
        .route(
            "/core/motions/{motion}/edit",
            get(motion::edit::endpoint).post(motion::edit::endpoint),
        )
        .route(
            "/core/motions/{motion}/status",
            axum::routing::post(motion::status),
        )
        .route("/core/videos/", get(directory::page))
        .route("/core/videos", get(append_slash))
        .route("/core/videos/random", get(directory::page))
        .route(
            "/core/videos/create",
            get(video::edit::endpoint).post(video::edit::endpoint),
        )
        .route(
            "/core/videos/{video}/edit",
            get(video::edit::endpoint).post(video::edit::endpoint),
        )
        .route(
            "/core/videos/{video}/delete",
            get(video::edit::endpoint).post(video::edit::endpoint),
        )
        .route("/core/videos/{video}", get(video::detail))
        .route("/core/resources/", get(directory::page))
        .route("/core/resources", get(append_slash))
        .route("/core/resources/my-resources", get(directory::page))
        .route("/core/resources/tags/{slug}", get(resource::tag))
        .route("/core/resources/{resource}", get(resource::detail))
        .route(
            "/core/resources/create",
            get(resource::edit::endpoint).post(resource::edit::endpoint),
        )
        .route(
            "/core/resources/{resource}/edit",
            get(resource::edit::endpoint).post(resource::edit::endpoint),
        )
        .route(
            "/core/resources/{resource}/delete",
            get(resource::edit::endpoint).post(resource::edit::endpoint),
        )
        .route("/core/videos/tags/{name}", get(video::tag))
        .route("/DOTY", get(doty::page))
        .route("/core/toty", get(directory::page))
        .route("/core/soty", get(directory::page))
        .route("/core/noty", get(directory::page))
        .route("/core/coty", get(directory::page))
        .route(
            "/summernote/upload_attachment/",
            get(summernote::upload).post(summernote::upload),
        )
        .route(
            "/django-summernote/{day}/{name}",
            get(summernote::attachment),
        )
        .route("/api/agent/v1/", axum::routing::any(agent::endpoint))
        .route("/api/agent/v1/guide/", axum::routing::any(agent::endpoint))
        .route(
            "/api/agent/v1/records/{resource}/",
            axum::routing::any(agent::endpoint),
        )
        .route(
            "/api/agent/v1/records/{resource}/{id}/",
            axum::routing::any(agent::endpoint),
        )
        .route(
            "/api/agent/v1/batches/",
            axum::routing::any(agent::endpoint),
        )
        .route(
            "/api/agent/v1/batches/{id}/",
            axum::routing::any(agent::endpoint),
        )
        .route(
            "/api/agent/v1/batches/{id}/undo/",
            axum::routing::any(agent::endpoint),
        )
        .route("/robots.txt", get(robots))
        .route("/llms.txt", get(discovery::documentation))
        .route("/llm/", get(discovery::proxy))
        .route("/llm", get(append_slash))
        .route("/llm/oty-guide/", get(oty_guide))
        .route("/llm/oty-guide", get(append_slash))
        .fallback(|| async { StatusCode::NOT_FOUND })
        .layer(axum::middleware::from_fn(api_protocol))
        .layer(axum::middleware::from_fn(security_headers))
        .with_state(state)
}

fn public_api_path(path: &str) -> bool {
    matches!(
        path,
        "/api/schools/"
            | "/api/schools/all/"
            | "/api/schedule/"
            | "/api/oty-guide/"
            | "/api/standings/"
            | "/api/standings/replay/"
            | "/api/standings/through-date/"
    ) || path
        .strip_prefix("/api/debaters/")
        .and_then(|tail| tail.strip_suffix('/'))
        .is_some_and(is_django_integer)
        || [
            "/api/schools/",
            "/api/teams/",
            "/api/tournaments/",
            "/api/debaters/",
        ]
        .into_iter()
        .any(|prefix| {
            path.strip_prefix(prefix)
                .and_then(|tail| tail.strip_suffix("/detail/"))
                .is_some_and(is_django_integer)
        })
}

async fn api_protocol(request: Request, next: axum::middleware::Next) -> Response {
    if !public_api_path(request.uri().path()) {
        return next.run(request).await;
    }
    let head = *request.method() == Method::HEAD;
    let format = url::form_urlencoded::parse(request.uri().query().unwrap_or_default().as_bytes())
        .filter(|(name, _)| name == "format")
        .map(|(_, value)| value)
        .last();
    let mut indent = 0;
    let mut suitable = false;
    for media in request
        .headers()
        .get_all(header::ACCEPT)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
    {
        let mut parts = media.split(';');
        if let Some("*/*" | "application/*" | "application/json" | "text/html" | "text/*") =
            parts.next().map(str::trim)
        {
            suitable = true;
            indent = parts
                .filter_map(|part| part.trim().strip_prefix("indent="))
                .filter_map(|value| value.trim_matches('"').parse::<u8>().ok())
                .map(|width| width.min(8))
                .next()
                .unwrap_or(indent);
        }
    }
    let response = if format
        .as_deref()
        .is_some_and(|format| !matches!(format, "" | "json" | "api"))
    {
        negotiation_error(StatusCode::NOT_FOUND, "Not found.")
    } else if !suitable && request.headers().contains_key(header::ACCEPT) {
        negotiation_error(
            StatusCode::NOT_ACCEPTABLE,
            "Could not satisfy the request Accept header.",
        )
    } else if !matches!(
        *request.method(),
        Method::GET | Method::HEAD | Method::OPTIONS
    ) {
        json_error(
            StatusCode::METHOD_NOT_ALLOWED,
            &format!("Method \"{}\" not allowed.", request.method()),
        )
    } else {
        next.run(request).await
    };
    if head {
        return response.map(|_| axum::body::Body::empty());
    }
    if indent == 0
        || response
            .headers()
            .get(header::CONTENT_TYPE)
            .is_none_or(|value| value != "application/json")
    {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, usize::MAX).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let value: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(_) => return Response::from_parts(parts, bytes.into()),
    };
    let mut output = Vec::new();
    let spacing = [b' '; 8];
    let formatter = serde_json::ser::PrettyFormatter::with_indent(&spacing[..usize::from(indent)]);
    let mut serializer = serde_json::Serializer::with_formatter(&mut output, formatter);
    if serde::Serialize::serialize(&value, &mut serializer).is_err() {
        return Response::from_parts(parts, bytes.into());
    }
    parts.headers.remove(header::CONTENT_LENGTH);
    Response::from_parts(parts, output.into())
}

async fn security_headers(request: Request, next: axum::middleware::Next) -> Response {
    let may_embed = request.uri().path() == "/core/widgets/recent-results/";
    let mut response = next.run(request).await;
    let redirect = response.status() == StatusCode::MOVED_PERMANENTLY;
    let embeddable_response = may_embed && response.status().is_success();
    let headers = response.headers_mut();
    if !redirect && !embeddable_response {
        headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    }
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(
        "cross-origin-opener-policy",
        HeaderValue::from_static("same-origin"),
    );
    response
}

async fn append_slash(OriginalUri(uri): OriginalUri) -> Response {
    if let Some(id) = uri.path().strip_prefix("/api/debaters/")
        && !is_django_integer(id)
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let mut location = uri.path().to_owned();
    location.push('/');
    if let Some(query) = uri.query() {
        location.push('?');
        location.push_str(query);
    }
    (
        StatusCode::MOVED_PERMANENTLY,
        [
            (header::LOCATION, location),
            (header::CONTENT_TYPE, "text/html; charset=utf-8".into()),
        ],
    )
        .into_response()
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

fn negotiation_error(status: StatusCode, detail: &str) -> Response {
    let mut response = json_error(status, detail);
    response
        .headers_mut()
        .insert(header::VARY, HeaderValue::from_static("Accept"));
    response
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
