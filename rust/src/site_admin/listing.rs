mod dates;
mod facets;
pub(super) mod filters;

use std::{
    borrow::Cow,
    collections::{BTreeMap, HashMap},
    sync::LazyLock,
};

use axum::{
    body::Body,
    http::Method,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use minijinja::{Value, context};
use sqlx::{AssertSqlSafe, Row, any::AnyRow};

use super::{
    AdminRequest, cap_first, redirect,
    schema::{self, Field, Model, schema},
};
use crate::{ApiError, flash, login};
use filters::Filter;

pub(super) type Parameters = BTreeMap<String, String>;

fn link(params: &Parameters, name: &str, value: Option<&str>) -> String {
    let mut params = params.clone();
    params.remove("p");
    match value {
        Some(value) => {
            params.insert(name.to_owned(), value.to_owned());
        }
        None => {
            params.remove(name);
        }
    }
    query_string(&params)
}

fn query_string(params: &Parameters) -> String {
    format!(
        "?{}",
        url::form_urlencoded::Serializer::new(String::new())
            .extend_pairs(params)
            .finish()
            .replace('*', "%2A")
            .replace("%7E", "~")
    )
}

pub(super) struct Source<'a> {
    model: &'a Model,
    joins: Vec<String>,
    paths: HashMap<String, (usize, &'static Model)>,
    allow_many: bool,
}

impl<'a> Source<'a> {
    pub(super) fn new(model: &'a Model) -> Self {
        Self {
            model,
            joins: Vec::new(),
            paths: HashMap::new(),
            allow_many: false,
        }
    }

    pub(super) fn resolve(&mut self, path: &str) -> Option<(String, &Field)> {
        let mut model = self.model;
        let mut index = 0;
        let mut prefix = String::new();
        let mut parts = path.split("__").peekable();
        while let Some(raw) = parts.next() {
            let name = if raw == "pk" {
                "id"
            } else {
                raw.strip_suffix("_id")
                    .filter(|name| model.fields.contains_key(*name))
                    .unwrap_or(raw)
            };
            let field = model.fields.get(name)?;
            if parts.peek().is_none() {
                let column = field.column.as_ref()?;
                return Some((format!(r#"t{index}."{column}""#), field));
            }
            if !prefix.is_empty() {
                prefix.push_str("__");
            }
            prefix.push_str(name);
            if let Some(&(found, related)) = self.paths.get(&prefix) {
                index = found;
                model = related;
                continue;
            }
            let (app, table) = field.related.as_deref()?.split_once('.')?;
            let related = schema().model(app, table)?;
            let expr = if let Some([table, from, to]) = &field.through {
                if !self.allow_many || field.generic_through.unwrap_or_default() {
                    return None;
                }
                let member = self.joins.len() + 1;
                self.joins.push(format!(
                    r#"LEFT JOIN {table} t{member} ON t{member}."{from}"=t{index}.id"#
                ));
                format!(r#"t{member}."{to}""#)
            } else {
                let column = field.column.as_ref()?;
                format!(r#"t{index}."{column}""#)
            };
            let next = self.joins.len() + 1;
            self.joins.push(format!(
                "LEFT JOIN {} t{next} ON t{next}.id={expr}",
                related.table
            ));
            self.paths.insert(prefix.clone(), (next, related));
            index = next;
            model = related;
        }
        None
    }

    pub(super) fn sql(&self) -> String {
        format!("{} t0 {}", self.model.table, self.joins.join(" "))
    }
}

fn add_values(data: &mut Vec<String>, value: &str) -> String {
    data.push(value.to_owned());
    format!("${}", data.len())
}

pub(super) fn projection(source: &mut Source, name: &str) -> String {
    if name == "__str__" {
        return label(source, source.model, "");
    }
    if name == "name" && source.model.slug() == "debateraliasgroup" {
        return label(source, source.model, "");
    }
    if matches!(
        name,
        "debater_names" | "old_debaters" | "new_debaters" | "old_team_name" | "new_team_name"
    ) {
        let team = if name.starts_with("old_") {
            "t0.old_team_id"
        } else if name.starts_with("new_") {
            "t0.new_team_id"
        } else {
            "t0.team_id"
        };
        let person = "TRIM(d.first_name || ' ' || d.last_name)";
        let (value, separator) = if matches!(name, "old_team_name" | "new_team_name") {
            let escape =
                format!("REPLACE(REPLACE(REPLACE({person},'&','&amp;'),'<','&lt;'),'>','&gt;')");
            (
                format!(
                    r#"'<a href="/core/debaters/' || CAST(d.id AS TEXT) || '">' || {escape} || '</a>'"#
                ),
                " and ",
            )
        } else {
            (person.to_owned(), ", ")
        };
        return format!("(SELECT STRING_AGG(name,'{separator}') FROM (
            SELECT {value} AS name FROM core_team_debaters td JOIN core_debater d ON td.debater_id=d.id
            WHERE td.team_id={team} AND NOT(d.temporary OR d.synthetic) ORDER BY d.id
        ) names)");
    }
    let expanded = match name {
        "debater_name" => "debater",
        "team_name" => "team",
        "tournament_name" if source.model.slug() == "roundstats" => "round__tournament",
        "tournament_name" if source.model.slug() == "importedroundmetadata" => "round__tournament",
        "tournament_name" => "tournament",
        "tournament_season" => "tournament__season",
        "type_of_place_display" => "type_of_place",
        "victor_display" => "victor",
        "school_name" => "school",
        "host_name" => "host",
        "old_team_name" => "old_team",
        "new_team_name" => "new_team",
        "short_text" => "text",
        "user_email" => "user__email",
        name => name,
    };
    if let Some((expr, field)) = source.resolve(expanded) {
        if (field.kind == "ForeignKey" || field.kind == "OneToOneField")
            && let Some((app, model)) = field
                .related
                .as_deref()
                .and_then(|name| name.split_once('.'))
            && let Some(model) = schema().model(app, model)
        {
            return label(source, model, expanded);
        }
        if field.related.as_deref() == Some("contenttypes.contenttype") {
            let variants = schema()
                .content_types
                .iter()
                .map(|(key, title)| {
                    format!(
                        "WHEN '{}' THEN '{}'",
                        key.replace('\'', "''"),
                        title.replace('\'', "''")
                    )
                })
                .collect::<Vec<_>>()
                .join(" ");
            return format!("(SELECT CASE ct.app_label || '.' || ct.model {variants}
                ELSE ct.app_label || ' | ' || ct.model END FROM django_content_type ct WHERE ct.id={expr})");
        }
        if field.kind == "BooleanField" {
            return format!(
                "CASE WHEN {expr} THEN 'True' WHEN {expr} IS NULL THEN NULL ELSE 'False' END"
            );
        }
        return format!("CAST({expr} AS TEXT)");
    }
    match name {
        "round_summary" => {
            let round = if source.model.slug() == "round" {
                ""
            } else {
                "round__"
            };
            let number = source
                .resolve(&format!("{round}round_number"))
                .map(|(sql, _)| sql);
            let name = source
                .resolve(&format!("{round}round_label"))
                .map(|(sql, _)| sql);
            if let (Some(number), Some(name)) = (number, name) {
                let fallback = format!("'Round ' || CAST({number} AS TEXT)");
                if round.is_empty() {
                    return format!(
                        "CASE WHEN {name}='' THEN {fallback} ELSE {name} || ' (#' || CAST({number} AS TEXT) || ')' END"
                    );
                }
                let team = schema().model("core", "team").expect("Team schema");
                let gov = label(source, team, "round__gov");
                let opp = label(source, team, "round__opp");
                return format!(
                    "COALESCE(NULLIF({name},''),{fallback}) || ' / ' || {gov} || ' vs ' || {opp}"
                );
            }
            "NULL".to_owned()
        }
        "stats_count" => {
            "CAST((SELECT COUNT(*) FROM core_roundstats stat WHERE stat.round_id=t0.id) AS TEXT)"
                .to_owned()
        }
        "tournament_import_count" => {
            "CAST((SELECT COUNT(*) FROM core_tournamentimport im WHERE im.batch_id=t0.id) AS TEXT)"
                .to_owned()
        }
        "topic_list" if source.model.slug()=="motion" => {
            "(SELECT STRING_AGG(name, ', ') FROM (
                SELECT topic.name FROM core_taggedmotion link
                JOIN core_motiontopic topic ON topic.id=link.tag_id WHERE link.content_object_id=t0.id
                ORDER BY link.id
             ) names)".to_owned()
        }
        "hidden_members" if source.model.slug()=="teamresult" => {
            "COALESCE((SELECT STRING_AGG(name, ', ') FROM (
                SELECT TRIM(d.first_name || ' ' || d.last_name) AS name
                FROM core_teamresult_hidden_from_debaters m JOIN core_debater d ON d.id=m.debater_id
                WHERE m.teamresult_id=t0.id AND NOT d.temporary AND NOT d.synthetic ORDER BY d.id
            ) names),'—')".to_owned()
        }
        "usage_count" if source.model.slug() == "motiontopic" => {
            "CAST((SELECT COUNT(*) FROM core_taggedmotion WHERE tag_id=t0.id) AS TEXT)".to_owned()
        }
        "usage_count" if source.model.key == "taggit.tag" => {
            "CAST((SELECT COUNT(*) FROM taggit_taggeditem WHERE tag_id=t0.id) AS TEXT)".to_owned()
        }
        _ => "NULL".to_owned(),
    }
}

