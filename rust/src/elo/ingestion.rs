use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    sync::LazyLock,
};

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use regex::Regex;
use serde_json::Value;
use sqlx::{AnyConnection, AssertSqlSafe, Row};
use unicode_normalization::UnicodeNormalization;

use super::rating::Debate;
use crate::{
    ApiError,
    round_contract::{metadata_elimination, metadata_ids, metadata_winner},
};

static NOVICE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\bnovice\b").unwrap());
static PRO_AM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\bpro\s*ams?\b").unwrap());
static UNAFFILIATED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\bunaffiliated\b").unwrap());

pub(super) fn season_year(value: &str) -> Option<i32> {
    let year = value.trim().split('-').next()?;
    year.bytes()
        .all(|digit| digit.is_ascii_digit())
        .then(|| year.parse().ok())
        .flatten()
}
pub(super) fn school(value: &str) -> String {
    let normalized = value
        .nfkd()
        .filter(char::is_ascii)
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    if UNAFFILIATED.is_match(&normalized) {
        "unaffiliated".to_owned()
    } else {
        normalized
    }
}

#[derive(Default)]
pub(super) struct People {
    pub(super) representatives: HashMap<i64, i64>,
    pub(super) profiles: HashMap<i64, Person>,
    pub(super) linked: HashMap<i64, Vec<i64>>,
    first: HashMap<i64, i32>,
}

pub(super) struct Person {
    pub(super) display: String,
    pub(super) school_id: Option<i64>,
    pub(super) school: String,
    pub(super) normalized_school: String,
    pub(super) first: Option<i32>,
    pub(super) latest: Option<i32>,
    pub(super) manual: String,
    pub(super) alias: Option<i64>,
}

impl People {
    pub(super) async fn load(conn: &mut AnyConnection) -> Result<Self, ApiError> {
        let mut lookup = Self::default();
        let mut groups = HashMap::new();
        let rows=sqlx::query("SELECT CAST(d.id AS BIGINT) AS id, CAST(d.alias_group_id AS BIGINT) AS alias_id,
            TRIM(d.first_name || ' ' || d.last_name) AS display, d.first_season,d.latest_season,d.elo_manual_opt,
            CAST(d.school_id AS BIGINT) AS school_id,COALESCE(s.name,'') AS school
            FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id ORDER BY d.id").fetch_all(conn).await?;
        for row in rows {
            let id = row.try_get("id")?;
            let alias = row.try_get::<Option<i64>, _>("alias_id")?;
            let rep = alias.map_or(id, |group| *groups.entry(group).or_insert(id));
            lookup.representatives.insert(id, rep);
            lookup.linked.entry(rep).or_default().push(id);
            let first = row
                .try_get::<Option<&str>, _>("first_season")?
                .and_then(season_year);
            let latest = row
                .try_get::<Option<&str>, _>("latest_season")?
                .and_then(season_year);
            if let Some(first) = first {
                let prior = lookup.first.entry(rep).or_insert(first);
                *prior = (*prior).min(first);
            }
            let name = row.try_get::<&str, _>("school")?.trim().to_owned();
            let normalized_school = school(&name);
            let manual = row
                .try_get::<Option<&str>, _>("elo_manual_opt")?
                .unwrap_or_default()
                .trim()
                .to_lowercase();
            lookup.profiles.insert(
                id,
                Person {
                    display: row.try_get("display")?,
                    school_id: row.try_get("school_id")?,
                    school: name,
                    normalized_school,
                    first,
                    latest,
                    alias,
                    manual,
                },
            );
        }
        Ok(lookup)
    }
    pub(super) fn id(&self, id: i64) -> i64 {
        self.representatives.get(&id).copied().unwrap_or(id)
    }
    fn pro_am(&self, members: &[(i64, String)], season: &str) -> bool {
        let [left, right] = members else { return false };
        let Some(season) = season_year(season) else {
            return false;
        };
        (self.first.get(&left.0) == Some(&season)) != (self.first.get(&right.0) == Some(&season))
    }
}

type Roster = Vec<(i64, String)>;

async fn load_teams(
    conn: &mut AnyConnection,
    keys: &BTreeSet<i64>,
) -> Result<HashMap<i64, Roster>, ApiError> {
    let keys = keys.iter().copied().collect::<Vec<_>>();
    let mut teams = HashMap::<i64, Roster>::new();
    for ids in keys.chunks(200) {
        let args = (1..=ids.len())
            .map(|id| format!("${id}"))
            .collect::<Vec<_>>()
            .join(",");
        let statement = format!(
            "SELECT CAST(m.team_id AS BIGINT) AS team,CAST(m.debater_id AS BIGINT) AS person,
            TRIM(TRIM(d.first_name) || ' ' || TRIM(d.last_name)) AS name
            FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id
            WHERE m.team_id IN ({args}) ORDER BY m.team_id,m.id"
        );
        let mut query = sqlx::query(AssertSqlSafe(statement));
        for &id in ids {
            query = query.bind(id);
        }
        for row in query.fetch_all(&mut *conn).await? {
            teams
                .entry(row.try_get("team")?)
                .or_default()
                .push((row.try_get("person")?, row.try_get("name")?));
        }
    }
    Ok(teams)
}

async fn scored_people(
    conn: &mut AnyConnection,
    ids: &BTreeSet<i64>,
    people: &People,
) -> Result<HashMap<i64, HashSet<i64>>, ApiError> {
    let ids = ids.iter().copied().collect::<Vec<_>>();
    let mut scores = HashMap::<i64, HashSet<i64>>::new();
    for ids in ids.chunks(220) {
        let args = (1..=ids.len())
            .map(|id| format!("${id}"))
            .collect::<Vec<_>>()
            .join(",");
        let statement=format!("SELECT DISTINCT CAST(round_id AS BIGINT) AS round,CAST(debater_id AS BIGINT) AS debater
            FROM core_roundstats WHERE round_id IN ({args}) AND speaks IS NOT NULL");
        let mut query = sqlx::query(AssertSqlSafe(statement));
        for &id in ids {
            query = query.bind(id);
        }
        for row in query.fetch_all(&mut *conn).await? {
            scores
                .entry(row.try_get("round")?)
                .or_default()
                .insert(people.id(row.try_get("debater")?));
        }
    }
    Ok(scores)
}

fn value_text(value: &Value) -> String {
    crate::tournament_import::text(value).trim().to_owned()
}

fn season_allowed(allowed: &BTreeSet<String>, season: &str) -> bool {
    allowed.is_empty() || allowed.contains(season)
}

fn timestamp(value: &str, fallback: NaiveDate) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|value| value.with_timezone(&Utc))
        .or_else(|| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
                .ok()
                .map(|value| value.and_utc())
        })
        .or_else(|| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f")
                .ok()
                .map(|value| value.and_utc())
        })
        .unwrap_or_else(|| fallback.and_hms_opt(12, 0, 0).unwrap().and_utc())
}

