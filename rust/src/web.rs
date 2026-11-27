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
    environment.add_filter("one_decimal", |value: f64| format!("{value:.1}"));
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
    for (name, text) in VIEWS {
        environment.add_template_owned(*name, adapt_template(text))?;
    }
    Ok(environment)
});

fn adapt_template(template: &str) -> String {
    static DJANGO_TAGS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\{%\s*(load|get_menu|render_bundle|static|url|endblock|cache|endcache)(?:\s+([^%]*?))?\s*%\}").unwrap()
    });
    static INCLUDE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"\{% include "((?:core/stats/)[^"]+)" with ([^%]+)%\}"#).unwrap()
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
                    "api:season_standings" => "/api/standings/",
                    "api:school_detail" => "/api/schools/{{ object.id }}/detail/",
                    "api:team_detail" => "/api/teams/{{ object.id }}/detail/",
                    "core:tournament_detail" if arguments.contains("result.result") => {
                        "{{ result.result.tournament.url }}"
                    }
                    "core:tournament_detail" => "{{ result.tournament.url }}",
                    "core:ai_plugin_manifest" => "/.well-known/ai-plugin.json",
                    "core:openapi_schema" => "/.well-known/openapi.json",
                    "core:standings_replay" => "/standings/replay/",
                    "core:immaculate_grid" => "/immaculate-grid/",
                    "core:import_sources" => "/core/tournaments/import/",
                    "core:live_standings" => "/live",
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
        .replace("{% empty %}", "{% else %}")
        .replace("payload is not None", "payload is not none")
        .replace("forloop.counter", "loop.index")
        .replace("forloop.revcounter", "loop.revindex")
        .replace("forloop.first", "loop.first")
        .replace("forloop.last", "loop.last")
        .replace("loop.index|range_filter:3", "range(loop.index, 3)")
        .replace("{{ object }}", "{{ object.name }}")
        .replace("|floatformat:1", "|one_decimal")
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
            .map(|live| live.active_sources(&state.pool, season))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_templates_compile() {
        for (name, template) in VIEWS {
            let adapted = adapt_template(template);
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
