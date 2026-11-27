use std::collections::{HashMap, HashSet};

use chrono::{Datelike, Months, NaiveDate};
use sqlx::{AnyConnection, AssertSqlSafe, Row, any::AnyRow};
use tokio_stream::StreamExt;
use unicode_normalization::UnicodeNormalization;

use crate::ApiError;

use super::scoring::{Clue, person_key};

const AGE: &[(f64, f64)] = &[
    (0.0, 0.0),
    (2.0, 1.0),
    (3.0, 12.0),
    (4.0, 29.0),
    (6.0, 49.0),
    (10.0, 74.0),
    (13.0, 87.0),
    (17.0, 94.0),
    (18.0, 101.0),
    (24.0, 117.0),
];
const TOP_CUTOFFS: [(i64, &str); 4] = [
    (1, "Won"),
    (2, "Finals or better"),
    (4, "Semis or better"),
    (8, "Quarters or better"),
];
const EVENT_TYPES: &[(i64, &str)] = &[
    (7, "Pro-Am"),
    (13, "Pro-Am"),
    (9, "Novice"),
    (15, "BIPOC"),
    (10, "Gender Minority"),
    (14, "Gender Minority"),
    (8, "Nationals"),
];

const SCHOOLS: &str =
    "SELECT CAST(id AS BIGINT) AS id, name FROM core_school WHERE NOT synthetic AND NOT temporary";
const PROFILES: &str = "
    SELECT CAST(id AS BIGINT) AS id, CAST(alias_group_id AS BIGINT) AS alias_group,
        CAST(school_id AS BIGINT) AS school, TRIM(first_name || ' ' || last_name) AS name, first_season, latest_season
    FROM core_debater WHERE NOT synthetic AND NOT temporary ORDER BY id
";
const TEAMS: &str = "
    SELECT CAST(m.team_id AS BIGINT) AS team, CAST(m.debater_id AS BIGINT) AS debater
    FROM core_team_debaters m JOIN core_team t ON t.id = m.team_id JOIN core_debater d ON d.id = m.debater_id
    WHERE NOT t.synthetic AND NOT d.synthetic AND NOT d.temporary
";
const REDACTED: &str = "
    SELECT DISTINCT CAST(r.team_id AS BIGINT) AS team, CAST(r.tournament_id AS BIGINT) AS tournament
    FROM core_teamresult r JOIN core_teamresult_hidden_from_debaters h ON h.teamresult_id = r.id
";
const TOURNAMENT: &str = "
    CAST(t.id AS BIGINT) AS tournament, t.season, CAST(t.date AS TEXT) AS event_day,
    CAST(t.host_id AS BIGINT) AS host, CAST(t.qual_type AS BIGINT) AS kind
";
const TEAM_RESULTS: &str = "
    SELECT CAST(r.team_id AS BIGINT) AS team, CAST(r.place AS BIGINT) AS place,
        CAST(r.type_of_place AS BIGINT) AS division, {tournament}
    FROM core_teamresult r JOIN core_tournament t ON t.id = r.tournament_id
    JOIN core_team g ON g.id = r.team_id
    WHERE NOT g.synthetic AND CAST(t.date AS TEXT) <= $1
        AND NOT EXISTS (SELECT 1 FROM core_teamresult_hidden_from_debaters h WHERE h.teamresult_id = r.id)
";
const SPEAKER_RESULTS: &str = "
    SELECT CAST(r.debater_id AS BIGINT) AS debater, CAST(r.place AS BIGINT) AS place,
        CAST(r.type_of_place AS BIGINT) AS division, CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS tie,
        {tournament}
    FROM core_speakerresult r JOIN core_tournament t ON t.id = r.tournament_id
    WHERE r.place > 0 AND NOT r.hidden_from_profile AND CAST(t.date AS TEXT) <= $1
