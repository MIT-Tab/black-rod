use std::{fs, path::Path, sync::LazyLock};

use crate::standings::{self, View};
use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::{AutoEscape, Environment, context};
use regex::{Captures, Regex};
use serde::Serialize;
use serde_json::Value;

use crate::{
    AppState,
    schedule::{requested_season, season_display},
    session::User,
};

const VIEWS: &[(&str, &str)] = &[
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

static TEMPLATES: LazyLock<Result<Environment<'static>, minijinja::Error>> = LazyLock::new(|| {
    let mut environment = Environment::new();
    environment.set_auto_escape_callback(|_| AutoEscape::Html);
    environment.set_keep_trailing_newline(true);
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

fn adapt_debater_profile(template: &str) -> String {
    template
        .replace("{% url 'core:school_detail' pk=object.school.id %}", r#""{{ object.school.url }}""#)
        .replace("{% url 'api:debater_detail' object.id %}", "{{ object.api_url }}")
        .replace("{% url 'admin:core_teamresult_changelist' %}", "/admin/core/teamresult/")
        .replace("{% url 'admin:core_speakerresult_changelist' %}", "/admin/core/speakerresult/")
        .replace("{% url 'core:debater_tab_cards_csv' pk=object.id %}", "/core/debaters/{{ object.id }}/tab-cards-csv")
        .replace("{% url 'core:debater_profile_edit' object.pk %}", "/core/debater/{{ object.id }}/edit-profile/")
        .replace("object.user == request.user or request.user.is_superuser", "can_edit_profile")
        .replace(r#"{{ object.first_season }}-{{ object.first_season|add:"1"|stringformat:"s"|slice:"2:4" }}"#, "{{ object.first_season }}")
        .replace(r#"{{ object.latest_season }}-{{ object.latest_season|add:"1"|stringformat:"s"|slice:"2:4" }}"#, "{{ object.latest_season }}")
        .replace("object.show_region", "object.region_display")
        .replace("object.get_region_display", "object.region_display")
        .replace("{{ team|partner_name:object }}", r#"{% if team.partner %}<a href="{{ team.partner.url }}">{{ team.partner.name }}</a>{% else %}NO PARTNER{% endif %}"#)
        .replace("{{ team }}", "{{ team.name }}")
        .replace("round.round|opponent_url:tournament.team", "round.opponent.url")
        .replace("round.round|opponent:tournament.team", "round.opponent.name")
        .replace("round.round|opponent_side:tournament.team", "round.opponent_side")
        .replace("round.round|wl:tournament.team", "round.result")
        .replace("tournament.tournament.date|date:'M j'", "tournament.tournament.date|short_date")
        .replace("{{ tournament.team|partner_display:object }}", r#"{% if tournament.partner %}<a href="{{ tournament.partner.url }}">{{ tournament.partner.name }}</a>{% if tournament.partner.school %} (<a href="{{ tournament.partner.school.url }}">{{ tournament.partner.school.name }}</a>){% endif %}{% else %}NO PARTNER{% endif %}"#)
        .replace("award.get_type_of_place_display", "award.type")
        .replace("award.place|place_as_round_filter:award.ghost_points", "award.place|award_place(award.ghost_points)")
        .replace("{{ award.team }}", "{{ award.team.name }}")
}

fn adapt_profile_editor(template: &str) -> String {
    static FORM: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\{\{ form\.([a-z_]+)(\.label_tag)? \}\}").unwrap());
    FORM.replace_all(template, |capture: &Captures<'_>| {
        let partial = if capture.get(2).is_some() { "profile_label" } else { "profile_field" };
        format!(r#"{{% with field = form.{} %}}{{% include "debater/{partial}.html" %}}{{% endwith %}}"#, &capture[1])
    }).replace("{{ message }}", "{{ message.text }}")
        .replace(r#"|default:"Unknown""#, r#"|default("Unknown", true)"#)
        .replace(r#"|default:"—""#, r#"|default("—", true)"#)
        .replace("object.get_status_display", "object.status")
        .replace(r#"{{ form.instance.DINO|default:"2" }}"#, "2")
        .replace(r#"<form method="post" novalidate>"#, r#"<form method="post" novalidate>
            {% for error in form.non_field_errors %}<div class="alert alert-danger">{{ error }}</div>{% endfor %}"#)
}

fn adapt_scheduler(template: &str) -> String {
    static SETTINGS_INPUT: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\{\{ settings_form\.(\w+) \}\}").unwrap());
    let template = template
        .replace("{{ upload_form.workspace_version }}", r#"<input type="hidden" name="workspace_version" value="{{ version }}" id="id_workspace_version">"#)
        .replace("{{ settings_form.workspace_version }}", r#"<input type="hidden" name="workspace_version" value="{{ version }}" id="id_workspace_version">"#)
        .replace("{{ upload_form.schools_csv }}", r#"<input type="file" name="schools_csv" class="form-control-file" id="id_schools_csv">"#)
        .replace("{{ upload_form.dates_csv }}", r#"<input type="file" name="dates_csv" class="form-control-file" id="id_dates_csv">"#)
        .replace("upload_form.schools_csv.errors", "upload_errors.schools_csv")
        .replace("upload_form.dates_csv.errors", "upload_errors.dates_csv")
        .replace("upload_form.non_field_errors", "upload_errors.non_field_errors")
        .replace("settings_form.non_field_errors", "upload_errors.workspace_version")
        .replace("settings_form.errors", "settings_errors")
        .replace("for field in settings_form", "for field in settings_errors")
        .replace(r#"|join:", ""#, r#"|join(", ")"#)
        .replace("|default:'Upload CSVs first'", r#"|default("Upload CSVs first", true)"#);
    SETTINGS_INPUT
        .replace_all(&template, |capture: &Captures<'_>| {
            let field = &capture[1];
            let bounds = if field == "max_workers" { r#" min="1" max="64""# } else { "" };
            format!(r#"<input type="number" name="{field}" class="form-control"{bounds} required id="id_{field}" value="{{{{ settings.{field}.value }}}}">"#)
        })
        .replace("settings_form.", "settings.")
        .replace("<h4 class=\"card-title mb-1\">Scheduling Workspace</h4>", r#"<h4 class="card-title mb-1">Scheduling Workspace</h4>
        {% for message in notices %}<div class="alert alert-{{ message.tags }}" role="alert">{{ message.text }}</div>{% endfor %}
        {% if inline_notice %}<div class="alert alert-danger" role="alert">{{ inline_notice }}</div>{% endif %}"#)
}

fn adapt_template(name: &str, template: &str) -> String {
    let adapted;
    let template = match name {
        "admin/tournament_pipeline_audit.html" | "admin/tournament_audit_detail.html" => {
            adapted = template
                .replace(r#"|join:", ""#, r#"|join(", ")"#)
                .replace(r#"|date:"Y-m-d""#, "")
                .replace(r#"|date:"Y-m-d H:i""#, "")
                .replace(r#"|default:"-""#, r#"|default("-", true)"#)
                .replace("{{ tournament.get_absolute_url }}", "{{ tournament.url }}")
                .replace("{{ import_move_form_media }}", "")
                .replace(
                    "{{ row.move_form.tournament_import_id }}",
                    r#"<input type="hidden" name="import-{{ row.id }}-tournament_import_id" value="{{ row.id }}">"#,
                )
                .replace(
                    "{{ row.move_form.target_tournament }}",
                    r#"<select name="import-{{ row.id }}-target_tournament" id="id_import-{{ row.id }}-target_tournament"
                            class="modelselect2" data-rust-select-url="/core/tournaments/all_autocomplete" required
                            data-rust-select-placeholder="Select target tournament"><option value="">---------</option></select>"#,
                );
            &adapted
        }
        "tournaments/import_sources.html" => {
            adapted = template.replace("sources.items", "sources|items");
            &adapted
        }
        "tournaments/data_entry_setup.html" => {
            adapted = template
                .replace(
                    "{{ form.api_url|as_crispy_field }}",
                    r#"{% include "tournaments/includes/result_setup.html" %}"#,
                )
                .replace("form.non_field_errors", "[]")
                .replace(
                    "{% url 'core:tournament_detail' pk=tournament.id %}",
                    "/core/tournaments/{{ tournament.id }}",
                );
            &adapted
        }
        "tournaments/data_entry.html" => {
            let media =
                Regex::new(r"(?s)\{% for formset in formsets.values %\}.*?\{% endfor %\}").unwrap();
            let panes = Regex::new(r#"(?s)(id="dataEntryTabContent">).*?(</div>\s*<div class="mt-4 pt-3 border-top">)"#).unwrap();
            adapted = panes
                .replace(
                    &media.replace(template, ""),
                    r#"${1}
                    {% for panel in panels %}
                      {% if (panel.kind == 'school' or panel.kind == 'debater') and not panel.rows and not panel.errors %}
                        <input type="hidden" name="{{ panel.key }}-TOTAL_FORMS" value="0" id="id_{{ panel.key }}-TOTAL_FORMS">
                        <input type="hidden" name="{{ panel.key }}-INITIAL_FORMS" value="0" id="id_{{ panel.key }}-INITIAL_FORMS">
                      {% else %}
                        {% include "tournaments/includes/formset_tab_pane.html" %}
                      {% endif %}
                    {% endfor %}
                    ${2}"#,
                )
                .replace("{{ autoqual_form.media }}", "")
                .replace(
                    "{{ autoqual_form.autoqual_debaters|as_crispy_field }}",
                    r#"{% include "tournaments/includes/result_autoqual.html" %}"#,
                )
                .replace(
                    "{% url 'core:tournament_detail' pk=tournament.id %}",
                    "/core/tournaments/{{ tournament.id }}",
                )
                .replace("{{ formsets.schools.management_form }}", "")
                .replace("{{ formsets.debaters.management_form }}", "");
            &adapted
        }
        "tournaments/includes/data_entry_sidebar.html" => {
            adapted = template
                .replace("{% url 'core:school_create' %}", "/core/schools/create")
                .replace(
                    "{{ debater_form.school|as_crispy_field }}",
                    r#"{% from "tournaments/includes/result_widgets.html" import field %}
                    {{ field("", "school", "School", none, "/core/schools/autocomplete", {}) }}"#,
                );
            &adapted
        }
        "tournaments/includes/formset_tab_pane.html" => {
            adapted = template
                .replace("{% if is_active %}", "{% if active_tab == panel.key %}")
                .replace("{{ tab_id }}", "{{ panel.key }}")
                .replace("{{ item_name }}", "{{ panel.item }}")
                .replace("{{ form_type }}", "{{ panel.kind }}")
                .replace("if has_ghost_points", "if panel.ghost")
                .replace("formset.non_form_errors", "panel.errors")
                .replace("{% url 'core:get_new_team_form' %}", "/core/tournaments/get_new_form")
                .replace("{% for form in formset.forms %}", "{% for row in panel.rows %}")
                .replace(
                    r#"{% include "tournaments/includes/formset_row_new.html" with form=form form_index=forloop.counter0 place_number=forloop.counter form_type=form_type has_ghost_points=has_ghost_points item_name=item_name prefix=tab_id %}"#,
                    r#"{% set form_index = loop.index0 %}{% set place_number = loop.index %}{% set item_name = panel.item %}
                    {% include "tournaments/includes/result_row.html" %}"#,
                )
                .replace(
                    "{{ formset.management_form }}",
                    r#"<input type="hidden" name="{{ panel.key }}-TOTAL_FORMS" value="{{ panel.rows|length }}" id="id_{{ panel.key }}-TOTAL_FORMS">
                    <input type="hidden" name="{{ panel.key }}-INITIAL_FORMS" value="{{ panel.initial_count }}" id="id_{{ panel.key }}-INITIAL_FORMS">
                    <input type="hidden" name="{{ panel.key }}-MIN_NUM_FORMS" value="0" id="id_{{ panel.key }}-MIN_NUM_FORMS">
                    <input type="hidden" name="{{ panel.key }}-MAX_NUM_FORMS" value="150" id="id_{{ panel.key }}-MAX_NUM_FORMS">"#,
                );
            &adapted
        }
        "tournaments/includes/result_row.html" | "tournaments/includes/result_row_ajax.html" => {
            adapted = template.replace(
                r#"{% include "tournaments/includes/form_fields.html" with form=form form_type=form_type has_ghost_points=has_ghost_points %}"#,
                r#"{% include "tournaments/includes/result_fields.html" %}"#,
            );
            &adapted
        }
        "tournaments/mittab_bundle_upload.html" => {
            adapted = template
                .replace(
                    "{{ form.bundle_file|as_crispy_field }}",
                    r#"{% include "tournaments/mittab_bundle_file.html" %}"#,
                )
                .replace(
                    "{% url 'core:tournament_detail' pk=tournament.id %}",
                    "/core/tournaments/{{ tournament.id }}",
                )
                .replace("form.non_field_errors", "[]");
            &adapted
        }
        "tournaments/detail.html" => {
            adapted = template.replace("{{ message }}", "{{ message.text }}");
            &adapted
        }
        "admin/speaks_statistics.html"
        | "admin/includes/speaks_record_distribution.html"
        | "admin/includes/speaks_percentile_chart.html" => {
            adapted = template
                .replace(r#"|floatformat:2|default:"—""#, "|score_decimal(2)")
                .replace("|floatformat:2", "|score_decimal(2)")
                .replace("|floatformat:1", "|score_decimal(1)")
                .replace(
                    "{{ season.round_speaks.tournament_count|pluralize }}",
                    "{% if season.round_speaks.tournament_count != 1 %}s{% endif %}",
                )
                .replace(
                    "{{ metric.tournament_count|pluralize }}",
                    "{% if metric.tournament_count != 1 %}s{% endif %}",
                )
                .replace(
                    "{{ season.round_speaks.correspondence|json_script:season.chart_id }}",
                    r#"<script id="{{ season.chart_id }}" type="application/json">{{ season.round_speaks.correspondence|html_json }}</script>"#,
                );
            &adapted
        }
        "school_admin/debater_form.html" => {
            adapted = template
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "school_admin/debater_fields.html" %}"#,
                )
                .replace("form.show_region_field", "show_region")
                .replace("{{ message }}", "{{ message.text }}");
            &adapted
        }
        "school_admin/dashboard.html" | "school_admin/superuser_management.html" => {
            adapted = template
                .replace("schools.count", "schools|length")
                .replace("{{ csrf_token }}", "{{ csrf }}")
                .replace("{% empty %}", "{% else %}")
                .replace("school_admin_roles|dict_item:school.id", "school.role")
                .replace(
                    "current_school_admins|dict_item:school.id",
                    "school.current",
                )
                .replace("school_admins|dict_item:school.id", "school.admins")
                .replace("debaters_data|dict_item:school.id", "school.roster")
                .replace(
                    r#"{% url "core:user_autocomplete" %}"#,
                    "/core/user-autocomplete/",
                )
                .replace(
                    r#"{% url "core:school_admin_add" %}"#,
                    "/core/manage-school-admins/add/",
                )
                .replace(
                    r#"{% url "core:school_admin_remove" %}"#,
                    "/core/manage-school-admins/remove/",
                )
                .replace(
                    r#"{% url "core:school_admin_set_primary" %}"#,
                    "/core/manage-school-admins/primary/",
                )
                .replace(
                    "{% url 'core:debater_detail' debater.id %}",
                    "/core/debaters/{{ debater.id }}",
                );
            &adapted
        }
        "tournaments/update.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ form.media }}", "")
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "tournaments/fields.html" %}"#,
                );
            &adapted
        }
        "teams/update.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ form.media }}", "")
                .replace("{{ form|crispy }}", r#"{% include "teams/fields.html" %}"#);
            &adapted
        }
        "schools/update.html" | "debaters/update.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ form.media }}", "")
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "core/entity_fields.html" %}"#,
                );
            &adapted
        }
        "videos/create.html" | "videos/update.html" => {
            adapted = template.replace("{{ form.media }}", "")
                .replace("{% crispy form %}", r#"{% include "videos/fields.html" %}"#)
                .replace("{{ form|crispy }}", r#"{% include "videos/fields.html" %}"#)
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ debater_form.school|as_crispy_field }}", r#"<div class="form-group"><label for="id_school">School</label><select name="school" id="id_school" class="modelselect2 form-control" data-rust-select-url="/core/schools/autocomplete"><option value=""></option></select></div>"#);
            &adapted
        }
        "debaters/personal_partnership_stats.html"
        | "debaters/_partnership_stats_results.html"
        | "debaters/_partnership_rankings.html" => {
            adapted = template.replace("{{ form.media }}", "")
                .replace("{{ form.debater|as_crispy_field }}", r#"{% with select_name="debater", select_label="Debater" %}{% include "debaters/partnership_person.html" %}{% endwith %}"#)
                .replace(r#"{% include "debaters/_partnership_rankings.html" with rankings=reach_leaders only %}"#,
                    r#"{% with rankings=reach_leaders %}{% include "debaters/_partnership_rankings.html" %}{% endwith %}"#)
                .replace(r#"forloop.counter|add:"1""#, "loop.index + 1")
                .replace("{{ distance|pluralize }}", "{% if distance != 1 %}s{% endif %}")
                .replace("{{ diameter|pluralize }}", "{% if diameter != 1 %}s{% endif %}")
                .replace("{{ forloop.counter|pluralize }}", "{% if loop.index != 1 %}s{% endif %}")
                .replace(".pk }}", ".id }}")
                .replace("{{ debater.get_absolute_url }}", "/core/debaters/{{ debater.id }}")
                .replace("{{ first.get_absolute_url }}", "/core/debaters/{{ first.id }}")
                .replace("{{ second.get_absolute_url }}", "/core/debaters/{{ second.id }}");
            &adapted
        }
        "debaters/partnership_paths.html" => {
            adapted = template.replace("{{ form.media }}", "")
                .replace("{{ form.first_debater|as_crispy_field }}", r#"{% with select_name="first_debater", select_label="First debater" %}{% include "debaters/partnership_person.html" %}{% endwith %}"#)
                .replace("{{ form.second_debater|as_crispy_field }}", r#"{% with select_name="second_debater", select_label="Second debater" %}{% include "debaters/partnership_person.html" %}{% endwith %}"#)
                .replace(r#"path|length|add:"-1""#, "(path|length - 1)")
                .replace("{{ hops|pluralize }}", "{% if hops != 1 %}s{% endif %}")
                .replace("{{ debater.get_absolute_url }}", "/core/debaters/{{ debater.id }}")
                .replace("{{ debater.display_name }}", "{{ debater.name }}");
            &adapted
        }
        "debaters/partnership_network.html" => {
            let mut updated = template.replace("{{ form.media }}", "");
            for (field, label, axis) in [
                ("schools", "Schools", ""),
                ("from_season", "From season", ""),
                ("to_season", "Through season", ""),
                ("min_tournaments", "Minimum tournaments", ""),
                ("qualified", "Nationals qualified", ""),
                ("x_axis", "X axis", "x"),
                ("y_axis", "Y axis", "y"),
            ] {
                updated = updated.replace(&format!("{{{{ form.{field}|as_crispy_field }}}}"),
                    &format!(r#"{{% with field_name="{field}", label="{label}", axis="{axis}" %}}{{% include "debaters/network_filter.html" %}}{{% endwith %}}"#));
            }
            adapted = updated;
            &adapted
        }
        "motions/import.html" => {
            adapted = template
                .replace(r#"|join:", ""#, r#"|join(", ")"#)
                .replace(r#"|join:"; ""#, r#"|join("; ")"#)
                .replace("{{ form|crispy }}", r#"<div id="div_id_spreadsheet" class="form-group"><label for="id_spreadsheet" class="requiredField">Spreadsheet<span class="asteriskField">*</span></label><div><input type="file" name="spreadsheet" class="clearablefileinput form-control-file{% if warning %} is-invalid{% endif %}" required id="id_spreadsheet" aria-describedby="hint_id_spreadsheet"><small id="hint_id_spreadsheet" class="form-text text-muted">CSV or XLSX. Required column: motion_text. Optional: background_slide, date_set, tournament, tags.</small>{% if warning %}<p id="error_1_id_spreadsheet" class="invalid-feedback"><strong>{{ warning }}</strong></p>{% endif %}</div></div>"#);
            &adapted
        }
        "admin/school_short_name_audit.html" => {
            adapted = template
                .replace("{{ formset.management_form }}", r#"<input type="hidden" name="schools-TOTAL_FORMS" value="{{ total }}" id="id_schools-TOTAL_FORMS"><input type="hidden" name="schools-INITIAL_FORMS" value="{{ total }}" id="id_schools-INITIAL_FORMS"><input type="hidden" name="schools-MIN_NUM_FORMS" value="0" id="id_schools-MIN_NUM_FORMS"><input type="hidden" name="schools-MAX_NUM_FORMS" value="1000" id="id_schools-MAX_NUM_FORMS">"#)
                .replace("{% for form in formset %}", "{% for school in formset %}")
                .replace("{% with school=form.instance %}", "").replace("{% endwith %}", "")
                .replace("{{ form.id }}", r#"<input type="hidden" name="schools-{{ school.index }}-id" value="{{ school.id }}" id="id_schools-{{ school.index }}-id">"#)
                .replace("{{ form.short_name }}", r#"<input type="text" name="schools-{{ school.index }}-short_name" value="{{ school.short_name }}" class="form-control form-control-sm" placeholder="Short name" maxlength="64" id="id_schools-{{ school.index }}-short_name">"#)
                .replace("{% if form.short_name.errors %}", "{% if school.error %}")
                .replace("{% for error in form.short_name.errors %}", "")
                .replace("{{ error }}</div>
                                                    {% endfor %}", "{{ school.error }}</div>");
            &adapted
        }
        "admin/admin_tools.html" => {
            adapted = template
                .replace("{{ user.username }}", "{{ username }}")
                .replace("{{ round_amendment_form.amendment_file.id_for_label }}", "id_amendment_file")
                .replace("{{ round_amendment_form.amendment_file.label }}", "Round amendment JSON")
                .replace("{{ round_amendment_form.amendment_file }}", r#"<input type="file" name="amendment_file" accept=".json,application/json" required id="id_amendment_file">"#)
                .replace("{% if round_amendment_form.amendment_file.help_text %}", "{% if true %}")
                .replace("round_amendment_form.amendment_file.errors", "round_amendment_errors")
                .replace("{{ round_amendment_form.amendment_file.help_text }}", "Upload a JSON file with synthetic resolutions, round edits, or tournament import moves/deletes.")
                .replace(r#"{% url "core:debater_autocomplete" %}"#, "/core/debaters/autocomplete")
                .replace(r#"{% url "core:school_autocomplete" %}"#, "/core/schools/autocomplete")
                .replace(r#"{% url "core:team_autocomplete" %}"#, "/core/teams/autocomplete");
            &adapted
        }
        "resources/create.html" | "resources/update.html" => {
            adapted = template
                .replace("{{ form.media }}", "")
                .replace(
                    "{% crispy form %}",
                    r#"{% include "resources/fields.html" %}"#,
                )
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "resources/fields.html" %}"#,
                );
            &adapted
        }
        "base/delete.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ object.get_absolute_url }}", "{{ object.url }}");
            &adapted
        }
        "admin/includes/doty_leaderboard.html" => {
            adapted = template
                .replace(
                    "{{ leaderboard.standings|length|pluralize }}",
                    "{% if leaderboard.standings|length != 1 %}s{% endif %}",
                )
                .replace("{% empty %}", "{% else %}")
                .replace(
                    "{{ row.debater.get_absolute_url }}",
                    "/core/debaters/{{ row.debater.id }}",
                )
                .replace(
                    "{{ row.debater.school.get_absolute_url }}",
                    "/core/schools/{{ row.debater.school_id }}",
                )
                .replace("{{ row.debater.school.name }}", "{{ row.debater.school }}")
                .replace(
                    "{{ marker.tournament.get_absolute_url }}",
                    "/core/tournaments/{{ marker.tournament_id }}",
                )
                .replace("|floatformat:-2", "|doty_points");
            &adapted
        }
        "admin/scheduling_dashboard.html" => {
            adapted = adapt_scheduler(template);
            &adapted
        }
        "debaters/detail.html" => {
            adapted = adapt_debater_profile(template);
            &adapted
        }
        "debater/debater_profile_edit.html" => {
            adapted = adapt_profile_editor(template);
            &adapted
        }
        _ => template,
    };
    static DJANGO_TAGS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\{%\s*(load|get_menu|render_bundle|static|url|endblock|cache|endcache)(?:\s+([^%]*?))?\s*%\}").unwrap()
    });
    static INCLUDE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"\{% include "((?:core/stats/[^"]+|base/results/(?:team|speaker)\.html))" with ([^%]+)%\}"#).unwrap()
    });
    static ASSIGN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"[a-z0-9_]+=(?:"[^"]+"|[^ ]+)"#).unwrap());
    let template = INCLUDE.replace_all(template, |captures: &Captures<'_>| {
        let assignments = ASSIGN
            .find_iter(&captures[2])
            .map(|capture| capture.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "{{% with {assignments} %}}{{% include \"{}\" %}}{{% endwith %}}",
            &captures[1]
        )
    });
    let template = if matches!(
        name,
        "debater/my_profile.html"
            | "debater/claim_debater_request_form.html"
            | "admin/claim_debater_requests.html"
    ) {
        std::borrow::Cow::Owned(template
            .replace("{% url 'core:school_detail' debater.school.pk %}", "{{ debater.school.url }}")
            .replace("{% url 'core:debater_profile_edit' debater.pk %}", "/core/debater/{{ debater.id }}/edit-profile/")
            .replace(r#"{{ request.created_at|date:"M d, Y" }}"#, "{{ request.created_at }}")
            .replace(r#"{{ request.created_at|date:"M d, Y H:i" }}"#, "{{ request.created_at_full }}")
            .replace(r#"{{ request.processed_at|date:"M d, Y H:i" }}"#, "{{ request.processed_at }}")
            .replace(r#"|default:"Unknown""#, r#"|default("Unknown", true)"#)
            .replace("debater.get_status_display", "debater.status")
            .replace("request.requested_by.get_full_name|default:request.requested_by.username", "request.requested_by.name")
            .replace("request.processed_by.get_full_name|default:request.processed_by.username", "request.processed_by.name")
            .replace("{{ message }}", "{{ message.text }}")
            .replace("{{ form.media }}", "")
            .replace("{{ form.school.label_tag }}", "").replace("{{ form.debater.label_tag }}", "")
            .replace("{{ form.school }}", r#"{% with field="school" %}{% include "debater/claim_selects.html" %}{% endwith %}"#)
            .replace("{{ form.debater }}", r#"{% with field="debater" %}{% include "debater/claim_selects.html" %}{% endwith %}"#)
            .replace("{{ form.school.help_text }}", "First, select the school you debated for")
            .replace("{{ form.debater.help_text }}", "Then search for and select your debater profile")
            .replace("form.school.errors", "errors.school")
            .replace("form.debater.errors", "errors.debater")
            .replace("form.non_field_errors", "errors.non_field_errors"))
    } else {
        template
    };
    let template = if name == "motions/form.html" || name == "base/create.html" {
        std::borrow::Cow::Owned(
            template
                .replace("{{ form.media }}", "")
                .replace("{{ form|crispy }}", "{% include \"motions/fields.html\" %}"),
        )
    } else {
        template
    };
    let rendered = DJANGO_TAGS.replace_all(&template, |capture: &Captures<'_>| {
        let tag = &capture[1];
        let arguments = capture.get(2).map_or("", |value| value.as_str().trim());
        match tag {
            "load" | "get_menu" | "cache" | "endcache" => String::new(),
            "endblock" => "{% endblock %}".into(),
            "render_bundle" => {
                "{% for bundle in bundles %}<script src=\"{{ bundle }}\"></script>{% endfor %}"
                    .into()
            }
            "static" => format!("/static/{}", arguments.trim_matches('\'')),
            "url" => {
                let route = arguments.split('\'').nth(1).unwrap_or_default();
                let path = match route {
                    "core:index" => "/",
                    "core:school_create" => "/core/schools/create",
                    "core:admin_tools" => "/core/admin-tools/",
                    "core:school_admin_debater_create" => {
                        "/core/school-admin/{{ school.id }}/debater/create/"
                    }
                    "core:school_admin_debater_update" => {
                        "/core/school-admin/debater/{{ debater.id }}/edit/"
                    }
                    "core:school_admin_dashboard" => "/core/school-admin/",
                    "core:doty" => "/DOTY",
                    "core:partnership_paths" => "/core/debaters/partnership-paths/",
                    "core:partnership_stats" => "/core/debaters/partnership-paths/stats/",
                    "core:partnership_stats_results" => {
                        "/core/debaters/partnership-paths/stats/results/"
                    }
                    "core:clear_partnership_stats_cache" => {
                        "/core/debaters/partnership-paths/stats/clear-cache/"
                    }
                    "core:debater_partnership_stats" => "/core/debaters/partnership-paths/person/",
                    "core:partnership_network" => "/core/debaters/partnership-paths/network/",
                    "core:partnership_network_paths" => {
                        "/core/debaters/partnership-paths/network/paths/"
                    }
                    "core:speaks_statistics" => "/core/admin/speaks-stats/",
                    "core:tournament_dataentry" => "/core/tournaments/data_entry",
                    "core:mittab_dashboard" => "/core/mittab-dashboard/",
                    "core:rankings_recompute" => "/core/rankings-recompute/",
                    "core:scheduling_dashboard" => "/core/admin/scheduling/",
                    "core:tournament_pipeline_audit" => "/core/admin/tournament-pipeline-audit/",
                    "core:school_short_name_audit" => "/core/admin/school-short-names/",
                    "core:synthetic_resolution_suggestions" => {
                        "/core/admin/synthetic-resolution-suggestions/"
                    }
                    "core:synthetic_cleanup" => "/core/admin/synthetic-cleanup/",
                    "core:invalidate_elo_cache" => "/core/admin/elo-cache/invalidate/",
                    "core:synthetic_resolution" => "/core/admin/synthetic-resolve/",
                    "core:round_amendments_upload" => "/core/admin/round-amendments/",
                    "core:scheduling_download_csv" if arguments.contains("'schools'") => {
                        "/core/admin/scheduling/download/schools/"
                    }
                    "core:scheduling_download_csv" => "/core/admin/scheduling/download/dates/",
                    "core:scheduling_run_output" if arguments.contains("latest_completed_run") => {
                        "/core/admin/scheduling/runs/{{ latest_completed_run.id }}/output/"
                    }
                    "core:scheduling_run_output" => {
                        "/core/admin/scheduling/runs/{{ run.id }}/output/"
                    }
                    "api:season_standings" => "/api/standings/",
                    "api:school_detail" => "/api/schools/{{ object.id }}/detail/",
                    "api:tournament_detail" => "/api/tournaments/{{ object.id }}/detail/",
                    "core:tournament_mittab_bundle_upload" => {
                        "/core/tournaments/mittab_bundle/upload"
                    }
                    "core:tournament_dataentry_setup" => "/core/tournaments/data_entry/setup",
                    "core:resource_list" => "/core/resources/",
                    "core:my_debater_profile" => "/core/my-profile/",
                    "core:claim_debater_request_create" => "/core/claim-debater/request/",
                    "core:motion_status" => "/core/motions/{{ object.id }}/status",
                    "core:tag_detail" => "{{ tag_url(tag.name) }}",
                    "core:team_detail" => "{{ result.team.url }}",
                    "api:team_detail" => "/api/teams/{{ object.id }}/detail/",
                    "core:tournament_detail" if arguments.contains("result.result") => {
                        "{{ result.result.tournament.url }}"
                    }
                    "core:tournament_detail" => "{{ result.tournament.url }}",
                    "core:ai_plugin_manifest" => "/.well-known/ai-plugin.json",
                    "core:openapi_schema" => "/.well-known/openapi.json",
                    "core:standings_replay" => "/standings/replay/",
                    "core:immaculate_grid" => "/immaculate-grid/",
                    "core:immaculate_grid_guess" => "/immaculate-grid/guess/",
                    "core:immaculate_grid_answers" => "/immaculate-grid/answers/",
                    "core:immaculate_grid_search" => "/immaculate-grid/search/",
                    "core:immaculate_grid_infinite" => "/immaculate-grid/infinite/",
                    "core:immaculate_grid_infinite_guess" => "/immaculate-grid/infinite/guess/",
                    "core:immaculate_grid_leaderboard" => "/immaculate-grid/leaderboard/",
                    "core:immaculate_grid_player_board" => {
                        "/immaculate-grid/leaderboard/{{ entry.id }}/"
                    }
                    "core:import_sources" => "/core/tournaments/import/",
                    "core:live_standings" => "/live",
                    "core:debater_detail" if arguments.contains("result.debater") => {
                        "{{ result.debater.url }}"
                    }
                    "core:debater_detail" => "{{ speaker.debater.url }}",
                    "core:school_detail" => "{{ speaker.school.url }}",
                    _ => panic!("Unmapped template route {route}"),
                };
                if let Some((_, alias)) = arguments.split_once(" as ") {
                    format!("{{% set {alias} = '{path}' %}}")
                } else {
                    path.into()
                }
            }
            _ => unreachable!(),
        }
    });
    rendered
        .replace("{{ block.super }}", "{{ super() }}")
        .replace("{% csrf_token %}", r#"<input type="hidden" name="csrfmiddlewaretoken" value="{{ csrf }}">"#)
        .replace("object.authors.all", "object.authors")
        .replace("object.get_resource_type_display", "object.resource_type")
        .replace("object.get_viewing_permission_display", "object.viewing_permission")
        .replace("object.created_at|date:\"F d, Y\"", "object.created_at")
        .replace("object.date_set|default:\"Unknown\"", "object.date_set or 'Unknown'")
        .replace("{{ object.tournament }}", "{{ object.tournament.name }}")
        .replace("object.tournament.get_absolute_url", "object.tournament.url")
        .replace("{% empty %}", "{% else %}")
        .replace("payload is not None", "payload is not none")
        .replace("|date:'Y-m-d'", "")
        .replace(r#"|date:"F j, Y""#, r#"|calendar_date("%B %-d, %Y")"#)
        .replace("|date:'F j'", r#"|calendar_date("%B %-d")"#)
        .replace(r#"|date:"M j, Y""#, r#"|calendar_date("%b %-d, %Y")"#)
        .replace(r#"score_day|date:"M j""#, "score_day|short_date")
        .replace(r#"|date:"D""#, r#"|calendar_date("%a")"#)
        .replace(r#"|default_if_none:"—""#, "|grid_score")
        .replace("latest_guess.score is None", "latest_guess.score is none")
        .replace(r#"not infinite or request.method == "POST""#, "not infinite or practice_post_failed")
        .replace("score is None", "score is none")
        .replace("week_days|length|add:3", "(week_days|length) + 3")
        .replace(r#"|json_script:"immaculate-miss-names""#, "|grid_miss_names")
        .replace("forloop.parentloop.counter", "(cell.index // 3) + 1")
        .replace("forloop.counter", "loop.index")
        .replace("forloop.revcounter", "loop.revindex")
        .replace("forloop.first", "loop.first")
        .replace("forloop.last", "loop.last")
        .replace("loop.index|range_filter:3", "range(loop.index, 3)")
        .replace("{{ object }}", "{{ object.name }}")
        .replace("|floatformat:1", "|one_decimal")
        .replace("message.tags|default:'info'", "message.tags|default('info')")
        .replace("object.autoqual_recipients.exists", "recipients")
        .replace("object.autoqual_recipients.all", "recipients")
        .replace("object.videos.all", "videos")
        .replace("{{ object.date }}", "{{ object.date|long_date }}")
        .replace("|yesno:\"Yes,No\"", "|yesno")
        .replace("round.round|opponent_url:team", "round.opponent.url")
        .replace("round.round|opponent_side:team", "round.opponent_side")
        .replace("round.round|opponent:team", "round.opponent.name")
        .replace("round.round|wl:team", "round.result")
        .replace("loop.index|place_as_round_filter:result.ghost_points", "loop.index|award_place(result.ghost_points)")
        .replace("{{ result.team }}", "{{ result.team.name }}")
        .replace("{{ result.team.debaters_display|safe }}", r#"{% for debater in result.team.debaters %}<a href="{{ debater.url }}">{{ debater.name }}</a>{% if not loop.last %} and {% endif %}{% endfor %}"#)
        .replace("video.get_round_display", "video.round")
        .replace("video.tags.all", "video.tags")
        .replace("object.tags.all", "object.tags")
        .replace("object.get_round_display", "object.round")
        .replace("object.get_permissions_display", "object.permissions")
        .replace("{{ video.pm }}", "{{ video.pm.name }}")
        .replace("{{ video.mg }}", "{{ video.mg.name }}")
        .replace("{{ video.lo }}", "{{ video.lo.name }}")
        .replace("{{ video.mo }}", "{{ video.mo.name }}")
        .replace("item.debaters.all", "item.debater_list")
        .replace("{{ item.debaters_display }}", r#"{% for debater in item.debater_list %}<a href="{{ debater.url }}">{{ debater.name }}</a>{% if not loop.last %} and {% endif %}{% endfor %}"#)
        .replace("object.included_in_oty|yesno:\"Yes,No\"", "'Yes' if object.included_in_oty else 'No'")
        .replace("tournament.date|date:\"M j\"", "tournament.date|short_date")
        .replace(".get_season_display", ".season_display")
        .replace(
            "{% include \"base/results/full_team.html\" with results=team_results %}",
            "{% include \"base/results/full_team.html\" %}",
        )
        .replace("round.round.round_number", "round.round_number")
        .replace("round.round|opponent_url:object.get_absolute_url", "round.opponent.url")
        .replace("round.round|opponent_side:object", "round.opponent_side")
        .replace("round.round|opponent:object", "round.opponent.name")
        .replace("round.round|wl:object", "round.result")
        .replace("stat.debater_role|upper", "stat.role")
        .replace("result.result.place|place_as_round_filter:result.result.ghost_points", "result.result.place|award_place(result.result.ghost_points)")
        .replace("{{ result.result.place|place_as_round_filter }}", "")
        .replace("result.result.get_type_of_place_display", "result.result.type")
        .replace("{{ object.host }}", "")
        .replace("request.GET.season|years_on_team:debater.first_season", "debater.years_on_team")
        .replace("live_sources|join:\", \"", "live_sources|join(\", \")")
        .replace("school.school|relevant_debaters:current_season", "school.breakdown")
        .replace("debater.debater|qual_display:current_season", "debater.auto_quals")
        .replace("debater|qual_contribution:current_season|number", "debater.contribution|number")
        .replace(".get_absolute_url", ".url")
        .replace("speaker.debater.school.name", "speaker.school.name|default('')")
        .replace("{{ team.team.debaters_display|safe }}", r#"{% for debater in team.team.debaters %}<a href="{{ debater.url }}">{{ debater.name }}</a>{% if not loop.last %} and {% endif %}{% endfor %}"#)
}

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
            selected: submenu.iter().any(|item| item.selected),
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
        if user.is_school_admin {
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
    let live_sources = if is_home && season == state.current_season {
        state
            .live
            .as_ref()
            .map(|live| live.active_sources(&state.pool, season, state.memcached.as_ref()))
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let bindings = context! {
        request => context! { path, user, GET => query },
        left_menu,
        bundles => state.bundles.as_ref(),
        seasons,
        current_season => season.to_string(),
        season_display => season_display(season),
        replay_api_url => format!("/api/standings/replay/?season={season}"),
        live_sources,
        live_season => state.current_season.to_string(),
        render_noty => season <= 2020,
        using_online_quals => matches!(season, 2020 | 2021),
        online_qual_bar => 10,
        online_seasons => [(2021, "2021-22"), (2020, "2020-21")].map(|(year, label)| (year.to_string(), label)),
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
        bundles => state.bundles.as_ref(),
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
