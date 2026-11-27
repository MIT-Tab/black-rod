mod legacy;

use std::collections::HashMap;

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde::Serialize;
use serde_json::json;
use sqlx::{Any, AnyPool, AssertSqlSafe, Row, any::AnyArguments, query::Query};

use crate::{ApiError, AppState, session, team_profile, web};

#[derive(Clone, Copy)]
enum Kind {
    Schools,
    Teams,
    Debaters,
    Tournaments,
    Videos,
    Motions,
    Resources { owned: bool },
    Outreach { judge: bool },
    Standing(legacy::Standing),
}

#[derive(Clone, Copy)]
struct Field {
    name: &'static str,
    label: &'static str,
    order: &'static str,
}

const SCHOOL_FIELDS: &[Field] = &[
    Field {
        name: "id",
        label: "ID",
        order: "x.id",
    },
    Field {
        name: "name",
        label: "Name",
        order: "x.name",
    },
    Field {
        name: "included_in_oty",
        label: "APDA Member?",
        order: "x.included_in_oty",
    },
];
const TEAM_FIELDS: &[Field] = &[
    Field {
        name: "id",
        label: "ID",
        order: "x.id",
    },
    Field {
        name: "name",
        label: "Name",
        order: "x.name",
    },
    Field {
        name: "debaters_display",
        label: "Debaters display",
        order: "first_last, first_first, second_last, second_first, x.id",
    },
];
const DEBATER_FIELDS: &[Field] = &[
    Field {
        name: "id",
        label: "ID",
        order: "x.id",
    },
    Field {
        name: "first_name",
        label: "First name",
        order: "x.first_name",
    },
    Field {
        name: "last_name",
        label: "Last name",
        order: "x.last_name",
    },
    Field {
        name: "school_name",
        label: "School",
        order: "s.name",
    },
    Field {
        name: "status",
        label: "Status",
        order: "x.status",
    },
];

const TOURNAMENT_FIELDS: &[Field] = &[
    Field {
        name: "id",
        label: "ID",
        order: "x.id",
    },
    Field {
        name: "name",
        label: "Name",
        order: "x.name",
    },
    Field {
        name: "date",
        label: "Date",
        order: "x.date",
    },
    Field {
        name: "season_display",
        label: "Season",
        order: "x.season",
    },
    Field {
        name: "num_teams",
        label: "Teams",
        order: "x.num_teams",
    },
    Field {
        name: "points",
        label: "Points",
        order: "x.points",
    },
    Field {
        name: "winning_team",
        label: "Winning team",
        order: "winner_last_name, winner_first_name",
    },
    Field {
        name: "top_speaker",
        label: "Top speaker",
        order: "speaker_last_name, speaker_first_name",
    },
];

const TOURNAMENT_SOURCE: &str = "FROM (
    SELECT t.*, CASE
        WHEN NOT t.qual OR t.num_teams < 8 THEN 0
        WHEN t.num_teams < 16 THEN 8
        WHEN t.num_teams < 80 THEN 10 + (t.num_teams / 8)
        ELSE 20 END AS points
    FROM core_tournament t
) x WHERE (
    EXISTS (SELECT 1 FROM core_teamresult r WHERE r.tournament_id = x.id)
    OR EXISTS (SELECT 1 FROM core_speakerresult r WHERE r.tournament_id = x.id)
    OR EXISTS (SELECT 1 FROM core_tournamentautoqual r WHERE r.tournament_id = x.id)
)";

const TOURNAMENT_PROJECTION: &str = "CAST(x.id AS BIGINT) AS id, x.name, x.season,
    CAST(x.date AS TEXT) AS date, CAST(x.num_teams AS BIGINT) AS num_teams,
    CAST(x.points AS BIGINT) AS points,
    (SELECT CAST(r.team_id AS BIGINT) FROM core_teamresult r
        WHERE r.tournament_id = x.id AND r.type_of_place = 1 AND r.place = 1 LIMIT 1) AS winner,
    (SELECT d.first_name FROM core_speakerresult r JOIN core_debater d ON d.id = r.debater_id
        WHERE r.tournament_id = x.id AND r.type_of_place = 1 AND r.place = 1 LIMIT 1) AS speaker_first_name,
    (SELECT d.last_name FROM core_speakerresult r JOIN core_debater d ON d.id = r.debater_id
        WHERE r.tournament_id = x.id AND r.type_of_place = 1 AND r.place = 1 LIMIT 1) AS speaker_last_name,
    (SELECT d.first_name FROM core_teamresult r LEFT JOIN core_team_debaters m ON m.team_id = r.team_id
        LEFT JOIN core_debater d ON d.id = m.debater_id
        WHERE r.tournament_id = x.id AND r.type_of_place = 1 AND r.place = 1 LIMIT 1) AS winner_first_name,
    (SELECT d.last_name FROM core_teamresult r LEFT JOIN core_team_debaters m ON m.team_id = r.team_id
        LEFT JOIN core_debater d ON d.id = m.debater_id
        WHERE r.tournament_id = x.id AND r.type_of_place = 1 AND r.place = 1 LIMIT 1) AS winner_last_name";

const TOURNAMENT_TYPES: &[(i64, &str)] = &[
    (0, "Regular (Points)"),
    (1, "Brandeis IV"),
    (2, "Yale IV"),
    (3, "NorthAms"),
    (4, "Expansion"),
    (5, "Worlds"),
    (6, "NAUDC"),
    (16, "USUDC"),
    (7, "ProAms"),
    (8, "Nationals"),
    (9, "Novice"),
    (10, "Gender Minority"),
    (15, "BIPOC"),
    (11, "Online No Points"),
    (12, "Online Points"),
    (13, "Online Proams Points"),
    (14, "Online Gender Minority Points"),
];

const TOURNAMENT_RANGES: &[(&str, &str, &str)] = &[
    ("num_teams_min", "x.num_teams >=", "Minimum number of teams"),
    ("num_teams_max", "x.num_teams <=", "Maximum number of teams"),
    ("points_min", "x.points >=", "Minimum points"),
    ("points_max", "x.points <=", "Maximum points"),
];

