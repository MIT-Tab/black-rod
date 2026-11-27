mod template_compat;

use std::{fs, path::Path, sync::LazyLock};

use crate::standings::{self, View};
use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::{AutoEscape, Environment, context};
use serde::Serialize;
use serde_json::Value;
use template_compat::adapt_template;

use crate::{
    AppState,
    schedule::{requested_season, season_display},
    session::User,
};

const VIEWS: &[(&str, &str)] = &[
    (
        "school_admin/merge_debater_request_form.html",
        include_str!("../../core/templates/school_admin/merge_debater_request_form.html"),
    ),
    (
        "admin/merge_debater_requests.html",
        include_str!("../../core/templates/admin/merge_debater_requests.html"),
    ),
    (
        "autocomplete_media.html",
        include_str!("../templates/autocomplete_media.html"),
    ),
    (
        "school_admin/merge_controls.html",
        include_str!("../templates/merge_controls.html"),
    ),
    (
        "site_admin/object_history.html",
        include_str!("../templates/site_admin/object_history.html"),
    ),
    (
        "site_admin/delete_confirmation.html",
        include_str!("../templates/site_admin/delete_confirmation.html"),
    ),
    (
        "site_admin/change_form.html",
        include_str!("../templates/site_admin/change_form.html"),
    ),
    (
        "django_summernote/widget_iframe_editor.html",
        include_str!("../../core/templates/django_summernote/widget_iframe_editor.html"),
    ),
    (
        "django_summernote/widget_media.html",
        include_str!("../templates/django_summernote/widget_media.html"),
    ),
    (
        "django_summernote/widget_inplace.html",
        include_str!("../templates/django_summernote/widget_inplace.html"),
    ),
    (
        "django_summernote/widget_common.html",
        include_str!("../templates/django_summernote/widget_common.html"),
    ),
    (
        "rest_framework/browse.html",
        include_str!("../templates/rest_framework/browse.html"),
    ),
    (
        "site_admin/exchange.html",
        include_str!("../templates/site_admin/exchange.html"),
    ),
    (
        "site_admin/fieldset.html",
        include_str!("../templates/site_admin/fieldset.html"),
    ),
    (
        "site_admin/inline.html",
        include_str!("../templates/site_admin/inline.html"),
    ),
    (
        "site_admin/inline_fields.html",
        include_str!("../templates/site_admin/inline_fields.html"),
    ),
    (
        "site_admin/submit.html",
        include_str!("../templates/site_admin/submit.html"),
    ),
    (
        "site_admin/password_self.html",
        include_str!("../templates/site_admin/password_self.html"),
    ),
    (
        "site_admin/password_other.html",
        include_str!("../templates/site_admin/password_other.html"),
    ),
    (
        "site_admin/password_fields.html",
        include_str!("../templates/site_admin/password_fields.html"),
    ),
    (
        "site_admin/widget.html",
        include_str!("../templates/site_admin/widget.html"),
    ),
    (
        "site_admin/agent_credential_issued.html",
        include_str!("../templates/site_admin/agent_credential_issued.html"),
    ),
    (
        "site_admin/base.html",
        include_str!("../templates/site_admin/base.html"),
    ),
    (
        "site_admin/base_site.html",
        include_str!("../templates/site_admin/base_site.html"),
    ),
    (
        "site_admin/index.html",
        include_str!("../templates/site_admin/index.html"),
    ),
    (
        "site_admin/app_index.html",
        include_str!("../templates/site_admin/app_index.html"),
    ),
    (
        "site_admin/app_list.html",
        include_str!("../templates/site_admin/app_list.html"),
    ),
    (
        "site_admin/color_theme_toggle.html",
        include_str!("../templates/site_admin/color_theme_toggle.html"),
    ),
    (
        "site_admin/nav_sidebar.html",
        include_str!("../templates/site_admin/nav_sidebar.html"),
    ),
    (
        "site_admin/login.html",
        include_str!("../templates/site_admin/login.html"),
    ),
    (
        "site_admin/change_list.html",
        include_str!("../templates/site_admin/change_list.html"),
    ),
    (
        "account/legacy_passwords.html",
        include_str!("../templates/account/legacy_passwords.html"),
    ),
    (
        "account/reset.html",
        include_str!("../templates/account/reset.html"),
    ),
    (
        "socialaccount/signup.html",
        include_str!("../templates/socialaccount/signup.html"),
    ),
    (
        "socialaccount/login.html",
        include_str!("../../core/templates/socialaccount/login.html"),
    ),
    (
        "account/reauthenticate.html",
        include_str!("../templates/account/reauthenticate.html"),
    ),
    (
        "account/connections.html",
        include_str!("../templates/account/connections.html"),
    ),
    (
        "account/email.html",
        include_str!("../templates/account/email.html"),
    ),
    (
        "account/entrance.html",
        include_str!("../templates/account/entrance.html"),
    ),
    (
        "account/layout.html",
        include_str!("../templates/account/layout.html"),
    ),
    (
        "account/password.html",
        include_str!("../templates/account/password.html"),
    ),
    (
        "admin/rankings_recompute.html",
        include_str!("../../core/templates/admin/rankings_recompute.html"),
    ),
    (
        "admin/debater_alias_suggestions.html",
        include_str!("../../core/templates/admin/debater_alias_suggestions.html"),
    ),
    (
        "admin/merge_suggestions.html",
        include_str!("../../core/templates/admin/merge_suggestions.html"),
    ),
    (
        "admin/synthetic_resolution_suggestions.html",
        include_str!("../../core/templates/admin/synthetic_resolution_suggestions.html"),
    ),
    (
        "admin/mittab_dashboard.html",
        include_str!("../../core/templates/admin/mittab_dashboard.html"),
    ),
    (
        "admin/synthetic_cleanup.html",
        include_str!("../../core/templates/admin/synthetic_cleanup.html"),
    ),
    (
        "elo/dashboard.html",
        include_str!("../../core/templates/elo/dashboard.html"),
    ),
    (
        "admin/tournament_round_ballot.html",
        include_str!("../../core/templates/admin/tournament_round_ballot.html"),
    ),
    (
        "admin/tournament_ballot_control.html",
        include_str!("../templates/tournament_ballot_control.html"),
    ),
    (
        "admin/tournament_audit_detail.html",
        include_str!("../../core/templates/admin/tournament_audit_detail.html"),
    ),
    (
        "admin/tournament_pipeline_audit.html",
        include_str!("../../core/templates/admin/tournament_pipeline_audit.html"),
    ),
    (
        "tournaments/import_sources.html",
        include_str!("../../core/templates/tournaments/import_sources.html"),
    ),
    (
        "tournaments/data_entry.html",
        include_str!("../../core/templates/tournaments/data_entry.html"),
    ),
    (
        "tournaments/data_entry_setup.html",
        include_str!("../../core/templates/tournaments/data_entry_setup.html"),
    ),
    (
        "tournaments/includes/data_entry_tabs.html",
        include_str!("../../core/templates/tournaments/includes/data_entry_tabs.html"),
    ),
    (
        "tournaments/includes/data_entry_sidebar.html",
        include_str!("../../core/templates/tournaments/includes/data_entry_sidebar.html"),
    ),
    (
        "tournaments/includes/formset_tab_pane.html",
        include_str!("../../core/templates/tournaments/includes/formset_tab_pane.html"),
    ),
    (
        "tournaments/includes/result_row.html",
        include_str!("../../core/templates/tournaments/includes/formset_row_new.html"),
    ),
    (
        "tournaments/includes/result_row_ajax.html",
        include_str!("../../core/templates/tournaments/includes/formset_row.html"),
    ),
    (
        "tournaments/includes/result_fields.html",
        include_str!("../templates/tournament_result_fields.html"),
    ),
    (
        "tournaments/includes/result_widgets.html",
        include_str!("../templates/tournament_result_widgets.html"),
    ),
    (
        "tournaments/includes/result_setup.html",
        include_str!("../templates/tournament_result_setup.html"),
    ),
    (
        "tournaments/includes/result_autoqual.html",
        include_str!("../templates/tournament_result_autoqual.html"),
    ),
    (
        "tournaments/mittab_bundle_upload.html",
        include_str!("../../core/templates/tournaments/mittab_bundle_upload.html"),
    ),
    (
        "tournaments/mittab_bundle_file.html",
        include_str!("../templates/mittab_bundle_file.html"),
    ),
    (
        "admin/speaks_statistics.html",
        include_str!("../../core/templates/admin/speaks_statistics.html"),
    ),
    (
        "admin/includes/speaks_record_distribution.html",
        include_str!("../../core/templates/admin/includes/speaks_record_distribution.html"),
    ),
    (
        "admin/includes/speaks_percentile_chart.html",
        include_str!("../../core/templates/admin/includes/speaks_percentile_chart.html"),
    ),
    (
        "school_admin/debater_form.html",
        include_str!("../../core/templates/school_admin/debater_form.html"),
    ),
    (
        "school_admin/debater_fields.html",
        include_str!("../templates/school_admin_debater_fields.html"),
    ),
    (
        "school_admin/dashboard.html",
        include_str!("../../core/templates/school_admin/dashboard.html"),
    ),
    (
        "school_admin/superuser_management.html",
        include_str!("../../core/templates/school_admin/superuser_management.html"),
    ),
    (
        "tournaments/create.html",
        include_str!("../templates/tournament_create.html"),
    ),
    (
        "tournaments/update.html",
        include_str!("../../core/templates/tournaments/update.html"),
    ),
    (
        "tournaments/fields.html",
        include_str!("../templates/tournament_fields.html"),
    ),
    (
        "teams/update.html",
        include_str!("../../core/templates/teams/update.html"),
    ),
    (
        "teams/fields.html",
        include_str!("../templates/team_fields.html"),
    ),
    (
        "core/entity_fields.html",
        include_str!("../templates/entity_fields.html"),
    ),
    (
        "schools/update.html",
        include_str!("../../core/templates/schools/update.html"),
    ),
    (
        "debaters/update.html",
        include_str!("../../core/templates/debaters/update.html"),
    ),
    (
        "core/create_entity.html",
        include_str!("../templates/create_entity.html"),
    ),
    (
        "videos/create.html",
        include_str!("../../core/templates/videos/create.html"),
    ),
    (
        "videos/update.html",
        include_str!("../../core/templates/videos/update.html"),
    ),
    (
        "videos/delete.html",
        include_str!("../../core/templates/videos/delete.html"),
    ),
    (
        "videos/fields.html",
        include_str!("../templates/video_fields.html"),
    ),
    (
        "debaters/personal_partnership_stats.html",
        include_str!("../../core/templates/debaters/personal_partnership_stats.html"),
    ),
    (
        "debaters/partnership_stats.html",
        include_str!("../../core/templates/debaters/partnership_stats.html"),
    ),
    (
        "debaters/_partnership_stats_results.html",
        include_str!("../../core/templates/debaters/_partnership_stats_results.html"),
    ),
    (
        "debaters/_partnership_rankings.html",
        include_str!("../../core/templates/debaters/_partnership_rankings.html"),
    ),
    (
        "debaters/partnership_paths.html",
        include_str!("../../core/templates/debaters/partnership_paths.html"),
    ),
    (
        "debaters/partnership_network.html",
        include_str!("../../core/templates/debaters/partnership_network.html"),
    ),
    (
        "debaters/_partnership_nav.html",
        include_str!("../../core/templates/debaters/_partnership_nav.html"),
    ),
    (
        "debaters/partnership_person.html",
        include_str!("../templates/partnership_person.html"),
    ),
    (
        "debaters/network_filter.html",
        include_str!("../templates/network_filter.html"),
    ),
    (
        "core/generated_codes.html",
        include_str!("../../core/templates/core/generated_codes.html"),
    ),
    (
        "motions/import.html",
        include_str!("../../core/templates/motions/import.html"),
    ),
    (
        "admin/school_short_name_audit.html",
        include_str!("../../core/templates/admin/school_short_name_audit.html"),
    ),
    (
        "admin/admin_tools.html",
        include_str!("../../core/templates/admin/admin_tools.html"),
    ),
    (
        "resources/create.html",
        include_str!("../../core/templates/resources/create.html"),
    ),
    (
        "resources/update.html",
        include_str!("../../core/templates/resources/update.html"),
    ),
    (
        "resources/delete.html",
        include_str!("../../core/templates/resources/delete.html"),
    ),
    (
        "resources/fields.html",
        include_str!("../templates/resource_fields.html"),
    ),
    (
        "base/delete.html",
        include_str!("../../core/templates/base/delete.html"),
    ),
    (
        "admin/doty.html",
        include_str!("../../core/templates/admin/doty.html"),
    ),
    (
        "admin/includes/doty_leaderboard.html",
        include_str!("../../core/templates/admin/includes/doty_leaderboard.html"),
    ),
    (
        "admin/scheduling_dashboard.html",
        include_str!("../../core/templates/admin/scheduling_dashboard.html"),
    ),
    (
        "debater/debater_profile_edit.html",
        include_str!("../../core/templates/debater/debater_profile_edit.html"),
    ),
    (
        "debater/profile_label.html",
        include_str!("../templates/profile_label.html"),
    ),
    (
        "debater/profile_field.html",
        include_str!("../templates/profile_field.html"),
    ),
    (
        "base/_font_awesome.html",
        include_str!("../../core/templates/base/_font_awesome.html"),
    ),
    (
        "debater/my_profile.html",
        include_str!("../../core/templates/debater/my_profile.html"),
    ),
    (
        "debater/claim_debater_request_form.html",
        include_str!("../../core/templates/debater/claim_debater_request_form.html"),
    ),
    (
        "debater/claim_selects.html",
        include_str!("../templates/claim_selects.html"),
    ),
    (
        "admin/claim_debater_requests.html",
        include_str!("../../core/templates/admin/claim_debater_requests.html"),
    ),
    ("400.html", include_str!("../../core/templates/400.html")),
    ("404.html", include_str!("../../core/templates/404.html")),
    ("500.html", include_str!("../../core/templates/500.html")),
    ("403.html", include_str!("../../core/templates/403.html")),
    (
        "search/search.html",
        include_str!("../../core/templates/search/search.html"),
    ),
    (
        "base/create.html",
        include_str!("../../core/templates/base/create.html"),
    ),
    (
        "motions/form.html",
        include_str!("../../core/templates/motions/form.html"),
    ),
    (
        "motions/fields.html",
        include_str!("../templates/motion_fields.html"),
    ),
    (
        "immaculate_grid/square_content.html",
        include_str!("../../core/templates/immaculate_grid/square_content.html"),
    ),
    (
        "immaculate_grid/review_dialog.html",
        include_str!("../../core/templates/immaculate_grid/review_dialog.html"),
    ),
    (
        "immaculate_grid/player_board.html",
        include_str!("../../core/templates/immaculate_grid/player_board.html"),
    ),
    (
        "immaculate_grid/leaderboard.html",
        include_str!("../../core/templates/immaculate_grid/leaderboard.html"),
    ),
    (
        "immaculate_grid/grid.html",
        include_str!("../../core/templates/immaculate_grid/grid.html"),
    ),
    (
        "immaculate_grid/leaderboard_tables.html",
        include_str!("../../core/templates/immaculate_grid/leaderboard_tables.html"),
    ),
    (
        "immaculate_grid/board_cells.html",
        include_str!("../../core/templates/immaculate_grid/board_cells.html"),
    ),
    (
        "resources/detail.html",
        include_str!("../../core/templates/resources/detail.html"),
    ),
    (
        "debaters/detail.html",
        include_str!("../../core/templates/debaters/detail.html"),
    ),
    (
        "motions/detail.html",
        include_str!("../../core/templates/motions/detail.html"),
    ),
    (
        "tournaments/recent_results_widget.html",
        include_str!("../../core/templates/tournaments/recent_results_widget.html"),
    ),
    (
        "videos/detail.html",
        include_str!("../../core/templates/videos/detail.html"),
    ),
    (
        "tournaments/detail.html",
        include_str!("../../core/templates/tournaments/detail.html"),
    ),
    (
        "base/results/team.html",
        include_str!("../../core/templates/base/results/team.html"),
    ),
    (
        "base/results/speaker.html",
        include_str!("../../core/templates/base/results/speaker.html"),
    ),
    (
        "core/stats.html",
        include_str!("../../core/templates/core/stats.html"),
    ),
    (
        "core/stats/_kpi_card.html",
        include_str!("../../core/templates/core/stats/_kpi_card.html"),
    ),
    (
        "core/stats/_chart_section.html",
        include_str!("../../core/templates/core/stats/_chart_section.html"),
    ),
    (
        "core/stats/_participation_table.html",
        include_str!("../../core/templates/core/stats/_participation_table.html"),
    ),
    (
        "core/stats/_school_table.html",
        include_str!("../../core/templates/core/stats/_school_table.html"),
    ),
    (
        "core/directory.html",
        include_str!("../templates/directory.html"),
    ),
    (
        "base/detail.html",
        include_str!("../../core/templates/base/detail.html"),
    ),
    (
        "base/_buttons.html",
        include_str!("../templates/buttons.html"),
    ),
    (
        "tournaments/schedule.html",
        include_str!("../../core/templates/tournaments/schedule.html"),
    ),
    (
        "schools/detail.html",
        include_str!("../../core/templates/schools/detail.html"),
    ),
    ("auth/login.html", include_str!("../templates/login.html")),
    (
        "base/base.html",
        include_str!("../../core/templates/base/base.html"),
    ),
    (
        "base/_nav.html",
        include_str!("../../core/templates/base/_nav.html"),
    ),
    (
        "base/_theme_head.html",
        include_str!("../../core/templates/base/_theme_head.html"),
    ),
    (
        "base/_theme_picker.html",
        include_str!("../../core/templates/base/_theme_picker.html"),
    ),
    (
        "base/standalone.html",
        include_str!("../../core/templates/base/standalone.html"),
    ),
    (
        "api/llm_proxy.html",
        include_str!("../../core/templates/api/llm_proxy.html"),
    ),
    (
        "teams/detail.html",
        include_str!("../../core/templates/teams/detail.html"),
    ),
    (
        "base/results/full_team.html",
        include_str!("../../core/templates/base/results/full_team.html"),
    ),
    (
        "core/live.html",
        include_str!("../../core/templates/core/live.html"),
    ),
    (
        "core/index.html",
        include_str!("../../core/templates/core/index.html"),
    ),
    (
        "core/replay.html",
        include_str!("../../core/templates/core/replay.html"),
    ),
    (
        "core/games.html",
        include_str!("../../core/templates/core/games.html"),
    ),
];