";
const ATTENDANCE: &str = "
    SELECT CAST(t.id AS BIGINT) AS tournament, CAST(r.gov_id AS BIGINT) AS team
    FROM core_round r JOIN core_tournament t ON t.id = r.tournament_id
    WHERE r.victor NOT IN (0, 7) AND CAST(t.date AS TEXT) <= $1
    UNION
    SELECT CAST(t.id AS BIGINT) AS tournament, CAST(r.opp_id AS BIGINT) AS team
    FROM core_round r JOIN core_tournament t ON t.id = r.tournament_id
    WHERE r.victor NOT IN (0, 7) AND CAST(t.date AS TEXT) <= $1
";
const HIDDEN: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, CAST(d.alias_group_id AS BIGINT) AS alias_group
    FROM core_immaculategridhiddendebater h JOIN core_debater d ON d.id = h.debater_id
";

#[derive(Default)]
pub(super) struct Catalog {
    pub(super) aliases: HashMap<i64, String>,
    pub(super) clues: HashMap<String, Category>,
    pub(super) rarity: HashMap<String, f64>,
    pub(super) recent: HashSet<String>,
    pub(super) progress: HashMap<String, HashMap<String, i64>>,
}

pub(super) struct Category {
    pub(super) display: Clue,
    pub(super) topic: String,
    pub(super) people: HashSet<String>,
    pub(super) occurrences: HashMap<String, HashSet<i64>>,
    latest_host_result: Option<NaiveDate>,
}

impl Category {
    pub(super) fn kind(&self) -> (&str, &str) {
        let (category, variant) = self
            .display
            .key
            .split_once(':')
            .unwrap_or((&self.display.key, ""));
        if matches!(category, "event" | "host") {
            return ("placement", variant.rsplit(':').next().unwrap_or_default());
        }
        (category, "")
    }

    pub(super) fn frequency(&self, person: &str) -> f64 {
        self.occurrences
            .get(person)
            .map_or(1, |values| values.len().max(1)) as f64
    }
}

impl Catalog {
    fn add(
        &mut self,
        clue: Clue,
        topic: Option<&str>,
        person: &str,
        occurrences: impl IntoIterator<Item = i64>,
        date: Option<NaiveDate>,
    ) {
        let key = clue.key.clone();
        let category = self.clues.entry(key.clone()).or_insert_with(|| Category {
            display: clue,
            topic: topic.map_or(key, str::to_owned),
            people: HashSet::new(),
            occurrences: HashMap::new(),
            latest_host_result: None,
        });
        category.people.insert(person.to_owned());
        category
            .occurrences
            .entry(person.to_owned())
            .or_default()
            .extend(occurrences);
        category.latest_host_result = category.latest_host_result.max(date);
    }

    fn plain(
        &mut self,
        key: String,
        label: String,
        topic: Option<&str>,
        person: &str,
        occurrences: impl IntoIterator<Item = i64>,
    ) {
        self.add(
            Clue {
                key,
                label,
                subject: None,
            },
            topic,
            person,
            occurrences,
            None,
        );
    }

    fn measure(&mut self, topic: &str, person: &str, value: i64) {
        if let Some(progress) = self.progress.get_mut(topic) {
            progress
                .entry(person.to_owned())
                .and_modify(|old| *old = (*old).max(value))
                .or_insert(value);
        }
    }
}

#[derive(Default)]
struct Career {
    name: String,
    schools: HashSet<i64>,
    school_tournaments: HashMap<i64, HashSet<i64>>,
    attended: HashSet<i64>,
    first_season: Option<i32>,
    latest_season: Option<i32>,
    latest_affiliated: Option<i32>,
    latest_tournament: Option<NaiveDate>,
    points: HashMap<(&'static str, String), f64>,
    prestige: i64,
    top_ten: bool,
    winning_speaker_divisions: HashSet<i64>,
    winning_speaker_tournaments: HashSet<i64>,
    varsity_seasons: HashSet<i64>,
    speaker_seasons: HashSet<i64>,
    finalist_seasons: HashSet<i64>,
    semifinal_hosts_by_season: HashMap<i32, HashSet<i64>>,
    placed_partners: HashSet<String>,
    partner_schools: HashSet<i64>,
}

impl Career {
    fn season(&mut self, first: Option<i32>, latest: Option<i32>, affiliated: bool) {
        if let Some(first) = first {
            self.first_season = Some(self.first_season.map_or(first, |old| old.min(first)));
        }
        self.latest_season = self.latest_season.max(latest);
        if affiliated {
            self.latest_affiliated = self.latest_affiliated.max(latest);
        }
    }