pub(super) fn label(source: &mut Source, target: &Model, prefix: &str) -> String {
    let mut expr = |name: &str| {
        let path = if prefix.is_empty() {
            name.to_owned()
        } else {
            format!("{prefix}__{name}")
        };
        source.resolve(&path).map(|(expr, _)| expr)
    };
    if target.default_repr
        && let Some(id) = expr("id")
    {
        return format!(
            "'{} object (' || CAST({id} AS TEXT) || ')'",
            target.object_name
        );
    }
    match target.slug() {
        "debater" => {
            if let (Some(first), Some(last), Some(temporary)) =
                (expr("first_name"), expr("last_name"), expr("temporary"))
            {
                return format!(
                    "TRIM({first} || ' ' || {last}) || CASE WHEN {temporary} THEN ' (New)' ELSE '' END"
                );
            }
        }
        "school" => {
            if let (Some(name), Some(temporary)) = (expr("name"), expr("temporary")) {
                return format!("{name} || CASE WHEN {temporary} THEN ' (New)' ELSE '' END");
            }
        }
        "claimdebaterrequest" => {
            if let (Some(user), Some(first), Some(last), Some(status)) = (
                expr("requested_by__username"),
                expr("debater__first_name"),
                expr("debater__last_name"),
                expr("status"),
            ) {
                return format!(
                    "'Claim request by ' || {user} || ' for ' || TRIM({first} || ' ' || {last}) || ' (' || {status} || ')'"
                );
            }
        }
        "mergedebaterrequest" => {
            if let (
                Some(id),
                Some(primary_name),
                Some(secondary_name),
                Some(primary),
                Some(secondary),
            ) = (
                expr("id"),
                expr("primary_name"),
                expr("secondary_name"),
                expr("primary_debater_id"),
                expr("secondary_debater_id"),
            ) {
                return format!(
                    "'Merge request ' || CAST({id} AS TEXT) || ' (' ||
                    COALESCE(NULLIF({primary_name},''),CAST({primary} AS TEXT),'None') || ' <- ' ||
                    COALESCE(NULLIF({secondary_name},''),CAST({secondary} AS TEXT),'None') || ')'"
                );
            }
        }
        "debateralias" => {
            if let (Some(source), Some(first), Some(last)) = (
                expr("source_name"),
                expr("debater__first_name"),
                expr("debater__last_name"),
            ) {
                return format!("{source} || ' -> ' || TRIM({first} || ' ' || {last})");
            }
        }
        "debateraliasgroup" => {
            if let (Some(name), Some(id)) = (expr("label"), expr("id")) {
                return format!(
                    "COALESCE(NULLIF({name},''),'Alias Group #' || CAST({id} AS TEXT))"
                );
            }
        }
        "schooladmin" => {
            if let (Some(user), Some(school)) = (expr("user__username"), expr("school__name")) {
                return format!("{user} || ' - ' || {school}");
            }
        }
        "resource" => {
            if let (Some(title), Some(kind)) = (expr("title"), expr("resource_type")) {
                let code = display_choices(&kind, &target.fields["resource_type"]);
                return format!("{title} || ' (' || {code} || ')'");
            }
        }
        "video" => {
            if let (Some(name), Some(season), Some(round)) = (
                expr("tournament__name"),
                expr("tournament__season"),
                expr("round"),
            ) {
                let description = display_choices(&round, &target.fields["round"]);
                return format!(
                    "{name} || ' / ' || {season} || '-' || SUBSTR(CAST(CAST({season} AS INTEGER)+1 AS TEXT),3,2) || ' / ' || {description}"
                );
            }
        }
        "taggedresource" => {
            if let (Some(id), Some(tag)) = (expr("object_id"), expr("tag__name")) {
                let kind = schema()
                    .model("core", "resource")
                    .expect("resource")
                    .fields
                    .get("resource_type")
                    .expect("resource type");
                let category = display_choices("r.resource_type", kind);
                return format!(
                    "COALESCE((SELECT r.title || ' (' || {category} || ')' FROM core_resource r WHERE r.id={id}),'None') || ' tagged with ' || {tag}"
                );
            }
        }
        "sitesetting" => {
            if let (Some(key), Some(value)) = (expr("key"), expr("value")) {
                return format!("{key} || ': ' || CAST({value} AS TEXT)");
            }
        }
        "qualbar" => {
            if let (Some(season), Some(points)) = (expr("season"), expr("points")) {
                return format!("{season} || ' - ' || CAST({points} AS TEXT) || ' points'");
            }
        }
        "tournamentimport" => {
            if let (Some(filename), Some(name), Some(kind)) = (
                expr("original_file_name"),
                expr("tournament__name"),
                expr("import_type"),
            ) {
                return format!("COALESCE(NULLIF({filename},''),{name} || ' ' || {kind})");
            }
        }
        "importbatch" | "schedulingrun" => {
            if let Some(id) = expr("id") {
                let name = if target.slug() == "importbatch" {
                    "Import batch"
                } else {
                    "Scheduling run"
                };
                return format!("'{name} ' || CAST({id} AS TEXT)");
            }
        }
        "motionuserstatus" => {
            if let (Some(user), Some(motion), Some(status)) =
                (expr("user__username"), expr("motion_id"), expr("status"))
            {
                return format!(
                    "{user} || ': ' || CAST({motion} AS TEXT) || ' (' || {status} || ')'"
                );
            }
        }
        "generatedcode" => {
            if let Some(code) = expr("code") {
                return code;
            }
        }
        "tournamentautoqual" => {
            if let (Some(first), Some(last), Some(tournament)) = (
                expr("debater__first_name"),
                expr("debater__last_name"),
                expr("tournament__name"),
            ) {
                return format!("TRIM({first} || ' ' || {last}) || ' — ' || {tournament}");
            }
        }
        "syntheticresolutionlog" => {
            if let (Some(action), Some(kind), Some(synthetic), Some(resolved)) = (
                expr("action"),
                expr("entity_type"),
                expr("synthetic_id"),
                expr("resolved_to_id"),
            ) {
                let action = display_choices(&action, &target.fields["action"]);
                let kind = display_choices(&kind, &target.fields["entity_type"]);
                return format!(
                    "{action} || ' ' || {kind} || ' ' || CAST({synthetic} AS TEXT) || ' -> ' ||
                    COALESCE(CAST({resolved} AS TEXT),'None')"
                );
            }
        }
        "importedroundmetadata" => {
            if let Some(round) = expr("round_id") {
                return format!("'Imported metadata for round ' || CAST({round} AS TEXT)");
            }
        }
        _ => {}
    }
    for column in [
        "name",
        "username",
        "title",
        "email",
        "source_name",
        "original_name",
        "key",
        "label",
        "text",
        "slug",
    ] {
        if let Some(column) = expr(column) {
            return format!("CAST({column} AS TEXT)");
        }
    }
    expr("pk").map_or_else(|| "NULL".to_owned(), |expr| format!("CAST({expr} AS TEXT)"))
}

