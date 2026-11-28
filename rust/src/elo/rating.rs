use std::collections::{BTreeMap, HashMap};

use chrono::{DateTime, Utc};
use serde::Serialize;

#[derive(Clone)]
pub(super) struct Debate {
    pub(super) season: String,
    pub(super) timestamp: DateTime<Utc>,
    pub(super) order: [i64; 3],
    pub(super) tournament_key: String,
    pub(super) elimination: bool,
    pub(super) winner: usize,
    pub(super) teams: [Vec<(i64, String)>; 2],
    pub(super) pro_am: bool,
}

#[derive(Clone, Copy, Serialize)]
pub(super) struct Settings {
    pub(super) start: f64,
    pub(super) k_max: f64,
    pub(super) k_min: f64,
    pub(super) decay: f64,
    pub(super) win_share: f64,
    pub(super) loss_share: f64,
    pub(super) exclude_proam: bool,
}

#[derive(Clone, Copy)]
pub(super) struct Snapshot {
    pub(super) elo: f64,
    pub(super) rounds: usize,
    pub(super) out: usize,
}

pub(super) struct Player {
    pub(super) current: Snapshot,
    pub(super) seasons: BTreeMap<String, Snapshot>,
    pub(super) names: Vec<(String, usize)>,
}

impl Player {
    fn new(start: f64) -> Self {
        Self {
            current: Snapshot {
                elo: start,
                rounds: 0,
                out: 0,
            },
            seasons: BTreeMap::new(),
            names: Vec::new(),
        }
    }
}

pub(super) struct Computed {
    pub(super) tournaments: usize,
    pub(super) prelims: usize,
    pub(super) outrounds: usize,
    pub(super) excluded: usize,
    pub(super) players: Vec<(i64, Player)>,
}

fn k_factor(rounds: usize, settings: Settings) -> f64 {
    if settings.decay <= 0.0 {
        settings.k_min
    } else {
        settings.k_min
            + (settings.k_max - settings.k_min) * (-(rounds as f64) / settings.decay).exp()
    }
}

fn expected(a: f64, b: f64) -> f64 {
    1.0 / (1.0 + 10.0_f64.powf((b - a) / 400.0))
}

fn redistribute(
    indices: &[usize],
    delta: &mut [f64],
    students: &[(i64, Player)],
    higher_share: f64,
) {
    let [first, second] = indices else { return };
    let left = students[*first].1.current.elo;
    let right = students[*second].1.current.elo;
    let higher_first = if left == right {
        students[*first].0 < students[*second].0
    } else {
        left > right
    };
    let (high, low) = if higher_first { (0, 1) } else { (1, 0) };
    let total = delta[high] + delta[low];
    let share = higher_share.clamp(0.0, 1.0);
    let (mix, high_target, low_target) = if share >= 0.5 {
        ((share - 0.5) / 0.5, total, 0.0)
    } else {
        ((0.5 - share) / 0.5, 0.0, total)
    };
    delta[high] += mix * (high_target - delta[high]);
    delta[low] += mix * (low_target - delta[low]);
}

pub(super) fn compute(tournaments: usize, debates: &[Debate], settings: Settings) -> Computed {
    let mut result = Computed {
        tournaments,
        prelims: 0,
        outrounds: 0,
        excluded: 0,
        players: Vec::new(),
    };
    let mut indices = HashMap::new();
    for debate in debates {
        if settings.exclude_proam && debate.pro_am {
            result.excluded += 1;
            continue;
        }
        let sides = debate.teams.each_ref().map(|side| {
            side.iter()
                .map(|(id, _)| {
                    *indices.entry(*id).or_insert_with(|| {
                        let index = result.players.len();
                        result.players.push((*id, Player::new(settings.start)));
                        index
                    })
                })
                .collect::<Vec<_>>()
        });
        for (id, name) in debate.teams[0]
            .iter()
            .filter(|(id, _)| !sides[1].contains(&indices[id]))
            .chain(&debate.teams[1])
        {
            let name = name.trim();
            if name.is_empty() {
                continue;
            }
            let names = &mut result.players[indices[id]].1.names;
            if let Some((_, count)) = names.iter_mut().find(|(known, _)| known == name) {
                *count += 1;
            } else {
                names.push((name.to_owned(), 1));
            }
        }
        let rating = sides.each_ref().map(|side| {
            side.iter()
                .map(|id| result.players[*id].1.current.elo)
                .sum::<f64>()
                / side.len() as f64
        });
        let expected = expected(rating[0], rating[1]);
        let won = if debate.winner == 0 { 1.0 } else { 0.0 };
        let deltas = [won - expected, (1.0 - won) - (1.0 - expected)];
        let updates = sides
            .each_ref()
            .into_iter()
            .zip(deltas)
            .enumerate()
            .map(|(side, (indices, delta))| {
                let mut adjustments = indices
                    .iter()
                    .map(|id| k_factor(result.players[*id].1.current.rounds, settings) * delta)
                    .collect::<Vec<_>>();
                redistribute(
                    indices,
                    &mut adjustments,
                    &result.players,
                    if side == debate.winner {
                        settings.win_share
                    } else {
                        settings.loss_share
                    },
                );
                adjustments
            })
            .collect::<Vec<_>>();
        for (indices, delta) in sides.iter().zip(updates) {
            for (&index, delta) in indices.iter().zip(delta) {
                let player = &mut result.players[index].1;
                player.current.elo += delta;
                player.current.rounds += 1;
                player.current.out += usize::from(debate.elimination);
            }
        }
        for index in sides.into_iter().flatten() {
            let player = &mut result.players[index].1;
            player.seasons.insert(debate.season.clone(), player.current);
        }
        if debate.elimination {
            result.outrounds += 1;
        } else {
            result.prelims += 1;
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tied_partners_use_the_lower_id_for_higher_share() {
        let tournament = Debate {
            season: "2026".to_owned(),
            timestamp: Utc::now(),
            order: [0, 1, 1],
            tournament_key: "test".to_owned(),
            elimination: false,
            winner: 0,
            teams: [
                vec![(2, "A".into()), (1, "B".into())],
                vec![(3, "C".into()), (4, "D".into())],
            ],
            pro_am: false,
        };
        let settings = Settings {
            start: 1500.0,
            k_max: 40.0,
            k_min: 10.0,
            decay: 75.0,
            win_share: 1.0,
            loss_share: 0.5,
            exclude_proam: false,
        };
        let result = compute(1, &[tournament], settings);
        let rows = result
            .players
            .into_iter()
            .map(|(id, row)| (id, row.current.elo))
            .collect::<BTreeMap<_, _>>();
        assert_eq!(
            rows,
            BTreeMap::from([(1, 1540.0), (2, 1500.0), (3, 1480.0), (4, 1480.0)])
        );
    }
}