    fn tournament(&mut self, event: &Event, school: Option<i64>) {
        self.season(event.season, event.season, school.is_some());
        self.latest_tournament = self.latest_tournament.max(Some(event.day));
        self.attended.insert(event.id);
        if let Some(school) = school {
            self.school_tournaments
                .entry(school)
                .or_default()
                .insert(event.id);
        }
    }

    fn points(&mut self, kind: &'static str, season: &str, points: f64) {
        let entry = self.points.entry((kind, season.to_owned())).or_default();
        *entry = entry.max(points);
    }
}

#[derive(Default)]
struct History {
    schools: HashMap<i64, String>,
    careers: HashMap<String, Career>,
    affiliations: HashMap<i64, i64>,
    teams: HashMap<i64, Vec<i64>>,
    redacted_pairs: HashSet<(i64, i64)>,
    redacted_teams: HashSet<i64>,
    partners: HashMap<String, HashMap<String, HashSet<i64>>>,
}

fn year(season: &str) -> Option<i32> {
    season
        .trim()
        .split('-')
        .next()?
        .parse::<i32>()
        .ok()
        .filter(|value| *value > 0)
}

struct Event {
    id: i64,
    season: Option<i32>,
    day: NaiveDate,
    host: Option<i64>,
    kind: i64,
    place: i64,
    division: i64,
}

impl Event {
    fn from_row(row: &AnyRow) -> Result<Self, ApiError> {
        let day: &str = row.try_get("event_day")?;
        Ok(Self {
            id: row.try_get("tournament")?,
            season: year(row.try_get("season")?),
            day: day.parse().map_err(|_| ApiError::InvalidStoredDate)?,
            host: row.try_get("host")?,
            kind: row.try_get("kind")?,
            place: row.try_get("place")?,
            division: row.try_get("division")?,
        })
    }
}

pub(super) fn rarity(
    seasons_since_active: i32,
    points: f64,
    prestige: i64,
    span: i32,
    attended: usize,
) -> f64 {
    let age = f64::from(seasons_since_active.max(0));
    let rise = AGE
        .windows(2)
        .find_map(|pair| {
            let [(first, low), (last, high)] = [pair[0], pair[1]];
            (age <= last).then(|| low + (high - low) * (age - first) / (last - first))
        })
        .unwrap_or(117.0);
    let discount = 70.0 * points / (points + 120.0);
    let career_discount = (1.5 * f64::from(span)).clamp(0.0, 12.0);
    let recent = ((8.0 - f64::from(seasons_since_active)) / 4.0).clamp(0.0, 1.0);
    let activity = (1.0 + attended as f64 / 5.0).log2();
    64.0 + rise
        - discount
        - career_discount
        - (3.0 * activity + recent * (5.5 * activity).min(10.0))
        - 0.45 * prestige as f64
}

async fn profiles(
    conn: &mut AnyConnection,
    catalog: &mut Catalog,
    history: &mut History,
    end: i32,
) -> Result<(), ApiError> {
    let unaffiliated = regex::Regex::new(r"\bunaffiliated\b").expect("valid school regex");
    let mut rows = sqlx::query(SCHOOLS).fetch(&mut *conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        let name: String = row.try_get("name")?;
        let normalized: String = name
            .nfkd()
            .filter(char::is_ascii)
            .collect::<String>()
            .to_ascii_lowercase();
        if !normalized.trim().is_empty() && !unaffiliated.is_match(&normalized) {
            history.schools.insert(row.try_get("id")?, name);
        }
    }
    drop(rows);
    let mut rows = sqlx::query(PROFILES).fetch(&mut *conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        let id: i64 = row.try_get("id")?;
        let person = person_key(id, row.try_get("alias_group")?);
        catalog.aliases.insert(id, person.clone());
        let name: String = row.try_get("name")?;
        let career = history.careers.entry(person).or_insert_with(|| Career {
            name,
            ..Default::default()
        });
        if let Some(school) = row
            .try_get::<Option<i64>, _>("school")?
            .filter(|id| history.schools.contains_key(id))
        {
            career.schools.insert(school);
            history.affiliations.insert(id, school);
        }
        let cutoff = |value: Option<&str>| value.and_then(year).filter(|value| *value <= end);
        career.season(
            cutoff(row.try_get("first_season")?),
            cutoff(row.try_get("latest_season")?),
            history.affiliations.contains_key(&id),
        );
    }
    drop(rows);
    let mut rows = sqlx::query(TEAMS).fetch(&mut *conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        history
            .teams
            .entry(row.try_get("team")?)
            .or_default()
            .push(row.try_get("debater")?);
    }
    drop(rows);
    let mut rows = sqlx::query(REDACTED).fetch(conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        let team = row.try_get("team")?;
        history.redacted_teams.insert(team);
        history
            .redacted_pairs
            .insert((team, row.try_get("tournament")?));
    }
    Ok(())
}

fn placements(catalog: &mut Catalog, schools: &HashMap<i64, String>, person: &str, event: &Event) {
    let host = event
        .host
        .and_then(|id| schools.get(&id).map(|name| (format!("host:{id}"), name)));
    let topic = EVENT_TYPES
        .iter()
        .find(|(kind, _)| *kind == event.kind)
        .map(|(_, name)| {
            (
                format!("event:{name}"),
                if event.kind == 8 {
                    "Nationals".to_owned()
                } else {
                    format!("a {name} tournament")
                },
            )
        });
    for (key, _) in [
        &host.as_ref().map(|(k, v)| (k, v.as_str())),
        &topic.as_ref().map(|(k, v)| (k, v.as_str())),
    ]
    .into_iter()
    .flatten()
    {
        catalog.measure(key, person, -event.place);
    }
    for (cutoff, phrase) in TOP_CUTOFFS
        .into_iter()
        .filter(|(cutoff, _)| event.place <= *cutoff)
    {
        if let Some((topic, school)) = &host {
            let label = if cutoff == 1 {
                format!("Won a tournament hosted by {school}")
            } else {
                format!("{phrase} at a tournament hosted by {school}")
            };
            catalog.add(
                Clue {
                    key: format!("{topic}:{cutoff}"),
                    label,
                    subject: None,
                },
                Some(topic),
                person,
                [event.id],
                Some(event.day),
            );
        }
        if let Some((topic, tournament)) = &topic {
            let label = if cutoff == 1 {
                format!("{phrase} {tournament}")
            } else {
                format!("{phrase} at {tournament}")
            };
            catalog.plain(
                format!("{topic}:{cutoff}"),
                label,
                Some(topic),
                person,
                [event.id],
            );
        }
    }
}

async fn team_results(
    conn: &mut AnyConnection,
    date: &str,
    catalog: &mut Catalog,
    history: &mut History,
) -> Result<(), ApiError> {
    let query = TEAM_RESULTS.replace("{tournament}", TOURNAMENT);
    let mut rows = sqlx::query(AssertSqlSafe(query)).bind(date).fetch(conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        let team: i64 = row.try_get("team")?;
        let event = Event::from_row(&row)?;
        let Some(members) = history.teams.get(&team) else {
            continue;
        };
        let placing = event.place > 0;
        let redacted = history.redacted_teams.contains(&team);
        if !redacted {
            for (index, left) in members.iter().enumerate() {
                for right in &members[index + 1..] {
                    let first = &catalog.aliases[left];
                    let second = &catalog.aliases[right];
                    if first == second {
                        continue;
                    }
                    for (person, partner, profile) in
                        [(first, second, right), (second, first, left)]
                    {
                        history
                            .partners
                            .entry(person.clone())
                            .or_default()
                            .entry(partner.clone())
                            .or_default()
                            .insert(event.id);
                        if placing {
                            let career = history
                                .careers
                                .get_mut(person)
                                .expect("profile has a career");
                            career.placed_partners.insert(partner.clone());
                            career
                                .partner_schools
                                .extend(history.affiliations.get(profile).copied());
                        }
                    }
                }
            }
        }
        let hybrid = members
            .iter()
            .filter_map(|id| history.affiliations.get(id))
            .collect::<HashSet<_>>()
            .len()
            > 1;
        for id in members {
            let person = catalog.aliases[id].clone();
            let career = history
                .careers
                .get_mut(&person)
                .expect("profile has a career");
            career.tournament(&event, history.affiliations.get(id).copied());
            if !placing {
                continue;
            }
            placements(catalog, &history.schools, &person, &event);
            if hybrid && !redacted {
                catalog.plain(
                    "hybrid".into(),
                    "Placed on a hybrid team".into(),
                    None,
                    &person,
                    [event.id],
                );
            }
            if event.division == 1
                && let Some(season) = event.season
            {
                career.varsity_seasons.insert(i64::from(season));
                if event.place <= 4
                    && let Some(host) = event.host.filter(|id| history.schools.contains_key(id))
                {
                    career
                        .semifinal_hosts_by_season
                        .entry(season)
                        .or_default()
                        .insert(host);
                }
                if event.place <= 2 {
                    career.finalist_seasons.insert(i64::from(season));
                }
                career.prestige = career
                    .prestige
                    .max((9 - event.place) * if event.kind == 8 { 2 } else { 1 });
            }
        }
    }
    Ok(())
}

async fn speaker_results(
    conn: &mut AnyConnection,
    date: &str,
    catalog: &mut Catalog,
    history: &mut History,
) -> Result<(), ApiError> {
    let query = SPEAKER_RESULTS.replace("{tournament}", TOURNAMENT);
    let mut rows = sqlx::query(AssertSqlSafe(query)).bind(date).fetch(conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        let id: i64 = row.try_get("debater")?;
        let Some(person) = catalog.aliases.get(&id).cloned() else {
            continue;
        };
        let event = Event::from_row(&row)?;
        let career = history
            .careers
            .get_mut(&person)
            .expect("profile has a career");
        career.tournament(&event, history.affiliations.get(&id).copied());
        let rank = (event.place - row.try_get::<i64, _>("tie")?).max(1);
        if rank == 1 {
            career.winning_speaker_divisions.insert(event.division);
            career
                .winning_speaker_tournaments
                .insert(event.id * 2 + event.division);
        }
        if event.division != 1 {
            continue;
        }
        if let Some(season) = event.season {
            career.varsity_seasons.insert(i64::from(season));
            career.speaker_seasons.insert(i64::from(season));
        }
        catalog.measure("top_speaker", &person, -rank);
        if rank == 1 {
            catalog.plain(
                "top_speaker".into(),
                "Won at least one varsity top speak".into(),
                None,
                &person,
                [event.id],
            );
        }
        if let Some((host, school)) = event
            .host
            .and_then(|host| history.schools.get(&host).map(|name| (host, name)))
        {
            catalog.measure(&format!("speaker:{host}"), &person, -rank);
            for cutoff in [5, 10].into_iter().filter(|cutoff| rank <= *cutoff) {
                catalog.add(
                    Clue {
                        key: format!("speaker:{host}:{cutoff}"),
                        label: format!(
                            "Top-{cutoff} varsity speaker at a tournament hosted by {school}"
                        ),
                        subject: None,
                    },
                    Some(&format!("host:{host}")),
                    &person,
                    [event.id],
                    Some(event.day),
                );
            }
        }
        career.prestige = career
            .prestige
            .max((11 - rank) * if event.kind == 8 { 2 } else { 1 });
    }
    Ok(())
}

async fn standings(
    conn: &mut AnyConnection,
    end: i32,
    catalog: &mut Catalog,
    history: &mut History,
) -> Result<(), ApiError> {
    let last = end.to_string();
    for (table, kind, field) in [
        ("core_qualpoints", "QUAL", "debater_id"),
        ("core_soty", "SOTY", "debater_id"),
        ("core_toty", "TOTY", "team_id"),
    ] {
        let restriction = if kind == "QUAL" {
            "points > 0"
        } else {
            "place > 0"
        };
        let place = if kind == "QUAL" {
            "CAST(0 AS BIGINT)"
        } else {
            "CAST(place AS BIGINT)"
        };
        let query = format!("SELECT CAST({field} AS BIGINT) AS id, season, {place} AS place,
            CAST(points AS DOUBLE PRECISION) AS points FROM {table} WHERE season <= $1 AND {restriction}");
        let mut rows = sqlx::query(AssertSqlSafe(query))
            .bind(&last)
            .fetch(&mut *conn);
        while let Some(row) = rows.next().await {
            let row = row?;
            let id: i64 = row.try_get("id")?;
            let members = if kind != "TOTY" {
                std::slice::from_ref(&id)
            } else if history.redacted_teams.contains(&id) {
                &[]
            } else {
                history.teams.get(&id).map_or(&[][..], Vec::as_slice)
            };
            let season: &str = row.try_get("season")?;
            let points = row.try_get("points")?;
            let place: i64 = row.try_get("place")?;
            for member in members {
                let Some(person) = catalog.aliases.get(member).cloned() else {
                    continue;
                };
                let career = history
                    .careers
                    .get_mut(&person)
                    .expect("profile has a career");
                if kind != "TOTY" {
                    career.points(kind, season, points);
                }
                if kind == "QUAL" {
                    continue;
                }
                career.prestige = career.prestige.max(11 - place);
                career.top_ten |= place <= 10;
                catalog.measure(kind, &person, -place);
                for cutoff in [10, 20].into_iter().filter(|cutoff| place <= *cutoff) {
                    catalog.plain(
                        format!("{kind}:{cutoff}"),
                        format!("Top-{cutoff} {kind} in at least one season"),
                        Some(kind),
                        &person,
                        [i64::from(year(season).unwrap_or_default())],
                    );
                }
            }
        }
    }
    Ok(())
}

fn career_clues(
    catalog: &mut Catalog,
    schools: &HashMap<i64, String>,
    person: &str,
    career: &Career,
) {
    for school in &career.schools {
        catalog.plain(
            format!("school:{school}"),
            format!("Debated for {}", schools[school]),
            None,
            person,
            career
                .school_tournaments
                .get(school)
                .into_iter()
                .flatten()
                .copied(),
        );
    }
    for (kind, season, label) in [
        ("first", career.first_season, "First season was"),
        ("latest", career.latest_season, "Most recent season was"),
        (
            "affiliated",
            career.latest_affiliated,
            "Most recent season representing a school was",
        ),
    ] {
        if let Some(season) = season {
            catalog.plain(
                format!("{kind}:{season}"),
                format!("{label} {season}–{:02}", (season + 1) % 100),
                None,
                person,
                [],
            );
        }
    }
    if [0, 1]
        .into_iter()
        .all(|division| career.winning_speaker_divisions.contains(&division))
    {
        catalog.plain(
            "both_divisions".into(),
            "At least one novice top speak and at least one varsity top speak".into(),
            None,
            person,
            career.winning_speaker_tournaments.iter().copied(),
        );
    }
    let max_hosts = career
        .semifinal_hosts_by_season
        .values()
        .map(HashSet::len)
        .max()
        .unwrap_or_default();
    catalog.measure("placed_hosts", person, max_hosts as i64);
    for cutoff in 3..=max_hosts.min(6) {
        let years = career
            .semifinal_hosts_by_season
            .iter()
            .filter(|(_, hosts)| hosts.len() >= cutoff)
            .map(|(year, _)| i64::from(*year));
        catalog.plain(
            format!("placed_hosts:{cutoff}"),
            format!("Varsity semis or better at {cutoff}+ different hosts in one season"),
            Some("placed_hosts"),
            person,
            years,
        );
    }
    catalog.measure(
        "multiple_finals",
        person,
        career.finalist_seasons.len() as i64,
    );
    if career.finalist_seasons.len() >= 2 {
        catalog.plain(
            "multiple_finals".into(),
            "Varsity team finalist or better in at least 2 seasons".into(),
            None,
            person,
            career.finalist_seasons.iter().copied(),
        );
    }
    let partner_keys = 0..career.placed_partners.len() as i64;
    let partners: HashSet<_> = partner_keys.collect();
    for (topic, count, records, min, max, label) in [
        (
            "speaks_seasons",
            career.speaker_seasons.len(),
            &career.speaker_seasons,
            2,
            4,
            "Varsity speaker awards in {}+ seasons",
        ),
        (
            "varsity_seasons",
            career.varsity_seasons.len(),
            &career.varsity_seasons,
            2,
            4,
            "Varsity placements in {}+ seasons",
        ),
        (
            "placed_partners",
            career.placed_partners.len(),
            &partners,
            3,
            6,
            "Placed with {}+ different partners",
        ),
        (
            "partner_schools",
            career.partner_schools.len(),
            &career.partner_schools,
            2,
            4,
            "Placed with partners from {}+ schools",
        ),
    ] {
        catalog.measure(topic, person, count as i64);
        for cutoff in min..=max.min(count) {
            catalog.plain(
                format!("{topic}:{cutoff}"),
                label.replace("{}", &cutoff.to_string()),
                Some(topic),
                person,
                records.iter().copied(),
            );
        }
    }
}

pub(super) async fn load(
    conn: &mut AnyConnection,
    date: NaiveDate,
    progress: &[String],
) -> Result<Catalog, ApiError> {
    let end = date.year() - i32::from(date.month() < 8);
    let mut catalog = Catalog {
        progress: progress
            .iter()
            .map(|topic| (topic.clone(), HashMap::new()))
            .collect(),
        ..Default::default()
    };
    let mut history = History::default();
    profiles(conn, &mut catalog, &mut history, end).await?;
    let day = date.to_string();
    team_results(conn, &day, &mut catalog, &mut history).await?;
    speaker_results(conn, &day, &mut catalog, &mut history).await?;
    standings(conn, end, &mut catalog, &mut history).await?;
    let mut rows = sqlx::query(ATTENDANCE).bind(&day).fetch(&mut *conn);
    while let Some(row) = rows.next().await {
        let row = row?;
        let tournament: i64 = row.try_get("tournament")?;
        let team: i64 = row.try_get("team")?;
        if !history.redacted_pairs.contains(&(team, tournament)) {
            for id in history.teams.get(&team).into_iter().flatten() {
                if let Some(career) = history.careers.get_mut(&catalog.aliases[id]) {
                    career.attended.insert(tournament);
                }
            }
        }
    }
    drop(rows);
    let hidden = sqlx::query(HIDDEN)
        .fetch_all(conn)
        .await?
        .into_iter()
        .map(|row| Ok(person_key(row.try_get("id")?, row.try_get("alias_group")?)))
        .collect::<Result<HashSet<_>, sqlx::Error>>()?;
    let recent = date.checked_sub_months(Months::new(48)).unwrap_or(date);
    for (person, career) in &history.careers {
        career_clues(&mut catalog, &history.schools, person, career);
        let latest = career.latest_season.unwrap_or(end);
        let span = latest - career.first_season.unwrap_or(end);
        catalog.rarity.insert(
            person.clone(),
            rarity(
                end - latest,
                career.points.values().sum(),
                career.prestige,
                span,
                career.attended.len(),
            ),
        );
        if career
            .latest_tournament
            .is_some_and(|latest| latest >= recent)
        {
            catalog.recent.insert(person.clone());
            if career.top_ten && !hidden.contains(person) {
                for (partner, tournaments) in history.partners.get(person).into_iter().flatten() {
                    catalog.add(
                        Clue {
                            key: format!("partner:{person}"),
                            label: format!("Partnered with {}", career.name),
                            subject: Some(person.clone()),
                        },
                        None,
                        partner,
                        tournaments.iter().copied(),
                        None,
                    );
                }
            }
        }
    }
    let host_cutoff = date.checked_sub_months(Months::new(36)).unwrap_or(date);
    catalog.clues.retain(|_, clue| {
        clue.latest_host_result
            .is_none_or(|date| date >= host_cutoff)
    });
    Ok(catalog)
}