fn display_choices(expression: &str, field: &Field) -> String {
    let choices = field
        .choices
        .iter()
        .map(|(key, label)| {
            let value = key
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| key.to_string());
            format!(
                "WHEN '{}' THEN '{}'",
                value.replace('\'', "''"),
                label.replace('\'', "''")
            )
        })
        .collect::<Vec<_>>()
        .join(" ");
    format!("CASE CAST({expression} AS TEXT) {choices} ELSE CAST({expression} AS TEXT) END")
}

fn tracked_column<'a>(model: &'a Model, column: &str) -> Option<&'a Field> {
    let field = match column {
        "victor_display" => "victor",
        "type_of_place_display" => "type_of_place",
        name => name,
    };
    model.fields.get(field)
}

pub(super) fn display(value: Option<&str>, field: Option<&Field>, boolean: bool) -> Value {
    if boolean || field.is_some_and(|field| field.kind == "BooleanField") {
        let state = match value {
            Some("True" | "true" | "1") => ("yes", "True"),
            Some("False" | "false" | "0") => ("no", "False"),
            _ => ("unknown", "None"),
        };
        return Value::from_safe_string(format!(
            r#"<img src="/static/admin/img/icon-{}.svg" alt="{}">"#,
            state.0, state.1
        ));
    }
    let Some(value) = value.filter(|value| !value.is_empty()) else {
        return Value::from("-");
    };
    if let Some(field) = field {
        if let Some((_, label)) = field.choices.iter().find(|(key, _)| {
            key == value
                || key
                    .as_i64()
                    .is_some_and(|key| value.parse::<i64>() == Ok(key))
        }) {
            return Value::from(label.as_str());
        }
        if matches!(field.kind.as_str(), "DateField" | "DateTimeField") {
            return Value::from(date(value));
        }
        if let Some(scale) = field.scale
            && let Ok(value) = value.parse::<f64>()
        {
            return Value::from(format!("{value:.scale$}"));
        }
    }
    Value::from(value)
}

pub(super) fn date(input: &str) -> String {
    use chrono::{DateTime, Datelike, NaiveDate, NaiveDateTime, Timelike, Utc};

    let render = |date: NaiveDate| {
        let months = [
            "Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.",
            "Nov.", "Dec.",
        ];
        format!(
            "{} {}, {}",
            months[date.month0() as usize],
            date.day(),
            date.year()
        )
    };
    if let Ok(date) = NaiveDate::parse_from_str(input, "%Y-%m-%d") {
        return render(date);
    }
    let stamp = DateTime::parse_from_rfc3339(input)
        .ok()
        .map(|stamp| stamp.with_timezone(&Utc).naive_utc())
        .or_else(|| {
            let clean = input.split_once('+').map_or(input, |(time, _)| time);
            NaiveDateTime::parse_from_str(clean, "%Y-%m-%d %H:%M:%S%.f").ok()
        });
    if let Some(stamp) = stamp {
        let minutes = stamp.minute();
        let hour = stamp.hour();
        let time = match (hour, minutes) {
            (0, 0) => "midnight".to_owned(),
            (12, 0) => "noon".to_owned(),
            _ => {
                let suffix = if hour < 12 { "a.m." } else { "p.m." };
                let hour = if hour % 12 == 0 { 12 } else { hour % 12 };
                if minutes == 0 {
                    format!("{hour} {suffix}")
                } else {
                    format!("{hour}:{minutes:02} {suffix}")
                }
            }
        };
        return format!("{}, {time}", render(stamp.date()));
    }
    input.to_owned()
}

fn search_terms(search: &str) -> impl Iterator<Item = Cow<'_, str>> {
    static TOKENS: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(r#"((?:[^\s'"]*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')[^\s'"]*)+|\S+)"#)
            .expect("Django admin query token pattern")
    });
    TOKENS.find_iter(search).map(|term| {
        let term = term.as_str();
        let quote = term
            .starts_with('"')
            .then_some('"')
            .or_else(|| term.starts_with('\'').then_some('\''));
        if let Some(quote) = quote
            && let Some(quoted) = term
                .strip_prefix(quote)
                .and_then(|value| value.strip_suffix(quote))
        {
            let unescaped = quoted
                .replace(&format!("\\{quote}"), &quote.to_string())
                .replace("\\\\", "\\");
            Cow::Owned(unescaped)
        } else {
            Cow::Borrowed(term)
        }
    })
}