pub(crate) fn debug_assets() -> bool {
    static DEBUG: LazyLock<bool> = LazyLock::new(|| {
        std::env::var("DEBUG").map_or_else(
            |_| std::env::var("ENV").as_deref() != Ok("production"),
            |value| value == "1",
        )
    });
    *DEBUG
}

static TEMPLATES: LazyLock<Result<Environment<'static>, minijinja::Error>> = LazyLock::new(|| {
    let mut environment = Environment::new();
    environment.set_auto_escape_callback(|_| AutoEscape::Html);
    environment.set_keep_trailing_newline(true);
    environment.add_filter("vendor_asset", |asset: String| {
        if !debug_assets()
            && matches!(
                asset.as_str(),
                "admin/css/vendor/select2/select2.css"
                    | "admin/js/vendor/select2/select2.full.js"
                    | "autocomplete_light/autocomplete_light.js"
                    | "autocomplete_light/select2.js"
                    | "admin/js/vendor/jquery/jquery.js"
                    | "admin/js/vendor/xregexp/xregexp.js"
            )
            && let Some((name, extension)) = asset.rsplit_once('.')
        {
            return format!("{name}.min.{extension}");
        }
        asset
    });
    environment.add_filter("number", |number: minijinja::Value| {
        f64::try_from(number)
            .ok()
            .map(|number| number.to_string())
            .unwrap_or_default()
    });
    environment.add_filter("urlencode", |value: String| {
        url::form_urlencoded::byte_serialize(value.as_bytes())
            .collect::<String>()
            .replace('+', "%20")
            .replace("%2F", "/")
    });
    environment.add_filter("ordinal", |number: i64| {
        let suffix = match (number % 100, number % 10) {
            (11..=13, _) => "th",
            (_, 1) => "st",
            (_, 2) => "nd",
            (_, 3) => "rd",
            _ => "th",
        };
        format!("{number}{suffix}")
    });
    environment.add_filter("calendar_date", |value: String, format: String| {
        chrono::NaiveDate::parse_from_str(&value, "%Y-%m-%d")
            .map_or(String::new(), |date| date.format(&format).to_string())
    });
    environment.add_filter("grid_score", |value: minijinja::Value| {
        if value.is_none() {
            "—".to_owned()
        } else {
            value.to_string()
        }
    });
    let html_json = |value: &minijinja::Value| {
        serde_json::to_string(value)
            .unwrap_or_else(|_| "{}".to_owned())
            .replace('<', r"\u003C")
            .replace('>', r"\u003E")
            .replace('&', r"\u0026")
    };
    environment.add_filter("html_json", move |value: minijinja::Value| {
        minijinja::Value::from_safe_string(html_json(&value))
    });
    environment.add_filter("grid_miss_names", move |value: minijinja::Value| {
        let data = html_json(&value);
        minijinja::Value::from_safe_string(format!(
            r#"<script id="immaculate-miss-names" type="application/json">{data}</script>"#
        ))
    });
    environment.add_filter("long_date", |value: String| django_date(&value));
    environment.add_function("tag_url", |name: String| {
        let mut url = url::Url::parse("http://localhost/core/videos/tags/").unwrap();
        url.path_segments_mut().unwrap().pop_if_empty().push(&name);
        url.path().to_owned()
    });
    environment.add_filter("short_date", |value: String| {
        chrono::NaiveDate::parse_from_str(&value, "%Y-%m-%d")
            .map_or(String::new(), |date| date.format("%b %-d").to_string())
    });
    environment.add_filter(
        "award_place",
        |place: minijinja::Value, ghost: Option<bool>| {
            if ghost.unwrap_or(false) {
                return "Alternate".to_owned();
            }
            i64::try_from(place)
                .ok()
                .filter(|place| *place > 0)
                .map(crate::standings::place_display)
                .unwrap_or_default()
        },
    );
    environment.add_filter("doty_points", |value: f64| {
        if value.fract() == 0.0 {
            format!("{value:.0}")
        } else {
            format!("{value:.2}")
        }
    });
    environment.add_filter("one_decimal", |value: f64| {
        format!("{:.1}", (value * 10.0).round() / 10.0)
    });
    environment.add_filter(
        "yesno",
        |value: minijinja::Value| if value.is_true() { "Yes" } else { "No" },
    );
    environment.add_filter("intcomma", |value: i64| {
        let raw = value.to_string();
        let mut result = String::new();
        let digits = raw.trim_start_matches('-');
        if value < 0 {
            result.push('-');
        }
        for (pos, ch) in digits.chars().enumerate() {
            if pos > 0 && (digits.len() - pos).is_multiple_of(3) {
                result.push(',');
            }
            result.push(ch);
        }
        result
    });
    environment.add_filter("score_decimal", |value: Option<String>, places: usize| {
        use rust_decimal::{Decimal, RoundingStrategy};
        value
            .and_then(|text| text.parse::<Decimal>().ok())
            .map_or_else(
                || "—".to_owned(),
                |number| {
                    let rounded = number.round_dp_with_strategy(
                        places as u32,
                        RoundingStrategy::MidpointAwayFromZero,
                    );
                    format!("{rounded:.places$}")
                },
            )
    });
    for (name, text) in VIEWS {
        environment.add_template_owned(*name, adapt_template(name, text))?;
    }
    Ok(environment)
});

