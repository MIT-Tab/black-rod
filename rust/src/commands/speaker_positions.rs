mod backup;

use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
};

use chrono::{SecondsFormat, Utc};
use rust_decimal::Decimal;
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};
use unicode_casefold::UnicodeCaseFold;

use super::Result;
use crate::{AppState, team_profile::read_transaction};

const SLOTS: [&str; 4] = ["gov_1", "gov_2", "opp_1", "opp_2"];
const ROLES: [&str; 4] = ["PM", "MG", "LO", "MO"];
const SUMMARY: [&str; 5] = [
    "already_correct",
    "missing_backup_file",
    "missing_file_backup_import",
    "no_unique_backup_match",
    "unmatched_backup_names",
];

#[derive(Default)]
struct Options {
    root: PathBuf,
    output: PathBuf,
    report: Option<PathBuf>,
    debater: Option<i64>,
    name: Option<String>,
    rounds: HashSet<i64>,
}

const USAGE: &str = "Usage: black-rod generate_speaker_position_amendments --source-root PATH \
    --output PATH [--report PATH] [--debater-id ID] [--debater-name NAME] [--round-id ID ...]";

impl Options {
    fn parse(args: &[String]) -> Result<Self> {
        let mut options = Self::default();
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            let (key, inline) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(key, value)| (key, Some(value)));
            let value = inline
                .or_else(|| args.next().map(String::as_str))
                .ok_or_else(|| format!("Supply a value for {key}"))?;
            match key {
                "--source-root" => options.root = value.into(),
                "--output" => options.output = value.into(),
                "--report" => options.report = Some(value.into()),
                "--debater-id" => options.debater = Some(value.parse()?),
                "--debater-name" => options.name = Some(value.to_owned()),
                "--round-id" => {
                    options.rounds.insert(value.parse()?);
                }
                _ => return Err(format!("Unknown amendment option: {key}").into()),
            }
        }
        if options.root.as_os_str().is_empty()
            || options.output.as_os_str().is_empty()
            || (options.debater.is_none_or(|id| id == 0)
                && options.name.as_deref().is_none_or(str::is_empty)
                && options.rounds.is_empty())
        {
            return Err(USAGE.into());
        }
        Ok(options)
    }
}

struct Participant {
    name: String,
    aliases: HashSet<String>,
}

struct Round {
    metadata: i64,
    id: i64,
    tournament: i64,
    tournament_name: String,
    label: String,
    number: i64,
    key: String,
    sides: [i64; 2],
    slots: [Option<i64>; 4],
}

struct Source {
    id: i64,
    file: String,
}

#[derive(Clone, Copy)]
struct Evidence<'a> {
    source: &'a Source,
    path: &'a Path,
}