pub(super) fn search_condition(
    model: &Model,
    search: &str,
    values: &mut Vec<String>,
) -> Option<String> {
    let mut source = Source::new(model);
    source.allow_many = true;
    let mut where_parts = Vec::new();
    for term in search_terms(search) {
        let mut options = Vec::new();
        for name in &model.search {
            let exact = name.starts_with('=');
            let name = name.trim_start_matches(['^', '=', '@']);
            if let Some((entry, _)) = source.resolve(name) {
                let comparable = format!("LOWER(CAST({entry} AS TEXT))");
                if exact {
                    let marker = add_values(values, &term);
                    options.push(format!("{comparable}=LOWER({marker})"));
                } else {
                    let value = term
                        .replace('\\', "\\\\")
                        .replace('%', "\\%")
                        .replace('_', "\\_");
                    let marker = add_values(values, &format!("%{value}%"));
                    options.push(format!("{comparable} LIKE LOWER({marker}) ESCAPE '\\'"));
                }
            }
        }
        if !options.is_empty() {
            where_parts.push(format!("({})", options.join(" OR ")));
        }
    }
    // Restrict by matching primary keys so through-table matches never multiply results.
    (!where_parts.is_empty()).then(|| {
        format!(
            "t0.id IN (SELECT t0.id FROM {} WHERE {})",
            source.sql(),
            where_parts.join(" AND ")
        )
    })
}

fn sort_expressions(
    source: &mut Source,
    path: &str,
    descending: bool,
    depth: usize,
    output: &mut Vec<String>,
) {
    let Some((column, field)) = source.resolve(path) else {
        return;
    };
    if !path.ends_with("_id")
        && depth < 3
        && let Some((app, slug)) = field.related.as_deref().and_then(|key| key.split_once('.'))
        && let Some(model) = schema().model(app, slug)
        && !model.model_ordering.is_empty()
    {
        for order in &model.model_ordering {
            sort_expressions(
                source,
                &format!("{path}__{}", order.trim_start_matches('-')),
                descending ^ order.starts_with('-'),
                depth + 1,
                output,
            );
        }
        return;
    }
    output.push(format!(
        "{column} {}",
        if descending { "DESC" } else { "ASC" }
    ));
}

pub(super) fn ordering_sql(source: &mut Source, ordering: &[String]) -> String {
    let mut expressions = Vec::new();
    for name in ordering {
        sort_expressions(
            source,
            name.trim_start_matches('-'),
            name.starts_with('-'),
            0,
            &mut expressions,
        );
    }
    expressions.push("t0.id DESC".to_owned());
    expressions.join(",")
}

async fn get_list(
    admin: &AdminRequest<'_>,
    model: &'static Model,
    params: &Parameters,
    filter: Filter<'_>,
) -> Result<Value, ApiError> {
    let Filter {
        mut source,
        condition,
        values,
    } = filter;
    let search = params.get("q").map_or("", String::as_str);
    let actions = model
        .actions
        .iter()
        .filter(|(action, _)| action != "delete_selected" || admin.viewer.allows(model, "delete"))
        .map(|(action, label)| {
            (
                action.clone(),
                label.replace("%(verbose_name_plural)s", &model.plural),
            )
        })
        .collect::<Vec<_>>();
    let offset = usize::from(!actions.is_empty());
    let sortable = model
        .list_display
        .iter()
        .map(|column| column.order_by.as_ref())
        .collect::<Vec<_>>();
    let available_sort = |index: usize| {
        index
            .checked_sub(offset)
            .and_then(|i| sortable.get(i))
            .and_then(|name| *name)
    };
    let mut requested = params.get("o").map_or(Vec::new(), |ordering| {
        ordering
            .split('.')
            .filter_map(|field| {
                let field = field.parse::<i32>().ok()?;
                let index = field.unsigned_abs() as usize;
                available_sort(index)?;
                Some((index, field < 0))
            })
            .collect()
    });
    if requested.is_empty() && !params.contains_key("o") {
        requested = model
            .ordering
            .iter()
            .filter_map(|order| {
                let name = order.trim_start_matches('-');
                let idx = sortable.iter().position(|field| {
                    field.is_some_and(|field| field.trim_start_matches('-') == name)
                })?;
                Some((idx + offset, order.starts_with('-')))
            })
            .collect();
    }
    let ordering = params
        .contains_key("o")
        .then(|| {
            requested
                .iter()
                .filter_map(|(index, descending)| {
                    available_sort(*index)
                        .map(|field| format!("{}{field}", if *descending { "-" } else { "" }))
                })
                .collect::<Vec<_>>()
        })
        .filter(|values| !values.is_empty())
        .unwrap_or_else(|| model.ordering.clone());
    let ordered = ordering_sql(&mut source, &ordering);
    let columns=model.list_display.iter().enumerate().map(|(i,col)|{
        let idx=i+offset;
        let sort=requested.iter().position(|(index,_)|*index==idx);
        let descending=sort.is_some_and(|i|requested[i].1);
        let sortable=col.order_by.is_some()||col.name=="__str__";
        let css=format!("{}column-{}{}",if sortable{"sortable "}else{""},col.name,
            sort.map_or("", |_|if descending{" sorted descending"}else{" sorted ascending"}));
        let encode=|(idx,rev):(usize,bool)|format!("{}{idx}",if rev{"-"}else{""});
        let remaining=requested.iter().copied().filter(|(index,_)|*index!=idx).map(encode).collect::<Vec<_>>();
        let suffix=remaining.join(".");
        let changed=std::iter::once(encode((idx,sort.is_some()&&!descending))).chain(remaining.clone()).collect::<Vec<_>>().join(".");
        let toggle=requested.iter().copied().map(|(number,rev)|encode((number,if number==idx{!rev}else{rev}))).collect::<Vec<_>>().join(".");
        context! {label=>cap_first(&col.label),classes=>css,position=>sort.map(|i|i+1),
            sort=>sortable.then(||link(params,"o",Some(&changed))),
            remove=>link(params,"o",Some(&suffix)),
            toggle=>link(params,"o",Some(&toggle)),direction=>if descending{"descending"}else{"ascending"}}
    }).collect::<Vec<_>>();
    let projection = model
        .list_display
        .iter()
        .enumerate()
        .map(|(idx, column)| format!("{} AS cell{idx}", projection(&mut source, &column.name)))
        .collect::<Vec<_>>();
    let label = label(&mut source, model, "");
    let table = source.sql();
    let count_sql = format!("SELECT COUNT(*) FROM {table}{condition}");
    let mut count_query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(count_sql));
    for value in &values {
        count_query = count_query.bind(value);
    }
    let count = count_query.fetch_one(&admin.state.pool).await?;
    let count = usize::try_from(count).unwrap_or_default();
    let total = if condition.is_empty() {
        count
    } else {
        usize::try_from(
            sqlx::query_scalar::<_, i64>(AssertSqlSafe(format!(
                "SELECT COUNT(*) FROM {}{}",
                model.table,
                if model.slug() == "school" {
                    " WHERE NOT temporary"
                } else {
                    ""
                }
            )))
            .fetch_one(&admin.state.pool)
            .await?,
        )
        .unwrap_or_default()
    };
    let page_count = count.div_ceil(model.per_page).max(1);
    let page = params
        .get("p")
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|page| (1..=page_count).contains(page))
        .unwrap_or(1);
    let all = params.contains_key("all") && count <= model.max_show_all;
    let range = if all {
        String::new()
    } else {
        format!(
            " LIMIT {} OFFSET {}",
            model.per_page,
            (page - 1) * model.per_page
        )
    };
    let sql = format!(
        "SELECT CAST(t0.id AS TEXT) AS id,{label} AS label,{} FROM {table}{condition} ORDER BY {}{range}",
        projection.join(","),
        ordered
    );
    let mut query = sqlx::query(AssertSqlSafe(sql));
    for value in &values {
        query = query.bind(value);
    }
    let edit_speakers = model.slug() == "speakerresult"
        && admin.viewer.allows(model, "change")
        && !params.contains_key("_popup");
    let mut rows = Vec::new();
    for row in query.fetch_all(&admin.state.pool).await? {
        rows.push(cells(&row, model, params, edit_speakers)?);
    }
    let app = schema().app(model.app()).expect("admin app");
    let add_link = admin
        .viewer
        .allows(model, "add")
        .then(|| with_preserved_filters(format!("{}add/", model.url()), params));
    let mut pages = Vec::new();
    if page_count > 1 && !all {
        let mut last = 0;
        for number in pagination_numbers(page, page_count) {
            if last > 0 && number > last + 1 {
                pages.push(context! {gap=>true});
            }
            pages.push(context!{label=>number,active=>number==page,url=>link(params,"p",Some(&number.to_string())),end=>number==page_count});
            last = number;
        }
    }
    let filters = filters(admin, model, params).await?;
    let date_navigation = if model.slug() == "motion" {
        dates::navigation(admin, &table, &condition, &values, params).await?
    } else {
        Value::UNDEFINED
    };
    let mut clear = params.clone();
    clear.retain(|name, _| {
        matches!(
            name.as_str(),
            "q" | "o" | "_popup" | "_to_field" | "_facets"
        )
    });
    let clear_filters = clear.len() != params.len()
        && params
            .keys()
            .any(|name| name != "p" && !clear.contains_key(name));
    Ok(context! {
        title=>if params.contains_key("_popup"){format!("Select {}",model.singular)}
            else{format!("Select {} to {}",model.singular,if admin.viewer.allows(model,"change"){"change"}else{"view"})},
        heading_plural=>cap_first(&model.plural),plural=>&model.plural,singular=>&model.singular,
        app_label=>model.app(),app_name=>&app.name,app_url=>app.url(),model_name=>model.slug(),model_url=>model.url(),
        searchable=>!model.search.is_empty(),query=>search,params=>params.iter().collect::<Vec<_>>(),
        css=>&model.list_media().css,js=>&model.list_media().js,import_export=>model.import_export, add_link,
        export_link=>format!("{}export/?{}",model.url(),admin.uri.query().unwrap_or_default()),
        sort_count=>requested.len(), columns, rows, actions, count, total, pages, filters, edit_speakers,date_navigation,
        show_all=>(page_count>1&&!all&&count<=model.max_show_all).then(||link(params,"all",Some(""))),
        count_url=>context!{hidden=>params.contains_key("_facets"),url=>link(params,"_facets",(!params.contains_key("_facets")).then_some("True"))},
        clear_filters=>clear_filters.then(||link(&clear,"p",None))
    })
}

