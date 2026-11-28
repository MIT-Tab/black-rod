use std::{
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    sync::LazyLock,
};

use regex::Regex;
use serde_json::Value;

use super::{Result, normalize, role};

pub(super) struct RawRound {
    pub number: i64,
    pub gov: [String; 2],
    pub opp: [String; 2],
    pub roles: HashMap<String, usize>,
}

#[derive(Default)]
struct Tables {
    debaters: HashMap<i64, String>,
    teams: HashMap<i64, Vec<i64>>,
    rounds: Vec<(i64, i64, i64, i64)>,
    stats: HashMap<i64, Vec<(i64, usize)>>,
}

impl Tables {
    fn rounds(self) -> Vec<RawRound> {
        let names = |team| {
            let [first, second] = self.teams.get(&team)?.as_slice() else {
                return None;
            };
            Some(
                [first, second]
                    .map(|id| normalize(self.debaters.get(id).map_or("", String::as_str))),
            )
        };
        self.rounds
            .into_iter()
            .filter_map(|(id, number, gov, opp)| {
                Some(RawRound {
                    number,
                    gov: names(gov)?,
                    opp: names(opp)?,
                    roles: self
                        .stats
                        .get(&id)
                        .into_iter()
                        .flatten()
                        .filter_map(|(person, role)| {
                            let name = self.debaters.get(person)?.trim();
                            (!name.is_empty()).then(|| (normalize(name), *role))
                        })
                        .collect(),
                })
            })
            .collect()
    }
}