pub(super) async fn generate(state: &AppState, args: &[String]) -> Result<()> {
    if matches!(args, [arg] if matches!(arg.as_str(), "-h" | "--help")) {
        println!("{USAGE}");
        return Ok(());
    }
    let options = Options::parse(args)?;
    let mut tx = read_transaction(&state.pool)
        .await
        .map_err(|err| format!("{err:?}"))?;
    let names = participants(&mut tx).await?;
    let target_ids = if let Some(id) = options.debater {
        Some(HashSet::from([id]))
    } else if let Some(name) = options.name.as_deref().filter(|name| !name.is_empty()) {
        let name = normalize(name);
        Some(
            names
                .iter()
                .filter_map(|(id, participant)| participant.aliases.contains(&name).then_some(*id))
                .collect(),
        )
    } else {
        None
    };
    let mut cache = HashMap::<PathBuf, Vec<backup::RawRound>>::new();
    let mut paths = HashMap::<String, Vec<PathBuf>>::new();
    let mut actions = Vec::new();
    let mut reports = Vec::new();
    for round in target_rounds(&mut tx).await? {
        if !options.rounds.is_empty() && !options.rounds.contains(&round.id) {
            continue;
        }
        if target_ids
            .as_ref()
            .is_some_and(|ids| !round.slots.iter().flatten().any(|id| ids.contains(id)))
        {
            continue;
        }
        let gov = side(&mut tx, round.sides[0], &round.slots[..2]).await?;
        let opp = side(&mut tx, round.sides[1], &round.slots[2..]).await?;
        let sources = sources(&mut tx, round.metadata).await?;
        for source in &sources {
            if !paths.contains_key(&source.file) {
                paths.insert(
                    source.file.clone(),
                    backup::candidates(&options.root, &source.file)?,
                );
            }
        }
        let mut seen = HashSet::new();
        let evidence: Vec<_> = sources
            .iter()
            .flat_map(|source| {
                paths[&source.file]
                    .iter()
                    .map(|path| Evidence { source, path })
            })
            .filter(|entry| entry.path.exists() && seen.insert(entry.path))
            .collect();
        let mut matches = Vec::new();
        let mut name_mismatch = false;
        for entry in &evidence {
            if !cache.contains_key(entry.path) {
                cache.insert(entry.path.to_owned(), backup::load(entry.path)?);
            }
            let matching = cache[entry.path]
                .iter()
                .filter_map(|raw| {
                    if raw.number != round.number {
                        return None;
                    }
                    Some((
                        raw,
                        match_side(&raw.gov, &gov, &names)?,
                        match_side(&raw.opp, &opp, &names)?,
                    ))
                })
                .collect::<Vec<_>>();
            let [(raw, matched_gov, matched_opp)] = matching.as_slice() else {
                continue;
            };
            if let Some(roles) = assigned_slots(raw, *matched_gov, *matched_opp) {
                matches.push((*entry, roles));
            } else {
                name_mismatch = true;
            }
        }
        let (status, first) = if let Some((entry, assignments)) = matches.first().copied() {
            if matches.iter().any(|(_, roles)| *roles != assignments) {
                ("no_unique_backup_match", Some(entry))
            } else if let Some(change) = action(&mut tx, &round, &names, assignments).await? {
                actions.push(change);
                ("patched", Some(entry))
            } else {
                ("already_correct", Some(entry))
            }
        } else if let Some(entry) = evidence.first().copied() {
            (
                if name_mismatch {
                    "unmatched_backup_names"
                } else {
                    "no_unique_backup_match"
                },
                Some(entry),
            )
        } else {
            (
                if sources.is_empty() {
                    "missing_file_backup_import"
                } else {
                    "missing_backup_file"
                },
                None,
            )
        };
        reports.push(report(&round, &names, status, first));
    }
    tx.commit().await?;
    let generated = Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true);
    let target = if let Some(id) = options.debater {
        format!(" debater_id={id}")
    } else if let Some(name) = options.name.as_deref().filter(|name| !name.is_empty()) {
        format!(" debater_name={}", name.trim())
    } else {
        String::new()
    };
    let document = json!({
        "description":format!("Correct inround speaker positions from raw backup files only.{target}"),
        "generated_at":generated,
        "actions":actions,
    });
    let mut summary = json!({"target_rounds": reports.len(), "actions_written": actions.len()});
    for status in SUMMARY {
        summary[status] = json!(reports.iter().filter(|row| row["status"] == status).count());
    }
    write_json(&options.output, &document)?;
    if let Some(path) = &options.report {
        write_json(
            path,
            &json!({
                "generated_at":generated, "source_root":options.root, "summary":summary, "rounds":reports,
            }),
        )?;
    }
    println!(
        "target_rounds={} actions_written={} already_correct={} missing_backup_file={} no_unique_backup_match={} unmatched_backup_names={}",
        summary["target_rounds"],
        summary["actions_written"],
        summary["already_correct"],
        summary["missing_backup_file"],
        summary["no_unique_backup_match"],
        summary["unmatched_backup_names"]
    );
    println!("amendments={}", options.output.display());
    if let Some(path) = &options.report {
        println!("report={}", path.display());
    }
    Ok(())
}

fn write_json(path: &Path, value: &Value) -> Result<()> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, format!("{}\n", serde_json::to_string_pretty(value)?))?;
    Ok(())
}

async fn participants(conn: &mut AnyConnection) -> Result<HashMap<i64, Participant>> {
    let mut participants = HashMap::new();
    for (id, first, last) in sqlx::query_as::<_, (i64, String, String)>(
        "SELECT CAST(id AS BIGINT),first_name,last_name FROM core_debater",
    )
    .fetch_all(&mut *conn)
    .await?
    {
        let name = format!("{first} {last}").trim().to_owned();
        participants.insert(
            id,
            Participant {
                aliases: HashSet::from([normalize(&name)]),
                name,
            },
        );
    }
    for (person, name) in sqlx::query_as::<_, (i64, String)>(
        "SELECT CAST(debater_id AS BIGINT),source_name FROM core_debateralias",
    )
    .fetch_all(conn)
    .await?
    {
        if let Some(participant) = participants.get_mut(&person) {
            participant.aliases.insert(normalize(&name));
        }
    }
    Ok(participants)
}

