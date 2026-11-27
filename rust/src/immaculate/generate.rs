use std::collections::{HashMap, HashSet};

use chrono::NaiveDate;
use ring::digest::{SHA512, digest};

use super::{
    catalog::{Catalog, Category},
    scoring::{Board, Clue, WIDTH},
};

const CATEGORY_COOLDOWN: i64 = 21;
const PAIR_COOLDOWN: i64 = 100;
const POINT_CURVE: &[(f64, f64)] = &[
    (0.0, 0.0),
    (25.0, 30.0),
    (35.0, 45.0),
    (50.0, 65.0),
    (65.0, 85.0),
    (75.0, 95.0),
    (100.0, 100.0),
];

pub(super) struct HistoryBoard {
    pub(super) date: NaiveDate,
    pub(super) rows: Vec<Clue>,
    pub(super) columns: Vec<Clue>,
}

// CPython seeds MT19937 with the integer represented by the bytes of the calendar
// seed and its SHA-512 digest. Using its shuffle preserves the published boards.
struct PythonRandom {
    words: [u32; 624],
    index: usize,
}

impl PythonRandom {
    fn seed_words(words: &[u32]) -> Self {
        let mut result = Self {
            words: [0; 624],
            index: 624,
        };
        result.words[0] = 19650218;
        for i in 1..624 {
            let previous = result.words[i - 1];
            result.words[i] = 1812433253_u32
                .wrapping_mul(previous ^ (previous >> 30))
                .wrapping_add(i as u32);
        }
        let mut i = 1;
        let mut j = 0;
        for _ in 0..624.max(words.len()) {
            let previous = result.words[i - 1];
            result.words[i] = (result.words[i]
                ^ 1664525_u32.wrapping_mul(previous ^ (previous >> 30)))
            .wrapping_add(words[j])
            .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                result.words[0] = result.words[623];
                i = 1;
            }
            if j >= words.len() {
                j = 0;
            }
        }
        for _ in 0..623 {
            let previous = result.words[i - 1];
            result.words[i] = (result.words[i]
                ^ 1566083941_u32.wrapping_mul(previous ^ (previous >> 30)))
            .wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                result.words[0] = result.words[623];
                i = 1;
            }
        }
        result.words[0] = 0x80000000;
        result
    }

    fn string(value: &str) -> Self {
        let bytes = [value.as_bytes(), digest(&SHA512, value.as_bytes()).as_ref()].concat();
        let words: Vec<_> = bytes
            .rchunks(4)
            .map(|chunk| {
                chunk
                    .iter()
                    .fold(0_u32, |word, byte| (word << 8) | u32::from(*byte))
            })
            .collect();
        Self::seed_words(&words)
    }

    fn integer(value: u128) -> Self {
        let words: Vec<_> = value
            .to_le_bytes()
            .chunks_exact(4)
            .map(|chunk| u32::from_le_bytes(chunk.try_into().unwrap()))
            .collect();
        let last = words
            .iter()
            .rposition(|value| *value != 0)
            .unwrap_or_default();
        Self::seed_words(&words[..=last])
    }

    fn next(&mut self) -> u32 {
        if self.index == 624 {
            for i in 0..624 {
                let value = (self.words[i] & 0x80000000) | (self.words[(i + 1) % 624] & 0x7fffffff);
                self.words[i] = self.words[(i + 397) % 624]
                    ^ (value >> 1)
                    ^ if value & 1 != 0 { 0x9908b0df } else { 0 };
            }
            self.index = 0;
        }
        let mut value = self.words[self.index];
        self.index += 1;
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c5680;
        value ^= (value << 15) & 0xefc60000;
        value ^ (value >> 18)
    }

    fn below(&mut self, upper: usize) -> usize {
        let width = upper.ilog2() + 1;
        loop {
            let draw = (self.next() >> (32 - width)) as usize;
            if draw < upper {
                return draw;
            }
        }
    }

    fn shuffle<T>(&mut self, values: &mut [T]) {
        for index in (1..values.len()).rev() {
            values.swap(index, self.below(index + 1));
        }
    }
}

fn pair<'a>(first: &'a str, second: &'a str) -> Pair<'a> {
    if first <= second {
        (first, second)
    } else {
        (second, first)
    }
}

type Pair<'a> = (&'a str, &'a str);

fn cooldowns(day: NaiveDate, history: &[HistoryBoard]) -> (HashSet<&str>, HashSet<Pair<'_>>) {
    let mut clues = HashSet::new();
    let mut pairs = HashSet::new();
    for old in history {
        let distance = (day - old.date).num_days().abs();
        if distance <= CATEGORY_COOLDOWN {
            clues.extend(
                old.rows
                    .iter()
                    .chain(&old.columns)
                    .map(|clue| clue.key.as_str()),
            );
        }
        if distance <= PAIR_COOLDOWN {
            for row in &old.rows {
                for column in &old.columns {
                    pairs.insert(pair(&row.key, &column.key));
                }
            }
        }
    }
    (clues, pairs)
}

