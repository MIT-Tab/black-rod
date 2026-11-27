use std::{
    collections::{BTreeSet, HashMap, HashSet},
    sync::{Arc, RwLock},
    time::{Duration, Instant},
};

use axum::{
    extract::{OriginalUri, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{Days, NaiveDate};
use minijinja::context;
use reqwest::Client;
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};
use tokio::{sync::Mutex, task::JoinSet, time::timeout};
use url::Url;

use crate::{
    ApiError, AppState,
    cache::Memcached,
    points::{speaker_points_for_size, team_points_for_size},
    schedule::qualification_label,
    session,
    standings::{Board, View},
    web,
};

const FRESH: Duration = Duration::from_secs(300);
const STALE: Duration = Duration::from_secs(86_400);
const FETCH_BUDGET: Duration = Duration::from_secs(15);
const SOURCE_TIMEOUT: Duration = Duration::from_secs(8);
const MARKER_LIMITS: [usize; 3] = [5, 6, 5];

mod snapshots;

pub struct Live {
    directory: Option<String>,
    sources: Vec<String>,
    client: Client,
    last: RwLock<Option<(Instant, Arc<Payload>)>>,
    refreshing: Arc<Mutex<()>>,
}

impl Live {
    pub fn new(directory: Option<String>, sources: Vec<String>) -> Result<Self, reqwest::Error> {
        Ok(Self {
            directory,
            sources,
            client: Client::builder()
                .redirect(reqwest::redirect::Policy::limited(3))
                .build()?,
            last: RwLock::default(),
            refreshing: Arc::default(),
        })
    }

    fn last(&self, age: Duration) -> Option<Arc<Payload>> {
        self.last
            .read()
            .ok()?
            .as_ref()
            .filter(|(time, _)| time.elapsed() < age)
            .map(|(_, payload)| payload.clone())
    }

    async fn rebuild(
        &self,
        pool: &AnyPool,
        season: i32,
        cache: Option<&Memcached>,
    ) -> Result<Arc<Payload>, ApiError> {
        let mut boards = base_boards(pool, season).await?;
        let mut coty = base_coty(pool, season).await?;
        let mut names = BTreeSet::new();
        if !matches!(season, 2020 | 2021) {
            let payloads = self.fetch(cache).await;
            let people = People::load(pool, &payloads).await?;
            for source in payloads {
                if apply(pool, season, &source, &people, &mut boards, &mut coty).await?
                    && source["_cached_only"].as_bool() != Some(true)
                {
                    names.insert(
                        text(&source["tournament"]["name"])
                            .unwrap_or("Tournament")
                            .to_owned(),
                    );
                }
            }
        }
        let [toty, soty, noty] = boards;
        let payload = Arc::new(Payload {
            boards: json!({"toty": finalize(toty, 5), "soty": finalize(soty, 6),
                          "noty": finalize(noty, 5), "coty": coty.finalize()}),
            active_sources: names.into_iter().collect(),
        });
        if let Ok(mut last) = self.last.write() {
            *last = Some((Instant::now(), payload.clone()));
        }
        Ok(payload)
    }

    pub(crate) fn active_sources(
        self: &Arc<Self>,
        pool: &AnyPool,
        season: i32,
        cache: Option<&Memcached>,
    ) -> Vec<String> {
        if let Some(payload) = self.last(FRESH) {
            return payload.active_sources.clone();
        }
        if let Ok(guard) = self.refreshing.clone().try_lock_owned() {
            let live = self.clone();
            let pool = pool.clone();
            let cache = cache.cloned();
            tokio::spawn(async move {
                let _guard = guard;
                let _ = live.rebuild(&pool, season, cache.as_ref()).await;
            });
        }
        self.last(STALE)
            .map(|payload| payload.active_sources.clone())
            .unwrap_or_default()
    }

    async fn page_data(
        &self,
        pool: &AnyPool,
        season: i32,
        cache: Option<&Memcached>,
    ) -> Result<Arc<Payload>, ApiError> {
        if let Some(payload) = self.last(FRESH) {
            return Ok(payload);
        }
        if let Ok(_guard) = self.refreshing.try_lock() {
            return self.rebuild(pool, season, cache).await;
        }
        if let Some(payload) = self.last(STALE) {
            return Ok(payload);
        }
        let _guard = self.refreshing.lock().await;
        match self.last(FRESH) {
            Some(payload) => Ok(payload),
            None => self.rebuild(pool, season, cache).await,
        }
    }

    async fn fetch(&self, cache: Option<&Memcached>) -> Vec<Value> {
        let mut sources = self.sources.clone();
        if let Some(directory) = &self.directory
            && let Some(payload) = read_json(&self.client, directory, Duration::from_secs(5)).await
        {
            sources.extend(
                array(&payload["tournaments"])
                    .iter()
                    .filter_map(|row| text(&row["url"]).map(str::to_owned)),
            );
        }
        let mut seen = HashSet::new();
        let mut pending = sources
            .into_iter()
            .filter_map(|value| safe_url(&value))
            .filter(|value| seen.insert(value.clone()));
        let mut running = JoinSet::new();
        let add = |running: &mut JoinSet<(String, Option<Value>)>, url: String| {
            let client = self.client.clone();
            running.spawn(async move {
                let payload =
                    read_json(&client, &format!("{url}/api/live-results"), SOURCE_TIMEOUT)
                        .await
                        .filter(|payload| payload["schema_version"].as_u64() == Some(1));
                (url, payload)
            });
        };
        for url in pending.by_ref().take(8) {
            add(&mut running, url);
        }
        let mut loaded = HashMap::new();
        let _ = timeout(FETCH_BUDGET, async {
            while let Some(result) = running.join_next().await {
                if let Ok((url, Some(payload))) = result {
                    let canonical = text(&payload["source_url"])
                        .and_then(safe_url)
                        .unwrap_or(url);
                    loaded.insert(canonical, payload);
                }
                if let Some(url) = pending.next() {
                    add(&mut running, url);
                }
            }
        })
        .await;
        if let Some(cache) = cache {
            let snapshots = snapshots::missing(cache, &loaded).await;
            loaded.extend(snapshots);
        }
        loaded.into_values().collect()
    }
}

async fn read_json(client: &Client, url: &str, duration: Duration) -> Option<Value> {
    client
        .get(url)
        .timeout(duration)
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?
        .json()
        .await
        .ok()
}

fn safe_url(value: &str) -> Option<String> {
    let value = value.trim().trim_end_matches('/');
    let parsed = Url::parse(value).ok()?;
    (matches!(parsed.scheme(), "http" | "https")
        && parsed.host().is_some()
        && parsed.username().is_empty()
        && parsed.password().is_none())
    .then(|| value.to_owned())
}

fn text(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.is_empty())
}

