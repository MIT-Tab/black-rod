use std::collections::{BTreeMap, BTreeSet, HashMap};

use rust_decimal::Decimal;
use serde_json::Value;
use sqlx::{AnyConnection, Row};

use crate::{
    ApiError,
    round_contract::{metadata_elimination, metadata_ids, metadata_winner},
};

pub(super) type Partnership = [i64; 2];

#[derive(Default)]
pub(super) struct Identities {
    canonical: HashMap<i64, i64>,
    first_seasons: BTreeMap<String, BTreeSet<i64>>,
}

impl Identities {
    pub(super) async fn load(conn: &mut AnyConnection) -> Result<Self, ApiError> {
        let rows=sqlx::query("SELECT CAST(id AS BIGINT) AS id, CAST(alias_group_id AS BIGINT) AS alias_id, first_season
            FROM core_debater ORDER BY id").fetch_all(conn).await?;
        let mut lookup = Self::default();
        let mut aliases = HashMap::new();
        let mut seasons = HashMap::<i64, String>::new();
        for row in rows {
            let id = row.try_get("id")?;
            let group = row.try_get::<Option<i64>, _>("alias_id")?;
            let canonical = group.map_or(id, |group| *aliases.entry(group).or_insert(id));
            lookup.canonical.insert(id, canonical);
            if let Some(season) = row
                .try_get::<Option<String>, _>("first_season")?
                .filter(|season| !season.is_empty())
            {
                let first = seasons.entry(canonical).or_insert_with(|| season.clone());
                if season < *first {
                    *first = season;
                }
            }
        }
        for (speaker, season) in seasons {
            lookup
                .first_seasons
                .entry(season)
                .or_default()
                .insert(speaker);
        }
        Ok(lookup)
    }

    fn id(&self, id: i64) -> i64 {
        self.canonical.get(&id).copied().unwrap_or(id)
    }

    pub(super) fn novices(&self, season: &str) -> BTreeSet<i64> {
        self.first_seasons.get(season).cloned().unwrap_or_default()
    }
}

pub(super) struct Debate {
    pub(super) number: i64,
    pub(super) elimination: bool,
    pub(super) novice: bool,
    pub(super) partnerships: [Option<Partnership>; 2],
    pub(super) victor: i64,
    pub(super) winner: Option<usize>,
    pub(super) scores: HashMap<i64, Ballots>,
}

#[derive(Default)]
pub(super) struct Ballots {
    speaks: Decimal,
    ranks: Decimal,
    speak_count: i64,
    rank_count: i64,
}

impl Ballots {
    pub(super) fn averages(&self) -> (Option<Decimal>, Option<Decimal>) {
        (
            (self.speak_count != 0).then(|| self.speaks / Decimal::from(self.speak_count)),
            (self.rank_count != 0).then(|| self.ranks / Decimal::from(self.rank_count)),
        )
    }

