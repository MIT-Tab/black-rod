mod protocol;

pub(crate) use protocol::public_api_path;
use protocol::{api_protocol, append_slash, report_server_errors, security_headers, validate_host};

use axum::{Router, http::StatusCode, routing::get};

use crate::{
    AppState, account, admin, agent, autocomplete, claims, debater_profile, directory, discovery,
    doty, elo, entity_delete, entity_forms, exports, generated_code, immaculate, live, login,
    merge_requests, motion, oauth, oty_guide, oty_guide_api, oty_guide_options, partnership,
    private_api, recent_results, replay, resource, robots, schedule, scheduling, school_admin,
    school_profile, schools, search, site_admin, standings, stats, summernote, team_profile,
    tournament_import, tournament_profile, video, web,
};

fn api_routes() -> Router<AppState> {
    Router::new()
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

pub(crate) fn local_api(state: AppState) -> Router {
    api_routes()
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            api_protocol,
        ))
        .with_state(state)
}

pub fn app(state: AppState) -> Router {
    let router = api_routes()
        .route("/admin", get(append_slash))
        .route("/admin/", get(site_admin::dispatch))
        .route(
            "/admin/login/",
            get(site_admin::sign_in).post(site_admin::sign_in),
        )
        .route("/admin/logout/", axum::routing::post(site_admin::logout))
        .route(
            "/admin/{*path}",
            get(site_admin::dispatch)
                .post(site_admin::dispatch)
                .layer(axum::extract::DefaultBodyLimit::max(65 * 1024 * 1024)),
        )
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
        .route(
            "/accounts/login/",
            get(account::entrance).post(account::entrance),
        )
        .route(
            "/accounts/login/code/confirm/",
            axum::routing::any(account::unavailable_login_code),
        )
        .route("/accounts/login/code/confirm", get(append_slash))
        .route("/accounts/login", get(append_slash))
        .route(
            "/accounts/signup/",
            get(account::entrance).post(account::entrance),
        )
        .route("/accounts/signup", get(append_slash))
        .route(
            "/accounts/logout/",
            get(account::entrance).post(account::entrance),
        )
        .route("/accounts/logout", get(append_slash))
        .route("/accounts/inactive/", get(account::entrance))
        .route("/accounts/confirm-email/", get(account::entrance))
        .route(
            "/accounts/confirm-email/{key}/",
            get(account::emails).post(account::emails),
        )
        .route(
            "/accounts/email/",
            get(account::emails).post(account::emails),
        )
        .route("/accounts/email", get(append_slash))
        .route(
            "/accounts/reauthenticate/",
            get(account::reauthenticate).post(account::reauthenticate),
        )
        .route(
            "/accounts/3rdparty/",
            get(account::connections).post(account::connections),
        )
        .route("/accounts/social/connections/", get(account::connections))
        .route("/accounts/social/login/error/", get(oauth::legacy))
        .route("/accounts/social/login/cancelled/", get(oauth::legacy))
        .route("/accounts/social/signup/", get(oauth::legacy))
        .route("/accounts/3rdparty/login/error/", get(account::entrance))
        .route(
            "/accounts/3rdparty/login/cancelled/",
            get(account::entrance),
        )
        .route(
            "/accounts/3rdparty/signup/",
            get(oauth::social_signup).post(oauth::social_signup),
        )
        .route(
            "/accounts/apdaonline/login/",
            get(oauth::provider).post(oauth::provider),
        )
        .route("/accounts/apdaonline/login", get(append_slash))
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
        .route(
            "/auth/password_change/",
            get(account::legacy_passwords).post(account::legacy_passwords),
        )
        .route(
            "/auth/password_change/done/",
            get(account::legacy_passwords),
        )
        .route(
            "/auth/password_reset/",
            get(account::legacy_passwords).post(account::legacy_passwords),
        )
        .route("/auth/password_reset/done/", get(account::legacy_passwords))
        .route("/auth/reset/done/", get(account::legacy_passwords))
        .route(
            "/auth/reset/{uid}/{token}/",
            get(account::legacy_passwords).post(account::legacy_passwords),
        )
        .route("/auth/password_change", get(append_slash))
        .route("/auth/password_reset", get(append_slash))
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
            "/core/school-admin/{school}/debaters/",
            get(school_admin::debater_list),
        )
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
        .route(
            "/core/admin/synthetic-resolution-suggestions/",
            get(admin::synthetic::suggestions::endpoint)
                .post(admin::synthetic::suggestions::endpoint),
        )
        .route(
            "/core/admin/synthetic-resolution-suggestions",
            get(append_slash),
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
        .route("/summernote/editor/{id}/", get(summernote::editor))
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
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            api_protocol,
        ))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            validate_host,
        ))
        .layer(axum::middleware::from_fn(security_headers))
        .with_state(state);
    if sentry::Hub::current().client().is_some() {
        router.layer(axum::middleware::from_fn(report_server_errors))
    } else {
        router
    }
}