fn with_preserved_filters(url: String, params: &Parameters) -> String {
    let clean = url::form_urlencoded::Serializer::new(String::new())
        .extend_pairs(params)
        .finish();
    if clean.is_empty() {
        url
    } else {
        let tail = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("_changelist_filters", &clean)
            .finish();
        format!("{url}?{tail}")
    }
}

fn cells(
    row: &AnyRow,
    model: &Model,
    params: &Parameters,
    edit_speakers: bool,
) -> Result<Value, ApiError> {
    let id: &str = row.try_get("id")?;
    let display_label: Option<&str> = row.try_get("label")?;
    let mut link_added = false;
    let cells = model
        .list_display
        .iter()
        .enumerate()
        .map(|(index, column)| {
            let key = format!("cell{index}");
            let data: Option<&str> = row.try_get(key.as_str())?;
            let linked = model
                .list_links
                .as_ref()
                .is_some_and(|names| names.contains(&column.name));
            let element = if linked && !link_added { "th" } else { "td" };
            link_added |= linked;
            let destination = linked.then(|| {
                with_preserved_filters(
                    format!("{}{}/change/", model.url(), schema::quote(id)),
                    params,
                )
            });
            let classes = format!(
                "field-{}{}",
                column.name,
                if tracked_column(model, &column.name).is_some_and(|field| matches!(
                    field.kind.as_str(),
                    "DateField" | "DateTimeField" | "TimeField" | "ForeignKey" | "OneToOneField"
                )) {
                    " nowrap"
                } else {
                    ""
                }
            );
            let value = if matches!(column.name.as_str(),"old_team_name"|"new_team_name") && model.slug()=="totyreaff" {
                Value::from_safe_string(data.unwrap_or("-").to_owned())
            } else {
                display(data, tracked_column(model, &column.name), column.boolean)
            };
            let speaker_checkbox = edit_speakers && column.name=="hidden_from_profile";
            Ok::<_, sqlx::Error>(context! {element,classes,value,url=>destination,speaker_checkbox,checked=>data==Some("True")})
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(context! {id,repr=>display_label.unwrap_or(id),cells})
}

async fn filters(
    admin: &AdminRequest<'_>,
    model: &'static Model,
    params: &Parameters,
) -> Result<Vec<Value>, ApiError> {
    let mut filters = Vec::new();
    for filter in &model.filter {
        if model.slug() == "teamresult" && filter == "profile_visibility" {
            let active = params.get(filter).map(String::as_str);
            let mut cleared = params.clone();
            cleared.remove(filter);
            let counts = facets::grouped(admin, model, filter, &cleared).await?;
            let choices = [
                (None, "All"),
                (Some("hidden"), "Hidden for a member"),
                (Some("visible"), "Visible for everyone"),
            ]
            .into_iter()
            .map(|(value, label)| {
                let label = value.map_or_else(
                    || label.to_owned(),
                    |value| facets::label(label, &counts, Some(value)),
                );
                context! {label,url=>link(params,filter,value),selected=>value==active}
            })
            .collect::<Vec<_>>();
            filters.push(context! {title=>"profile visibility",choices});
            continue;
        }
        if let Some(field) = model
            .fields
            .get(filter)
            .filter(|field| field.through.is_some())
        {
            if let Some(target) = field
                .related
                .as_ref()
                .and_then(|label| label.split_once('.'))
                .and_then(|(app, model)| schema().model(app, model))
            {
                let mut source = Source::new(target);
                let label = label(&mut source, target, "");
                let sql = format!(
                    "SELECT CAST(t0.id AS TEXT),{label} AS label FROM {} ORDER BY label,t0.id",
                    source.sql()
                );
                let rows = sqlx::query_as::<_, (String, String)>(AssertSqlSafe(sql))
                    .fetch_all(&admin.state.pool)
                    .await?;
                if !rows.is_empty() {
                    let exact = format!("{filter}__id__exact");
                    let null = format!("{filter}__isnull");
                    let active = params.get(&exact);
                    let empty = params.contains_key(&null);
                    let mut clean = params.clone();
                    clean.remove(&exact);
                    clean.remove(&null);
                    let counts = facets::grouped(admin, model, filter, &clean).await?;
                    let mut links = vec![
                        context! {label=>"All",url=>link(&clean,&exact,None),selected=>active.is_none()&&!empty},
                    ];
                    links.extend(rows.iter().map(|(id,label)| {
                        let label = facets::label(label, &counts, Some(id));
                        context!{label,url=>link(&clean,&exact,Some(id)),selected=>active==Some(id)}
                    }));
                    links.push(context! {
                        label=>facets::label("-", &counts, None),url=>link(&clean,&null,Some("True")),selected=>empty
                    });
                    filters.push(context! {title=>&field.label,choices=>links});
                }
            }
            continue;
        }
        let mut source = Source::new(model);
        let Some((expression, field)) = source.resolve(filter) else {
            continue;
        };
        let name = field.label.clone();
        let exact = if field.kind == "ForeignKey" {
            format!("{filter}__id__exact")
        } else if !field.choices.is_empty() || field.kind == "BooleanField" {
            format!("{filter}__exact")
        } else {
            filter.to_owned()
        };
        let active = params.get(&exact).or_else(|| params.get(filter));
        let mut cleared = params.clone();
        let null_key = format!("{filter}__isnull");
        let empty = params.contains_key(&null_key);
        cleared.remove(&exact);
        cleared.remove(filter);
        cleared.remove(&null_key);
        if matches!(field.kind.as_str(), "DateField" | "DateTimeField") {
            let choices = date_filters(admin, model, filter, field, params).await?;
            filters.push(context! {title=>name,choices});
            continue;
        }
        let counts = facets::grouped(admin, model, filter, &cleared).await?;
        let mut choices = vec![
            context! {label=>"All",url=>link(&cleared,&exact,None),selected=>active.is_none()&&!empty},
        ];
        if field.kind == "BooleanField" {
            for (value, label) in [("1", "Yes"), ("0", "No")] {
                choices.push(context!{label=>facets::label(label,&counts,Some(value)),url=>link(&cleared,&exact,Some(value)),selected=>active.is_some_and(|active|active==value)&&!empty});
            }
            if field.null {
                choices.push(context! {label=>facets::label("Unknown",&counts,None),url=>link(&cleared,&null_key,Some("True")),selected=>empty});
            }
        } else if !field.choices.is_empty() {
            for (key, label) in &field.choices {
                let value = key
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| key.to_string());
                choices.push(context!{label=>facets::label(label,&counts,Some(&value)),url=>link(&cleared,&exact,Some(&value)),selected=>active==Some(&value)});
            }
        } else if matches!(field.kind.as_str(), "ForeignKey" | "OneToOneField") {
            if let Some(target) = field
                .related
                .as_ref()
                .and_then(|related| related.split_once('.'))
                .and_then(|(app, slug)| schema().model(app, slug))
            {
                let mut source = Source::new(target);
                let repr = label(&mut source, target, "");
                let ordering = choice_ordering(&mut source, target);
                let condition = match target.slug() {
                    "school" => " WHERE NOT t0.temporary",
                    "debater" => " WHERE NOT t0.temporary AND NOT t0.synthetic",
                    _ => "",
                };
                let query = format!(
                    "SELECT CAST(t0.id AS TEXT),{repr} FROM {}{condition} ORDER BY {ordering}",
                    source.sql()
                );
                for (value, label) in sqlx::query_as::<_, (String, String)>(AssertSqlSafe(query))
                    .fetch_all(&admin.state.pool)
                    .await?
                {
                    choices.push(context!{label=>facets::label(&label,&counts,Some(&value)),url=>link(&cleared,&exact,Some(&value)),selected=>active==Some(&value)});
                }
            }
            if field.related.as_deref() == Some("contenttypes.contenttype") {
                for (id, app, slug) in sqlx::query_as::<_, (String, String, String)>(
                    "SELECT CAST(id AS TEXT),app_label,model FROM django_content_type ORDER BY id",
                )
                .fetch_all(&admin.state.pool)
                .await?
                {
                    let key = format!("{app}.{slug}");
                    let label = schema()
                        .content_types
                        .get(&key)
                        .cloned()
                        .unwrap_or_else(|| format!("{app} | {slug}"));
                    choices.push(context!{label=>facets::label(&label,&counts,Some(&id)),url=>link(&cleared,&exact,Some(&id)),selected=>active==Some(&id)});
                }
            }
            if choices.len() + usize::from(field.null) <= 2 {
                continue;
            }
            if field.null {
                choices.push(context!{label=>facets::label("-",&counts,None),url=>link(&cleared,&null_key,Some("True")),selected=>empty});
            }
        } else {
            let ordering = if matches!(
                field.kind.as_str(),
                "AutoField" | "BigAutoField" | "IntegerField"
            ) {
                "CAST(value AS BIGINT)"
            } else {
                "label"
            };
            let root = filter_owner(model, filter);
            let mut parent;
            let (source, path, expression) = if let Some((related, path)) = root {
                parent = Source::new(related);
                if let Some((selected, _)) = parent.resolve(path) {
                    (&mut parent, path, selected)
                } else {
                    (&mut source, filter.as_str(), expression)
                }
            } else {
                (&mut source, filter.as_str(), expression)
            };
            let projected = projection(source, path);
            let query = format!(
                "SELECT value,label FROM (SELECT DISTINCT CAST({expression} AS TEXT) AS value,{projected} AS label
                FROM {} WHERE {expression} IS NOT NULL{}) choices ORDER BY {ordering}",
                source.sql(),
                if source.model.slug() == "school" {
                    " AND NOT t0.temporary"
                } else {
                    ""
                }
            );
            for row in sqlx::query(AssertSqlSafe(query))
                .fetch_all(&admin.state.pool)
                .await?
            {
                let value: &str = row.try_get("value")?;
                let label: Option<&str> = row.try_get("label")?;
                let label = label.unwrap_or("-");
                choices.push(context!{label=>facets::label(label,&counts,Some(value)),url=>link(&cleared,&exact,Some(value)),selected=>active.is_some_and(|active|active==value)});
            }
        }
        if choices.len() > 1 || matches!(filter.as_str(), "provider" | "app__provider") {
            filters.push(context! {title=>name,choices});
        }
    }
    Ok(filters)
}