fn array(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or_default()
}

fn number(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str()?.trim().parse().ok())
}

fn known_id(value: &Value) -> Option<i64> {
    number(value).filter(|id| *id != -1)
}

fn round_points(points: f64) -> f64 {
    (points * 10_000.0).round_ties_even() / 10_000.0
}

struct Payload {
    boards: Value,
    active_sources: Vec<String>,
}

#[derive(Clone, Serialize)]
struct Marker {
    tournament: String,
    url: String,
    points: f64,
    live: bool,
}

#[derive(Serialize)]
struct Standing {
    name: String,
    detail: String,
    url: String,
    points: f64,
    place: usize,
    tied: bool,
    has_live: bool,
    marker_slots: Vec<Option<Marker>>,
    #[serde(skip)]
    markers: Vec<Marker>,
}

impl Standing {
    fn new(name: String, detail: String, url: String) -> Self {
        Self {
            name,
            detail,
            url,
            points: 0.0,
            place: 0,
            tied: false,
            has_live: false,
            marker_slots: Vec::new(),
            markers: Vec::new(),
        }
    }
}

type Results = HashMap<String, Standing>;

fn team_key(ids: &[i64]) -> String {
    let mut ids = ids.to_vec();
    ids.sort_unstable();
    format!(
        "team:{}",
        ids.iter().map(i64::to_string).collect::<Vec<_>>().join(":")
    )
}

