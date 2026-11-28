use std::collections::{BTreeSet, HashMap, HashSet};

use csv::{ReaderBuilder, StringRecord};
use serde::Serialize;
use serde_json::{Value, json};

#[derive(Serialize)]
struct School {
    name: String,
    region: String,
    desired_tournaments: Option<i64>,
    priority: Option<i64>,
    tags: Vec<String>,
}

#[derive(Serialize)]
struct Date {
    date: String,
    num_tournaments: Value,
    tags: Vec<String>,
}

#[derive(Serialize)]
pub(super) struct Summary {
    pub(super) school_count: usize,
    pub(super) date_count: usize,
    active_date_count: usize,
    flexible_date_count: usize,
    scenario_count: Value,
    preview_schools: Vec<School>,
    preview_dates: Vec<Date>,
    tags: Vec<String>,
}

impl Summary {
    pub(super) fn counts(&self) -> Value {
        json!({
            "school_count":self.school_count,"date_count":self.date_count,
            "active_date_count":self.active_date_count,"flexible_date_count":self.flexible_date_count,
            "scenario_count":self.scenario_count,"tags":self.tags,
        })
    }
}

fn read(text: &str) -> Result<Vec<StringRecord>, String> {
    ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .from_reader(text.as_bytes())
        .records()
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| error.to_string())
}

fn cell(row: &StringRecord, index: usize) -> &str {
    row.get(index).unwrap_or_default().trim()
}

fn tags(text: &str) -> Vec<String> {
    text.split(',')
        .map(str::trim)
        .filter(|tag| !tag.is_empty())
        .map(str::to_owned)
        .collect()
}

fn dates(text: &str) -> Result<Vec<Date>, String> {
    let rows = read(text)?;
    if rows.len() < 2 {
        return Err("Dates CSV must include a header row and at least one date.".into());
    }
    let mut dates = Vec::new();
    let mut indexes = HashMap::new();
    for row in &rows[1..] {
        let date = cell(row, 0);
        if date.is_empty() {
            continue;
        }
        let raw = cell(row, 1);
        let count = if raw.contains('-') {
            json!([1, 2])
        } else if raw.is_empty() {
            json!(0)
        } else {
            json!(raw.parse::<i64>().map_err(|_| format!(
                "Number of tournaments for {date} must be a whole number or 1-2."
            ))?)
        };
        let mut tags = tags(cell(row, 2));
        if let Some(index) = tags.iter().position(|tag| tag == "Unopposed") {
            tags.remove(index);
        }
        let data = Date {
            date: date.to_owned(),
            num_tournaments: count,
            tags,
        };
        if let Some(index) = indexes.get(date).copied() {
            dates[index] = data;
        } else {
            indexes.insert(date, dates.len());
            dates.push(data);
        }
    }
    if dates.is_empty() {
        return Err("Dates CSV did not contain any schedule dates.".into());
    }
    Ok(dates)
}

fn schools(text: &str, known: &HashSet<&str>) -> Result<Vec<School>, String> {
    let mut rows = read(text)?;
    let first = text.strip_prefix('\u{feff}').unwrap_or(text);
    // A blank first row is a valid header when there are no date preferences.
    if first.starts_with(['\n', '\r']) {
        rows.insert(0, StringRecord::new());
    }
    if rows.len() < 3 {
        return Err(
            "Schools CSV must include the date row, the label row, and at least one school.".into(),
        );
    }
    let unknown: BTreeSet<_> = rows[0]
        .iter()
        .map(str::trim)
        .filter(|date| !date.is_empty() && !known.contains(date))
        .collect();
    if !unknown.is_empty() {
        return Err(format!(
            "Schools CSV includes dates that are missing from the dates CSV: {}",
            unknown.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    let label = if rows[1]
        .iter()
        .any(|entry| entry.trim() == "# of Tournaments")
    {
        2
    } else {
        1
    };
    if rows.len() <= label + 1 {
        return Err("Schools CSV did not contain any school rows.".into());
    }
    if !cell(&rows[label], 0).starts_with("School Name") {
        return Err("Schools CSV must include a label row starting with 'School Name'.".into());
    }
    let mut result = Vec::new();
    for row in &rows[label + 1..] {
        let name = cell(row, 0);
        if name.is_empty() {
            continue;
        }
        let parse = |index, label| {
            let raw = cell(row, index);
            if raw.is_empty() {
                Ok(None)
            } else {
                raw.parse::<i64>()
                    .map(Some)
                    .map_err(|_| format!("{label} for {name} must be a whole number."))
            }
        };
        result.push(School {
            name: name.to_owned(),
            region: cell(row, 1).to_owned(),
            desired_tournaments: parse(2, "Desired tournaments")?,
            priority: parse(3, "Priority")?,
            tags: tags(&cell(row, 4).replace('"', "")),
        });
    }
    if result.is_empty() {
        return Err("Schools CSV did not contain any school rows.".into());
    }
    Ok(result)
}

pub(super) fn summarize(school_csv: &str, date_csv: &str) -> Result<Summary, String> {
    let dates = dates(date_csv)?;
    let known = dates.iter().map(|date| date.date.as_str()).collect();
    let schools = schools(school_csv, &known)?;
    let flexible = dates
        .iter()
        .filter(|date| date.num_tournaments.is_array())
        .count();
    let active = dates
        .iter()
        .filter(|date| date.num_tournaments.as_i64() != Some(0))
        .count();
    let scenarios = 1_u64
        .checked_shl(flexible as u32)
        .map_or_else(|| json!(2_f64.powi(flexible as i32)), |count| json!(count));
    let school_tags = schools
        .iter()
        .flat_map(|school| school.tags.iter().cloned())
        .collect::<BTreeSet<_>>();
    Ok(Summary {
        school_count: schools.len(),
        date_count: dates.len(),
        active_date_count: active,
        flexible_date_count: flexible,
        scenario_count: scenarios,
        preview_schools: schools,
        preview_dates: dates,
        tags: school_tags.into_iter().collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_flexible_dates_quoted_tags_and_google_sheet_labels() {
        let schools = ",,,,,9/8,1/6\n,# of Tournaments,,,,,\nSchool Name,Region,Desired,Priority,Tags,,,,\n\"University, College\",North,1,2,\"Novice, Open\",Rank 1,Rank 3\n";
        let dates = "Date,Count,Tags\n9/8,1-2,\"Unopposed,Novice\"\n1/6,0,\n";
        let summary = summarize(schools, dates).unwrap();
        assert_eq!(
            summary.counts(),
            json!({"school_count":1,"date_count":2,"active_date_count":1,
            "flexible_date_count":1,"scenario_count":2,"tags":["Novice","Open"]})
        );
        assert_eq!(summary.preview_dates[0].tags, ["Novice"]);
        assert_eq!(summary.preview_schools[0].name, "University, College");
        assert_eq!(
            summarize(schools, &dates.replace("1/6", "1/7"))
                .err()
                .unwrap(),
            "Schools CSV includes dates that are missing from the dates CSV: 1/6"
        );
    }

    #[test]
    fn schools_without_date_preferences_accept_a_blank_date_header() {
        for newline in ["\n", "\r\n"] {
            let data = format!(
                "{newline}School Name,Region,Desired,Priority,Tags{newline}Alpha,East,1,2,Open{newline}"
            );
            let overview = summarize(&data, "Date,Tournaments,Tags\n9/8,1,Open\n").unwrap();
            assert_eq!(overview.school_count, 1);
            assert_eq!(overview.preview_schools[0].name, "Alpha");
        }
    }
}