struct RawDebate {
    id: i64,
    tournament: i64,
    name: String,
    date: NaiveDate,
    season: String,
    round: i64,
    stage: String,
    label: String,
    victor: i64,
    origin: String,
    metadata: Value,
    ts: [i64; 2],
    aliases: [Roster; 2],
}

impl RawDebate {
    fn metadata_roster(&self, side: usize) -> Roster {
        let ids = metadata_ids(
            &self.metadata,
            if side == 0 {
                "team_a_ids"
            } else {
                "team_b_ids"
            },
        )
        .unwrap_or_default();
        let names = self.metadata[if side == 0 {
            "team_a_names"
        } else {
            "team_b_names"
        }]
        .as_array();
        ids.into_iter()
            .enumerate()
            .map(|(index, id)| {
                (
                    id,
                    names
                        .and_then(|rows| rows.get(index))
                        .map(value_text)
                        .unwrap_or_default(),
                )
            })
            .collect()
    }
    fn contract(&self) -> bool {
        self.aliases.iter().all(|rows| !rows.is_empty())
            || [0, 1].into_iter().all(|side| {
                metadata_ids(
                    &self.metadata,
                    if side == 0 {
                        "team_a_ids"
                    } else {
                        "team_b_ids"
                    },
                )
                .is_some_and(|ids| !ids.is_empty())
            })
    }
    fn roster(
        &self,
        side: usize,
        people: &People,
        teams: &HashMap<i64, Roster>,
        scored: Option<&HashSet<i64>>,
    ) -> Roster {
        let mut roster = if !self.aliases[side].is_empty() {
            self.aliases[side].clone()
        } else {
            self.metadata_roster(side)
        };
        if roster.is_empty() {
            roster = teams.get(&self.ts[side]).cloned().unwrap_or_default();
        }
        let mut result = Roster::new();
        for (id, name) in roster {
            let id = if id > 0 { people.id(id) } else { id };
            if let Some((_, previous)) = result.iter_mut().find(|(other, _)| *other == id) {
                if previous.is_empty() && !name.is_empty() {
                    *previous = name;
                }
            } else {
                result.push((id, name));
            }
        }
        if result.len() >= 2
            && let Some(scored) = scored
        {
            let matching = result
                .iter()
                .filter(|(id, _)| *id > 0 && scored.contains(id))
                .collect::<Vec<_>>();
            if let [person] = matching.as_slice() {
                return vec![(*person).clone()];
            }
        }
        result
    }
}