    fn push(&mut self, speaks: Option<&str>, ranks: Option<&str>) -> Result<(), ApiError> {
        if let Some(speaks) = speaks {
            self.speaks += speaks
                .parse::<Decimal>()
                .map_err(|_| ApiError::Serialization)?;
            self.speak_count += 1;
        }
        if let Some(ranks) = ranks {
            self.ranks += ranks
                .parse::<Decimal>()
                .map_err(|_| ApiError::Serialization)?;
            self.rank_count += 1;
        }
        Ok(())
    }
}

pub(super) struct Award {
    pub(super) speaker: i64,
    pub(super) novice: bool,
    pub(super) place: i64,
}

pub(super) struct Tournament {
    pub(super) id: i64,
    pub(super) rounds: i64,
    pub(super) novice: bool,
    pub(super) debates: BTreeMap<i64, Debate>,
    pub(super) breaks: [BTreeSet<Partnership>; 2],
    pub(super) awards: Vec<Award>,
}

fn partnership(left: i64, right: i64) -> Option<Partnership> {
    (left != right).then(|| [left.min(right), left.max(right)])
}

fn team(people: &[i64]) -> Option<Partnership> {
    match people {
        [a, b] => partnership(*a, *b),
        _ => None,
    }
}

pub(super) async fn seasons(conn: &mut AnyConnection) -> Result<Vec<String>, ApiError> {
    Ok(sqlx::query_scalar::<_, String>(
        "SELECT DISTINCT season FROM core_tournament ORDER BY season DESC",
    )
    .fetch_all(conn)
    .await?)
}

pub(super) async fn tournaments(
    conn: &mut AnyConnection,
    season: &str,
    identities: &Identities,
) -> Result<BTreeMap<i64, Tournament>, ApiError> {
    let mut tournaments = BTreeMap::new();
    for row in sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id, CAST(num_rounds AS BIGINT) AS rounds,
        CAST(qual_type AS BIGINT) AS qual_type FROM core_tournament WHERE season=$1",
    )
    .bind(season)
    .fetch_all(&mut *conn)
    .await?
    {
        let id = row.try_get("id")?;
        tournaments.insert(
            id,
            Tournament {
                id,
                rounds: row.try_get("rounds")?,
                novice: row.try_get::<i64, _>("qual_type")? == 9,
                debates: BTreeMap::new(),
                breaks: Default::default(),
                awards: Vec::new(),
            },
        );
    }
    let rows=sqlx::query("WITH teams AS (
        SELECT r.gov_id AS id FROM core_round r JOIN core_tournament t ON t.id=r.tournament_id WHERE t.season=$1
        UNION SELECT r.opp_id FROM core_round r JOIN core_tournament t ON t.id=r.tournament_id WHERE t.season=$1
        UNION SELECT r.team_id FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
            WHERE t.season=$1 AND r.place>0
        ) SELECT CAST(m.team_id AS BIGINT) AS team_id,CAST(m.debater_id AS BIGINT) AS debater_id
        FROM core_team_debaters m JOIN teams ON teams.id=m.team_id").bind(season).fetch_all(&mut *conn).await?;
    let mut members: HashMap<i64, Vec<i64>> = HashMap::new();
    for row in rows {
        members
            .entry(row.try_get("team_id")?)
            .or_default()
            .push(identities.id(row.try_get("debater_id")?));
    }
    for people in members.values_mut() {
        people.sort_unstable();
        people.dedup();
    }