fn minimums(partner: bool) -> (usize, usize) {
    if partner { (5, 3) } else { (15, 10) }
}

fn partner(clue: &Category) -> bool {
    clue.display.key.starts_with("partner:")
}

fn answer_rarity(catalog: &Catalog, first: &Category, second: &Category, person: &str) -> f64 {
    let discount =
        |clue: &Category| (if partner(clue) { 8.0 } else { 2.0 }) * clue.frequency(person).log2();
    catalog.rarity[person] - (discount(first) + discount(second))
}

fn top_two(scores: impl Iterator<Item = f64>) -> (f64, f64, f64) {
    let (mut best, mut second, mut minimum) = (f64::NEG_INFINITY, f64::NEG_INFINITY, f64::INFINITY);
    for score in scores {
        if score > best {
            second = best;
            best = score;
        } else if score > second {
            second = score;
        }
        minimum = minimum.min(score);
    }
    (best, second, minimum)
}

fn compatible(catalog: &Catalog, first: &Category, second: &Category) -> bool {
    if first.topic == second.topic {
        return false;
    }
    let partnered = partner(first) || partner(second);
    let (minimum, recent) = minimums(partnered);
    let overlap: Vec<_> = first
        .people
        .intersection(&second.people)
        .take(101)
        .collect();
    if !(minimum..=100).contains(&overlap.len())
        || overlap
            .iter()
            .filter(|person| catalog.recent.contains(person.as_str()))
            .count()
            < recent
        || (!partnered
            && (overlap.len() == first.people.len() || overlap.len() == second.people.len()))
    {
        return false;
    }
    let (best, second_best, minimum) = top_two(
        overlap
            .iter()
            .map(|person| answer_rarity(catalog, first, second, person)),
    );
    best >= 20.0 && second_best >= 15.0 && minimum < best
}

fn curve(raw: f64) -> f64 {
    let raw = raw.max(0.0);
    POINT_CURVE
        .windows(2)
        .find_map(|pair| {
            let [(lo, left), (hi, right)] = [pair[0], pair[1]];
            (raw <= hi).then(|| left + (right - left) * (raw - lo) / (hi - lo))
        })
        .unwrap_or(100.0)
}

fn answer_scores(catalog: &Catalog, row: &Category, column: &Category) -> HashMap<String, i64> {
    let scores: Vec<_> = row
        .people
        .intersection(&column.people)
        .map(|person| (person, answer_rarity(catalog, row, column, person)))
        .collect();
    let (best, second, _) = top_two(scores.iter().map(|(_, score)| *score));
    let floors: Vec<_> = [(best, 35.0), (second, 25.0)]
        .into_iter()
        .filter(|(anchor, target)| curve(*anchor) < *target)
        .collect();
    scores
        .into_iter()
        .map(|(person, raw)| {
            let floor = floors
                .iter()
                .map(|(anchor, target)| target - 2.0 * (anchor - raw).max(0.0))
                .fold(1.0, f64::max);
            let cap = if raw >= 100.0 { 100 } else { 99 };
            (
                person.clone(),
                (curve(raw).max(floor).round_ties_even() as i64).min(cap),
            )
        })
        .collect()
}

type Triple<'a> = [&'a str; WIDTH];

fn columns<'a>(
    catalog: &'a Catalog,
    shared: impl Iterator<Item = &'a str>,
    order: &HashMap<&str, usize>,
    rows: Triple<'a>,
) -> Option<Triple<'a>> {
    let row_topics: HashSet<_> = rows
        .iter()
        .map(|key| catalog.clues[*key].topic.as_str())
        .collect();
    let row_kinds: HashSet<_> = rows.iter().map(|key| catalog.clues[*key].kind()).collect();
    let mut candidates: Vec<_> = shared
        .filter(|key| {
            let item = &catalog.clues[*key];
            !partner(item) && !row_topics.contains(item.topic.as_str())
        })
        .collect();
    candidates.sort_by_key(|key| (row_kinds.contains(&catalog.clues[*key].kind()), order[key]));
    for (i, first) in candidates.iter().enumerate() {
        let a = &catalog.clues[*first];
        for (j, second) in candidates.iter().enumerate().skip(i + 1) {
            let b = &catalog.clues[*second];
            if a.topic == b.topic || a.kind() == b.kind() {
                continue;
            }
            for third in candidates.iter().skip(j + 1) {
                let c = &catalog.clues[*third];
                if c.topic == a.topic
                    || c.topic == b.topic
                    || c.kind() == a.kind()
                    || c.kind() == b.kind()
                {
                    continue;
                }
                let mut kinds = row_kinds.clone();
                kinds.extend([a.kind(), b.kind(), c.kind()]);
                if kinds.len() >= 4 && kinds.iter().any(|(kind, _)| *kind != "placement") {
                    return Some([first, second, third]);
                }
            }
        }
    }
    None
}

