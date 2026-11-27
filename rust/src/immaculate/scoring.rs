use std::collections::{BTreeMap, HashMap, HashSet};

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub(super) const WIDTH: usize = 3;
pub(super) const CELLS: usize = WIDTH * WIDTH;
const EXTRA_GUESS_PENALTY: i64 = 5;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct Guess {
    pub(super) cell: usize,
    pub(super) person: String,
    pub(super) debater_id: Option<i64>,
    pub(super) name: String,
    pub(super) score: Option<i64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct Clue {
    pub(super) key: String,
    pub(super) label: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) subject: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub(super) struct Board {
    pub(super) id: u128,
    pub(super) date: NaiveDate,
    pub(super) rows: Vec<Clue>,
    pub(super) columns: Vec<Clue>,
    pub(super) answers: Vec<HashMap<String, i64>>,
    pub(super) aliases: HashMap<i64, String>,
    pub(super) clue_memberships: HashMap<String, HashSet<String>>,
}

impl Board {
    pub(super) fn valid_shape(&self) -> bool {
        self.rows.len() == WIDTH && self.columns.len() == WIDTH && self.answers.len() == CELLS
    }

    fn best(&self, cell: usize) -> Option<i64> {
        self.answers.get(cell)?.values().copied().max()
    }

    pub(super) fn person(&self, id: i64, group: Option<i64>) -> String {
        self.aliases
            .get(&id)
            .cloned()
            .unwrap_or_else(|| person_key(id, group))
    }
}

pub(super) fn person_key(id: i64, group: Option<i64>) -> String {
    group
        .filter(|group| *group != 0)
        .map_or_else(|| format!("d{id}"), |group| format!("a{group}"))
}

#[derive(Clone, Copy, Serialize)]
pub(super) struct Tier {
    key: &'static str,
    label: &'static str,
    emoji: &'static str,
    minimum: i64,
    maximum: i64,
}

pub(super) const TIERS: [Tier; 5] = [
    Tier {
        key: "common",
        label: "Common",
        emoji: "🟩",
        minimum: 1,
        maximum: 24,
    },
    Tier {
        key: "uncommon",
        label: "Uncommon",
        emoji: "🟦",
        minimum: 25,
        maximum: 49,
    },
    Tier {
        key: "rare",
        label: "Rare",
        emoji: "🟪",
        minimum: 50,
        maximum: 74,
    },
    Tier {
        key: "epic",
        label: "Epic",
        emoji: "🟧",
        minimum: 75,
        maximum: 89,
    },
    Tier {
        key: "galaxy",
        label: "Galaxy",
        emoji: "🌌",
        minimum: 90,
        maximum: 100,
    },
];

pub(super) fn tier(score: i64, best: Option<i64>) -> Tier {
    let band = if Some(score) == best {
        score.max(50)
    } else {
        score
    };
    TIERS
        .iter()
        .rev()
        .copied()
        .find(|tier| band >= tier.minimum)
        .unwrap_or(TIERS[0])
}

pub(super) fn solved(guesses: &[Guess]) -> BTreeMap<usize, &Guess> {
    guesses
        .iter()
        .filter(|guess| guess.score.is_some())
        .map(|guess| (guess.cell, guess))
        .collect()
}

pub(super) fn missed(guesses: &[Guess]) -> BTreeMap<usize, Vec<&Guess>> {
    let mut cells = BTreeMap::<usize, Vec<_>>::new();
    for guess in guesses.iter().filter(|guess| guess.score.is_none()) {
        cells.entry(guess.cell).or_default().push(guess);
    }
    cells
}

#[derive(Clone, Copy, Serialize)]
pub(super) struct Score {
    pub(super) attempts: usize,
    pub(super) rarity: i64,
    pub(super) penalty: i64,
    pub(super) best: Option<i64>,
    pub(super) worst: Option<i64>,
    pub(super) score: i64,
}

pub(super) fn breakdown(guesses: &[Guess]) -> Score {
    let solved = solved(guesses);
    let scores = || solved.values().filter_map(|guess| guess.score);
    let rarity = scores().sum();
    let penalty = guesses.len().saturating_sub(CELLS) as i64 * EXTRA_GUESS_PENALTY;
    Score {
        attempts: guesses.len(),
        rarity,
        penalty,
        best: scores().max(),
        worst: scores().min(),
        score: rarity - penalty,
    }
}

pub(super) fn guess_issue(
    guesses: &[Guess],
    attempts: usize,
    cell: usize,
    people: &HashSet<String>,
    profile_ids: &HashSet<i64>,
) -> Option<&'static str> {
    if guesses.len() != attempts {
        return Some("Your grid was updated. Choose a square to continue.");
    }
    for guess in guesses {
        if guess.cell == cell && guess.score.is_some() {
            return Some("That square is already solved.");
        }
        let already_used = people.contains(&guess.person)
            || guess.debater_id.is_some_and(|id| profile_ids.contains(&id));
        if already_used && (guess.score.is_some() || guess.cell == cell) {
            return Some("You’ve already used that debater.");
        }
    }
    None
}

