use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde_json::{Value, json};

use super::data::{Partnership, Tournament};

pub(super) const METRICS: [&str; 9] = [
    "Breaking teams combined speaks",
    "Novice breaking teams combined speaks",
    "Top breaking seed combined speaks",
    "Bottom breaking seed combined speaks",
    "Top full-field seed combined speaks",
    "Bottom full-field seed combined speaks",
    "1st varsity speaker speaks",
    "10th varsity speaker speaks",
    "1st novice speaker speaks",
];

type Card = Vec<(i64, Option<Decimal>)>;

fn complete_numbers(card: &Card, rounds: i64) -> bool {
    rounds > 0
        && card.len() as i64 == rounds
        && card
            .iter()
            .map(|&(number, _)| number)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .eq(1..=rounds)
}

fn complete_total(card: &Card, rounds: i64) -> Option<Decimal> {
    complete_numbers(card, rounds)
        .then(|| {
            card.iter()
                .map(|&(_, score)| score.filter(|value| *value > Decimal::ZERO))
                .sum()
        })
        .flatten()
}

fn totals<K: std::hash::Hash + Eq + Copy>(
    cards: &HashMap<K, Card>,
    rounds: i64,
) -> HashMap<K, Decimal> {
    cards
        .iter()
        .filter_map(|(&id, card)| complete_total(card, rounds).map(|total| (id, total)))
        .collect()
}

#[derive(Default)]
pub(super) struct Samples {
    values: [Vec<Decimal>; 9],
    coverage: [usize; 9],
    count: usize,
    scores: Vec<Decimal>,
    by_record: BTreeMap<Option<(i64, i64)>, Vec<Decimal>>,
    round_counts: BTreeSet<i64>,
    tournaments: BTreeSet<i64>,
}

struct Observation {
    number: i64,
    speaker: i64,
    score: Decimal,
    partner: Option<Partnership>,
}