    let rows=sqlx::query("SELECT CAST(r.id AS BIGINT) AS id,CAST(t.id AS BIGINT) AS tournament_id,
        CAST(r.round_number AS BIGINT) AS number,r.stage,r.division,CAST(r.victor AS BIGINT) AS victor,
        CAST(r.metadata AS TEXT) AS metadata,CAST(r.gov_id AS BIGINT) AS gov_id,CAST(r.opp_id AS BIGINT) AS opp_id,
        CAST(g1.debater_id AS BIGINT) AS gov_one, CAST(g2.debater_id AS BIGINT) AS gov_two,
        CAST(o1.debater_id AS BIGINT) AS opp_one, CAST(o2.debater_id AS BIGINT) AS opp_two
        FROM core_round r JOIN core_tournament t ON t.id=r.tournament_id
        LEFT JOIN core_importedroundmetadata m ON m.round_id=r.id
        LEFT JOIN core_debateralias g1 ON g1.id=m.gov_1_alias_id
        LEFT JOIN core_debateralias g2 ON g2.id=m.gov_2_alias_id
        LEFT JOIN core_debateralias o1 ON o1.id=m.opp_1_alias_id
        LEFT JOIN core_debateralias o2 ON o2.id=m.opp_2_alias_id
        WHERE t.season=$1").bind(season).fetch_all(&mut *conn).await?;
    for row in rows {
        let metadata: Value = serde_json::from_str(row.try_get("metadata")?)?;
        let side = |a, b, field, key| -> Result<Option<Partnership>, sqlx::Error> {
            if let Some((a, b)) = row
                .try_get::<Option<i64>, _>(a)?
                .zip(row.try_get::<Option<i64>, _>(b)?)
            {
                return Ok(partnership(identities.id(a), identities.id(b)));
            }
            if let Some(ids) = metadata_ids(&metadata, field)
                && let [a, b] = ids.as_slice()
            {
                return Ok(partnership(identities.id(*a), identities.id(*b)));
            }
            Ok(members
                .get(&row.try_get::<i64, _>(key)?)
                .and_then(|people| team(people)))
        };
        let victor = row.try_get("victor")?;
        let tournament = row.try_get("tournament_id")?;
        tournaments
            .get_mut(&tournament)
            .ok_or(ApiError::Serialization)?
            .debates
            .insert(
                row.try_get("id")?,
                Debate {
                    number: row.try_get("number")?,
                    elimination: metadata_elimination(&metadata, row.try_get("stage")?),
                    novice: row.try_get::<Option<&str>, _>("division")? == Some("novice"),
                    partnerships: [
                        side("gov_one", "gov_two", "team_a_ids", "gov_id")?,
                        side("opp_one", "opp_two", "team_b_ids", "opp_id")?,
                    ],
                    victor,
                    winner: metadata_winner(&metadata, victor),
                    scores: HashMap::new(),
                },
            );
    }
    let rows=sqlx::query("SELECT CAST(t.id AS BIGINT) AS tournament_id,CAST(r.id AS BIGINT) AS round_id,
        CAST(s.debater_id AS BIGINT) AS debater_id,CAST(s.speaks AS TEXT) AS speaks,CAST(s.ranks AS TEXT) AS ranks
        FROM core_roundstats s JOIN core_round r ON r.id=s.round_id JOIN core_tournament t ON t.id=r.tournament_id
        WHERE t.season=$1 AND r.stage='prelim'").bind(season).fetch_all(&mut *conn).await?;
    for row in rows {
        let tournament = row.try_get("tournament_id")?;
        let round = row.try_get("round_id")?;
        tournaments
            .get_mut(&tournament)
            .and_then(|tournament| tournament.debates.get_mut(&round))
            .ok_or(ApiError::Serialization)?
            .scores
            .entry(identities.id(row.try_get("debater_id")?))
            .or_default()
            .push(row.try_get("speaks")?, row.try_get("ranks")?)?;
    }
    let rows=sqlx::query("SELECT CAST(r.tournament_id AS BIGINT) AS tournament_id,CAST(r.team_id AS BIGINT) AS team_id,
        CAST(r.type_of_place AS BIGINT) AS division FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
        WHERE t.season=$1 AND r.place>0").bind(season).fetch_all(&mut *conn).await?;
    for row in rows {
        let tournament = row.try_get("tournament_id")?;
        if let Some(pair) = members
            .get(&row.try_get("team_id")?)
            .and_then(|people| team(people))
        {
            let novice = row.try_get::<i64, _>("division")? == 0;
            tournaments
                .get_mut(&tournament)
                .ok_or(ApiError::Serialization)?
                .breaks[usize::from(novice)]
            .insert(pair);
        }
    }
    let rows=sqlx::query("SELECT CAST(r.tournament_id AS BIGINT) AS tournament_id,CAST(r.debater_id AS BIGINT) AS debater_id,
        CAST(r.type_of_place AS BIGINT) AS division,CAST(r.place AS BIGINT) AS place,
        CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS tied
        FROM core_speakerresult r JOIN core_tournament t ON t.id=r.tournament_id WHERE t.season=$1 AND r.place>0")
        .bind(season).fetch_all(conn).await?;
    for row in rows {
        let tournament = row.try_get("tournament_id")?;
        tournaments
            .get_mut(&tournament)
            .ok_or(ApiError::Serialization)?
            .awards
            .push(Award {
                speaker: identities.id(row.try_get("debater_id")?),
                novice: row.try_get::<i64, _>("division")? == 0,
                place: (row.try_get::<i64, _>("place")? - row.try_get::<i64, _>("tied")?).max(1),
            });
    }
    Ok(tournaments)
}