pub(super) fn grid(board: &Board, guesses: &[Guess], infinite: bool) -> Value {
    let correct = solved(guesses);
    let misses = missed(guesses);
    let breakdown = breakdown(guesses);
    let finished = correct.len() == CELLS;
    let rows = board.rows.iter().enumerate().map(|(row, clue)| {
        let cells: Vec<_> = board.columns.iter().enumerate().map(|(column, clue)| {
            let index = row * WIDTH + column;
            let answer = correct.get(&index).and_then(|guess| {
                let score = guess.score?;
                Some(json!({"name":guess.name, "score":score, "tier":tier(score, board.best(index))}))
            });
            json!({"index":index, "column":clue.label, "answer":answer,
                "miss_count":misses.get(&index).map_or(0, Vec::len)})
        }).collect();
        json!({"label": clue.label, "cells": cells})
    }).collect::<Vec<_>>();
    let miss_names: BTreeMap<_, Vec<_>> = misses
        .iter()
        .map(|(cell, items)| (*cell, items.iter().map(|guess| &guess.name).collect()))
        .collect();
    let mut text = vec![format!(
        "APDA Immaculate Grid · {}",
        if infinite {
            "Infinite mode".to_owned()
        } else {
            board.date.to_string()
        }
    )];
    for row in 0..WIDTH {
        let squares: Vec<_> = (row * WIDTH..(row + 1) * WIDTH)
            .map(|index| {
                correct
                    .get(&index)
                    .and_then(|guess| guess.score)
                    .map_or_else(
                        || "⬜ —".to_owned(),
                        |score| format!("{} {score}", tier(score, board.best(index)).emoji),
                    )
            })
            .collect();
        text.push(squares.join(" | "));
    }
    text.push(format!(
        "{}/9 solved · {} points",
        correct.len(),
        breakdown.score
    ));
    text.push(format!(
        "{} guesses · {} penalty points",
        breakdown.attempts, breakdown.penalty
    ));
    json!({
        "day":board.date.to_string(), "board_id":board.id.to_string(), "columns":board.columns, "rows":rows,
        "miss_names":miss_names, "solved":correct.len(), "finished":finished, "can_view_boards":finished,
        "attempts":breakdown.attempts, "rarity":breakdown.rarity, "penalty":breakdown.penalty, "best":breakdown.best,
        "worst":breakdown.worst, "score":breakdown.score, "share_results":text.join("\n"), "rarity_tiers":TIERS,
        "latest_guess":guesses.last(), "infinite":infinite,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guess(cell: usize, id: i64, score: Option<i64>) -> Guess {
        Guess {
            cell,
            person: person_key(id, None),
            debater_id: Some(id),
            name: format!("Player {id}"),
            score,
        }
    }

    #[test]
    fn counts_only_solved_squares_and_penalizes_extra_guesses() {
        let mut guesses = vec![guess(0, 1, Some(52)), guess(2, 2, Some(22))];
        guesses.extend((3..=10).map(|id| guess(3, id, None)));
        let score = breakdown(&guesses);
        assert_eq!(
            (score.attempts, score.rarity, score.penalty, score.score),
            (10, 74, 5, 69)
        );
        assert_eq!((score.best, score.worst), (Some(52), Some(22)));
        assert_eq!(solved(&guesses).len(), 2);
        assert_eq!(missed(&guesses)[&3].len(), 8);
    }

    #[test]
    fn duplicate_identity_is_blocked_except_after_a_miss_on_another_square() {
        let guesses = [guess(0, 1, Some(75)), guess(1, 2, None)];
        let known = HashSet::from(["a1".to_owned()]);
        assert!(
            guess_issue(&guesses, 0, 3, &known, &HashSet::from([2]))
                .unwrap()
                .contains("updated")
        );
        assert!(
            guess_issue(&guesses, 2, 0, &known, &HashSet::from([2]))
                .unwrap()
                .contains("solved")
        );
        assert!(
            guess_issue(&guesses, 2, 3, &known, &HashSet::from([1]))
                .unwrap()
                .contains("already used")
        );
        assert!(
            guess_issue(&guesses, 2, 1, &known, &HashSet::from([2]))
                .unwrap()
                .contains("already used")
        );
        assert_eq!(
            guess_issue(&guesses, 2, 2, &known, &HashSet::from([2])),
            None
        );
    }

    #[test]
    fn square_best_receives_at_least_rare_band_without_changing_its_score() {
        assert_eq!(tier(24, None).key, "common");
        assert_eq!(tier(24, Some(24)).key, "rare");
        assert_eq!(tier(25, None).key, "uncommon");
        assert_eq!(tier(89, None).key, "epic");
        assert_eq!(tier(90, None).key, "galaxy");
    }
}