const VIDEO_FIELDS: &[Field] = &[
    Field {
        name: "id",
        label: "ID",
        order: "x.id",
    },
    Field {
        name: "tournament",
        label: "Tournament",
        order: "t.date",
    },
    Field {
        name: "tournament_season",
        label: "Season",
        order: "t.season",
    },
    Field {
        name: "round",
        label: "Round",
        order: "x.round",
    },
    Field {
        name: "pm",
        label: "PM",
        order: "x.pm_id",
    },
    Field {
        name: "mg",
        label: "MG",
        order: "x.mg_id",
    },
    Field {
        name: "lo",
        label: "LO",
        order: "x.lo_id",
    },
    Field {
        name: "mo",
        label: "MO",
        order: "x.mo_id",
    },
    Field {
        name: "permissions",
        label: "Permissions",
        order: "x.permissions",
    },
];
const VIDEO_SOURCE: &str = "FROM core_video x JOIN core_tournament t ON t.id = x.tournament_id
    JOIN core_debater pm ON pm.id = x.pm_id JOIN core_debater mg ON mg.id = x.mg_id
    JOIN core_debater lo ON lo.id = x.lo_id JOIN core_debater mo ON mo.id = x.mo_id WHERE 1=1";
const VIDEO_TAG: &str = "SELECT 1 FROM taggit_tag t JOIN taggit_taggeditem ti ON ti.tag_id = t.id
    JOIN django_content_type ct ON ct.id = ti.content_type_id
    WHERE ct.app_label = 'core' AND ct.model = 'video' AND ti.object_id = x.id AND t.name = {}";
const VIDEO_PROJECTION: &str = "CAST(x.id AS BIGINT) AS id, t.name, t.season,
    CAST(x.round AS BIGINT) AS round, CAST(x.permissions AS BIGINT) AS permissions,
    TRIM(pm.first_name || ' ' || pm.last_name) || CASE WHEN pm.temporary THEN ' (New)' ELSE '' END AS pm,
    TRIM(mg.first_name || ' ' || mg.last_name) || CASE WHEN mg.temporary THEN ' (New)' ELSE '' END AS mg,
    TRIM(lo.first_name || ' ' || lo.last_name) || CASE WHEN lo.temporary THEN ' (New)' ELSE '' END AS lo,
    TRIM(mo.first_name || ' ' || mo.last_name) || CASE WHEN mo.temporary THEN ' (New)' ELSE '' END AS mo";
const VIDEO_ROUNDS: &[i64] = &[0, 1, 2, 3, 4, 5, 14, 6, 7, 8, 9, 10, 11, 12, 13];

const MOTION_FIELDS: &[Field] = &[
    Field {
        name: "text",
        label: "Motion text",
        order: "x.text",
    },
    Field {
        name: "date_set",
        label: "Date set",
        order: "x.date_set",
    },
    Field {
        name: "tournament",
        label: "Tournament",
        order: "t.date",
    },
    Field {
        name: "topics",
        label: "Topics",
        order: "",
    },
    Field {
        name: "user_status",
        label: "My status",
        order: "",
    },
];
const MOTION_SOURCE: &str =
    "FROM core_motion x LEFT JOIN core_tournament t ON t.id = x.tournament_id
    LEFT JOIN core_motionuserstatus u ON u.motion_id = x.id AND u.user_id = $1 WHERE 1=1";
const MOTION_PROJECTION: &str =
    "CAST(x.id AS BIGINT) AS id, x.text, CAST(x.date_set AS TEXT) AS date,
    t.name AS tournament, u.status AS progress";
const MOTION_TAG: &str =
    "SELECT 1 FROM core_taggedmotion m JOIN core_motiontopic topic ON topic.id = m.tag_id
    WHERE m.content_object_id = x.id AND topic.name = {}";
const MOTION_PROGRESS: &[(&str, &str)] = &[
    ("exclude_done_ignore", "Exclude done and ignored"),
    ("exclude_ignore", "Exclude ignored"),
    ("all", "Show all"),
    ("done", "Only done"),
    ("ignore", "Only ignored"),
];

const RESOURCE_FIELDS: &[Field] = &[
    Field {
        name: "title",
        label: "Title",
        order: "x.title",
    },
    Field {
        name: "authors",
        label: "Authors",
        order: "author_sort",
    },
    Field {
        name: "resource_type",
        label: "Type",
        order: "x.resource_type",
    },
    Field {
        name: "tags",
        label: "Tags",
        order: "",
    },
    Field {
        name: "created_at",
        label: "Created",
        order: "x.created_at",
    },
];
const RESOURCE_SOURCE: &str = "FROM core_resource x WHERE 1=1";
const RESOURCE_PROJECTION: &str = "CAST(x.id AS BIGINT) AS id, x.title,
    x.resource_type, CAST(x.created_at AS TEXT) AS created,
    (SELECT MIN(d.last_name) FROM core_resource_authors a JOIN core_debater d ON d.id = a.debater_id
        WHERE a.resource_id = x.id) AS author_sort";
const RESOURCE_TAG: &str =
    "SELECT 1 FROM core_resourcetag t JOIN core_taggedresource i ON i.tag_id = t.id
    JOIN django_content_type ct ON ct.id = i.content_type_id
    WHERE ct.app_label = 'core' AND ct.model = 'resource' AND i.object_id = x.id AND t.name = {}";
const RESOURCE_AUTHOR: &str =
    "SELECT 1 FROM core_resource_authors a JOIN core_debater d ON d.id = a.debater_id
    WHERE a.resource_id = x.id";

const OUTREACH_FIELDS: &[Field] = &[
    Field {
        name: "id",
        label: "ID",
        order: "x.id",
    },
    Field {
        name: "first_name",
        label: "First Name",
        order: "x.first_name",
    },
    Field {
        name: "last_name",
        label: "Last Name",
        order: "x.last_name",
    },
    Field {
        name: "school_name",
        label: "School",
        order: "",
    },
    Field {
        name: "region",
        label: "Region",
        order: "",
    },
    Field {
        name: "latest_year",
        label: "Last Year Debated",
        order: "x.latest_season",
    },
];