fn filter_owner<'a>(
    mut model: &'static Model,
    mut path: &'a str,
) -> Option<(&'static Model, &'a str)> {
    let mut found = false;
    while let Some((name, tail)) = path.split_once("__") {
        let (app, slug) = model
            .fields
            .get(name)?
            .related
            .as_deref()?
            .split_once('.')?;
        model = schema().model(app, slug)?;
        path = tail;
        found = true;
    }
    found.then_some((model, path))
}

pub(super) fn choice_ordering(source: &mut Source, model: &Model) -> String {
    let mut order = Vec::new();
    for name in &model.choice_ordering {
        sort_expressions(
            source,
            name.trim_start_matches('-'),
            name.starts_with('-'),
            0,
            &mut order,
        );
    }
    order.push("t0.id ASC".to_owned());
    order.join(", ")
}

async fn date_filters(
    admin: &AdminRequest<'_>,
    model: &Model,
    path: &str,
    field: &Field,
    params: &Parameters,
) -> Result<Vec<Value>, ApiError> {
    use chrono::{Datelike, Days, NaiveDate, Utc};

    let today = Utc::now().date_naive();
    let month = today.with_day(1).expect("first day of current month");
    let year = today
        .with_month(1)
        .and_then(|date| date.with_day(1))
        .expect("first day of current year");
    let next_month = month
        .checked_add_months(chrono::Months::new(1))
        .expect("first day of next month");
    let next_year = year
        .checked_add_months(chrono::Months::new(12))
        .expect("first day of next year");
    let tomorrow = today + Days::new(1);
    let prefix = format!("{path}__");
    let since = format!("{prefix}gte");
    let until = format!("{prefix}lt");
    let null = format!("{prefix}isnull");
    let format = |date: NaiveDate| {
        if field.kind == "DateTimeField" {
            format!("{date} 00:00:00+00:00")
        } else {
            date.to_string()
        }
    };
    let active = params
        .iter()
        .filter(|(name, _)| name.starts_with(&prefix))
        .map(|(name, value)| (name.clone(), value.clone()))
        .collect::<Parameters>();
    let mut clean = params.clone();
    clean.retain(|name, _| !name.starts_with(&prefix) && name != "p");
    let mut choices = vec![("Any date", Parameters::new())];
    for (label, start, end) in [
        ("Today", today, tomorrow),
        ("Past 7 days", today - Days::new(7), tomorrow),
        ("This month", month, next_month),
        ("This year", year, next_year),
    ] {
        choices.push((
            label,
            Parameters::from([(since.clone(), format(start)), (until.clone(), format(end))]),
        ));
    }
    if field.null {
        choices.extend([
            (
                "No date",
                Parameters::from([(null.clone(), "True".to_owned())]),
            ),
            ("Has date", Parameters::from([(null, "False".to_owned())])),
        ]);
    }
    let counts = facets::date_ranges(admin, model, path, field, &clean, &choices).await?;
    Ok(choices
        .into_iter()
        .enumerate()
        .map(|(index, (label, options))| {
            let selected = active == options;
            let mut query = clean.clone();
            query.extend(options);
            let label = counts.as_ref().map_or_else(
                || label.to_owned(),
                |counts| format!("{label} ({})", counts[index]),
            );
            context! {label,url=>query_string(&query),selected}
        })
        .collect())
}