async fn base_boards(pool: &AnyPool, season: i32) -> Result<[Results; 3], ApiError> {
    let mut result = [Results::new(), Results::new(), Results::new()];
    for (index, (board, key)) in [
        (Board::Teams, "team"),
        (
            Board::Speakers {
                table: "core_soty",
                markers: MARKER_LIMITS[1],
                online: false,
            },
            "debater",
        ),
        (
            Board::Speakers {
                table: "core_noty",
                markers: MARKER_LIMITS[2],
                online: false,
            },
            "debater",
        ),
    ]
    .into_iter()
    .enumerate()
    {
        for row in board.rows(pool, season, "", View::Api).await? {
            let who = &row[key];
            let (key, detail) = if index == 0 {
                let ids = array(&who["debaters"])
                    .iter()
                    .filter_map(|debater| number(&debater["id"]))
                    .collect::<Vec<_>>();
                let names = array(&who["debaters"])
                    .iter()
                    .filter_map(|debater| text(&debater["name"]))
                    .collect::<Vec<_>>()
                    .join(" and ");
                (team_key(&ids), names)
            } else {
                (
                    format!("debater:{}", number(&who["id"]).unwrap_or_default()),
                    text(&who["school_name"])
                        .unwrap_or("Unaffiliated")
                        .to_owned(),
                )
            };
            let mut row_out = Standing::new(
                text(&who["name"]).unwrap_or_default().into(),
                detail,
                text(&who["url"]).unwrap_or_default().into(),
            );
            row_out
                .markers
                .extend(array(&row["markers"]).iter().map(|marker| {
                    Marker {
                        tournament: text(&marker["tournament"]["display"])
                            .unwrap_or_default()
                            .into(),
                        url: text(&marker["tournament"]["url"])
                            .unwrap_or_default()
                            .into(),
                        points: marker["points"].as_f64().unwrap_or_default(),
                        live: false,
                    }
                }));
            result[index].insert(key, row_out);
        }
    }
    Ok(result)
}

#[derive(Clone)]
struct School {
    key: String,
    name: String,
    url: String,
    eligible: bool,
}
impl School {
    fn from_row(row: &AnyRow) -> Result<Option<Self>, sqlx::Error> {
        let Some(id) = row.try_get::<Option<i64>, _>("school_id")? else {
            return Ok(None);
        };
        Ok(Some(Self {
            key: format!("school:{id}"),
            name: row.try_get("school_name")?,
            url: format!("/core/schools/{id}"),
            eligible: row.try_get::<i64, _>("eligible")? != 0,
        }))
    }
}

#[derive(Clone)]
struct Person {
    key: String,
    id: Option<i64>,
    name: String,
    initial: char,
    url: String,
    school: Option<School>,
}

impl Person {
    fn from_row(row: &AnyRow) -> Result<Self, sqlx::Error> {
        let id = row.try_get::<i64, _>("debater_id")?;
        let first = row.try_get::<&str, _>("first_name")?;
        let last = row.try_get::<&str, _>("last_name")?;
        Ok(Self {
            key: format!("debater:{id}"),
            id: Some(id),
            name: format!("{first} {last}").trim().into(),
            initial: last
                .chars()
                .next()
                .or_else(|| first.chars().next())
                .unwrap_or('?'),
            url: format!("/core/debaters/{id}"),
            school: School::from_row(row)?,
        })
    }
}

const PERSON_COLUMNS: &str = "
    CAST(d.id AS BIGINT) AS debater_id, d.first_name, d.last_name,
    CAST(s.id AS BIGINT) AS school_id, s.name AS school_name,
    CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS eligible";

struct People {
    debaters: HashMap<i64, Person>,
    schools: HashMap<i64, School>,
}