pub(crate) fn bundles(path: &Path) -> Result<Vec<String>, Box<dyn std::error::Error>> {
    let stats: Value = serde_json::from_slice(&fs::read(path)?)?;
    let Some(main) = stats["chunks"]["main"].as_array() else {
        return Err("Webpack stats has no main chunk".into());
    };
    Ok(main
        .iter()
        .filter_map(Value::as_str)
        .filter(|name| {
            name.ends_with(".js") && !name.ends_with(".hot-update.js") && !name.contains('/')
        })
        .map(|name| format!("/static/webpack_bundles/{name}"))
        .collect())
}

#[derive(Serialize)]
pub(crate) struct MenuItem {
    name: &'static str,
    url: &'static str,
    selected: bool,
    submenu: Vec<Self>,
}

impl MenuItem {
    fn link(name: &'static str, url: &'static str, actual_path: &str) -> Self {
        let selected = url == actual_path
            || (url == "/stats/" && actual_path == "/core/debaters/partnership-paths/stats/")
            || (url.starts_with("/core/")
                && url != "/core/debaters/partnership-paths/"
                && actual_path.starts_with(url));
        Self {
            name,
            url,
            selected,
            submenu: Vec::new(),
        }
    }

    fn dropdown(name: &'static str, entries: &[(&'static str, &'static str)], path: &str) -> Self {
        let submenu: Vec<_> = entries
            .iter()
            .map(|&(name, url)| Self::link(name, url, path))
            .collect();
        Self {
            name,
            url: "",
            selected: submenu.iter().any(|item| item.selected)
                && !(name == "Fun" && path == "/core/debaters/partnership-paths/stats/")
                && !(name == "My Account" && path == "/core/resources/create"),
            submenu,
        }
    }
}