impl Kind {
    fn title(self) -> &'static str {
        match self {
            Self::Standing(standing) => standing.title(),
            Self::Schools => "Schools",
            Self::Teams => "Teams",
            Self::Debaters => "Debaters",
            Self::Tournaments => "Tournaments",
            Self::Videos => "Videos",
            Self::Motions => "Motions",
            Self::Resources { owned: true } => "My Resources",
            Self::Resources { owned: false } => "Resources",
            Self::Outreach { judge: true } => "Dino Judge Database",
            Self::Outreach { judge: false } => "Dino TO Database",
        }
    }

    fn columns(self) -> &'static [Field] {
        match self {
            Self::Standing(standing) => standing.columns(),
            Self::Schools => SCHOOL_FIELDS,
            Self::Teams => TEAM_FIELDS,
            Self::Debaters => DEBATER_FIELDS,
            Self::Tournaments => TOURNAMENT_FIELDS,
            Self::Videos => VIDEO_FIELDS,
            Self::Motions => MOTION_FIELDS,
            Self::Resources { .. } => RESOURCE_FIELDS,
            Self::Outreach { .. } => OUTREACH_FIELDS,
        }
    }

    fn count(self) -> &'static str {
        match self {
            Self::Standing(standing) => standing.source(),
            Self::Schools => "FROM core_school x WHERE NOT x.temporary AND NOT x.synthetic",
            Self::Teams => "FROM core_team x WHERE NOT x.synthetic",
            Self::Tournaments => TOURNAMENT_SOURCE,
            Self::Videos => VIDEO_SOURCE,
            Self::Motions => MOTION_SOURCE,
            Self::Resources { .. } => RESOURCE_SOURCE,
            Self::Debaters | Self::Outreach { .. } => {
                "FROM core_debater x LEFT JOIN core_school s ON s.id = x.school_id WHERE NOT x.temporary AND NOT x.synthetic"
            }
        }
    }

    fn projection(self, order_by: &str) -> &'static str {
        match self {
            Self::Standing(_) => unreachable!(),
            Self::Schools => "CAST(x.id AS BIGINT) AS id, x.name,
                   CAST(CASE WHEN x.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included",
            Self::Teams if order_by.trim_start_matches('-') == "debaters_display" => "CAST(x.id AS BIGINT) AS id, x.name,
                (SELECT MIN(LOWER(d.last_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS first_last,
                (SELECT MIN(LOWER(d.first_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS first_first,
                (SELECT MAX(LOWER(d.last_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS second_last,
                (SELECT MAX(LOWER(d.first_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS second_first",
            Self::Teams => "CAST(x.id AS BIGINT) AS id, x.name",
            Self::Tournaments => TOURNAMENT_PROJECTION,
            Self::Videos => VIDEO_PROJECTION,
            Self::Motions => MOTION_PROJECTION,
            Self::Resources { .. } => RESOURCE_PROJECTION,
            Self::Debaters | Self::Outreach { .. } => "CAST(x.id AS BIGINT) AS id, x.first_name, x.last_name, s.name AS school,
                                CAST(x.status AS BIGINT) AS status",
        }
    }

    fn initial_order(self) -> &'static str {
        match self {
            Self::Standing(_) => "x.id DESC",
            Self::Schools => "x.name",
            Self::Tournaments => "x.date",
            Self::Motions => "x.date_set DESC, x.id DESC",
            Self::Resources { .. } => "x.created_at DESC",
            Self::Teams | Self::Debaters | Self::Videos | Self::Outreach { .. } => "x.id",
        }
    }
}

#[derive(Clone)]
enum FilterValue {
    Number(i64),
    Decimal(f64),
    Text(String),
}

pub(crate) struct Filters {
    pub(crate) query: String,
    params: Vec<FilterValue>,
    default_order: Option<String>,
}

impl Filters {
    fn add(&mut self, condition: &str, value: FilterValue) {
        let placeholder = self.argument(value);
        self.query.push_str(" AND ");
        self.query.push_str(&condition.replace("{}", &placeholder));
    }

    fn argument(&mut self, value: FilterValue) -> String {
        self.params.push(value);
        format!("${}", self.params.len())
    }

    pub(crate) fn bind<'a>(
        &self,
        mut statement: Query<'a, Any, AnyArguments>,
    ) -> Query<'a, Any, AnyArguments> {
        for value in &self.params {
            statement = match value {
                FilterValue::Number(value) => statement.bind(*value),
                FilterValue::Decimal(value) => statement.bind(*value),
                FilterValue::Text(value) => statement.bind(value.clone()),
            };
        }
        statement
    }
}

fn filter(kind: Kind, query: &HashMap<&str, &str>, current: i32, user_id: Option<i64>) -> Filters {
    let mut result = Filters {
        query: kind.count().to_owned(),
        params: Vec::new(),
        default_order: None,
    };
    if let Kind::Standing(standing) = kind {
        standing.filters(&mut result, query);
        return result;
    }
    if matches!(kind, Kind::Motions) {
        result.argument(FilterValue::Number(user_id.unwrap_or(0)));
    }
    let value = |key| query.get(key).copied().unwrap_or_default().trim();
    if !matches!(kind, Kind::Motions)
        && let Ok(id) = value("id").parse::<i64>()
    {
        result.add("x.id = {}", FilterValue::Number(id));
    }
    let text_fields: &[(&str, &str)] = match kind {
        Kind::Standing(_) => unreachable!(),
        Kind::Schools | Kind::Teams => &[("name__icontains", "x.name")],
        Kind::Tournaments => &[("name__icontains", "x.name")],
        Kind::Resources { .. } => &[("title__icontains", "x.title")],
        Kind::Videos => &[("tournament__name__icontains", "t.name")],
        Kind::Motions => &[
            ("text__icontains", "x.text"),
            ("tournament__name__icontains", "t.name"),
        ],
        Kind::Debaters | Kind::Outreach { .. } => &[
            ("first_name__icontains", "x.first_name"),
            ("last_name__icontains", "x.last_name"),
            ("school__name__icontains", "s.name"),
        ],
    };
    for (field, column) in text_fields {
        if !value(field).is_empty() {
            let literal = value(field)
                .replace('!', "!!")
                .replace('%', "!%")
                .replace('_', "!_");
            result.add(
                &format!("LOWER({column}) LIKE LOWER({{}}) ESCAPE '!'"),
                FilterValue::Text(format!("%{literal}%")),
            );
        }
    }
    if matches!(kind, Kind::Debaters | Kind::Outreach { .. }) {
        for (label, column) in [("school", "school_id"), ("status", "status")] {
            if let Ok(number) = value(label).parse::<i64>() {
                result.add(&format!("x.{column} = {{}}"), FilterValue::Number(number));
            }
        }
        let continue_season = value("season_filter_1");
        if !continue_season.is_empty() {
            let condition = match value("season_filter_0") {
                "first_season" => "x.first_season = {}",
                "latest_season" => "x.latest_season = {}",
                "competed_during" => "x.first_season <= {} AND x.latest_season >= {}",
                _ => "",
            };
            if !condition.is_empty() {
                result.add(condition, FilterValue::Text(continue_season.to_owned()));
            }
        }
    }
    if matches!(kind, Kind::Tournaments) {
        let season = value("season");
        if query.is_empty() {
            result.add("x.season = {}", FilterValue::Text(current.to_string()));
        } else if season
            .parse::<i32>()
            .is_ok_and(|year| (2004..=current).contains(&year) && year.to_string() == season)
        {
            result.add("x.season = {}", FilterValue::Text(season.to_owned()));
        }
        if let Ok(qual_type) = value("qual_type").parse::<i64>()
            && TOURNAMENT_TYPES.iter().any(|(id, _)| *id == qual_type)
        {
            result.add("x.qual_type = {}", FilterValue::Number(qual_type));
        }
        for (name, condition, _) in TOURNAMENT_RANGES {
            if let Ok(number) = value(name).parse::<f64>()
                && number.is_finite()
            {
                result.add(
                    &format!("{condition} CAST({{}} AS DOUBLE PRECISION)"),
                    FilterValue::Decimal(number),
                );
            }
        }
    }
    result
}

fn video_filters(
    filters: &mut Filters,
    raw: &[(std::borrow::Cow<'_, str>, std::borrow::Cow<'_, str>)],
    current: i32,
) {
    let value = |key| {
        raw.iter()
            .rfind(|(name, _)| *name == key)
            .map(|(_, value)| value.as_ref())
            .unwrap_or_default()
    };
    let season = value("tournament__season");
    if season
        .parse::<i32>()
        .is_ok_and(|year| (2004..=current).contains(&year) && year.to_string() == season)
    {
        filters.add("t.season = {}", FilterValue::Text(season.to_owned()));
    }
    if let Ok(round) = value("round").parse::<i64>()
        && VIDEO_ROUNDS.contains(&round)
    {
        filters.add("x.round = {}", FilterValue::Number(round));
    }
    for key in ["tags_all", "tags"] {
        let matches = tag_clauses(filters, raw, key, VIDEO_TAG);
        if !matches.is_empty() {
            let op = if key == "tags_all" { " AND " } else { " OR " };
            filters
                .query
                .push_str(&format!(" AND ({})", matches.join(op)));
        }
    }
}

type QueryEntry<'a> = (std::borrow::Cow<'a, str>, std::borrow::Cow<'a, str>);

fn motion_filters(filters: &mut Filters, raw: &[QueryEntry<'_>], user: &session::User) {
    let value = |key| {
        raw.iter()
            .rfind(|(name, _)| *name == key)
            .map(|(_, value)| value.as_ref())
    };
    for (key, comparison) in [("date_from", ">="), ("date_to", "<=")] {
        if let Some(date) = value(key).and_then(|value| {
            ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]
                .iter()
                .find_map(|format| chrono::NaiveDate::parse_from_str(value, format).ok())
        }) {
            filters.add(
                &format!("CAST(x.date_set AS TEXT) {comparison} {{}}"),
                FilterValue::Text(date.to_string()),
            );
        }
    }
    if user.is_authenticated {
        match value("progress").unwrap_or("exclude_done_ignore") {
            "exclude_done_ignore" => filters.query.push_str(" AND u.status IS NULL"),
            "exclude_ignore" => filters
                .query
                .push_str(" AND (u.status IS NULL OR u.status <> 'ignore')"),
            progress @ ("done" | "ignore") => {
                filters.add("u.status = {}", FilterValue::Text(progress.to_owned()))
            }
            _ => {}
        }
    }
    for key in ["tags_all", "topics"] {
        let clauses = tag_clauses(filters, raw, key, MOTION_TAG);
        if !clauses.is_empty() {
            let op = if key == "tags_all" { " AND " } else { " OR " };
            filters
                .query
                .push_str(&format!(" AND ({})", clauses.join(op)));
            if key == "topics" {
                let score = clauses
                    .iter()
                    .map(|clause| format!("CASE WHEN {clause} THEN 1 ELSE 0 END"))
                    .collect::<Vec<_>>()
                    .join(" + ");
                filters.default_order = Some(format!("({score}) DESC, x.date_set DESC, x.id DESC"));
            }
        }
    }
}

fn resource_filters(
    filters: &mut Filters,
    raw: &[QueryEntry<'_>],
    user: &session::User,
    owned: bool,
) {
    let value = |key| {
        raw.iter()
            .rfind(|(name, _)| *name == key)
            .map(|(_, value)| value.as_ref())
            .unwrap_or_default()
            .trim()
    };
    let author = format!(
        "EXISTS ({RESOURCE_AUTHOR} AND d.user_id = {{}} AND NOT d.synthetic AND NOT d.temporary)"
    );
    if owned {
        filters.add(&author, FilterValue::Number(user.id.unwrap_or(0)));
    } else if !user.is_superuser && !user.can_view_private_videos {
        if let Some(id) = user.id {
            filters.add(
                &format!("(x.viewing_permission = 0 OR {author})"),
                FilterValue::Number(id),
            );
        } else {
            filters.query.push_str(" AND x.viewing_permission = 0");
        }
    }
    let resource_type = value("resource_type");
    if crate::resource::TYPES
        .iter()
        .any(|(key, _)| *key == resource_type)
    {
        filters.add(
            "x.resource_type = {}",
            FilterValue::Text(resource_type.to_owned()),
        );
    }
    for (key, column) in [
        ("authors__first_name__icontains", "first_name"),
        ("authors__last_name__icontains", "last_name"),
    ] {
        if !value(key).is_empty() {
            let term = value(key)
                .replace('!', "!!")
                .replace('%', "!%")
                .replace('_', "!_");
            filters.add(
                &format!(
                    "EXISTS ({RESOURCE_AUTHOR} AND LOWER(d.{column}) LIKE LOWER({{}}) ESCAPE '!')"
                ),
                FilterValue::Text(format!("%{term}%")),
            );
        }
    }
    for key in ["tags_all", "tags"] {
        let matches = tag_clauses(filters, raw, key, RESOURCE_TAG);
        if !matches.is_empty() {
            let op = if key == "tags_all" { " AND " } else { " OR " };
            filters
                .query
                .push_str(&format!(" AND ({})", matches.join(op)));
        }
    }
}

fn tag_clauses(
    filters: &mut Filters,
    raw: &[QueryEntry<'_>],
    key: &str,
    template: &str,
) -> Vec<String> {
    crate::tag_filter::parse(
        raw.iter()
            .filter(|(name, _)| *name == key)
            .map(|(_, value)| value),
    )
    .into_iter()
    .map(|tag| {
        let placeholder = filters.argument(FilterValue::Text(tag));
        format!("EXISTS ({})", template.replace("{}", &placeholder))
    })
    .collect()
}

fn updated_uri(
    input: &[(std::borrow::Cow<'_, str>, std::borrow::Cow<'_, str>)],
    key: &str,
    value: &str,
) -> String {
    let mut encoded = url::form_urlencoded::Serializer::new(String::new());
    let mut replaced = false;
    for (old_key, old_value) in input {
        if old_key != key {
            encoded.append_pair(old_key, old_value);
        } else if !replaced {
            encoded.append_pair(key, value);
            replaced = true;
        }
    }
    if !replaced {
        encoded.append_pair(key, value);
    }
    format!("?{}", encoded.finish())
}

fn pages(current: i64, total: i64) -> Vec<Option<i64>> {
    if total < 2 {
        return Vec::new();
    }
    if total <= 10 {
        return (1..=total).map(Some).collect();
    }
    let start = (current - 5).max(1).min(total - 9);
    let mut list = (start..start + 10).map(Some).collect::<Vec<_>>();
    if start != 1 {
        list[0] = Some(1);
        list[1] = None;
    }
    if start + 9 != total {
        list[8] = None;
        list[9] = Some(total);
    }
    list
}

pub(crate) type RowCells = Vec<Vec<(String, String)>>;

async fn rows(
    pool: &AnyPool,
    kind: Kind,
    filters: &Filters,
    order_by: &str,
    offset: i64,
    limit: i64,
    raw: &[QueryEntry<'_>],
) -> Result<Vec<RowCells>, ApiError> {
    let order = kind
        .columns()
        .iter()
        .find(|field| !field.order.is_empty() && field.name == order_by.trim_start_matches('-'))
        .map(|field| {
            field
                .order
                .split(", ")
                .map(|column| {
                    if order_by.starts_with('-') {
                        format!("{column} DESC")
                    } else {
                        column.to_owned()
                    }
                })
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_else(|| {
            filters
                .default_order
                .as_deref()
                .unwrap_or(kind.initial_order())
                .to_owned()
        });
    let projection = match kind {
        Kind::Standing(standing) => std::borrow::Cow::Owned(standing.projection()),
        _ => std::borrow::Cow::Borrowed(kind.projection(order_by)),
    };
    let query = format!(
        "SELECT {projection} {} ORDER BY {order} LIMIT ${} OFFSET ${}",
        filters.query,
        filters.params.len() + 1,
        filters.params.len() + 2
    );
    let result = filters
        .bind(sqlx::query(AssertSqlSafe(query)))
        .bind(limit)
        .bind(offset)
        .fetch_all(pool)
        .await?;
    let mut table = Vec::new();
    let mut connection = pool.acquire().await?;
    for row in result {
        let id: i64 = row.try_get("id")?;
        let url = format!("/core/{}/{id}", kind.title().to_ascii_lowercase());
        let empty = String::new;
        let mut cells = vec![vec![(id.to_string(), url.clone())]];
        match kind {
            Kind::Standing(standing) => {
                cells = standing.cells(&row, &mut connection).await?;
            }
            Kind::Schools => {
                cells.push(vec![(row.try_get("name")?, url)]);
                let text = if row.try_get::<i64, _>("included")? != 0 {
                    "True"
                } else {
                    "False"
                };
                cells.push(vec![(text.into(), empty())]);
            }
            Kind::Teams => {
                cells.push(vec![(row.try_get("name")?, url)]);
                let people = team_profile::members(&mut connection, id, "").await?;
                cells.push(
                    people
                        .iter()
                        .map(|person| {
                            (
                                person["name"].as_str().unwrap_or_default().into(),
                                person["url"].as_str().unwrap_or_default().into(),
                            )
                        })
                        .collect(),
                );
            }
            Kind::Resources { .. } => {
                cells.clear();
                cells.push(vec![(
                    row.try_get("title")?,
                    format!("/core/resources/{id}"),
                )]);
                let mut authors = Vec::new();
                for row in sqlx::query(crate::resource::AUTHORS)
                    .bind(id)
                    .fetch_all(&mut *connection)
                    .await?
                {
                    let id: i64 = row.try_get("id")?;
                    authors.push((row.try_get("name")?, format!("/core/debaters/{id}")));
                }
                cells.push(authors);
                let kind: &str = row.try_get("resource_type")?;
                cells.push(vec![(
                    crate::resource::type_label(kind).to_owned(),
                    updated_uri(raw, "resource_type", kind),
                )]);
                let existing = raw
                    .iter()
                    .rfind(|(key, _)| key == "tags")
                    .map(|(_, value)| value.as_ref())
                    .unwrap_or_default();
                let mut tags = Vec::new();
                for row in sqlx::query(crate::resource::TAGS)
                    .bind(id)
                    .fetch_all(&mut *connection)
                    .await?
                {
                    let slug: &str = row.try_get("slug")?;
                    let tag = if existing.is_empty() {
                        slug.to_owned()
                    } else {
                        format!("{existing},{slug}")
                    };
                    tags.push((row.try_get("name")?, updated_uri(raw, "tags", &tag)));
                }
                cells.push(tags);
                cells.push(vec![(
                    crate::resource::created_date(row.try_get("created")?),
                    empty(),
                )]);
            }
            Kind::Motions => {
                cells.clear();
                cells.push(vec![(row.try_get("text")?, url)]);
                let date = row
                    .try_get::<Option<String>, _>("date")?
                    .and_then(|date| chrono::NaiveDate::parse_from_str(&date, "%Y-%m-%d").ok())
                    .map(|date| date.format("%m/%d/%Y").to_string())
                    .unwrap_or_else(|| "—".to_owned());
                let topics = sqlx::query_scalar::<_, String>(crate::motion::TOPICS)
                    .bind(id)
                    .fetch_all(&mut *connection)
                    .await?
                    .join(", ");
                for text in [
                    date,
                    row.try_get::<Option<String>, _>("tournament")?
                        .unwrap_or_else(|| "—".into()),
                    topics,
                    row.try_get::<Option<String>, _>("progress")?
                        .unwrap_or_else(|| "—".into()),
                ] {
                    cells.push(vec![(text, empty())]);
                }
            }
            Kind::Videos => {
                let season: String = row.try_get("season")?;
                let season = season
                    .parse()
                    .map(crate::schedule::season_display)
                    .unwrap_or(season);
                let (round, permissions) = crate::tournament_profile::labels(
                    row.try_get("round")?,
                    row.try_get("permissions")?,
                );
                let label = |value: serde_json::Value| {
                    value
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| value.to_string())
                };
                for text in [
                    row.try_get("name")?,
                    season,
                    label(round),
                    row.try_get("pm")?,
                    row.try_get("mg")?,
                    row.try_get("lo")?,
                    row.try_get("mo")?,
                    label(permissions),
                ] {
                    cells.push(vec![(text, empty())]);
                }
            }
            Kind::Tournaments => {
                cells.push(vec![(row.try_get("name")?, url)]);
                let date: String = row.try_get("date")?;
                let date = chrono::NaiveDate::parse_from_str(&date, "%Y-%m-%d")
                    .map(|date| date.format("%m/%d/%Y").to_string())
                    .unwrap_or(date);
                let season: String = row.try_get("season")?;
                let season = season
                    .parse()
                    .map(crate::schedule::season_display)
                    .unwrap_or(season);
                for text in [
                    date,
                    season,
                    row.try_get::<i64, _>("num_teams")?.to_string(),
                    row.try_get::<i64, _>("points")?.to_string(),
                ] {
                    cells.push(vec![(text, empty())]);
                }
                let winner: Option<i64> = row.try_get("winner")?;
                let mut names = Vec::new();
                if let Some(team) = winner {
                    for member in team_profile::members(&mut connection, team, "").await? {
                        if let Some(name) = member["name"].as_str() {
                            names.push((name.to_owned(), empty()));
                        }
                    }
                }
                if names.is_empty() {
                    names.push(("—".to_owned(), empty()));
                }
                cells.push(names);
                let first: Option<String> = row.try_get("speaker_first_name")?;
                let last: Option<String> = row.try_get("speaker_last_name")?;
                let speaker = first.zip(last).map_or_else(
                    || "—".to_owned(),
                    |(first, last)| format!("{first} {last}").trim().to_owned(),
                );
                cells.push(vec![(speaker, empty())]);
            }
            Kind::Debaters | Kind::Outreach { .. } => {
                cells.push(vec![(row.try_get("first_name")?, url.clone())]);
                let last_name = row.try_get::<String, _>("last_name")?;
                cells.push(vec![(
                    if last_name.is_empty() {
                        "—".into()
                    } else {
                        last_name
                    },
                    url,
                )]);
                cells.push(vec![(
                    row.try_get::<Option<String>, _>("school")?
                        .unwrap_or_else(|| "—".into()),
                    empty(),
                )]);
                let status: i64 = row.try_get("status")?;
                let label = match status {
                    0 => "Novice".into(),
                    1 => "Varsity".into(),
                    2 => "Dino".into(),
                    _ => status.to_string(),
                };
                cells.push(vec![(label, empty())]);
            }
        }
        table.push(cells);
    }
    Ok(table)
}

#[derive(Serialize)]
struct Input<'a> {
    name: &'a str,
    label: &'a str,
    value: &'a str,
    numeric: bool,
    options: Vec<(String, String)>,
    autocomplete: Option<&'static str>,
    date: bool,
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let kind = match uri.path() {
        "/core/schools/" => Kind::Schools,
        "/core/toty" => Kind::Standing(legacy::Standing::Team),
        "/core/soty" => Kind::Standing(legacy::Standing::Speaker),
        "/core/noty" => Kind::Standing(legacy::Standing::Novice),
        "/core/coty" => Kind::Standing(legacy::Standing::Club),
        "/core/dino-judges/" => Kind::Outreach { judge: true },
        "/core/dino-tos/" => Kind::Outreach { judge: false },
        "/core/teams/" => Kind::Teams,
        "/core/tournaments/" => Kind::Tournaments,
        "/core/videos/" | "/core/videos/random" => Kind::Videos,
        "/core/motions/" | "/core/motions/random" => Kind::Motions,
        "/core/resources/" => Kind::Resources { owned: false },
        "/core/resources/my-resources" => Kind::Resources { owned: true },
        _ => Kind::Debaters,
    };
    let raw: Vec<_> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let query: HashMap<_, _> = raw
        .iter()
        .map(|(key, value)| (key.as_ref(), value.as_ref()))
        .collect();
    let param = |key| query.get(key).copied().unwrap_or_default();
    let order_by = param("sort");
    if matches!(kind, Kind::Standing(_)) && param("season").is_empty() {
        return (
            StatusCode::FOUND,
            [(
                header::LOCATION,
                format!("{}?season={}", uri.path(), state.current_season),
            )],
        )
            .into_response();
    }
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if matches!(kind, Kind::Resources { owned: true }) {
        if !user.is_authenticated {
            return crate::login::redirect_to_login(&uri);
        }
        if !session::has_permission(&state.pool, &user, "view_resource")
            .await
            .unwrap_or(false)
        {
            return web::permission_denied(&state, &user, &uri);
        }
    }
    let mut filters = filter(kind, &query, state.current_season, user.id);
    match kind {
        Kind::Videos => video_filters(&mut filters, &raw, state.current_season),
        Kind::Motions => motion_filters(&mut filters, &raw, &user),
        Kind::Resources { owned } => resource_filters(&mut filters, &raw, &user, owned),
        _ => (),
    }
    if uri.path().ends_with("/random") {
        return random_media(&state, &user, &uri, kind, filters).await;
    }
    let outreach = if let Kind::Outreach { judge } = kind {
        let region = param("region");
        let region = if crate::outreach::REGIONS.contains(&region) {
            region
        } else {
            ""
        };
        match crate::outreach::rows(&state.pool, &filters, judge, region, order_by).await {
            Ok(rows) => Some(rows),
            Err(error) => {
                eprintln!("Outreach lookup failed: {error:?}");
                return StatusCode::INTERNAL_SERVER_ERROR.into_response();
            }
        }
    } else {
        None
    };
    let count = if let Some(outreach) = &outreach {
        outreach.len() as i64
    } else {
        let count_sql = format!("SELECT CAST(COUNT(*) AS BIGINT) AS count {}", filters.query);
        let count = filters
            .bind(sqlx::query(AssertSqlSafe(count_sql)))
            .fetch_one(&state.pool)
            .await
            .and_then(|row| row.try_get::<i64, _>("count"));
        let Ok(count) = count else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        count
    };
    let per_page = param("per_page")
        .parse::<i64>()
        .ok()
        .filter(|n| *n > 0)
        .unwrap_or(25)
        .min(1000);
    let total = ((count + per_page - 1) / per_page).max(1);
    let page = param("page")
        .parse::<i64>()
        .ok()
        .map(|page| if page <= 0 { total } else { page.min(total) })
        .unwrap_or(1);
    let offset = (page - 1) * per_page;
    let rows = if let Some(outreach) = outreach {
        outreach
            .into_iter()
            .skip(offset as usize)
            .take(per_page as usize)
            .collect()
    } else {
        match rows(
            &state.pool,
            kind,
            &filters,
            order_by,
            offset,
            per_page,
            &raw,
        )
        .await
        {
            Ok(rows) => rows,
            Err(error) => {
                eprintln!("Directory lookup failed: {error:?}");
                return StatusCode::INTERNAL_SERVER_ERROR.into_response();
            }
        }
    };
    let mut buttons = Vec::new();
    let actions: &[(&str, &str, &str, &str)] = match kind {
        Kind::Schools => &[(
            "add_school",
            "/core/schools/create",
            "Create",
            "btn-success",
        )],
        Kind::Debaters => &[(
            "add_debater",
            "/core/debaters/create",
            "Create",
            "btn-success",
        )],
        Kind::Tournaments => &[
            (
                "add_tournament",
                "/core/tournaments/create",
                "Create",
                "btn-success",
            ),
            (
                "change_tournament",
                "/core/tournaments/data_entry",
                "Enter Results",
                "btn-primary",
            ),
        ],
        Kind::Teams | Kind::Outreach { .. } | Kind::Standing(_) => &[],
        Kind::Resources { owned: false } => &[(
            "add_resource",
            "/core/resources/create",
            "Create",
            "btn-success",
        )],
        Kind::Resources { owned: true } => &[],
        Kind::Motions => &[
            (
                "add_motion",
                "/core/motions/create",
                "Create",
                "btn-success",
            ),
            (
                "add_motion",
                "/core/motions/import",
                "Bulk import",
                "btn-info",
            ),
        ],
        Kind::Videos => &[("add_video", "/core/videos/create", "Create", "btn-success")],
    };
    for (permission, href, name, class) in actions {
        if session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false)
        {
            buttons.push(json!({"name":name, "class":class, "href":href}));
        }
    }
    let input = |name, label, numeric, options| Input {
        name,
        label,
        value: param(name),
        numeric,
        options,
        autocomplete: None,
        date: false,
    };
    let mut fields = vec![input("id", "ID", true, Vec::new())];
    let topic_input = |name, label, source| {
        let tags = raw
            .iter()
            .filter(|(key, _)| key == name)
            .flat_map(|(_, value)| value.split(','))
            .filter(|tag| !tag.is_empty())
            .map(|tag| (tag.to_owned(), tag.to_owned()))
            .collect();
        Input {
            autocomplete: Some(source),
            ..input(name, label, false, tags)
        }
    };
    let mut season_filters = Vec::new();
    let current = state.current_season.to_string();
    if matches!(kind, Kind::Debaters | Kind::Outreach { .. }) {
        let schools = sqlx::query("SELECT CAST(id AS BIGINT) AS id, name FROM core_school WHERE NOT temporary ORDER BY name")
            .fetch_all(&state.pool).await;
        let Ok(schools) = schools else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        let mut options = vec![(String::new(), "---------".to_owned())];
        options.extend(schools.iter().filter_map(|row| {
            Some((
                row.try_get::<i64, _>("id").ok()?.to_string(),
                row.try_get::<String, _>("name").ok()?,
            ))
        }));
        fields.extend([
            input(
                "first_name__icontains",
                "First name contains",
                false,
                Vec::new(),
            ),
            input(
                "last_name__icontains",
                "Last name contains",
                false,
                Vec::new(),
            ),
            input("school", "School", false, options),
            input(
                "school__name__icontains",
                "School name contains",
                false,
                Vec::new(),
            ),
            input(
                "status",
                "Status",
                false,
                [
                    ("", "---------"),
                    ("1", "Varsity"),
                    ("0", "Novice"),
                    ("2", "Dino"),
                ]
                .map(|(value, label)| (value.to_owned(), label.to_owned()))
                .to_vec(),
            ),
        ]);
        if matches!(kind, Kind::Outreach { .. }) {
            let mut options = vec![(String::new(), "All regions".to_owned())];
            options.extend(crate::outreach::REGIONS.iter().map(|region| {
                (
                    (*region).to_owned(),
                    crate::schools::region_label(region).to_owned(),
                )
            }));
            fields.push(input("region", "Region", false, options));
        }
        let mut seasons = vec![(String::new(), "Select Season".into())];
        seasons.extend(web::seasons(state.current_season));
        season_filters.extend([
            input(
                "season_filter_0",
                "",
                false,
                [
                    ("", "Select Filter Type"),
                    ("first_season", "Started in Season"),
                    ("latest_season", "Last Competed in Season"),
                    ("competed_during", "Competed During Season"),
                ]
                .map(|(value, label)| (value.to_owned(), label.to_owned()))
                .to_vec(),
            ),
            input("season_filter_1", "", false, seasons),
        ]);
    } else if matches!(kind, Kind::Videos) {
        fields.push(input(
            "tournament__name__icontains",
            "Tournament name contains",
            false,
            Vec::new(),
        ));
        let mut rounds = vec![(String::new(), "---------".to_owned())];
        rounds.extend(VIDEO_ROUNDS.iter().map(|round| {
            let (label, _) = crate::tournament_profile::labels(*round, 0);
            (
                round.to_string(),
                label.as_str().unwrap_or_default().to_owned(),
            )
        }));
        fields.push(input("round", "Round", false, rounds));
        for (name, label) in [
            ("tags_all", "Contains all of these tags"),
            ("tags", "Contains any of these tags"),
        ] {
            fields.push(topic_input(name, label, "/core/videos/tags_no_create"));
        }
        let mut seasons = vec![(String::new(), "Any Season".to_owned())];
        seasons.extend(web::seasons(state.current_season));
        fields.push(input("tournament__season", "Season", false, seasons));
    } else if matches!(kind, Kind::Resources { .. }) {
        fields.extend([
            input("title__icontains", "Title contains", false, Vec::new()),
            input(
                "authors__first_name__icontains",
                "Authors first name contains",
                false,
                Vec::new(),
            ),
            input(
                "authors__last_name__icontains",
                "Authors last name contains",
                false,
                Vec::new(),
            ),
            topic_input(
                "tags_all",
                "Contains all of these tags",
                "/core/resources/tags_no_create",
            ),
            topic_input(
                "tags",
                "Contains any of these tags",
                "/core/resources/tags_no_create",
            ),
        ]);
        let mut types = vec![(String::new(), "Any Type".to_owned())];
        types.extend(
            crate::resource::TYPES
                .iter()
                .map(|(key, label)| ((*key).to_owned(), (*label).to_owned())),
        );
        fields.push(input("resource_type", "Type", false, types));
    } else if matches!(kind, Kind::Motions) {
        fields = vec![
            input("text__icontains", "Motion text contains", false, Vec::new()),
            input(
                "tournament__name__icontains",
                "Tournament name contains",
                false,
                Vec::new(),
            ),
            topic_input(
                "tags_all",
                "Contains all topics",
                "/core/motions/topics_no_create",
            ),
            topic_input(
                "topics",
                "Best match for topics",
                "/core/motions/topics_no_create",
            ),
            Input {
                date: true,
                ..input("date_from", "Date set from", false, Vec::new())
            },
            Input {
                date: true,
                ..input("date_to", "Date set through", false, Vec::new())
            },
            Input {
                value: query
                    .get("progress")
                    .copied()
                    .unwrap_or("exclude_done_ignore"),
                ..input(
                    "progress",
                    "Progress",
                    false,
                    MOTION_PROGRESS
                        .iter()
                        .map(|(key, label)| ((*key).to_owned(), (*label).to_owned()))
                        .collect(),
                )
            },
        ];
    } else if let Kind::Standing(standing) = kind {
        fields = standing.inputs(&query);
    } else if matches!(kind, Kind::Tournaments) {
        fields.push(input("name__icontains", "Name contains", false, Vec::new()));
        let mut types = vec![(String::new(), "---------".to_owned())];
        types.extend(
            TOURNAMENT_TYPES
                .iter()
                .map(|(id, name)| (id.to_string(), (*name).to_owned())),
        );
        fields.push(input("qual_type", "Tournament Type", false, types));
        let mut seasons = vec![(String::new(), "Any Season".to_owned())];
        seasons.extend(web::seasons(state.current_season));
        fields.push(input("season", "Season", false, seasons));
        if query.is_empty() {
            fields.last_mut().expect("season input").value = &current;
        }
        fields.extend(
            TOURNAMENT_RANGES
                .iter()
                .map(|(name, _, label)| input(name, label, true, Vec::new())),
        );
    } else {
        fields.push(input("name__icontains", "Name contains", false, Vec::new()));
    }
    let displayed_order = if order_by.is_empty() && matches!(kind, Kind::Outreach { .. }) {
        "latest_year"
    } else {
        order_by
    };
    let columns = kind
        .columns()
        .iter()
        .map(|field| {
            let desc = displayed_order.starts_with('-')
                && displayed_order.trim_start_matches('-') == field.name;
            let asc = displayed_order == field.name;
            let next_order = if asc {
                format!("-{}", field.name)
            } else {
                field.name.to_owned()
            };
            let class = if desc {
                "desc orderable"
            } else if asc {
                "asc orderable"
            } else {
                "orderable"
            };
            let url = (!field.order.is_empty()).then(|| updated_uri(&raw, "sort", &next_order));
            json!({"label":field.label, "asc":asc, "desc":desc, "class":class, "url":url})
        })
        .collect::<Vec<_>>();
    let page_link = |page: i64| updated_uri(&raw, "page", &page.to_string());
    let pagination = pages(page, total)
        .into_iter()
        .map(|p| {
            json!({
                "label": p.map(|n| n.to_string()).unwrap_or_else(|| "...".into()),
                "url": p.map(page_link), "active": p == Some(page),
            })
        })
        .collect::<Vec<_>>();
    let common = web::base_context(&state, &user, &uri);
    let is_motion = matches!(kind, Kind::Motions);
    let is_media = matches!(kind, Kind::Motions | Kind::Videos);
    let empty_random = is_media && session::cookie(&headers, "br_media_random_empty") == Some("1");
    let messages = if is_motion {
        state
            .secret
            .as_deref()
            .map(|secret| crate::flash::messages(&headers, secret))
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let page_values = context! {
        messages, fields, season_filters, title => kind.title(), buttons, rows, columns, pagination,
        random_url => is_media.then(|| format!("{}random", uri.path())),
        clear_url => is_motion.then_some("/core/motions/"),
        warning => empty_random.then_some(if is_motion { "No motions match those filters." }
            else { "No results found, or you don't have access to any matching videos." }),
        motion_warning => is_motion,
        resource_rows => matches!(kind, Kind::Resources { .. }),
        comma_cells => matches!(kind, Kind::Resources { .. } | Kind::Outreach { .. }),
        striped => matches!(kind, Kind::Outreach { .. }),
        previous => (page > 1).then(|| page_link(page - 1)),
        next => (page < total).then(|| page_link(page + 1)),
    };
    match web::render("core/directory.html", context! {..common, ..page_values}) {
        Ok(html) => {
            let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
            if !messages.is_empty() {
                crate::flash::clear(
                    &mut response,
                    state
                        .origin(&headers)
                        .is_some_and(|url| url.starts_with("https://")),
                );
            }
            if empty_random {
                crate::login::set_cookie(
                    &mut response,
                    "br_media_random_empty",
                    "",
                    Some(0),
                    state
                        .origin(&headers)
                        .is_some_and(|url| url.starts_with("https://")),
                    true,
                );
            }
            response
        }
        Err(error) => {
            eprintln!("Directory template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn random_media(
    state: &AppState,
    user: &session::User,
    uri: &axum::http::Uri,
    kind: Kind,
    mut filters: Filters,
) -> Response {
    if matches!(kind, Kind::Videos) && !user.can_view_video {
        let max_permission = if user.can_view_private_videos { 1 } else { 0 };
        filters.add(
            "x.permissions IN (0, {})",
            FilterValue::Number(max_permission),
        );
    }
    let sql = format!(
        "SELECT CAST(x.id AS BIGINT) AS id {} ORDER BY RANDOM() LIMIT 1",
        filters.query
    );
    let row = filters
        .bind(sqlx::query(AssertSqlSafe(sql)))
        .fetch_optional(&state.pool)
        .await;
    let id = match row {
        Ok(Some(row)) => Some(row.get::<i64, _>("id")),
        Ok(None) => None,
        Err(error) => {
            eprintln!("Random video lookup failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let base = uri.path().strip_suffix("random").unwrap_or(uri.path());
    let location = if let Some(id) = id {
        format!("{base}{id}")
    } else {
        let query = uri.query().unwrap_or_default();
        let parsed: Vec<_> = url::form_urlencoded::parse(query.as_bytes()).collect();
        if matches!(kind, Kind::Motions) && !parsed.iter().any(|(key, _)| key == "progress") {
            format!(
                "{base}{}",
                updated_uri(&parsed, "progress", "exclude_done_ignore")
            )
        } else if query.is_empty() {
            base.to_owned()
        } else {
            format!("{base}?{query}")
        }
    };
    let mut response = (
        StatusCode::FOUND,
        [
            (header::LOCATION, location),
            (header::VARY, "Cookie".to_owned()),
        ],
    )
        .into_response();
    if id.is_none() {
        crate::login::set_cookie(
            &mut response,
            "br_media_random_empty",
            "1",
            Some(60),
            state
                .public_origin
                .as_ref()
                .is_some_and(|url| url.starts_with("https://")),
            true,
        );
    }
    response
}