static ENDPOINT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\d{8,}").unwrap());
static CREATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^CREATE TABLE `([^`]+)` \($").unwrap());
static INSERT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^INSERT INTO `([^`]+)`(?: \(([^)]*)\))? VALUES (.*);$").unwrap());
const TABLES: [&str; 4] = [
    "tab_debater",
    "tab_team_debaters",
    "tab_round",
    "tab_roundstats",
];

pub(super) fn candidates(root: &Path, file: &str) -> Result<Vec<PathBuf>> {
    let file = file.trim();
    if let Some(stem) = file
        .strip_suffix(".json")
        .and_then(|_| Path::new(file).file_stem())
        .and_then(|stem| stem.to_str())
        && let Some((parent, endpoint)) = stem.rsplit_once('-')
        && !matches!(parent, "." | "..")
        && !endpoint.is_empty()
        && endpoint.chars().all(|digit| digit.is_ascii_digit())
    {
        return Ok(vec![
            root.join(parent).join(endpoint).join("final-backup.json"),
        ]);
    }
    let Some(endpoint) = ENDPOINT.find(file) else {
        return Ok(Vec::new());
    };
    let mut directories = entries(root)?
        .into_iter()
        .filter_map(|entry| {
            let path = entry.join(endpoint.as_str());
            path.is_dir().then_some(path)
        })
        .collect::<Vec<_>>();
    directories.sort();
    let mut paths = Vec::new();
    for directory in directories {
        let mut files = entries(&directory)?;
        files.sort_by(|left, right| right.cmp(left));
        let mut seen = HashSet::new();
        for (prefix, suffix) in [
            ("final-backup", ".json"),
            ("manual_backup", ".dump.sql"),
            ("before_pairing", ".dump.sql"),
            ("", ".dump.sql"),
        ] {
            for path in &files {
                if path.is_file()
                    && path
                        .file_name()
                        .and_then(|name| name.to_str())
                        .is_some_and(|name| {
                            if suffix == ".json" {
                                name == "final-backup.json"
                            } else {
                                name.starts_with(prefix) && name.ends_with(suffix)
                            }
                        })
                    && seen.insert(path)
                {
                    paths.push(path.clone());
                }
            }
        }
    }
    Ok(paths)
}

fn entries(path: &Path) -> Result<Vec<PathBuf>> {
    match fs::read_dir(path) {
        Ok(directory) => directory.map(|entry| Ok(entry?.path())).collect(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn load(path: &Path) -> Result<Vec<RawRound>> {
    let reader = BufReader::new(File::open(path)?);
    let tables = if path.extension().is_some_and(|ext| ext == "json") {
        read_json(serde_json::from_reader(reader)?)
    } else {
        read_dump(reader)?
    };
    Ok(tables.rounds())
}

fn integer(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str()?.trim().parse().ok())
}

fn read_json(rows: Vec<Value>) -> Tables {
    let mut tables = Tables::default();
    for row in rows {
        let Some(id) = integer(&row["pk"]) else {
            continue;
        };
        let field = &row["fields"];
        match row["model"].as_str() {
            Some("tab.debater") => {
                tables.debaters.insert(
                    id,
                    field["name"].as_str().unwrap_or_default().trim().to_owned(),
                );
            }
            Some("tab.team") => {
                let people = field["debaters"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(integer);
                tables.teams.insert(id, people.collect());
            }
            Some("tab.round") => {
                if let (Some(number), Some(gov), Some(opp)) = (
                    integer(&field["round_number"]),
                    integer(&field["gov_team"]),
                    integer(&field["opp_team"]),
                ) {
                    tables.rounds.push((id, number, gov, opp));
                }
            }
            Some("tab.roundstats") => {
                if let (Some(round), Some(person), Some(role)) = (
                    integer(&field["round"]),
                    integer(&field["debater"]),
                    field["debater_role"].as_str().and_then(role),
                ) {
                    tables.stats.entry(round).or_default().push((person, role));
                }
            }
            _ => (),
        }
    }
    tables
}

fn read_dump(reader: impl BufRead) -> Result<Tables> {
    let mut tables = Tables::default();
    let mut columns = HashMap::<String, Vec<String>>::new();
    let mut current = None;
    for line in reader.lines() {
        let line = line?;
        if let Some(table) = &current {
            if line.starts_with(") ENGINE=") {
                current = None;
            } else if let Some((column, _)) = line
                .trim_start()
                .strip_prefix('`')
                .and_then(|line| line.split_once('`'))
            {
                columns
                    .entry(String::clone(table))
                    .or_default()
                    .push(column.to_owned());
            }
            continue;
        }
        if let Some(table) = CREATE.captures(&line).and_then(|found| found.get(1))
            && TABLES.contains(&table.as_str())
        {
            current = Some(table.as_str().to_owned());
            continue;
        }
        let Some(insert) = INSERT.captures(&line) else {
            continue;
        };
        let table = &insert[1];
        if !TABLES.contains(&table) {
            continue;
        }
        let names: Vec<&str> = if let Some(explicit) = insert.get(2) {
            explicit
                .as_str()
                .split(',')
                .map(|name| name.trim().trim_matches('`'))
                .collect()
        } else {
            columns
                .get(table)
                .into_iter()
                .flatten()
                .map(String::as_str)
                .collect()
        };
        for values in insert_rows(&insert[3]) {
            let row: HashMap<_, _> = names.iter().copied().zip(values).collect();
            let field = |name| row.get(name).and_then(Option::as_deref);
            let id = |name| field(name)?.trim().parse::<i64>().ok();
            let either = |first, second| {
                id(first)
                    .filter(|number| *number != 0)
                    .or_else(|| id(second))
            };
            match table {
                "tab_debater" => {
                    if let (Some(person), Some(name)) = (id("id"), field("name").map(str::trim))
                        && !name.is_empty()
                    {
                        tables.debaters.insert(person, name.to_owned());
                    }
                }
                "tab_team_debaters" => {
                    if let (Some(team), Some(person)) = (id("team_id"), id("debater_id")) {
                        tables.teams.entry(team).or_default().push(person);
                    }
                }
                "tab_roundstats" => {
                    if let (Some(round), Some(person), Some(role)) = (
                        either("round_id", "round"),
                        either("debater_id", "debater"),
                        field("debater_role").and_then(role),
                    ) {
                        tables.stats.entry(round).or_default().push((person, role));
                    }
                }
                "tab_round" => {
                    if let (Some(round), Some(number), Some(gov), Some(opp)) = (
                        id("id"),
                        id("round_number"),
                        either("gov_team_id", "gov_team"),
                        either("opp_team_id", "opp_team"),
                    ) {
                        tables.rounds.push((round, number, gov, opp));
                    }
                }
                _ => unreachable!(),
            }
        }
    }
    Ok(tables)
}

fn insert_rows(input: &str) -> Vec<Vec<Option<String>>> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut value = String::new();
    let mut in_row = false;
    let mut quoted = false;
    let mut escaped = false;
    for character in input.chars() {
        if !in_row {
            in_row = character == '(';
            continue;
        }
        if quoted {
            value.push(character);
            if escaped {
                escaped = false;
            } else if character == '\\' {
                escaped = true;
            } else if character == '\'' {
                quoted = false;
            }
        } else if matches!(character, ',' | ')') {
            row.push(sql_scalar(value.trim()));
            value.clear();
            if character == ')' {
                rows.push(std::mem::take(&mut row));
                in_row = false;
            }
        } else {
            quoted = character == '\'';
            value.push(character);
        }
    }
    rows
}

fn sql_scalar(value: &str) -> Option<String> {
    if value == "NULL" {
        return None;
    }
    Some(
        if let Some(value) = value
            .strip_prefix('\'')
            .and_then(|value| value.strip_suffix('\''))
        {
            value
                .replace("\\\\", "\\")
                .replace("\\'", "'")
                .replace("\\\"", "\"")
                .replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
        } else {
            value.to_owned()
        },
    )
}
