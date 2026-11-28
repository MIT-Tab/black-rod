use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use sqlx::{AnyPool, AssertSqlSafe};

use super::{Person, School, Selection, schools};
use crate::{
    ApiError,
    admin::merge_suggestions::{matching_ratio, merge_score, totals},
};

#[derive(Default, Deserialize, Serialize)]
pub(super) struct Suggestions {
    pub(super) debaters: Vec<DebaterMatch>,
    pub(super) schools: Vec<SchoolMatch>,
}

#[derive(Deserialize, Serialize)]
pub(super) struct DebaterMatch {
    synthetic_debater: Person,
    canonical_debater: Person,
    same_school: bool,
    percent_match: u8,
    min_results: i64,
    max_results: i64,
}

#[derive(Deserialize, Serialize)]
pub(super) struct SchoolMatch {
    synthetic_school: School,
    canonical_school: School,
    same_short_name: bool,
    percent_match: u8,
}

fn text(input: &str) -> String {
    input
        .to_lowercase()
        .split(|letter: char| !letter.is_ascii_alphanumeric())
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

fn prefix(value: &str, length: usize) -> &str {
    &value[..length.min(value.len())]
}

fn suffix(value: &str, length: usize) -> &str {
    &value[value.len().saturating_sub(length)..]
}

fn ratio(one: &str, two: &str) -> f64 {
    matching_ratio(
        &one.chars().collect::<Vec<_>>(),
        &two.chars().collect::<Vec<_>>(),
    )
}

fn percent(value: f64) -> u8 {
    (100.0 * value).round_ties_even() as u8
}

struct Names {
    first: String,
    last: String,
    full: String,
    school: String,
}

impl From<&Person> for Names {
    fn from(person: &Person) -> Self {
        let first = text(&person.first);
        let last = text(&person.last);
        let full = [first.as_str(), last.as_str()]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        let school = person
            .school
            .as_ref()
            .map(|school| text(&school.name))
            .unwrap_or_default();
        Self {
            first,
            last,
            full,
            school,
        }
    }
}

#[derive(Eq, Hash, PartialEq)]
enum DebaterKey<'a> {
    Full(&'a str, &'a str),
    Last(&'a str),
    Initial(&'a str, &'a str),
    Prefix(&'a str, &'a str),
    LastPrefix(&'a str),
    LastSuffix(&'a str),
    School(i64, &'a str),
}

impl Names {
    fn keys(&self, school_id: Option<i64>) -> impl Iterator<Item = DebaterKey<'_>> {
        let first = self.first.as_str();
        let last = self.last.as_str();
        [
            DebaterKey::Full(first, last),
            DebaterKey::Last(last),
            DebaterKey::Initial(prefix(first, 1), prefix(last, 4)),
            DebaterKey::Prefix(prefix(first, 3), prefix(last, 4)),
            DebaterKey::LastPrefix(prefix(last, 3)),
            DebaterKey::LastSuffix(suffix(last, 4)),
        ]
        .into_iter()
        .chain(school_id.map(|id| DebaterKey::School(id, prefix(first, 1))))
    }

    fn matches_block(&self, other: &Person) -> bool {
        let first = other.first.to_lowercase();
        let last = other.last.to_lowercase();
        !self.last.is_empty()
            && (last == self.last
                || last.starts_with(prefix(&self.last, 4))
                || last.starts_with(prefix(&self.last, 3))
                || (self.last.len() >= 4 && last.ends_with(suffix(&self.last, 4)))
                || (!self.first.is_empty()
                    && first.starts_with(prefix(&self.first, 1))
                    && last.starts_with(prefix(&self.last, 2))))
    }
}

async fn rejected(pool: &AnyPool, kind: &str) -> Result<HashSet<(i64, i64)>, ApiError> {
    Ok(sqlx::query_as::<_, (i64, i64)>(
        "SELECT CAST(synthetic_id AS BIGINT),CAST(resolved_to_id AS BIGINT)
        FROM core_syntheticresolutionlog WHERE action='rejected' AND entity_type=$1",
    )
    .bind(kind)
    .fetch_all(pool)
    .await?
    .into_iter()
    .collect())
}

async fn people(
    pool: &AnyPool,
    synthetic: bool,
    selection: &Selection,
    limit: bool,
) -> Result<Vec<Person>, ApiError> {
    let restricted = synthetic && !selection.is_empty();
    let scope = if restricted {
        let people = super::sql_ids(&selection.debaters);
        let schools = super::sql_ids(&selection.schools);
        format!(" AND (d.id IN ({people}) OR d.school_id IN ({schools}))")
    } else {
        String::new()
    };
    let active = if synthetic {
        "d.synthetic"
    } else {
        "NOT d.synthetic AND NOT d.temporary"
    };
    let count = if !limit || restricted {
        ""
    } else if synthetic {
        " LIMIT 500"
    } else {
        " LIMIT 10000"
    };
    let sql = format!(
        "{} WHERE {active} AND d.alias_group_id IS NULL AND d.first_name<>'' AND d.last_name<>''
        {scope} ORDER BY d.id DESC{count}",
        super::PEOPLE
    );
    Ok(sqlx::query(AssertSqlSafe(sql))
        .fetch_all(pool)
        .await?
        .iter()
        .map(Person::read)
        .collect::<Result<_, _>>()?)
}

async fn debaters(pool: &AnyPool, selection: &Selection) -> Result<Vec<DebaterMatch>, ApiError> {
    let synthetic = people(pool, true, selection, true).await?;
    if synthetic.is_empty() {
        return Ok(Vec::new());
    }
    let names = synthetic.iter().map(Names::from).collect::<Vec<_>>();
    let blocked = (!selection.is_empty() || synthetic.len() <= 20) && synthetic.len() <= 80;
    let mut canonical = people(pool, false, selection, !blocked).await?;
    if blocked {
        let exact = synthetic
            .iter()
            .filter_map(|person| person.school_id)
            .collect::<HashSet<_>>();
        canonical.retain(|person| {
            person
                .school_id
                .is_some_and(|school| exact.contains(&school))
                || names.iter().any(|name| name.matches_block(person))
        });
    }
    let canonical_names = canonical.iter().map(Names::from).collect::<Vec<_>>();
    let mut index: HashMap<_, Vec<usize>> = HashMap::new();
    for (offset, (person, name)) in canonical.iter().zip(&canonical_names).enumerate() {
        for key in name.keys(person.school_id) {
            index.entry(key).or_default().push(offset);
        }
    }
    let rejected = rejected(pool, "debater").await?;
    let mut pairs = Vec::new();
    for (offset, (source, name)) in synthetic.iter().zip(&names).enumerate() {
        let mut seen = HashSet::new();
        let mut best = Vec::new();
        for candidate in name
            .keys(source.school_id)
            .filter_map(|key| index.get(&key))
            .flatten()
            .copied()
        {
            let target = &canonical[candidate];
            if !seen.insert(candidate) || rejected.contains(&(source.id, target.id)) {
                continue;
            }
            let target_name = &canonical_names[candidate];
            let first = ratio(&name.first, &target_name.first);
            let last = ratio(&name.last, &target_name.last);
            let similarity = ratio(&name.full, &target_name.full);
            let school = if name.school.is_empty() || target_name.school.is_empty() {
                0.0
            } else {
                ratio(&name.school, &target_name.school)
            };
            let same = source.school_id == target.school_id;
            let eligible = similarity >= 0.74
                || (same && similarity >= 0.64)
                || (last >= 0.82 && first >= 0.45)
                || (school >= 0.78 && last >= 0.72);
            if eligible {
                let age = (source.id.max(target.id) as f64 / 200.0).min(50.0);
                let rating = similarity * 100.0
                    + last * 20.0
                    + age
                    + if same { 30.0 } else { 0.0 }
                    + school * 10.0;
                best.push((candidate, similarity, same, rating));
            }
        }
        best.sort_by(|a, b| b.3.total_cmp(&a.3));
        pairs.extend(
            best.into_iter()
                .take(5)
                .map(|(candidate, similarity, same, _)| {
                    (
                        offset,
                        candidate,
                        similarity,
                        same,
                        source.id.max(canonical[candidate].id),
                    )
                }),
        );
    }
    pairs.sort_by(|a, b| b.2.total_cmp(&a.2).then(b.3.cmp(&a.3)).then(b.4.cmp(&a.4)));
    pairs.truncate(400);
    let ids = pairs
        .iter()
        .flat_map(|&(a, b, _, _, _)| [synthetic[a].id, canonical[b].id])
        .collect::<HashSet<_>>();
    let totals = totals(&mut *pool.acquire().await?, &ids).await?;
    let mut rated = pairs
        .into_iter()
        .filter_map(|(a, b, similarity, same, last)| {
            let mut first = synthetic[a].clone();
            let mut second = canonical[b].clone();
            first.total_results = totals.get(&first.id).copied().unwrap_or_default();
            second.total_results = totals.get(&second.id).copied().unwrap_or_default();
            let min = first.total_results.min(second.total_results);
            let max = first.total_results.max(second.total_results);
            let score = merge_score(similarity, last, same, min, max);
            (score > 0.0).then_some((
                score,
                DebaterMatch {
                    synthetic_debater: first,
                    canonical_debater: second,
                    same_school: same,
                    percent_match: percent(similarity),
                    min_results: min,
                    max_results: max,
                },
            ))
        })
        .collect::<Vec<_>>();
    rated.sort_by(|a, b| b.0.total_cmp(&a.0));
    Ok(rated.into_iter().take(100).map(|(_, pair)| pair).collect())
}

struct SchoolName {
    full: String,
    compact: String,
    tokens: Vec<String>,
    acronym: String,
}

impl SchoolName {
    fn new(name: &str) -> Self {
        let full = text(name);
        let words: Vec<_> = full.split_whitespace().collect();
        let mut tokens = words
            .iter()
            .copied()
            .filter(|token| {
                !matches!(
                    *token,
                    "college" | "campus" | "institute" | "of" | "school" | "the" | "university"
                )
            })
            .collect::<Vec<_>>();
        if tokens.is_empty() {
            tokens = words.clone();
        }
        let compact = tokens.join(" ");
        let acronym = if words.len() == 1 {
            full.clone()
        } else {
            words
                .iter()
                .filter(|word| !matches!(**word, "the" | "of"))
                .filter_map(|word| word.chars().next())
                .collect()
        };
        let tokens = tokens.into_iter().map(str::to_owned).collect();
        Self {
            full,
            compact,
            tokens,
            acronym,
        }
    }
}

#[derive(Eq, Hash, PartialEq)]
enum SchoolKey<'a> {
    Name(&'a str),
    Compact(&'a str),
    Prefix(&'a str),
    Short(&'a str),
    Acronym(&'a str),
    Token(&'a str),
}

fn school_keys<'a>(
    long: &'a SchoolName,
    short: &'a SchoolName,
    canonical: bool,
) -> impl Iterator<Item = SchoolKey<'a>> {
    [
        Some(SchoolKey::Name(&long.full)),
        Some(SchoolKey::Compact(&long.compact)),
        (!long.compact.is_empty()).then(|| SchoolKey::Prefix(prefix(&long.compact, 5))),
        (!short.full.is_empty()).then_some(SchoolKey::Short(&short.full)),
        (!canonical && !short.compact.is_empty()).then_some(SchoolKey::Short(&short.compact)),
        (!long.acronym.is_empty()).then_some(SchoolKey::Acronym(&long.acronym)),
        (!short.full.is_empty()).then_some(SchoolKey::Acronym(&short.full)),
        (!short.compact.is_empty()).then_some(SchoolKey::Acronym(&short.compact)),
    ]
    .into_iter()
    .flatten()
    .chain(long.tokens.iter().map(|token| SchoolKey::Token(token)))
}

async fn school_matches(
    pool: &AnyPool,
    selection: &Selection,
) -> Result<Vec<SchoolMatch>, ApiError> {
    let synthetic = schools(pool, true, &selection.schools, true).await?;
    if synthetic.is_empty() {
        return Ok(Vec::new());
    }
    let canonical = schools(pool, false, &[], false).await?;
    let names = canonical
        .iter()
        .map(|school| {
            (
                SchoolName::new(&school.name),
                SchoolName::new(&school.short_name),
            )
        })
        .collect::<Vec<_>>();
    let mut index: HashMap<_, Vec<usize>> = HashMap::new();
    for (position, (name, short)) in names.iter().enumerate() {
        for key in school_keys(name, short, true) {
            index.entry(key).or_default().push(position);
        }
    }
    let rejected = rejected(pool, "school").await?;
    let mut suggestions = Vec::new();
    for source in &synthetic {
        let name = SchoolName::new(&source.name);
        let short = SchoolName::new(&source.short_name);
        let tokens: HashSet<_> = name.tokens.iter().collect();
        let mut best = Vec::new();
        let mut seen = HashSet::new();
        for position in school_keys(&name, &short, false)
            .filter_map(|key| index.get(&key))
            .flatten()
            .copied()
        {
            let target = &canonical[position];
            if !seen.insert(position) || rejected.contains(&(source.id, target.id)) {
                continue;
            }
            let (other, other_short) = &names[position];
            let similarity = ratio(&name.compact, &other.compact);
            let short_similarity = if short.full.is_empty() || other_short.full.is_empty() {
                0.0
            } else {
                ratio(&short.compact, &other_short.compact)
            };
            let best_similarity = similarity.max(short_similarity);
            let other_tokens: HashSet<_> = other.tokens.iter().collect();
            let overlap = if tokens.is_empty() || other_tokens.is_empty() {
                0.0
            } else {
                tokens.intersection(&other_tokens).count() as f64
                    / tokens.len().max(other_tokens.len()) as f64
            };
            let same_short = !short.full.is_empty() && short.full == other_short.full;
            let acronym = same_short
                || (!name.acronym.is_empty() && name.acronym == other.acronym)
                || (!short.compact.is_empty() && short.compact == other_short.compact);
            if best_similarity < 0.68 && overlap < 0.5 && !same_short && !acronym {
                continue;
            }
            let score = best_similarity * 100.0
                + overlap * 25.0
                + if name.compact == other.compact {
                    25.0
                } else {
                    0.0
                }
                + if same_short { 30.0 } else { 0.0 }
                + if acronym { 20.0 } else { 0.0 };
            best.push((score, position, best_similarity, same_short));
        }
        best.sort_by(|a, b| b.0.total_cmp(&a.0));
        suggestions.extend(best.into_iter().take(3).map(
            |(score, position, similarity, same_short_name)| {
                (
                    score,
                    SchoolMatch {
                        synthetic_school: source.clone(),
                        canonical_school: canonical[position].clone(),
                        same_short_name,
                        percent_match: percent(similarity),
                    },
                )
            },
        ));
    }
    suggestions.sort_by(|a, b| b.0.total_cmp(&a.0));
    Ok(suggestions
        .into_iter()
        .take(60)
        .map(|(_, pair)| pair)
        .collect())
}

pub(super) async fn build(pool: &AnyPool, selection: &Selection) -> Result<Suggestions, ApiError> {
    let (debaters, schools) =
        tokio::try_join!(debaters(pool, selection), school_matches(pool, selection))?;
    Ok(Suggestions { debaters, schools })
}