impl People {
    async fn load(pool: &AnyPool, sources: &[Value]) -> Result<Self, ApiError> {
        let mut ids = HashSet::new();
        let mut school_ids = HashSet::new();
        let mut remember = |person: &Value| {
            ids.extend(known_id(&person["apda_id"]));
            school_ids.extend(known_id(&person["school"]["apda_id"]));
        };
        for source in sources {
            for result in array(&source["team_results"]) {
                array(&result["team"]["debaters"])
                    .iter()
                    .for_each(&mut remember);
            }
            for result in array(&source["speaker_results"]) {
                remember(&result["debater"]);
            }
        }
        let mut debaters = HashMap::new();
        for chunk in ids.into_iter().collect::<Vec<_>>().chunks(200) {
            let marks = placeholders(chunk.len());
            let query = format!(
                "SELECT {PERSON_COLUMNS} FROM core_debater d
                LEFT JOIN core_school s ON s.id = d.school_id WHERE d.id IN ({marks})"
            );
            let mut statement = sqlx::query(AssertSqlSafe(query));
            for id in chunk {
                statement = statement.bind(*id);
            }
            for row in statement.fetch_all(pool).await? {
                let person = Person::from_row(&row)?;
                debaters.insert(person.id.unwrap_or_default(), person);
            }
        }
        let mut schools = HashMap::new();
        for chunk in school_ids.into_iter().collect::<Vec<_>>().chunks(200) {
            let marks = placeholders(chunk.len());
            let query = format!(
                "SELECT CAST(id AS BIGINT) AS school_id, name AS school_name,
                CAST(CASE WHEN included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS eligible
                FROM core_school WHERE id IN ({marks})"
            );
            let mut statement = sqlx::query(AssertSqlSafe(query));
            for id in chunk {
                statement = statement.bind(*id);
            }
            for row in statement.fetch_all(pool).await? {
                if let Some(school) = School::from_row(&row)? {
                    schools.insert(row.try_get("school_id")?, school);
                }
            }
        }
        Ok(Self { debaters, schools })
    }

    fn resolve(&self, value: &Value) -> Person {
        if let Some(known) = known_id(&value["apda_id"]).and_then(|id| self.debaters.get(&id)) {
            return known.clone();
        }
        let school = &value["school"];
        let school = known_id(&school["apda_id"])
            .and_then(|id| self.schools.get(&id))
            .cloned()
            .or_else(|| {
                text(&school["name"])
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(|name| School {
                        key: format!("school-name:{}", name.to_lowercase()),
                        name: name.into(),
                        url: String::new(),
                        eligible: true,
                    })
            });
        let name = text(&value["name"]).unwrap_or_default().trim();
        let school_key = school
            .as_ref()
            .map(|school| school.key.as_str())
            .unwrap_or_default();
        Person {
            key: format!("debater-name:{}:{school_key}", name.to_lowercase()),
            id: None,
            name: if name.is_empty() {
                "Unknown debater".into()
            } else {
                name.into()
            },
            initial: name
                .split_whitespace()
                .last()
                .and_then(|word| word.chars().next())
                .unwrap_or('?'),
            url: String::new(),
            school,
        }
    }
}

fn placeholders(len: usize) -> String {
    (1..=len)
        .map(|i| format!("${i}"))
        .collect::<Vec<_>>()
        .join(",")
}

#[derive(Default)]
struct Coty {
    schools: HashMap<String, School>,
    entries: HashMap<String, Contribution>,
}

struct Contribution {
    school: String,
    name: String,
    url: String,
    base_points: f64,
    points: f64,
    base_qualified: bool,
    qualified: bool,
    base_bonus: bool,
    bonus: bool,
    quals: Vec<Value>,
}

impl Coty {
    fn get(&mut self, person: &Person, reviewed: bool) -> Option<&mut Contribution> {
        let school = person.school.as_ref().filter(|school| school.eligible)?;
        self.schools.entry(school.key.clone()).or_insert_with(|| {
            let mut school = school.clone();
            if !reviewed {
                school.url.clear();
            }
            school
        });
        Some(
            self.entries
                .entry(person.key.clone())
                .or_insert_with(|| Contribution {
                    school: school.key.clone(),
                    name: person.name.clone(),
                    url: person.url.clone(),
                    base_points: 0.0,
                    points: 0.0,
                    base_qualified: false,
                    qualified: false,
                    base_bonus: false,
                    bonus: false,
                    quals: Vec::new(),
                }),
        )
    }

