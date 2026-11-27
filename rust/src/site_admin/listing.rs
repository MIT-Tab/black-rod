use std::collections::{BTreeMap, HashMap};

use axum::{
    body::{Body, to_bytes},
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
use crate::{ApiError, login};

type Parameters = BTreeMap<String, String>;

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
    let mut query = url::form_urlencoded::Serializer::new(String::new());
    format!("?{}", query.extend_pairs(params).finish())
}

pub(super) struct Source<'a> {
    model: &'a Model,
    joins: Vec<String>,
    paths: HashMap<String, (usize, &'static Model)>,
}

impl<'a> Source<'a> {
    pub(super) fn new(model: &'a Model) -> Self {
        Self {
            model,
            joins: Vec::new(),
            paths: HashMap::new(),
        }
    }

    fn resolve(&mut self, path: &str) -> Option<(String, &Field)> {
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
            let field_column = field.column.as_ref()?;
            let expr = format!("t{index}.{field_column}");
            if parts.peek().is_none() {
                return Some((expr, field));
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
        if field.kind == "BooleanField" {
            return format!(
                "CASE WHEN {expr} THEN 'True' WHEN {expr} IS NULL THEN NULL ELSE 'False' END"
            );
        }
        return format!("CAST({expr} AS TEXT)");
    }
    match name {
        "stats_count" => {
            "CAST((SELECT COUNT(*) FROM core_roundstats stat WHERE stat.round_id=t0.id) AS TEXT)"
                .to_owned()
        }
        "tournament_import_count" => {
            "CAST((SELECT COUNT(*) FROM core_tournamentimport im WHERE im.batch_id=t0.id) AS TEXT)"
                .to_owned()
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
        "round" => {
            if let (Some(label), Some(number)) = (expr("round_label"), expr("round_number")) {
                return format!("COALESCE(NULLIF({label},''),'Round ' || CAST({number} AS TEXT))");
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
    }
    Value::from(value)
}

pub(super) fn date(input: &str) -> String {
    use chrono::{NaiveDate, NaiveDateTime, Timelike};
    let value = input.split('+').next().unwrap_or(input);
    if let Ok(date) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        return date
            .format("%b. %-d, %Y")
            .to_string()
            .replace("May.", "May")
            .replace("June.", "June")
            .replace("July.", "July");
    }
    if let Ok(stamp) = NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f") {
        let hour = stamp.hour() % 12;
        let hour = if hour == 0 { 12 } else { hour };
        let suffix = if stamp.hour() < 12 { "a.m." } else { "p.m." };
        return format!(
            "{}, {hour}:{:02} {suffix}",
            date(&stamp.date().to_string()),
            stamp.minute()
        );
    }
    input.to_owned()
}

async fn get_list(
    admin: &AdminRequest<'_>,
    model: &Model,
    params: &Parameters,
) -> Result<Value, ApiError> {
    let mut source = Source::new(model);
    let mut values = Vec::new();
    let search = params.get("q").map_or("", String::as_str);
    let mut where_parts = Vec::new();
    if model.slug() == "school" {
        where_parts.push("NOT t0.temporary".to_owned());
    }
    for term in search.split_whitespace() {
        let mut options = Vec::new();
        for name in &model.search {
            let name = name.trim_start_matches(['^', '=', '@']);
            if let Some((entry, _)) = source.resolve(name) {
                let value = term
                    .to_lowercase()
                    .replace('\\', "\\\\")
                    .replace('%', "\\%")
                    .replace('_', "\\_");
                let marker = add_values(&mut values, &format!("%{value}%"));
                options.push(format!(
                    "LOWER(CAST({entry} AS TEXT)) LIKE {marker} ESCAPE '\\'"
                ));
            }
        }
        if !options.is_empty() {
            where_parts.push(format!("({})", options.join(" OR ")));
        }
    }
    for (parameter, value) in params {
        if matches!(
            parameter.as_str(),
            "q" | "p" | "o" | "all" | "_popup" | "_to_field" | "_facets"
        ) {
            continue;
        }
        let (path, look_up) = parameter.rsplit_once("__").unwrap_or((parameter, "exact"));
        let path = if matches!(look_up, "exact" | "isnull" | "id") {
            path
        } else {
            parameter
        };
        let allowed = model
            .filter
            .iter()
            .any(|filter| path == filter || path.starts_with(&format!("{filter}__")));
        if !allowed {
            continue;
        }
        if let Some((column, field)) = source.resolve(path) {
            if look_up == "isnull" {
                where_parts.push(format!(
                    "{column} IS {}NULL",
                    if matches!(value.as_str(), "True" | "1") {
                        ""
                    } else {
                        "NOT "
                    }
                ));
            } else if field.kind == "BooleanField" {
                where_parts.push(format!(
                    "{column} {}",
                    if matches!(value.as_str(), "True" | "1") {
                        ""
                    } else {
                        "IS FALSE"
                    }
                ));
            } else {
                let placeholder = add_values(&mut values, value);
                where_parts.push(format!("CAST({column} AS TEXT)={placeholder}"));
            }
        }
    }
    let condition = if where_parts.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", where_parts.join(" AND "))
    };
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
                let column_name = if name == "pk" { "id" } else { name };
                let idx = sortable.iter().position(|field| {
                    field.is_some_and(|field| field.trim_start_matches('-') == column_name)
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
    let mut ordered = Vec::new();
    for name in &ordering {
        if let Some((entry, _)) = source.resolve(name.trim_start_matches('-')) {
            ordered.push(format!(
                "{entry} {}",
                if name.starts_with('-') { "DESC" } else { "ASC" }
            ));
        }
    }
    ordered.push("t0.id DESC".to_owned());
    let columns=model.list_display.iter().enumerate().map(|(i,col)|{
        let idx=i+offset;
        let sort=requested.iter().position(|(index,_)|*index==idx);
        let descending=sort.is_some_and(|i|requested[i].1);
        let css=format!("{}column-{}{}",if col.order_by.is_some(){"sortable "}else{""},col.name,
            sort.map_or("", |_|if descending{" sorted descending"}else{" sorted ascending"}));
        let encode=|(idx,rev):(usize,bool)|format!("{}{idx}",if rev{"-"}else{""});
        let remaining=requested.iter().copied().filter(|(index,_)|*index!=idx).map(encode).collect::<Vec<_>>();
        let suffix=remaining.join(".");
        let changed=std::iter::once(encode((idx,sort.is_some()&&!descending))).chain(remaining.clone()).collect::<Vec<_>>().join(".");
        let toggle=requested.iter().copied().map(|(number,rev)|encode((number,if number==idx{!rev}else{rev}))).collect::<Vec<_>>().join(".");
        context! {label=>cap_first(&col.label),classes=>css,position=>sort.map(|i|i+1),
            sort=>col.order_by.as_ref().map(|_|link(params,"o",Some(&changed))),
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
        ordered.join(",")
    );
    let mut query = sqlx::query(AssertSqlSafe(sql));
    for value in &values {
        query = query.bind(value);
    }
    let mut rows = Vec::new();
    for row in query.fetch_all(&admin.state.pool).await? {
        rows.push(cells(&row, model, params)?);
    }
    let app = schema().app(model.app()).expect("admin app");
    let add_link = admin
        .viewer
        .allows(model, "add")
        .then(|| with_preserved_filters(format!("{}add/", model.url()), params));
    let mut pages = Vec::new();
    if page_count > 1 && !all {
        let mut last = 0;
        for number in (1..=page_count).filter(|number| {
            *number <= 2 || *number > page_count.saturating_sub(2) || number.abs_diff(page) <= 3
        }) {
            if last > 0 && number > last + 1 {
                pages.push(context! {gap=>true});
            }
            pages.push(context!{label=>number,active=>number==page,url=>link(params,"p",Some(&number.to_string())),end=>number==page_count});
            last = number;
        }
    }
    let filters = filters(admin, model, params).await?;
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
        css=>&model.list_media.css,js=>&model.list_media.js,import_export=>model.import_export, add_link,
        export_link=>format!("{}export/{}",model.url(),admin.uri.query().map(|query|format!("?{query}")).unwrap_or_default()),
        sort_count=>requested.len(), columns, rows, actions, count, total, pages, filters,
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

fn cells(row: &AnyRow, model: &Model, params: &Parameters) -> Result<Value, ApiError> {
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
                    "DateField" | "DateTimeField" | "TimeField"
                )) {
                    " nowrap"
                } else {
                    ""
                }
            );
            let value = display(data, tracked_column(model, &column.name), column.boolean);
            Ok::<_, sqlx::Error>(context! {element,classes,value,url=>destination})
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(context! {id,repr=>display_label.unwrap_or(id),cells})
}

async fn filters(
    admin: &AdminRequest<'_>,
    model: &Model,
    params: &Parameters,
) -> Result<Vec<Value>, ApiError> {
    let mut filters = Vec::new();
    for filter in &model.filter {
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
        cleared.remove(&exact);
        cleared.remove(filter);
        let mut choices = vec![
            context! {label=>"All",url=>link(&cleared,&exact,None),selected=>active.is_none()},
        ];
        if field.kind == "BooleanField" {
            for (value, label) in [("1", "Yes"), ("0", "No")] {
                choices.push(context!{label,url=>link(&cleared,&exact,Some(value)),selected=>active.is_some_and(|active|active==value)});
            }
        } else if !field.choices.is_empty() {
            for (key, label) in &field.choices {
                let value = key
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| key.to_string());
                choices.push(context!{label,url=>link(&cleared,&exact,Some(&value)),selected=>active==Some(&value)});
            }
        } else if matches!(field.kind.as_str(), "DateField" | "DateTimeField") {
            for label in [
                "Any date",
                "Today",
                "Past 7 days",
                "This month",
                "This year",
            ] {
                choices.push(context! {label,url=>"?",selected=>label=="Any date"});
            }
            choices.remove(0);
        } else {
            let projected = projection(&mut source, filter);
            let query = format!(
                "SELECT DISTINCT CAST({expression} AS TEXT) AS value,{projected} AS label
                FROM {} WHERE {expression} IS NOT NULL{} ORDER BY label",
                source.sql(),
                if model.slug() == "school" {
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
                let label = label.filter(|label| !label.is_empty()).unwrap_or("-");
                choices.push(context!{label,url=>link(&cleared,&exact,Some(value)),selected=>active.is_some_and(|active|active==value)});
            }
        }
        if choices.len() > 1 {
            filters.push(context! {title=>name,choices});
        }
    }
    Ok(filters)
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
    if method == Method::POST {
        let Ok(data) = to_bytes(body, 2_621_440).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        let values = login::form_values(&data);
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
        if values
            .get("action")
            .is_some_and(|action| action == "delete_selected")
        {
            let ids = url::form_urlencoded::parse(&data)
                .filter(|(name, _)| name == "_selected_action")
                .filter_map(|(_, id)| id.parse::<i64>().ok())
                .collect::<Vec<_>>();
            return super::records::delete(admin, model, &ids, true, values.contains_key("post"))
                .await;
        }
        if model.slug() == "agentcredential"
            && values
                .get("action")
                .is_some_and(|action| action == "revoke_credentials")
        {
            if !admin.viewer.allows(model, "change") {
                return Ok(StatusCode::FORBIDDEN.into_response());
            }
            let mut tx = admin.state.pool.begin().await?;
            for (name, id) in url::form_urlencoded::parse(&data) {
                if name == "_selected_action"
                    && let Ok(id) = id.parse::<i64>()
                {
                    sqlx::query("UPDATE core_agentcredential SET revoked_at=CURRENT_TIMESTAMP WHERE id=$1 AND revoked_at IS NULL")
                        .bind(id).execute(&mut *tx).await?;
                }
            }
            tx.commit().await?;
            return Ok(redirect(
                admin
                    .uri
                    .path_and_query()
                    .map(|target| target.as_str())
                    .unwrap_or(admin.uri.path()),
            ));
        }
    }
    let context = get_list(admin, model, &params).await?;
    admin.view("site_admin/change_list.html", context)
}
