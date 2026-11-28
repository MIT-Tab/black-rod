use std::collections::{BTreeMap, HashMap, HashSet};

use rust_decimal::{Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};
use sqlx::{AnyConnection, Row};

use super::{
    ingestion::{People, season_year},
    rating::Computed,
};
use crate::ApiError;

#[derive(Debug, Clone, Copy, Serialize)]
pub(super) struct Settings {
    pub(super) active: (i32, i32),
    pub(super) min_rounds: usize,
    pub(super) min_out: usize,
    pub(super) exclude_dino: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(super) struct School {
    id: i64,
    name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(super) struct Ranking {
    rank: usize,
    name: String,
    schools: Vec<School>,
    debater_id: Option<i64>,
    elo: f64,
    elo_display: String,
    rounds: usize,
    prelim_rounds: usize,
    outround_rounds: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(super) struct Dashboard {
    matched_tournaments: usize,
    debates_processed: usize,
    prelims_processed: usize,
    outrounds_processed: usize,
    excluded_proam_debates: usize,
    qual_data_available: bool,
    excluded_default_opt_out_debaters: usize,
    pub(super) ranking_rows: Vec<Ranking>,
}

fn include_season(bounds: &mut Option<(i32, i32)>, season: i32) {
    *bounds = Some(match *bounds {
        Some((first, last)) => (first.min(season), last.max(season)),
        None => (season, season),
    });
}

pub(super) fn display_elo(elo: f64) -> String {
    let source = elo.to_string();
    source
        .parse::<Decimal>()
        .or_else(|_| Decimal::from_scientific(&source))
        .map_or_else(
            |_| format!("{elo:.2}"),
            |rating| {
                format!(
                    "{:.2}",
                    rating.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero)
                )
            },
        )
}

pub(super) async fn dashboard(
    conn: &mut AnyConnection,
    people: &People,
    computed: &Computed,
    settings: Settings,
) -> Result<Dashboard, ApiError> {
    let mut result = Dashboard {
        matched_tournaments: computed.tournaments,
        debates_processed: computed.prelims + computed.outrounds,
        prelims_processed: computed.prelims,
        outrounds_processed: computed.outrounds,
        excluded_proam_debates: computed.excluded,
        qual_data_available: false,
        excluded_default_opt_out_debaters: 0,
        ranking_rows: Vec::new(),
    };
    if computed.players.is_empty() {
        return Ok(result);
    }

    let qual_ids: HashSet<i64> =
        sqlx::query_scalar("SELECT DISTINCT CAST(debater_id AS BIGINT) FROM core_qual")
            .fetch_all(&mut *conn)
            .await?
            .into_iter()
            .collect();
    result.qual_data_available = !qual_ids.is_empty();
    let mut appearances: HashMap<i64, HashMap<String, HashSet<i64>>> = HashMap::new();
    for row in sqlx::query(
        "SELECT DISTINCT CAST(s.debater_id AS BIGINT) AS debater_id,
         CAST(t.id AS BIGINT) AS tournament_id, t.season
         FROM core_roundstats s JOIN core_round r ON r.id = s.round_id
         JOIN core_tournament t ON t.id = r.tournament_id",
    )
    .fetch_all(conn)
    .await?
    {
        let season: &str = row.try_get("season")?;
        appearances
            .entry(row.try_get("debater_id")?)
            .or_default()
            .entry(season.trim().to_owned())
            .or_default()
            .insert(row.try_get("tournament_id")?);
    }

    let mut used_names = HashSet::new();
    for (id, stats) in &computed.players {
        let linked = people
            .linked
            .get(id)
            .map(Vec::as_slice)
            .unwrap_or_else(|| std::slice::from_ref(id));
        let mut bounds = None;
        let mut latest_affiliated = None;
        let mut has_active_season = false;
        let mut opted_in = false;
        let mut opted_out = false;
        let mut schools = BTreeMap::new();
        for profile in linked
            .iter()
            .filter_map(|member| people.profiles.get(member))
        {
            for season in [profile.first, profile.latest].into_iter().flatten() {
                include_season(&mut bounds, season);
            }
            opted_in |= profile.manual == "opt_in";
            opted_out |= profile.manual == "opt_out";
            if profile.normalized_school == "unaffiliated" {
                continue;
            }
            if let Some(school_id) = profile.school_id.filter(|_| !profile.school.is_empty()) {
                schools.insert(school_id, &profile.school);
            }
            if profile.normalized_school.is_empty() {
                continue;
            }
            if let Some(latest) = profile.latest.or(profile.first) {
                let first = profile.first.unwrap_or(latest);
                latest_affiliated = latest_affiliated.max(Some(latest));
                has_active_season |= first.min(latest) <= settings.active.1
                    && first.max(latest) >= settings.active.0;
            }
        }
        if !has_active_season {
            continue;
        }
        let all_appearances = linked.iter().filter_map(|member| appearances.get(member));
        for season in all_appearances.clone().flat_map(|events| events.keys()) {
            if let Some(year) = season_year(season) {
                include_season(&mut bounds, year);
            }
        }
        let affiliated_snapshot = latest_affiliated
            .filter(|season| bounds.is_some_and(|(_, last)| *season != last))
            .and_then(|season| stats.seasons.get(&season.to_string()));
        let display = if settings.exclude_dino {
            affiliated_snapshot.unwrap_or(&stats.current)
        } else {
            &stats.current
        };
        if display.rounds < settings.min_rounds || display.out < settings.min_out {
            continue;
        }
        let default_opt_out = !linked.iter().any(|member| qual_ids.contains(member))
            || bounds.is_some_and(|(first, last)| {
                last <= first
                    || (last - first == 1 && {
                        let first_year = first.to_string();
                        let distinct: HashSet<_> = all_appearances
                            .filter_map(|events| events.get(&first_year))
                            .flatten()
                            .collect();
                        distinct.len() < 3
                    })
            });
        if opted_out || (result.qual_data_available && default_opt_out && !opted_in) {
            result.excluded_default_opt_out_debaters += 1;
            continue;
        }
        let canonical = people.profiles.get(id);
        let name = stats
            .names
            .iter()
            .min_by(|(left_name, left_count), (right_name, right_count)| {
                right_count
                    .cmp(left_count)
                    .then_with(|| left_name.to_lowercase().cmp(&right_name.to_lowercase()))
            })
            .map(|(name, _)| name.trim())
            .filter(|name| !name.is_empty())
            .or_else(|| {
                canonical
                    .map(|profile| profile.display.as_str())
                    .filter(|name| !name.is_empty())
            })
            .map(str::to_owned)
            .unwrap_or_else(|| format!("Debater {id}"));
        let name = if used_names.contains(&name) {
            format!("{name} ({id})")
        } else {
            name
        };
        used_names.insert(name.clone());

        let mut schools: Vec<_> = schools
            .into_iter()
            .map(|(id, name)| School {
                id,
                name: name.clone(),
            })
            .collect();
        schools.sort_by_cached_key(|school| school.name.to_lowercase());
        result.ranking_rows.push(Ranking {
            rank: 0,
            name,
            schools,
            debater_id: linked.iter().copied().find(|member| *member > 0),
            elo: display.elo,
            elo_display: display_elo(display.elo),
            rounds: display.rounds,
            prelim_rounds: display.rounds - display.out,
            outround_rounds: display.out,
        });
    }
    result.ranking_rows.sort_by(|left, right| {
        right
            .elo
            .total_cmp(&left.elo)
            .then_with(|| right.rounds.cmp(&left.rounds))
            .then_with(|| left.name.to_lowercase().cmp(&right.name.to_lowercase()))
    });
    for (index, row) in result.ranking_rows.iter_mut().enumerate() {
        row.rank = index + 1;
    }
    Ok(result)
}