type ForumMatch = (String, String, [Vec<i64>; 2], usize);

fn forum_key(debate: &Debate, label: &str) -> ForumMatch {
    let mut names = debate.teams.each_ref().map(|team| {
        let mut people = team.iter().map(|(id, _)| *id).collect::<Vec<_>>();
        people.sort_unstable();
        people
    });
    names.sort();
    (
        debate.tournament_key.clone(),
        label.to_owned(),
        names,
        debate.winner,
    )
}

pub(super) struct Ingested {
    pub(super) tournaments: usize,
    pub(super) debates: Vec<Debate>,
}

pub(super) async fn ingest(
    conn: &mut AnyConnection,
    people: &People,
    allowed: &BTreeSet<String>,
) -> Result<Ingested, ApiError> {
    let rows=sqlx::query("SELECT CAST(r.id AS BIGINT) AS id,CAST(t.id AS BIGINT) AS tournament_id, t.name, t.short_name, t.manual_name,
        CAST(t.date AS TEXT) AS date,t.season,CAST(t.qual_type AS BIGINT) AS type,
        CAST(r.round_number AS BIGINT) AS number,r.stage,r.round_label,CAST(r.victor AS BIGINT) AS victor,
        r.import_origin,CAST(r.metadata AS TEXT) AS metadata,
        CAST(r.gov_id AS BIGINT) AS gov_id,CAST(r.opp_id AS BIGINT) AS opp_id,
        CAST(g1.debater_id AS BIGINT) AS gov_1,g1.source_name AS gov_name_1,
        CAST(g2.debater_id AS BIGINT) AS gov_2,g2.source_name AS gov_name_2,
        CAST(o1.debater_id AS BIGINT) AS opp_1,o1.source_name AS opp_name_1,
        CAST(o2.debater_id AS BIGINT) AS opp_2,o2.source_name AS opp_name_2,
        (SELECT src.import_type FROM core_importedroundmetadata_sources ms JOIN core_tournamentimport src ON src.id=ms.tournamentimport_id
            WHERE ms.importedroundmetadata_id=m.id ORDER BY src.id LIMIT 1) AS source
        FROM core_round r JOIN core_tournament t ON t.id=r.tournament_id
        LEFT JOIN core_importedroundmetadata m ON m.round_id=r.id
        LEFT JOIN core_debateralias g1 ON g1.id=m.gov_1_alias_id LEFT JOIN core_debateralias g2 ON g2.id=m.gov_2_alias_id
        LEFT JOIN core_debateralias o1 ON o1.id=m.opp_1_alias_id LEFT JOIN core_debateralias o2 ON o2.id=m.opp_2_alias_id
        WHERE t.qual_type NOT IN (7,9,13) ORDER BY t.date,t.id,r.round_number,r.id")
        .fetch_all(&mut *conn).await?;
    let mut raw = Vec::new();
    let mut team_ids = BTreeSet::new();
    let mut manual_rounds = BTreeSet::new();
    let mut tournament_ids = BTreeSet::new();
    for row in rows {
        let season = row.try_get::<&str, _>("season")?.trim().to_owned();
        if !season_allowed(allowed, &season) {
            continue;
        }
        let mut ignored = false;
        for column in ["name", "short_name", "manual_name"] {
            let name = row.try_get::<&str, _>(column)?;
            if PRO_AM.is_match(name) || NOVICE.is_match(name) {
                ignored = true;
                break;
            }
        }
        if ignored {
            continue;
        }
        let id = row.try_get("id")?;
        let date = NaiveDate::parse_from_str(row.try_get("date")?, "%Y-%m-%d")
            .map_err(|_| ApiError::InvalidStoredDate)?;
        let tournament = row.try_get("tournament_id")?;
        tournament_ids.insert(tournament);
        let mut aliases = [Roster::new(), Roster::new()];
        for (side, fields) in [
            [("gov_1", "gov_name_1"), ("gov_2", "gov_name_2")],
            [("opp_1", "opp_name_1"), ("opp_2", "opp_name_2")],
        ]
        .into_iter()
        .enumerate()
        {
            for (id, name) in fields {
                if let Some(id) = row.try_get::<Option<i64>, _>(id)? {
                    aliases[side].push((
                        id,
                        row.try_get::<Option<&str>, _>(name)?
                            .unwrap_or_default()
                            .trim()
                            .to_owned(),
                    ));
                }
            }
        }
        let origin = match row.try_get::<Option<String>, _>("source")? {
            Some(source) if source.trim().is_empty() => "round".to_owned(),
            Some(source) => source,
            None => row.try_get("import_origin")?,
        };
        let ts = [row.try_get("gov_id")?, row.try_get("opp_id")?];
        team_ids.extend(ts);
        let candidate = RawDebate {
            id,
            tournament,
            name: row.try_get("name")?,
            date,
            season,
            round: row.try_get("number")?,
            stage: row.try_get("stage")?,
            label: row.try_get("round_label")?,
            victor: row.try_get("victor")?,
            origin,
            metadata: serde_json::from_str(row.try_get("metadata")?)?,
            ts,
            aliases,
        };
        if !candidate.contract() {
            manual_rounds.insert(id);
        }
        raw.push(candidate);
    }
    let teams = load_teams(conn, &team_ids).await?;
    let scored = scored_people(conn, &manual_rounds, people).await?;
    let mut debates = Vec::new();
    let mut seen = HashSet::new();
    for row in raw {
        if matches!(row.victor, 3..=7) {
            continue;
        }
        let Some(winner) = metadata_winner(&row.metadata, row.victor) else {
            continue;
        };
        let participants = [
            row.roster(0, people, &teams, scored.get(&row.id)),
            row.roster(1, people, &teams, scored.get(&row.id)),
        ];
        if participants.iter().any(Vec::is_empty) {
            continue;
        }
        let metadata = &row.metadata;
        let elimination = metadata_elimination(metadata, &row.stage);
        let order = metadata_ids(metadata, "sort_key")
            .and_then(|values| values.try_into().ok())
            .unwrap_or([i64::from(elimination), row.round, row.id]);
        let name = value_text(&metadata["tournament_key"]);
        let tournament_key = if name.is_empty() {
            format!("{}:{}:{}", row.tournament, row.season, row.name)
        } else {
            name
        };
        let meta_label = value_text(&metadata["round_label"]);
        let label = if !meta_label.is_empty() {
            meta_label
        } else if !row.label.trim().is_empty() {
            row.label.trim().to_owned()
        } else {
            format!("{}{}", if elimination { "E" } else { "P" }, row.round)
        };
        let pro_am = crate::tournament_import::truthy(&metadata["is_proam_partnership"])
            || participants
                .iter()
                .any(|team| people.pro_am(team, &row.season));
        let debate = Debate {
            timestamp: timestamp(&value_text(&metadata["timestamp"]), row.date),
            season: row.season,
            order,
            tournament_key,
            elimination,
            winner,
            teams: participants,
            pro_am,
        };
        if row.origin.trim() == "forum" && !seen.insert(forum_key(&debate, &label)) {
            continue;
        }
        debates.push(debate);
    }
    debates.sort_by(|left, right| {
        (left.timestamp, left.order, &left.tournament_key).cmp(&(
            right.timestamp,
            right.order,
            &right.tournament_key,
        ))
    });
    Ok(Ingested {
        tournaments: tournament_ids.len(),
        debates,
    })
}

pub(super) async fn fingerprint(
    conn: &mut AnyConnection,
    people: &People,
) -> Result<String, ApiError> {
    use ring::digest::{Context, SHA256};
    let mut state = Context::new(&SHA256);
    for table in [
        "core_round",
        "core_roundstats",
        "core_qual",
        "core_agentchangeset",
    ] {
        let sql = format!(
            "SELECT COUNT(*) AS count,CAST(COALESCE(MAX(id),0) AS BIGINT) AS last FROM {table}"
        );
        let row = sqlx::query(AssertSqlSafe(sql))
            .fetch_one(&mut *conn)
            .await?;
        state.update(&row.try_get::<i64, _>("count")?.to_be_bytes());
        state.update(&row.try_get::<i64, _>("last")?.to_be_bytes());
    }
    let identities = people.profiles.iter().collect::<BTreeMap<_, _>>();
    for (&id, person) in identities {
        state.update(&id.to_be_bytes());
        state.update(&person.alias.unwrap_or_default().to_be_bytes());
        state.update(person.manual.as_bytes());
        state.update(&[0]);
    }
    Ok(hex::encode(state.finish()))
}
