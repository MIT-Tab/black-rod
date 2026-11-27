use std::collections::HashMap;

const RANKS: &[&str] = &["host", "event", "speaker", "SOTY", "TOTY", "top_speaker"];

fn count_labels(category: &str) -> Option<(&'static str, &'static str, &'static str)> {
    Some(match category {
        "speaks_seasons" => ("Varsity speaker awards in", "season", ""),
        "varsity_seasons" => ("Varsity placements in", "season", ""),
        "multiple_finals" => ("Varsity team finals in", "season", ""),
        "placed_hosts" => ("Varsity semis or better at", "host", " in one season"),
        "placed_partners" => ("Placed with", "partner", ""),
        "partner_schools" => ("Placed with partners from", "school", ""),
        _ => return None,
    })
}

pub(super) fn threshold(key: &str) -> Option<(&str, i64)> {
    match key {
        "top_speaker" => return Some((key, 1)),
        "multiple_finals" => return Some((key, 2)),
        _ => (),
    }
    let (topic, cutoff) = key.rsplit_once(':')?;
    let category = topic.split(':').next()?;
    if RANKS.contains(&category) || count_labels(category).is_some() {
        Some((topic, cutoff.parse().ok()?))
    } else {
        None
    }
}

fn ordinal(value: i64) -> String {
    let suffix = match (value % 100, value % 10) {
        (11..=13, _) => "th",
        (_, 1) => "st",
        (_, 2) => "nd",
        (_, 3) => "rd",
        _ => "th",
    };
    format!("{value}{suffix}")
}

pub(super) fn explain(
    progress: &HashMap<String, HashMap<String, i64>>,
    threshold: Option<(&str, i64)>,
    person: &str,
) -> Option<String> {
    let (topic, cutoff) = threshold?;
    let value = progress
        .get(topic)?
        .get(person)
        .copied()
        .unwrap_or_default();
    let category = topic.split(':').next()?;
    if RANKS.contains(&category) {
        let rank = -value;
        if rank <= cutoff {
            return None;
        }
        return Some(match category {
            "host" | "event" => match rank {
                1..=2 => "Reached finals".to_owned(),
                3..=4 => "Reached semifinals".to_owned(),
                5..=8 => "Reached quarterfinals".to_owned(),
                _ => format!("Best finish: {} place", ordinal(rank)),
            },
            "speaker" | "top_speaker" => format!("Best varsity speaker rank: {}", ordinal(rank)),
            _ => format!("Best {category} rank: {}", ordinal(rank)),
        });
    }
    let (prefix, unit, suffix) = count_labels(category)?;
    let plural = if value == 1 { "" } else { "s" };
    (0 < value && value < cutoff).then(|| format!("{prefix} {value} {unit}{plural}{suffix}"))
}