fn choose<'a>(
    catalog: &'a Catalog,
    neighbors: HashMap<&'a str, HashSet<&'a str>>,
    rng: &mut PythonRandom,
) -> Option<(Triple<'a>, Triple<'a>)> {
    let mut keys: Vec<_> = neighbors
        .iter()
        .filter(|(_, links)| links.len() >= WIDTH)
        .map(|(key, _)| *key)
        .collect();
    keys.sort_unstable();
    rng.shuffle(&mut keys);
    let order: HashMap<_, _> = keys
        .iter()
        .enumerate()
        .map(|(index, key)| (*key, index))
        .collect();
    let (partners, others): (Vec<_>, Vec<_>) = keys
        .into_iter()
        .partition(|key| partner(&catalog.clues[*key]));
    for first in partners {
        let a = &catalog.clues[first];
        for (index, second) in others.iter().enumerate() {
            let b = &catalog.clues[*second];
            if a.topic == b.topic || a.kind() == b.kind() {
                continue;
            }
            let shared: Vec<_> = neighbors[first]
                .intersection(&neighbors[second])
                .copied()
                .filter(|key| order.contains_key(key))
                .collect();
            if shared.len() < WIDTH {
                continue;
            }
            for third in &others[index + 1..] {
                let c = &catalog.clues[*third];
                if c.topic == a.topic
                    || c.topic == b.topic
                    || c.kind() == a.kind()
                    || c.kind() == b.kind()
                {
                    continue;
                }
                let rows = [first, second, third];
                let common = shared
                    .iter()
                    .copied()
                    .filter(|key| neighbors[third].contains(key));
                if let Some(columns) = columns(catalog, common, &order, rows) {
                    return Some(if rng.next() >> 31 != 0 {
                        (columns, rows)
                    } else {
                        (rows, columns)
                    });
                }
            }
        }
    }
    None
}

pub(super) fn build(
    date: NaiveDate,
    catalog: Catalog,
    history: &[HistoryBoard],
    practice: Option<u128>,
) -> Option<Board> {
    let (blocked_clues, blocked_pairs) = cooldowns(date, history);
    let candidates: Vec<_> = catalog
        .clues
        .values()
        .filter(|clue| {
            let (minimum, recent) = minimums(partner(clue));
            !blocked_clues.contains(clue.display.key.as_str())
                && (minimum..=500).contains(&clue.people.len())
                && clue
                    .people
                    .intersection(&catalog.recent)
                    .take(recent)
                    .count()
                    >= recent
        })
        .collect();
    let mut links = HashMap::<_, HashSet<_>>::new();
    for (index, first) in candidates.iter().enumerate() {
        for second in &candidates[index + 1..] {
            let (a, b) = (first.display.key.as_str(), second.display.key.as_str());
            if !blocked_pairs.contains(&pair(a, b)) && compatible(&catalog, first, second) {
                links.entry(a).or_default().insert(b);
                links.entry(b).or_default().insert(a);
            }
        }
    }
    let mut seed = practice.map_or_else(
        || PythonRandom::string(&format!("black-rod-immaculate-grid:{date}")),
        PythonRandom::integer,
    );
    let (rows, columns) = choose(&catalog, links, &mut seed)?;
    let answers: Vec<_> = rows
        .iter()
        .flat_map(|row| {
            let catalog = &catalog;
            columns.iter().map(move |column| {
                answer_scores(catalog, &catalog.clues[*row], &catalog.clues[*column])
            })
        })
        .collect();
    let eligible: HashSet<_> = answers.iter().flat_map(|scores| scores.keys()).collect();
    let aliases = catalog
        .aliases
        .iter()
        .filter(|(_, person)| eligible.contains(person))
        .map(|(id, person)| (*id, person.clone()))
        .collect();
    let clue_memberships = rows
        .iter()
        .chain(&columns)
        .map(|key| ((*key).to_owned(), catalog.clues[*key].people.clone()))
        .collect();
    Some(Board {
        id: practice.unwrap_or(0),
        date,
        rows: rows
            .iter()
            .map(|key| catalog.clues[*key].display.clone())
            .collect(),
        columns: columns
            .iter()
            .map(|key| catalog.clues[*key].display.clone())
            .collect(),
        answers,
        aliases,
        clue_memberships,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_the_reference_calendar_shuffle_and_practice_random_stream() {
        let mut rng = PythonRandom::string("black-rod-immaculate-grid:2025-11-06");
        let mut values: Vec<_> = (0..12).collect();
        rng.shuffle(&mut values);
        assert_eq!(values, [1, 11, 0, 4, 3, 10, 6, 2, 5, 7, 9, 8]);
        assert_eq!(rng.next() >> 31, 1);
        let mut rng = PythonRandom::integer(426485185465458164545986733);
        assert_eq!(
            (0..5).map(|_| rng.next()).collect::<Vec<_>>(),
            [428166794, 1532370037, 4026953653, 344029587, 2687824893]
        );
    }
}