async fn save_speaker_visibility(
    admin: &AdminRequest<'_>,
    model: &Model,
    params: &Parameters,
    filter: &Filter<'_>,
    fields: &HashMap<String, String>,
) -> Result<Response, ApiError> {
    if !admin.viewer.allows(model, "change") || params.contains_key("_popup") {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let count = fields
        .get("form-TOTAL_FORMS")
        .and_then(|value| value.parse::<usize>().ok());
    let Some(total) = count.filter(|count| *count <= 2000) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let mut tx = admin.state.pool.begin().await?;
    let mut changed = 0;
    let actor = admin.viewer.user.id.ok_or(ApiError::Serialization)?;
    let query = format!(
        "SELECT CAST(t0.id AS BIGINT) FROM {}{}",
        filter.source.sql(),
        filter.condition,
    );
    let mut valid_query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(query));
    for value in &filter.values {
        valid_query = valid_query.bind(value);
    }
    let valid = valid_query
        .fetch_all(&mut *tx)
        .await?
        .into_iter()
        .collect::<std::collections::HashSet<_>>();
    let mut posted = std::collections::HashSet::new();
    for index in 0..total {
        let Some(id) = fields
            .get(&format!("form-{index}-id"))
            .and_then(|value| value.parse::<i64>().ok())
            .filter(|id| valid.contains(id) && posted.insert(*id))
        else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        let hidden = fields
            .get(&format!("form-{index}-hidden_from_profile"))
            .is_some_and(|value| value != "false" && value != "False");
        let outcome = sqlx::query("UPDATE core_speakerresult SET hidden_from_profile=$1 WHERE id=$2 AND hidden_from_profile<>$1")
            .bind(hidden).bind(id).execute(&mut *tx).await?;
        if outcome.rows_affected() > 0 {
            super::form::log(
                &mut tx,
                model,
                actor,
                id,
                &format!("SpeakerResult object ({id})"),
                2,
                serde_json::json!([{"changed":{"fields":["Hidden from profile"]}}]),
            )
            .await?;
            changed += 1;
        }
    }
    tx.commit().await?;
    let target = admin
        .uri
        .path_and_query()
        .map_or(admin.uri.path(), |path| path.as_str());
    let mut response = redirect(target);
    if changed > 0
        && let Some(secret) = admin.state.secret.as_deref()
    {
        let message = if changed == 1 {
            "1 speaker result was changed successfully.".to_owned()
        } else {
            format!("{changed} speaker results were changed successfully.")
        };
        flash::push(
            &mut response,
            admin.headers,
            secret,
            25,
            &message,
            admin.secure(),
        );
    }
    Ok(response)
}

fn action_scope(filter: &Filter<'_>, data: &[(String, String)], across: bool) -> String {
    let ids = data
        .iter()
        .filter_map(|(name, id)| {
            (name == "_selected_action")
                .then(|| id.parse::<i64>().ok())
                .flatten()
        })
        .collect::<Vec<_>>();
    let restriction = if across {
        String::new()
    } else {
        let ids = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
        let ids = if ids.is_empty() { "NULL" } else { &ids };
        format!(
            "{} t0.id IN ({ids})",
            if filter.condition.is_empty() {
                "WHERE"
            } else {
                "AND"
            }
        )
    };
    format!("{}{} {restriction}", filter.source.sql(), filter.condition)
}