    fn finalize(self) -> Vec<Value> {
        let mut rows = HashMap::<String, Vec<Contribution>>::new();
        for entry in self.entries.into_values() {
            if entry.points > 0.0 || entry.qualified {
                rows.entry(entry.school.clone()).or_default().push(entry);
            }
        }
        let mut schools = Vec::new();
        for (key, mut entries) in rows {
            let Some(school) = self.schools.get(&key) else {
                continue;
            };
            entries.sort_by(|a, b| {
                b.points
                    .total_cmp(&a.points)
                    .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
            });
            let mut live = false;
            let mut total = 0.0;
            let debaters = entries.into_iter().map(|entry| {
                let points = contribution(entry.points, entry.bonus);
                let base = contribution(entry.base_points, entry.base_bonus);
                let points_live = entry.points != entry.base_points;
                let qualled_live = entry.qualified && !entry.base_qualified;
                let contribution_live = points != base;
                live |= points_live || qualled_live || contribution_live;
                total += points;
                json!({"name": entry.name, "url": entry.url, "points": entry.points, "contribution": points,
                       "qualled": entry.qualified, "quals": entry.quals,
                       "points_live": points_live, "qualled_live": qualled_live, "contribution_live": contribution_live})
            }).collect::<Vec<_>>();
            if total > 0.0 {
                schools.push(json!({"name": school.name, "url": school.url,
                                    "points": round_points(total), "debaters": debaters, "has_live": live}));
            }
        }
        assign_places(&mut schools);
        schools
    }
}

fn contribution(points: f64, bonus: bool) -> f64 {
    points.min(60.0) + if bonus { 6.0 } else { 0.0 }
}

fn awards_bonus(code: i64, season: i32) -> bool {
    season < 2026 || !matches!(code, 1 | 2 | 3 | 5 | 6 | 16)
}

async fn base_coty(pool: &AnyPool, season: i32) -> Result<Coty, ApiError> {
    let mut coty = Coty::default();
    let schools = sqlx::query(
        "
        SELECT CAST(s.id AS BIGINT) AS school_id, s.name AS school_name,
            CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS eligible
        FROM core_coty c JOIN core_school s ON s.id = c.school_id WHERE c.season = $1
    ",
    )
    .bind(season.to_string())
    .fetch_all(pool)
    .await?;
    for row in schools {
        if let Some(school) = School::from_row(&row)? {
            coty.schools.insert(school.key.clone(), school);
        }
    }
    if matches!(season, 2020 | 2021) {
        return Ok(coty);
    }
    for (table, value) in [
        (
            "core_qualpoints",
            "CAST(r.points AS DOUBLE PRECISION) AS points",
        ),
        ("core_qual", "CAST(r.qual_type AS BIGINT) AS qual_type"),
    ] {
        let query = format!("SELECT {PERSON_COLUMNS}, {value}
            FROM {table} r JOIN core_debater d ON r.debater_id = d.id
            JOIN core_school s ON s.id = d.school_id WHERE r.season = $1 AND s.included_in_oty ORDER BY r.id");
        for row in sqlx::query(AssertSqlSafe(query))
            .bind(season.to_string())
            .fetch_all(pool)
            .await?
        {
            let person = Person::from_row(&row)?;
            let Some(entry) = coty.get(&person, true) else {
                continue;
            };
            if table == "core_qualpoints" {
                let points = row.try_get("points")?;
                entry.base_points = points;
                entry.points = points;
            } else {
                let code: i64 = row.try_get("qual_type")?;
                entry.base_qualified = true;
                entry.qualified = true;
                entry.base_bonus |= awards_bonus(code, season);
                entry.bonus = entry.base_bonus;
                if code > 0 {
                    entry
                        .quals
                        .push(json!({"label": qualification_label(code), "live": false}));
                }
            }
        }
    }
    Ok(coty)
}

struct Flags {
    toty: bool,
    soty: bool,
    qual: bool,
    bar: i64,
}

fn flags(code: i64, season: i32) -> Flags {
    Flags {
        toty: matches!(code, 0 | 4 | 12),
        soty: matches!(code, 0 | 4 | 7 | 12 | 13),
        qual: matches!(code, 0 | 4 | 7 | 10 | 12 | 13 | 14 | 15),
        bar: match code {
            1 => 4,
            2 | 3 | 6 => 8,
            4 => 1,
            5 => 48,
            10 | 15 if season >= 2025 => 2,
            _ => 0,
        },
    }
}

async fn imported(
    pool: &AnyPool,
    year: i32,
    tournament: &Value,
    host: i64,
    code: i64,
) -> Result<bool, ApiError> {
    let Some(date) = text(&tournament["date"])
        .and_then(|value| NaiveDate::parse_from_str(value, "%Y-%m-%d").ok())
    else {
        return Ok(false);
    };
    let first = date
        .checked_sub_days(Days::new(7))
        .unwrap_or(date)
        .to_string();
    let last = date
        .checked_add_days(Days::new(7))
        .unwrap_or(date)
        .to_string();
    let value = sqlx::query_scalar::<_, i64>(
        "
        SELECT CAST(CASE WHEN EXISTS (SELECT 1 FROM core_tournament t
            WHERE t.season = $1 AND t.host_id = $2 AND CAST(t.date AS TEXT) BETWEEN $3 AND $4
              AND (CAST(t.date AS TEXT) = $5 OR t.qual_type = $6)
              AND (EXISTS (SELECT 1 FROM core_teamresult r WHERE r.tournament_id = t.id)
                OR EXISTS (SELECT 1 FROM core_speakerresult r WHERE r.tournament_id = t.id)))
             THEN 1 ELSE 0 END AS BIGINT)
    ",
    )
    .bind(year.to_string())
    .bind(host)
    .bind(first)
    .bind(last)
    .bind(date.to_string())
    .bind(code)
    .fetch_one(pool)
    .await?;
    Ok(value != 0)
}

async fn apply(
    pool: &AnyPool,
    season: i32,
    payload: &Value,
    people: &People,
    boards: &mut [Results; 3],
    coty: &mut Coty,
) -> Result<bool, ApiError> {
    let tournament = &payload["tournament"];
    if tournament["sanctioned"].as_bool() != Some(true)
        || number(&tournament["season"]) != Some(i64::from(season))
    {
        return Ok(false);
    }
    let Some(host) = known_id(&tournament["host_school"]["apda_id"]) else {
        return Ok(false);
    };
    let code = if tournament["tournament_type"].is_null() {
        0
    } else {
        let Some(code) = number(&tournament["tournament_type"]) else {
            return Ok(false);
        };
        code
    };
    let size = number(&tournament["num_teams"]).unwrap_or_default();
    if imported(pool, season, tournament, host, code).await? {
        return Ok(false);
    }
    let flags = flags(code, season);
    let url = text(&payload["source_url"])
        .and_then(safe_url)
        .unwrap_or_default();
    let name = text(&tournament["host_school"]["name"])
        .or_else(|| text(&tournament["name"]))
        .unwrap_or("Live tournament");
    let mark = |points| Marker {
        tournament: name.into(),
        url: url.clone(),
        points,
        live: true,
    };
    let mut used = false;
    for result in array(&payload["team_results"]) {
        if text(&result["division"]) != Some("varsity") {
            continue;
        }
        let Some(place) = number(&result["place"]) else {
            continue;
        };
        let roster: Vec<_> = array(&result["team"]["debaters"])
            .iter()
            .map(|p| people.resolve(p))
            .collect();
        let points = team_points_for_size(size, place, false);
        if let Some(school) = roster
            .first()
            .and_then(|p| p.school.as_ref())
            .filter(|s| s.eligible)
            && flags.toty
            && points > 0.0
            && roster
                .iter()
                .all(|p| p.school.as_ref().is_some_and(|s| s.key == school.key))
        {
            let known: Vec<_> = roster.iter().filter_map(|p| p.id).collect();
            let key = if known.len() == roster.len() {
                team_key(&known)
            } else {
                let mut names = roster.iter().map(|p| p.key.as_str()).collect::<Vec<_>>();
                names.sort_unstable();
                format!("team-name:{}", names.join(":"))
            };
            let initials: String = roster.iter().map(|p| p.initial).collect();
            let name = format!("{} {initials}", school.name);
            let detail = roster
                .iter()
                .map(|p| p.name.as_str())
                .collect::<Vec<_>>()
                .join(" and ");
            boards[0]
                .entry(key)
                .or_insert_with(|| Standing::new(name, detail, String::new()))
                .markers
                .push(mark(points));
            used = true;
        }
        for person in roster {
            if !flags.qual && flags.bar == 0 {
                continue;
            }
            let Some(entry) = coty.get(&person, false) else {
                continue;
            };
            if flags.qual && points > 0.0 {
                entry.points += points;
                used = true;
            }
            if place > 0 && place <= flags.bar {
                let label = if code == 0 {
                    json!("Points")
                } else {
                    qualification_label(code)
                };
                if !entry.quals.iter().any(|qual| qual["label"] == label) {
                    entry.quals.push(json!({"label":label,"live":true}));
                }
                entry.qualified = true;
                entry.bonus |= awards_bonus(code, season);
                used = true;
            }
            if flags.qual && entry.points >= 12.0 {
                entry.qualified = true;
                entry.bonus = true;
            }
        }
    }
    for result in array(&payload["speaker_results"]) {
        if !flags.soty || text(&result["division"]) != Some("varsity") {
            continue;
        }
        let Some(place) = number(&result["place"]) else {
            continue;
        };
        let points = speaker_points_for_size(size, place);
        if points <= 0.0 {
            continue;
        }
        let person = people.resolve(&result["debater"]);
        let Some(school) = person.school.filter(|s| s.eligible) else {
            continue;
        };
        boards[1]
            .entry(person.key)
            .or_insert_with(|| Standing::new(person.name, school.name, person.url))
            .markers
            .push(mark(points));
        used = true;
    }
    Ok(used)
}

fn finalize(rows: Results, limit: usize) -> Vec<Value> {
    let mut output = Vec::new();
    for mut row in rows.into_values() {
        row.markers.sort_by(|a, b| {
            b.points
                .total_cmp(&a.points)
                .then_with(|| a.live.cmp(&b.live))
        });
        row.markers.truncate(limit);
        row.points = round_points(row.markers.iter().map(|marker| marker.points).sum());
        row.has_live = row.markers.iter().any(|marker| marker.live);
        row.marker_slots = std::mem::take(&mut row.markers)
            .into_iter()
            .map(Some)
            .collect();
        row.marker_slots.resize(limit, None);
        if row.points > 0.0 {
            output.push(json!(row));
        }
    }
    assign_places(&mut output);
    output
}

fn assign_places(rows: &mut [Value]) {
    fn points(row: &Value) -> f64 {
        row["points"].as_f64().unwrap_or_default()
    }
    rows.sort_by(|a, b| {
        points(b).total_cmp(&points(a)).then_with(|| {
            text(&a["name"])
                .unwrap_or_default()
                .to_lowercase()
                .cmp(&text(&b["name"]).unwrap_or_default().to_lowercase())
        })
    });
    let mut place = 0;
    let mut last = None;
    for (index, row) in rows.iter_mut().enumerate() {
        let current = points(row);
        let tied = last == Some(current);
        if !tied {
            place = index + 1;
        }
        row["place"] = json!(place);
        row["tied"] = json!(tied);
        last = Some(current);
    }
}

pub(crate) async fn page(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(live) = &state.live else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let Ok(data) = live
        .page_data(&state.pool, state.current_season, state.memcached.as_ref())
        .await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let common = web::base_context(&state, &user, &uri);
    let bindings = context! {boards => &data.boards, active_sources => &data.active_sources,
    current_season => state.current_season.to_string()};
    match web::render("core/live.html", context! {..common, ..bindings}) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Live standings template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equal_reviewed_marker_stays_ahead_of_live_marker_at_the_slot_limit() {
        let mut row = Standing::new("A".into(), String::new(), String::new());
        row.markers = [true, false]
            .into_iter()
            .map(|live| Marker {
                tournament: if live {
                    "Live".into()
                } else {
                    "Reviewed".into()
                },
                url: String::new(),
                points: 10.0,
                live,
            })
            .collect();
        let result = finalize(HashMap::from([("team:1".into(), row)]), 1);
        assert_eq!(result[0]["points"], 10.0);
        assert_eq!(result[0]["marker_slots"][0]["tournament"], "Reviewed");
        assert_eq!(result[0]["has_live"], false);
    }
    #[test]
    fn current_cycle_bp_autoquals_and_ordinary_qual_points_use_distinct_bonus_rules() {
        assert!(awards_bonus(1, 2025));
        assert!(!awards_bonus(1, 2026));
        assert!(awards_bonus(0, 2026));
        assert_eq!(contribution(65.0, true), 66.0);
        assert_eq!(flags(10, 2024).bar, 0);
        assert_eq!(flags(10, 2025).bar, 2);
    }
}
