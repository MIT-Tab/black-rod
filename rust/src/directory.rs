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
}

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

impl Kind {
    fn title(self) -> &'static str {
        match self {
            Self::Schools => "Schools",
            Self::Teams => "Teams",
            Self::Debaters => "Debaters",
        }
    }

    fn columns(self) -> &'static [Field] {
        match self {
            Self::Schools => SCHOOL_FIELDS,
            Self::Teams => TEAM_FIELDS,
            Self::Debaters => DEBATER_FIELDS,
        }
    }

    fn count(self) -> &'static str {
        match self {
            Self::Schools => "FROM core_school x WHERE NOT x.temporary AND NOT x.synthetic",
            Self::Teams => "FROM core_team x WHERE NOT x.synthetic",
            Self::Debaters => {
                "FROM core_debater x LEFT JOIN core_school s ON s.id = x.school_id WHERE NOT x.temporary AND NOT x.synthetic"
            }
        }
    }

    fn projection(self, order_by: &str) -> &'static str {
        match self {
            Self::Schools => "CAST(x.id AS BIGINT) AS id, x.name,
                   CAST(CASE WHEN x.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included",
            Self::Teams if order_by.trim_start_matches('-') == "debaters_display" => "CAST(x.id AS BIGINT) AS id, x.name,
                (SELECT MIN(LOWER(d.last_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS first_last,
                (SELECT MIN(LOWER(d.first_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS first_first,
                (SELECT MAX(LOWER(d.last_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS second_last,
                (SELECT MAX(LOWER(d.first_name)) FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id WHERE td.team_id = x.id) AS second_first",
            Self::Teams => "CAST(x.id AS BIGINT) AS id, x.name",
            Self::Debaters => "CAST(x.id AS BIGINT) AS id, x.first_name, x.last_name, s.name AS school,
                                CAST(x.status AS BIGINT) AS status",
        }
    }

    fn initial_order(self) -> &'static str {
        match self {
            Self::Schools => "x.name",
            Self::Teams | Self::Debaters => "x.id",
        }
    }
}

#[derive(Clone)]
enum FilterValue {
    Number(i64),
    Text(String),
}

struct Filters {
    query: String,
    params: Vec<FilterValue>,
}

impl Filters {
    fn add(&mut self, condition: &str, value: FilterValue) {
        self.params.push(value);
        self.query.push_str(" AND ");
        self.query
            .push_str(&condition.replace("{}", &format!("${}", self.params.len())));
    }

    fn bind<'a>(
        &self,
        mut statement: Query<'a, Any, AnyArguments>,
    ) -> Query<'a, Any, AnyArguments> {
        for value in &self.params {
            statement = match value {
                FilterValue::Number(value) => statement.bind(*value),
                FilterValue::Text(value) => statement.bind(value.clone()),
            };
        }
        statement
    }
}

fn filter(kind: Kind, query: &HashMap<&str, &str>) -> Filters {
    let mut result = Filters {
        query: kind.count().to_owned(),
        params: Vec::new(),
    };
    let value = |key| query.get(key).copied().unwrap_or_default().trim();
    if let Ok(id) = value("id").parse::<i64>() {
        result.add("x.id = {}", FilterValue::Number(id));
    }
    let text_fields: &[(&str, &str)] = match kind {
        Kind::Schools | Kind::Teams => &[("name__icontains", "x.name")],
        Kind::Debaters => &[
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
    if matches!(kind, Kind::Debaters) {
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
    result
}

fn updated_uri(
    input: &[(std::borrow::Cow<'_, str>, std::borrow::Cow<'_, str>)],
    key: &str,
    value: &str,
) -> String {
    let mut encoded = url::form_urlencoded::Serializer::new(String::new());
    for (old_key, old_value) in input {
        if old_key != key {
            encoded.append_pair(old_key, old_value);
        }
    }
    encoded.append_pair(key, value);
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

type RowCells = Vec<Vec<(String, String)>>;

async fn rows(
    pool: &AnyPool,
    kind: Kind,
    filters: &Filters,
    order_by: &str,
    offset: i64,
    limit: i64,
) -> Result<Vec<RowCells>, ApiError> {
    let order = kind
        .columns()
        .iter()
        .find(|field| field.name == order_by.trim_start_matches('-'))
        .map_or(kind.initial_order(), |field| field.order);
    let order = order
        .split(", ")
        .map(|column| {
            if order_by.starts_with('-') {
                format!("{column} DESC")
            } else {
                column.to_owned()
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let query = format!(
        "SELECT {} {} ORDER BY {order} LIMIT ${} OFFSET ${}",
        kind.projection(order_by),
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
            Kind::Debaters => {
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
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let kind = match uri.path() {
        "/core/schools/" => Kind::Schools,
        "/core/teams/" => Kind::Teams,
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
    let filters = filter(kind, &query);
    let count_sql = format!("SELECT CAST(COUNT(*) AS BIGINT) AS count {}", filters.query);
    let count = filters
        .bind(sqlx::query(AssertSqlSafe(count_sql)))
        .fetch_one(&state.pool)
        .await
        .and_then(|row| row.try_get::<i64, _>("count"));
    let Ok(count) = count else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
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
    let result = rows(
        &state.pool,
        kind,
        &filters,
        order_by,
        (page - 1) * per_page,
        per_page,
    )
    .await;
    let Ok(rows) = result else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let button_user = session::user(&state.pool, &headers, state.secret.as_deref()).await;
    let Ok(user) = button_user else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let mut buttons = Vec::new();
    let button = match kind {
        Kind::Schools => Some(("add_school", "/core/schools/create")),
        Kind::Debaters => Some(("add_debater", "/core/debaters/create")),
        _ => None,
    };
    if let Some((permission, href)) = button
        && session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false)
    {
        buttons.push(json!({"name":"Create", "class":"btn-success", "href":href}));
    }
    let input = |name, label, numeric, options| Input {
        name,
        label,
        value: param(name),
        numeric,
        options,
    };
    let mut fields = vec![input("id", "ID", true, Vec::new())];
    let mut season_filters = Vec::new();
    if matches!(kind, Kind::Debaters) {
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
    } else {
        fields.push(input("name__icontains", "Name contains", false, Vec::new()));
    }
    let columns = kind.columns().iter().map(|field| {
        let desc = order_by.starts_with('-') && order_by.trim_start_matches('-') == field.name;
        let asc = order_by == field.name;
        let next_order = if asc { format!("-{}", field.name) } else { field.name.to_owned() };
        let class = if desc { "desc orderable" } else if asc { "asc orderable" } else { "orderable" };
        json!({"label":field.label, "asc":asc, "desc":desc, "class":class, "url":updated_uri(&raw,"sort",&next_order)})
    }).collect::<Vec<_>>();
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
    let page_values = context! {
        fields, season_filters, title => kind.title(), buttons, rows, columns, pagination,
        previous => (page > 1).then(|| page_link(page - 1)),
        next => (page < total).then(|| page_link(page + 1)),
    };
    match web::render("core/directory.html", context! {..common, ..page_values}) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Directory template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