async fn target_rounds(conn: &mut AnyConnection) -> Result<Vec<Round>> {
    let rows = sqlx::query(
        "SELECT CAST(m.id AS BIGINT) AS metadata,CAST(r.id AS BIGINT) AS id,
         CAST(t.id AS BIGINT) AS tournament,COALESCE(NULLIF(t.manual_name,''),t.name) AS tournament_name,
         r.round_label,CAST(r.round_number AS BIGINT) AS number,r.import_key,
         CAST(r.gov_id AS BIGINT) AS gov,CAST(r.opp_id AS BIGINT) AS opp,
         CAST(g1.debater_id AS BIGINT) AS gov_1,CAST(g2.debater_id AS BIGINT) AS gov_2,
         CAST(o1.debater_id AS BIGINT) AS opp_1,CAST(o2.debater_id AS BIGINT) AS opp_2
         FROM core_importedroundmetadata m JOIN core_round r ON r.id=m.round_id
         JOIN core_tournament t ON t.id=r.tournament_id
         LEFT JOIN core_debateralias g1 ON g1.id=m.gov_1_alias_id
         LEFT JOIN core_debateralias g2 ON g2.id=m.gov_2_alias_id
         LEFT JOIN core_debateralias o1 ON o1.id=m.opp_1_alias_id
         LEFT JOIN core_debateralias o2 ON o2.id=m.opp_2_alias_id
         WHERE r.stage='prelim' ORDER BY t.date,r.round_number,r.id",
    ).fetch_all(conn).await?;
    rows.into_iter()
        .map(|row| {
            Ok(Round {
                metadata: row.try_get("metadata")?,
                id: row.try_get("id")?,
                tournament: row.try_get("tournament")?,
                tournament_name: row.try_get("tournament_name")?,
                label: row.try_get("round_label")?,
                number: row.try_get("number")?,
                key: row.try_get("import_key")?,
                sides: [row.try_get("gov")?, row.try_get("opp")?],
                slots: [
                    row.try_get("gov_1")?,
                    row.try_get("gov_2")?,
                    row.try_get("opp_1")?,
                    row.try_get("opp_2")?,
                ],
            })
        })
        .collect()
}

async fn side(conn: &mut AnyConnection, team: i64, slots: &[Option<i64>]) -> Result<Vec<i64>> {
    let mut players = Vec::new();
    for &id in slots.iter().flatten() {
        if !players.contains(&id) {
            players.push(id);
        }
    }
    if players.is_empty() {
        players = sqlx::query_scalar(
            "SELECT CAST(d.id AS BIGINT) FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id
             WHERE m.team_id=$1 AND NOT (d.temporary OR d.synthetic) ORDER BY d.id",
        ).bind(team).fetch_all(conn).await?;
    }
    Ok(players)
}

async fn sources(conn: &mut AnyConnection, metadata: i64) -> Result<Vec<Source>> {
    let rows = sqlx::query_as::<_, (i64, String)>(
        "SELECT CAST(i.id AS BIGINT),i.original_file_name FROM core_importedroundmetadata_sources l
         JOIN core_tournamentimport i ON i.id=l.tournamentimport_id WHERE l.importedroundmetadata_id=$1
         ORDER BY i.tournament_id,i.imported_at DESC,i.id DESC",
    ).bind(metadata).fetch_all(conn).await?;
    Ok(rows
        .into_iter()
        .map(|(id, file)| Source { id, file })
        .collect())
}

fn normalize(value: &str) -> String {
    value
        .case_fold()
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .filter(|character| *character == ' ' || character.is_alphanumeric())
        .collect()
}

fn role(value: &str) -> Option<usize> {
    ROLES
        .iter()
        .position(|role| value.trim().eq_ignore_ascii_case(role))
}

fn match_side(
    raw: &[String; 2],
    people: &[i64],
    names: &HashMap<i64, Participant>,
) -> Option<[i64; 2]> {
    let &[left, right] = people else {
        return None;
    };
    let first = [left, right]
        .into_iter()
        .filter(|id| !raw[0].is_empty() && names[id].aliases.contains(&raw[0]))
        .collect::<Vec<_>>();
    let &[first] = first.as_slice() else {
        return None;
    };
    let second = if left == first { right } else { left };
    (!raw[1].is_empty() && names[&second].aliases.contains(&raw[1])).then_some([first, second])
}