impl Samples {
    pub(super) fn push(&mut self, tournament: Tournament, mut novices: BTreeSet<i64>) {
        let pre = self.values.each_ref().map(Vec::len);
        let mut breaks = tournament.breaks;
        novices.extend(
            tournament
                .awards
                .iter()
                .filter(|award| award.novice)
                .map(|award| award.speaker),
        );
        let mut teams = HashMap::<Partnership, Card>::new();
        let mut ranks = HashMap::<Partnership, Card>::new();
        let mut speakers = HashMap::<i64, Card>::new();
        let mut speaker_ranks = HashMap::<i64, Card>::new();
        let mut wins = HashMap::<Partnership, i64>::new();
        let mut unknown = HashSet::new();
        let mut observations = Vec::new();
        for debate in tournament.debates.into_values() {
            let number = debate.number;
            if debate.elimination || number > tournament.rounds {
                let novice = usize::from(debate.novice || tournament.novice);
                breaks[novice].extend(debate.partnerships.into_iter().flatten());
                continue;
            }
            if number < 1 {
                continue;
            }
            let scores = debate
                .scores
                .into_iter()
                .map(|(id, ballots)| (id, ballots.averages()))
                .collect::<HashMap<_, _>>();
            for (&speaker, &(speaks, rank)) in &scores {
                speakers.entry(speaker).or_default().push((number, speaks));
                speaker_ranks
                    .entry(speaker)
                    .or_default()
                    .push((number, rank));
                if let Some(score) = speaks.filter(|score| *score > Decimal::ZERO) {
                    let mut partners = debate
                        .partnerships
                        .into_iter()
                        .flatten()
                        .filter(|pair| pair.contains(&speaker));
                    let first = partners.next();
                    let second = partners.next();
                    let partner = if second.is_none() || first == second {
                        first
                    } else {
                        None
                    };
                    observations.push(Observation {
                        number,
                        speaker,
                        score,
                        partner,
                    });
                }
            }
            for (side, pair) in debate.partnerships.into_iter().enumerate() {
                let Some(pair) = pair else { continue };
                let values = pair.map(|speaker| scores.get(&speaker).copied().unwrap_or_default());
                let speaks = values
                    .into_iter()
                    .map(|(speak, _)| speak.filter(|value| *value > Decimal::ZERO))
                    .sum::<Option<Decimal>>();
                let rank = speaks.and_then(|_| {
                    values
                        .into_iter()
                        .map(|(_, rank)| rank)
                        .sum::<Option<Decimal>>()
                });
                teams.entry(pair).or_default().push((number, speaks));
                ranks.entry(pair).or_default().push((number, rank));
                *wins.entry(pair).or_default() += i64::from(
                    debate
                        .winner
                        .map_or(matches!(debate.victor, 6 | 7), |won| won == side),
                );
                if debate.winner.is_none() && !matches!(debate.victor, 5..=7) {
                    unknown.insert(pair);
                }
            }
        }
        let speakers_totals = totals(&speakers, tournament.rounds);
        let candidates = teams
            .keys()
            .copied()
            .chain(breaks.iter().flatten().copied())
            .collect::<HashSet<_>>();
        let mut totals_by_team = HashMap::new();
        for pair in candidates {
            if let Some(total) = pair
                .iter()
                .map(|id| speakers_totals.get(id).copied())
                .sum::<Option<Decimal>>()
            {
                totals_by_team.insert(pair, total);
            }
            if teams
                .get(&pair)
                .is_none_or(|card| !complete_numbers(card, tournament.rounds))
            {
                unknown.insert(pair);
            }
        }
        let team_ranks = totals(&ranks, tournament.rounds);
        let mut duplicate = HashSet::new();
        let mut seen = HashSet::new();
        for (&id, card) in &speakers {
            for &(round, _) in card {
                if !seen.insert((id, round)) {
                    duplicate.insert((id, round));
                }
            }
        }
        self.count += usize::from(!observations.is_empty());
        for observation in observations {
            if duplicate.contains(&(observation.speaker, observation.number)) {
                continue;
            }
            let record = observation
                .partner
                .filter(|team| !unknown.contains(team))
                .map(|team| (wins[&team], tournament.rounds - wins[&team]));
            self.scores.push(observation.score);
            self.by_record
                .entry(record)
                .or_default()
                .push(observation.score);
            self.round_counts.insert(tournament.rounds);
            self.tournaments.insert(tournament.id);
        }

        let [varsity_break, novice_break] = &mut breaks;
        novice_break.extend(
            varsity_break
                .iter()
                .copied()
                .filter(|pair| pair.iter().all(|speaker| novices.contains(speaker))),
        );
        for (index, group) in breaks.iter().enumerate() {
            self.values[index].extend(
                group
                    .iter()
                    .filter_map(|team| totals_by_team.get(team).copied()),
            );
        }
        for (start, group) in [
            (2, breaks[0].iter().copied().collect::<Vec<_>>()),
            (4, teams.keys().copied().collect()),
        ] {
            let available = group
                .into_iter()
                .filter(|team| !unknown.contains(team) && totals_by_team.contains_key(team))
                .collect::<Vec<_>>();
            let rank = available.iter().all(|team| team_ranks.contains_key(team));
            let keys = available
                .iter()
                .map(|team| {
                    (
                        wins[team],
                        totals_by_team[team],
                        if rank {
                            -team_ranks[team]
                        } else {
                            Decimal::ZERO
                        },
                    )
                })
                .collect::<Vec<_>>();
            for (index, target) in keys
                .iter()
                .max()
                .into_iter()
                .chain(keys.iter().min())
                .enumerate()
            {
                self.values[start + index]
                    .extend(keys.iter().filter(|key| *key == target).map(|key| key.1));
            }
        }
        let ranks = totals(&speaker_ranks, tournament.rounds);
        for (index, novice, place) in [(6, false, 1), (7, false, 10), (8, true, 1)] {
            let placed = tournament
                .awards
                .iter()
                .filter(|award| award.novice == novice && award.place == place)
                .map(|award| award.speaker)
                .collect::<HashSet<_>>();
            if !placed.is_empty() {
                self.values[index].extend(
                    placed
                        .iter()
                        .filter_map(|id| speakers_totals.get(id).copied()),
                );
                continue;
            }
            let candidates = speakers_totals
                .iter()
                .filter(|(id, _)| !novice || novices.contains(id))
                .collect::<Vec<_>>();
            let use_ranks = candidates.iter().all(|(id, _)| ranks.contains_key(id));
            let mut values = candidates
                .into_iter()
                .map(|(id, total)| (*total, if use_ranks { -ranks[id] } else { Decimal::ZERO }))
                .collect::<Vec<_>>();
            values.sort_unstable_by(|a, b| b.cmp(a));
            if let Some(target) = values.get((place - 1) as usize) {
                self.values[index].extend(
                    values
                        .iter()
                        .filter(|value| value == &target)
                        .map(|value| value.0),
                );
            }
        }
        for (index, size) in pre.into_iter().enumerate() {
            self.coverage[index] += usize::from(self.values[index].len() > size);
        }
    }