pub(crate) fn menu(user: &User, path: &str) -> Vec<MenuItem> {
    let mut results = vec![
        ("Schools", "/core/schools/"),
        ("Debaters", "/core/debaters/"),
        ("Tournaments", "/core/tournaments/"),
        ("Teams", "/core/teams/"),
    ];
    if user.has_exclusive_pre_access {
        results.push(("ELO Rankings", "/core/elo/"));
    }
    let mut menu = vec![
        MenuItem::dropdown("Results", &results, path),
        MenuItem::dropdown(
            "Resources",
            &[
                ("Videos", "/core/videos/"),
                ("Motions", "/core/motions/"),
                ("Schedule", "/core/schedules/"),
                ("Resource Database", "/core/resources/"),
                ("Judge Database", "/core/dino-judges/"),
                ("TO Database", "/core/dino-tos/"),
            ],
            path,
        ),
        MenuItem::dropdown(
            "Fun",
            &[
                ("Debater paths", "/core/debaters/partnership-paths/"),
                ("Games", "/games/"),
                ("Stats", "/stats/"),
            ],
            path,
        ),
    ];
    if user.is_authenticated {
        let mut account = vec![
            ("My Profile", "/core/my-profile/"),
            ("Claim Debater Profile", "/core/claim-debater/request/"),
            ("Create Resource", "/core/resources/create"),
            ("My Resources", "/core/resources/my-resources"),
        ];
        if user.is_superuser || user.is_school_admin {
            account.extend([
                ("School Dashboard", "/core/school-admin/"),
                ("Merge Debaters", "/core/merge-debaters/request/"),
            ]);
        }
        menu.push(MenuItem::dropdown("My Account", &account, path));
    }
    if user.is_superuser {
        menu.push(MenuItem::dropdown(
            "Admin",
            &[
                ("Django Admin", "/admin/"),
                ("Admin Tools", "/core/admin-tools/"),
                ("Scheduling Workspace", "/core/admin/scheduling/"),
                (
                    "Tournament Data Audit",
                    "/core/admin/tournament-pipeline-audit/",
                ),
                ("Merge Debater Requests", "/core/merge-debaters/review/"),
                ("Claim Debater Requests", "/core/claim-debater/review/"),
                ("Manage School Admins", "/core/manage-school-admins/"),
                ("Potential Debater Links", "/core/admin/debater-aliases/"),
                ("Potential Debater Merges", "/core/admin/merge-suggestions/"),
            ],
            path,
        ));
    }
    menu.push(MenuItem::link("apda.online", "http://apda.online", path));
    menu.push(if user.is_authenticated {
        MenuItem::link("Logout", "/auth/logout/", path)
    } else {
        MenuItem::link("Login", "/auth/login/", path)
    });
    menu
}