fn assigned_slots(raw: &backup::RawRound, gov: [i64; 2], opp: [i64; 2]) -> Option<[i64; 4]> {
    let mut roles = [None; 4];
    for (index, (person, name)) in gov
        .into_iter()
        .zip(&raw.gov)
        .chain(opp.into_iter().zip(&raw.opp))
        .enumerate()
    {
        let slot = *raw.roles.get(name)?;
        if slot / 2 != index / 2 || roles[slot].replace(person).is_some() {
            return None;
        }
    }
    if gov.iter().any(|id| opp.contains(id)) {
        return None;
    }
    Some(roles.map(Option::unwrap))
}

async fn action(
    conn: &mut AnyConnection,
    round: &Round,
    names: &HashMap<i64, Participant>,
    slots: [i64; 4],
) -> Result<Option<Value>> {
    let mut update =
        json!({"type":"update_round", "tournament_id":round.tournament, "import_key":round.key});
    if slots.map(Some) != round.slots {
        update["imported_metadata"] = Value::Object(SLOTS.iter().enumerate().map(|(index, slot)| {
            let id = slots[index];
            ((*slot).to_owned(), json!({"debater_id":id, "source_name":names[&id].name, "role":ROLES[index]}))
        }).collect());
    }
    let mut changed = false;
    let mut stats = Vec::new();
    for row in sqlx::query(
        "SELECT CAST(debater_id AS BIGINT) AS debater_id,CAST(speaks AS TEXT) AS speaks,
         CAST(ranks AS TEXT) AS ranks,debater_role,CAST(score_index AS BIGINT) AS score_index,source_status,
         CAST(metadata AS TEXT) AS metadata FROM core_roundstats WHERE round_id=$1 ORDER BY score_index,id",
    ).bind(round.id).fetch_all(conn).await? {
        let debater: i64 = row.try_get("debater_id")?;
        let old: Option<&str> = row.try_get("debater_role")?;
        let role = slots.iter().position(|id| *id == debater).map(|index| ROLES[index]).or(old);
        changed |= old.unwrap_or("") != role.unwrap_or("");
        let number = |field| -> Result<Option<String>> {
            Ok(row.try_get::<Option<&str>, _>(field)?
                .map(str::parse::<Decimal>).transpose()?.map(|value| format!("{value:.4}")))
        };
        let score: i64 = row.try_get("score_index")?;
        let metadata: Value = serde_json::from_str(row.try_get("metadata")?)?;
        let metadata = match &metadata {
            Value::Null => json!({}),
            Value::Array(values) if values.is_empty() => json!({}),
            Value::Bool(false) => json!({}),
            Value::String(value) if value.is_empty() => json!({}),
            Value::Number(value) if value.as_f64() == Some(0.0) => json!({}),
            _ => metadata,
        };
        stats.push(json!({"debater_id":debater, "speaks":number("speaks")?, "ranks":number("ranks")?,
            "debater_role":role, "score_index":if score == 0 {1} else {score},
            "source_status":row.try_get::<&str,_>("source_status")?, "metadata":metadata}));
    }
    if changed {
        update["stats"] = json!(stats);
    }
    Ok((changed || slots.map(Some) != round.slots).then_some(update))
}

fn report(
    round: &Round,
    names: &HashMap<i64, Participant>,
    status: &str,
    evidence: Option<Evidence<'_>>,
) -> Value {
    let slots = Map::from_iter(
        SLOTS
            .iter()
            .zip(round.slots)
            .map(|(&slot, id)| (slot.to_owned(), json!(id.map(|id| &names[&id].name)))),
    );
    json!({
        "status":status, "round_id":round.id, "tournament_id":round.tournament,
        "tournament_name":round.tournament_name, "round_label":round.label, "round_number":round.number,
        "import_key":round.key, "source_import_id":evidence.map(|entry| entry.source.id),
        "source_file_name":evidence.map_or("", |entry| &entry.source.file),
        "source_path":evidence.map_or("", |entry| entry.path.to_str().unwrap_or_default()),
        "current_slots":slots,
    })
}