    pub(super) fn finish(self, season: &str, index: usize) -> Option<Value> {
        if self.scores.is_empty() && self.values.iter().all(Vec::is_empty) {
            return None;
        }
        let mut columns = Vec::new();
        for rounds in self.round_counts {
            for wins in 0..=rounds {
                let loss = rounds - wins;
                let values = self
                    .by_record
                    .get(&Some((wins, loss)))
                    .map_or(&[][..], Vec::as_slice);
                columns.push(summary(&format!("{wins}-{loss}"), values));
            }
        }
        if let Some(values) = self.by_record.get(&None) {
            columns.push(summary("Unknown record", values));
        }
        let overall = summary("All records", &self.scores);
        columns.push(overall.clone());
        let correspondence = correspondence(&self.scores);
        let chart = chart(&correspondence);
        let rows=[("p25","25th percentile"),("p50","50th percentile"),("p75","75th percentile"),("mean","Mean")]
            .into_iter().map(|(key,label)|json!({"label":label,"values":columns.iter().map(|row|&row[key]).collect::<Vec<_>>()}))
            .collect::<Vec<_>>();
        let metrics = self
            .values
            .into_iter()
            .zip(self.coverage)
            .zip(METRICS)
            .map(|((values, tournament_count), label)| {
                let mut result = summary(label, &values);
                result["tournament_count"] = json!(tournament_count);
                result
            })
            .collect::<Vec<_>>();
        let display = if season.chars().all(|c| c.is_ascii_digit()) {
            season
                .parse::<i32>()
                .ok()
                .map(crate::schedule::season_display)
                .unwrap_or_else(|| season.to_owned())
        } else {
            season.to_owned()
        };
        Some(
            json!({"season":season,"season_display":display,"scored_tournaments":self.count,
            "chart_id":format!("speaks-cdf-{index}"),"metrics":metrics,"round_speaks":{
                "sample_size":self.scores.len(),"tournament_count":self.tournaments.len(),"columns":columns,
                "rows":rows,"overall":overall,"correspondence":correspondence,"chart":chart,
            }}),
        )
    }
}

fn summary(label: &str, values: &[Decimal]) -> Value {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    let quantile = |numerator: usize| {
        if sorted.is_empty() {
            return None;
        }
        let quarter_index = (sorted.len() - 1) * numerator;
        let lower = quarter_index / 4;
        let upper = (lower + 1).min(sorted.len() - 1);
        Some(
            sorted[lower]
                + (sorted[upper] - sorted[lower])
                    * Decimal::new((quarter_index % 4 * 25) as i64, 2),
        )
    };
    let mean =
        (!sorted.is_empty()).then(|| sorted.iter().sum::<Decimal>() / Decimal::from(sorted.len()));
    json!({"label":label,"sample_size":sorted.len(),"p25":quantile(1),"p50":quantile(2),"p75":quantile(3),"mean":mean})
}

#[derive(serde::Serialize)]
struct Threshold {
    score: Decimal,
    sample_size: usize,
    cumulative_count: usize,
    percentile: Decimal,
}

fn correspondence(values: &[Decimal]) -> Vec<Threshold> {
    let mut frequencies = BTreeMap::<Decimal, usize>::new();
    for &value in values {
        *frequencies.entry(value).or_default() += 1;
    }
    let mut cumulative = 0;
    if let Some(((first, _), (last, _))) = frequencies
        .first_key_value()
        .zip(frequencies.last_key_value())
    {
        for value in
            first.floor().to_i64().unwrap_or_default()..=last.ceil().to_i64().unwrap_or_default()
        {
            frequencies.entry(Decimal::from(value)).or_default();
        }
    }
    frequencies
        .into_iter()
        .map(|(score, sample_size)| {
            cumulative += sample_size;
            Threshold {
                score,
                sample_size,
                cumulative_count: cumulative,
                percentile: Decimal::from(100 * cumulative) / Decimal::from(values.len()),
            }
        })
        .collect()
}

fn chart(rows: &[Threshold]) -> Value {
    let Some((low, high)) = rows.first().zip(rows.last()) else {
        return Value::Null;
    };
    let (mut lower, mut upper) = (low.score, high.score);
    if lower == upper {
        lower -= Decimal::ONE;
        upper += Decimal::ONE;
    }
    let rounded = |value: f64| (value * 100.0).round_ties_even() / 100.0;
    let x = |value: Decimal| {
        rounded(
            56.0 + ((value - lower) / (upper - lower))
                .to_f64()
                .unwrap_or_default()
                * 644.0,
        )
    };
    let y = |value: f64| rounded(250.0 - value * 2.3);
    let mut path = String::from("M 56 250");
    let mut points = Vec::new();
    for row in rows {
        let (x_value, y_value) = (x(row.score), y(row.percentile.to_f64().unwrap_or_default()));
        use std::fmt::Write;
        let _ = write!(path, " H {x_value:?} V {y_value:?}");
        if row.sample_size > 0 {
            points.push(json!({"score":row.score,"sample_size":row.sample_size,"cumulative_count":row.cumulative_count,
                "percentile":row.percentile,"x":x_value,"y":y_value}));
        }
    }
    path.push_str(" H 700");
    let x_ticks = (0..=5)
        .map(|index| {
            json!({"value":lower+(upper-lower)*Decimal::from(index)/Decimal::from(5),
        "x":rounded(56.0+644.0*f64::from(index)/5.0)})
        })
        .collect::<Vec<_>>();
    let y_ticks = (0..=100)
        .step_by(25)
        .map(|value| json!({"value":value,"y":y(f64::from(value))}))
        .collect::<Vec<_>>();
    json!({"path":path,"points":points,"x_ticks":x_ticks,"y_ticks":y_ticks})
}