pub(crate) async fn show(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let user = match crate::session::user(&state.pool, &headers, state.secret.as_deref()).await {
        Ok(user) => user,
        Err(error) => {
            eprintln!("Session lookup failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let raw_query = uri.query().unwrap_or_default();
    let query: std::collections::HashMap<_, _> =
        url::form_urlencoded::parse(raw_query.as_bytes()).collect();
    let path = uri.path();
    let season = requested_season(raw_query, state.current_season);
    let is_home = path == "/";
    let default = query
        .get("default")
        .map(|value| value.as_ref())
        .filter(|value| matches!(*value, "toty" | "soty" | "coty") || (is_home && *value == "noty"))
        .unwrap_or("toty");
    let home = if is_home {
        let data = state
            .home
            .try_get_with(
                season,
                standings::boards(&state.pool, season, "", View::Home),
            )
            .await;
        match data {
            Ok(data) => Some(data),
            Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        }
    } else {
        None
    };
    let rows = |key| {
        home.as_ref()
            .and_then(|boards| boards.iter().find(|(name, _)| *name == key))
            .map(|(_, rows)| rows.as_slice())
            .unwrap_or_default()
    };
    let template_name = match path {
        "/" => "core/index.html",
        "/standings/replay/" => "core/replay.html",
        _ => "core/games.html",
    };
    let seasons: Vec<_> = (2004..=state.current_season)
        .rev()
        .map(|year| (year.to_string(), season_display(year)))
        .collect();
    let left_menu = menu(&user, path);
    let live_sources = if is_home
        && season == state.current_season
        && let Some(live) = &state.live
    {
        live.active_sources(&state.pool, season, state.memcached.as_ref())
            .await
    } else {
        Vec::new()
    };
    let bindings = context! {
        request => context! { path, user, GET => query },
        left_menu,
        bundles => state.bundles().as_ref(),
        seasons,
        current_season => season.to_string(),
        season_display => season_display(season),
        replay_api_url => format!("/api/standings/replay/?season={season}"),
        live_sources,
        live_season => state.current_season.to_string(),
        render_noty => season <= 2020,
        using_online_quals => matches!(season, 2020 | 2021),
        online_qual_bar => 10,
        online_seasons => matches!(season, 2020 | 2021).then_some([("2021", "2021-22"), ("2020", "2020-21")]),
        toty => rows("toty"),
        soty => rows("soty"),
        coty => rows("coty"),
        noty => rows("noty"),
        online_quals => rows("online_quals"),
        default,
    };
    let rendered = render(template_name, bindings);
    match rendered {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Page template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) fn base_context(
    state: &AppState,
    user: &User,
    uri: &axum::http::Uri,
) -> minijinja::Value {
    let path = uri.path();
    let query: std::collections::HashMap<_, _> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let left_menu = menu(user, path);
    context! {
        request => context! { path, user, GET => query },
        user,
        bundles => state.bundles().as_ref(),
        left_menu,
        seasons => seasons(state.current_season),
    }
}

pub(crate) fn permission_denied(state: &AppState, user: &User, uri: &axum::http::Uri) -> Response {
    let content = render("403.html", base_context(state, user, uri))
        .unwrap_or_else(|_| "<h1>Permission denied</h1>".to_owned());
    (
        StatusCode::FORBIDDEN,
        [(header::VARY, "Cookie")],
        Html(content),
    )
        .into_response()
}

pub(crate) fn seasons(current: i32) -> Vec<(String, String)> {
    (2004..=current)
        .rev()
        .map(|year| (year.to_string(), season_display(year)))
        .collect()
}

pub(crate) fn render(name: &str, bindings: minijinja::Value) -> Result<String, String> {
    TEMPLATES
        .as_ref()
        .map_err(|error| error.to_string())
        .and_then(|environment| {
            environment
                .get_template(name)
                .and_then(|template| template.render(bindings))
                .map_err(|error| error.to_string())
        })
}

pub(crate) fn django_date(value: &str) -> String {
    use chrono::Datelike;
    const MONTHS: [&str; 12] = [
        "Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.",
        "Dec.",
    ];
    chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d").map_or_else(
        |_| value.to_owned(),
        |date| {
            format!(
                "{} {}, {}",
                MONTHS[date.month0() as usize],
                date.day(),
                date.year()
            )
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_templates_compile() {
        for (name, template) in VIEWS {
            let adapted = adapt_template(name, template);
            let environment = Environment::new();
            environment
                .template_from_str(&adapted)
                .unwrap_or_else(|error| {
                    let line = error.line().and_then(|line| adapted.lines().nth(line - 1));
                    panic!("{name}: {error}: {line:?}")
                });
        }
        assert!(TEMPLATES.is_ok());
    }
}