async fn merge_topics(
    admin: &AdminRequest<'_>,
    model: &'static Model,
    filter: &Filter<'_>,
    data: &[(String, String)],
    across: bool,
) -> Result<Response, ApiError> {
    if !admin.viewer.allows(model, "change") {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let motion = model.slug() == "motiontopic";
    let scope = action_scope(filter, data, across);
    let sql = format!("SELECT CAST(t0.id AS BIGINT),t0.name FROM {scope} ORDER BY t0.name");
    let mut tx = admin.state.pool.begin().await?;
    if motion && tx.backend_name() == "PostgreSQL" {
        // The existing motion tag table has no unique constraint on the tag and motion pair.
        sqlx::query("LOCK TABLE core_taggedmotion IN SHARE ROW EXCLUSIVE MODE")
            .execute(&mut *tx)
            .await?;
    }
    let mut query = sqlx::query_as::<_, (i64, String)>(AssertSqlSafe(sql));
    for value in &filter.values {
        query = query.bind(value);
    }
    let topics = query.fetch_all(&mut *tx).await?;
    let (level, message) = if topics.len() < 2 {
        (
            30,
            if motion {
                "Select at least two motion topics."
            } else {
                "Select at least two topics."
            }
            .to_owned(),
        )
    } else {
        let (target, name) = &topics[0];
        let (table, copy) = if motion {
            (
                "core_taggedmotion",
                "INSERT INTO core_taggedmotion (tag_id,content_object_id)
                 SELECT DISTINCT CAST($1 AS BIGINT), source.content_object_id
                 FROM core_taggedmotion source
                 WHERE source.tag_id=$2 AND NOT EXISTS (
                     SELECT 1 FROM core_taggedmotion target
                     WHERE target.tag_id=$1 AND target.content_object_id=source.content_object_id
                 )",
            )
        } else {
            (
                "taggit_taggeditem",
                "INSERT INTO taggit_taggeditem (tag_id,content_type_id,object_id)
                 SELECT $1,content_type_id,object_id FROM taggit_taggeditem
                 WHERE tag_id=$2 ON CONFLICT DO NOTHING",
            )
        };
        let unlink = format!("DELETE FROM {table} WHERE tag_id=$1");
        let delete = format!("DELETE FROM {} WHERE id=$1", model.table);
        for (source, _) in &topics[1..] {
            sqlx::query(copy)
                .bind(target)
                .bind(source)
                .execute(&mut *tx)
                .await?;
            sqlx::query(AssertSqlSafe(unlink.as_str()))
                .bind(source)
                .execute(&mut *tx)
                .await?;
            sqlx::query(AssertSqlSafe(delete.as_str()))
                .bind(source)
                .execute(&mut *tx)
                .await?;
        }
        (
            20,
            format!("Merged {} topics into '{name}'.", topics.len() - 1),
        )
    };
    tx.commit().await?;
    let target = admin
        .uri
        .path_and_query()
        .map_or(admin.uri.path(), |path| path.as_str());
    let mut response = redirect(target);
    if let Some(secret) = admin.state.secret.as_deref() {
        flash::push(
            &mut response,
            admin.headers,
            secret,
            level,
            &message,
            admin.secure(),
        );
    }
    Ok(response)
}

// Django keeps short ranges intact and elides only beyond both end windows.
fn pagination_numbers(page: usize, total: usize) -> impl Iterator<Item = usize> {
    let left = if total <= 10 || page <= 7 {
        1
    } else {
        page - 3
    };
    let right = if total <= 10 || page >= total.saturating_sub(6) {
        total
    } else {
        page + 3
    };
    (1..=total).filter(move |&number| {
        number <= 2 || number > total.saturating_sub(2) || (left..=right).contains(&number)
    })
}

pub(super) async fn page(
    admin: &AdminRequest<'_>,
    model: &'static Model,
    method: Method,
    body: Body,
) -> Result<Response, ApiError> {
    if !admin.viewer.read(model) {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let params = url::form_urlencoded::parse(admin.uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect();
    let sqlite = admin.state.pool.connect_options().database_url.scheme() == "sqlite";
    let Some(filter) = Filter::new(model, &params, sqlite) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    if method == Method::POST {
        let data = match login::form_fields(admin.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let values = data.iter().cloned().collect::<HashMap<_, _>>();
        let Some(origin) = admin.origin() else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            admin.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        if model.slug() == "speakerresult" && values.contains_key("_save") {
            return save_speaker_visibility(admin, model, &params, &filter, &values).await;
        }
        let across = values
            .get("select_across")
            .is_some_and(|value| value == "1");
        if values
            .get("action")
            .is_some_and(|action| action == "delete_selected")
        {
            if !admin.viewer.allows(model, "delete") {
                return Ok(StatusCode::FORBIDDEN.into_response());
            }
            let confirmed = values.contains_key("post");
            let scope = action_scope(&filter, &data, across && !confirmed);
            let sql = format!("SELECT DISTINCT CAST(t0.id AS BIGINT) FROM {scope} ORDER BY 1");
            let mut query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql));
            for value in &filter.values {
                query = query.bind(value);
            }
            let ids = query.fetch_all(&admin.state.pool).await?;
            return super::records::delete(admin, model, &ids, true, confirmed, false).await;
        }
        if (model.slug() == "motiontopic" || model.key == "taggit.tag")
            && values
                .get("action")
                .is_some_and(|action| action == "merge_topics")
        {
            return merge_topics(admin, model, &filter, &data, across).await;
        }
        if model.slug() == "agentcredential"
            && values
                .get("action")
                .is_some_and(|action| action == "revoke_credentials")
        {
            if !admin.viewer.allows(model, "change") {
                return Ok(StatusCode::FORBIDDEN.into_response());
            }
            let scope = action_scope(&filter, &data, across);
            let statement = format!(
                "UPDATE core_agentcredential SET revoked_at=CURRENT_TIMESTAMP
                 WHERE revoked_at IS NULL AND id IN (SELECT t0.id FROM {scope})"
            );
            let mut query = sqlx::query(AssertSqlSafe(statement));
            for value in &filter.values {
                query = query.bind(value);
            }
            query.execute(&admin.state.pool).await?;
            return Ok(redirect(
                admin
                    .uri
                    .path_and_query()
                    .map(|target| target.as_str())
                    .unwrap_or(admin.uri.path()),
            ));
        }
    }
    let context = get_list(admin, model, &params, filter).await?;
    admin.view("site_admin/change_list.html", context)
}

#[cfg(test)]
mod tests {
    use super::pagination_numbers;

    #[test]
    fn admin_pagination_shows_short_ranges_and_keeps_links_beside_ellipses() {
        assert_eq!(
            pagination_numbers(1, 7).collect::<Vec<_>>(),
            [1, 2, 3, 4, 5, 6, 7]
        );
        assert_eq!(
            pagination_numbers(8, 20).collect::<Vec<_>>(),
            [1, 2, 5, 6, 7, 8, 9, 10, 11, 19, 20]
        );
        assert_eq!(
            pagination_numbers(14, 20).collect::<Vec<_>>(),
            [1, 2, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
        );
    }
}
