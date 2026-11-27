use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs,
    path::Path,
};

use serde_json::{Value, json};
use sqlx::{AssertSqlSafe, Row, any::AnyRow};

use crate::{AppState, schedule, team_profile};

use super::Result;

#[derive(Clone)]
struct Person {
    id: i64,
    first: String,
    last: String,
    first_season: Option<String>,
    latest_season: Option<String>,
    status: i64,
    school: Option<i64>,
    alias: Option<i64>,
    canonical: bool,
}

impl Person {
    fn load(row: AnyRow) -> Result<Self> {
        Ok(Self {
            id: row.try_get("id")?,
            first: row.try_get("first_name")?,
            last: row.try_get("last_name")?,
            first_season: row.try_get("first_season")?,
            latest_season: row.try_get("latest_season")?,
            status: row.try_get("status")?,
            school: row.try_get("school_id")?,
            alias: row.try_get("alias_group_id")?,
            canonical: row.try_get::<i64, _>("canonical")? != 0,
        })
    }

    fn name(&self) -> String {
        format!("{} {}", self.first, self.last).trim().to_owned()
    }

    fn name_key(&self) -> String {
        self.name()
            .to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    }

    fn status(&self) -> Value {
        label(self.status, &[(0, "Novice"), (1, "Varsity"), (2, "Dino")])
    }
}

#[derive(Clone)]
struct Team {
    id: i64,
    name: String,
    short_name: String,
    roster: Vec<i64>,
}

struct School {
    id: i64,
    name: String,
    short_name: String,
    included: bool,
    temporary: bool,
}

type IdMap<T> = BTreeMap<i64, T>;

fn label(value: i64, options: &[(i64, &str)]) -> Value {
    options
        .iter()
        .find(|(code, _)| *code == value)
        .map_or_else(|| json!(value), |(_, name)| json!(name))
}

fn path(resource: &str, id: i64) -> String {
    format!("/core/{resource}/{id}")
}

fn qualification_label(value: i64) -> Value {
    match value {
        0 => json!("Points"),
        11..=14 => json!(value),
        value => schedule::qualification_label(value),
    }
}

fn number(row: &AnyRow, key: &str) -> Result<Option<i64>> {
    Ok(row.try_get::<Option<i64>, _>(key)?)
}
fn text(row: &AnyRow, key: &str) -> Result<Option<String>> {
    Ok(row.try_get::<Option<String>, _>(key)?)
}
fn yes(row: &AnyRow, key: &str) -> Result<bool> {
    Ok(row.try_get::<i64, _>(key)? == 1)
}

fn columns(integers: &[&str], booleans: &[&str]) -> String {
    fn column(expression: &str) -> &str {
        expression.rsplit('.').next().unwrap_or(expression)
    }
    integers
        .iter()
        .map(|name| format!("CAST({name} AS BIGINT) AS {}", column(name)))
        .chain(booleans.iter().map(|name| {
            format!(
                "CAST(CASE WHEN {name} THEN 1 ELSE 0 END AS BIGINT) AS {}",
                column(name)
            )
        }))
        .collect::<Vec<_>>()
        .join(",")
}

pub(super) async fn export(state: &AppState, args: &[String]) -> Result<()> {
    let mut season = "2021";
    let mut file = "canonical_import_export.json";
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        let (key, inline) = arg
            .split_once('=')
            .map_or((arg.as_str(), None), |(key, value)| (key, Some(value)));
        let value = inline
            .or_else(|| args.next().map(String::as_str))
            .ok_or_else(|| format!("Supply a value for {key}"))?;
        match key {
            "--starting-season" => season = value,
            "--output" => file = value,
            _ => return Err(format!("Unknown canonical export option: {key}").into()),
        }
    }
    if season.trim().parse::<i64>().is_err() {
        return Err(format!("Invalid --starting-season value '{season}'. Expected a four-digit season key like 2021.").into());
    }
    let output = Path::new(file);
    if let Some(parent) = output.parent().filter(|path| !path.as_os_str().is_empty()) {
        fs::create_dir_all(parent)?;
    }
    let payload = payload(state, season).await?;
    fs::write(
        output,
        crate::django_json::ascii(&serde_json::to_string_pretty(&payload)?),
    )?;
    let totals = &payload["counts"];
    println!(
        "Exported canonical import data to {} ({} tournaments, {} debaters, {} teams)",
        output.display(),
        totals["tournaments"],
        totals["debaters"],
        totals["teams"]
    );
    Ok(())
}

async fn payload(state: &AppState, season: &str) -> Result<Value> {
    // Group all related rows in one snapshot so public writers cannot split an export.
    let mut tx = team_profile::read_transaction(&state.pool)
        .await
        .map_err(|_| "Could not begin the export snapshot.")?;
    let mut people = IdMap::new();
    let mut names = HashMap::<String, Vec<i64>>::new();
    let mut groups = IdMap::<Vec<i64>>::new();
    let mut seed = BTreeSet::new();
    let people_columns = columns(&["id", "school_id", "alias_group_id", "status"], &[]);
    let people_query = format!(
        "SELECT {people_columns},first_name,last_name,first_season,latest_season,
         CAST(CASE WHEN NOT temporary AND NOT synthetic THEN 1 ELSE 0 END AS BIGINT) AS canonical
         FROM core_debater ORDER BY id"
    );
    for row in sqlx::query(AssertSqlSafe(people_query))
        .fetch_all(&mut *tx)
        .await?
    {
        let person = Person::load(row)?;
        if person.canonical {
            names.entry(person.name_key()).or_default().push(person.id);
            if let Some(group) = person.alias {
                groups.entry(group).or_default().push(person.id);
            }
            if person
                .latest_season
                .as_deref()
                .is_some_and(|latest| latest >= season)
            {
                seed.insert(person.id);
            }
        }
        people.insert(person.id, person);
    }

    let mut teams = IdMap::new();
    for (id, name, short_name) in sqlx::query_as::<_, (i64, String, String)>(
        "SELECT CAST(id AS BIGINT),name,short_name FROM core_team ORDER BY id",
    )
    .fetch_all(&mut *tx)
    .await?
    {
        teams.insert(
            id,
            Team {
                id,
                name,
                short_name,
                roster: Vec::new(),
            },
        );
    }
    for (id,member) in sqlx::query_as::<_,(i64,i64)>(
        "SELECT CAST(team_id AS BIGINT),CAST(debater_id AS BIGINT) FROM core_team_debaters ORDER BY debater_id",
    ).fetch_all(&mut *tx).await? {
        if people.get(&member).is_some_and(|person|person.canonical) && let Some(team) = teams.get_mut(&id) {
            team.roster.push(member);
        }
    }

    let mut schools = IdMap::new();
    let school_columns = columns(&["id"], &["included_in_oty", "temporary"]);
    let school_query =
        format!("SELECT {school_columns},name,short_name FROM core_school ORDER BY id");
    for row in sqlx::query(AssertSqlSafe(school_query))
        .fetch_all(&mut *tx)
        .await?
    {
        let school = School {
            id: row.try_get("id")?,
            name: row.try_get("name")?,
            short_name: row.try_get("short_name")?,
            included: yes(&row, "included_in_oty")?,
            temporary: yes(&row, "temporary")?,
        };
        schools.insert(school.id, school);
    }

    let mut speaker_results = IdMap::<Vec<AnyRow>>::new();
    let speaker_columns = columns(
        &[
            "r.id",
            "r.tournament_id",
            "r.debater_id",
            "r.type_of_place",
            "r.place",
        ],
        &["r.tie", "r.counts_for_points"],
    );
    let speaker_query = format!(
        "SELECT {speaker_columns} FROM core_speakerresult r JOIN core_tournament t ON t.id=r.tournament_id
           WHERE t.season >= $1 ORDER BY r.tournament_id,r.type_of_place,r.place,r.id"
    );
    for row in sqlx::query(AssertSqlSafe(speaker_query))
        .bind(season)
        .fetch_all(&mut *tx)
        .await?
    {
        let id: i64 = row.try_get("debater_id")?;
        if people[&id].canonical {
            seed.insert(id);
        }
        speaker_results
            .entry(row.try_get("tournament_id")?)
            .or_default()
            .push(row);
    }
    let mut included_teams = BTreeSet::new();
    let mut team_results = IdMap::<Vec<AnyRow>>::new();
    let result_columns = columns(
        &[
            "r.id",
            "r.tournament_id",
            "r.team_id",
            "r.type_of_place",
            "r.place",
        ],
        &["r.ghost_points", "r.counts_for_points"],
    );
    let result_query = format!(
        "SELECT {result_columns} FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
         WHERE t.season >= $1 ORDER BY r.tournament_id,r.type_of_place,r.place,r.id"
    );
    for row in sqlx::query(AssertSqlSafe(result_query))
        .bind(season)
        .fetch_all(&mut *tx)
        .await?
    {
        let id: i64 = row.try_get("team_id")?;
        included_teams.insert(id);
        seed.extend(teams[&id].roster.iter().copied());
        team_results
            .entry(row.try_get("tournament_id")?)
            .or_default()
            .push(row);
    }

    let included_people = seed
        .iter()
        .flat_map(|id| {
            let person = &people[id];
            names
                .get(&person.name_key())
                .into_iter()
                .flatten()
                .chain(
                    person
                        .alias
                        .and_then(|alias| groups.get(&alias))
                        .into_iter()
                        .flatten(),
                )
                .copied()
        })
        .collect::<BTreeSet<_>>();
    let mut school_ids = included_people
        .iter()
        .filter_map(|id| people[id].school)
        .collect::<BTreeSet<_>>();
    let group_ids = included_people
        .iter()
        .filter_map(|id| people[id].alias)
        .collect::<BTreeSet<_>>();
    included_teams.extend(
        teams
            .values()
            .filter(|team| team.roster.iter().any(|id| included_people.contains(id)))
            .map(|team| team.id),
    );

    let school_name = |id: Option<i64>| {
        id.and_then(|key| schools.get(&key))
            .map(|school| &school.name)
    };
    let person_detail = |id: &i64| {
        let person = &people[id];
        json!({
           "id":id,"first_name":person.first,"last_name":person.last,"name":person.name(),
           "first_season":person.first_season,"latest_season":person.latest_season,
           "year_start":person.first_season,"year_end":person.latest_season,"status":person.status,
           "status_display":person.status(),"school_id":person.school,"alias_group_id":person.alias,
           "profile_path":path("debaters", *id),
           "same_name_debater_ids":names.get(&person.name_key()).map(Vec::as_slice).unwrap_or_default(),
           "linked_debater_ids":person.alias.and_then(|alias|groups.get(&alias)).map(Vec::as_slice).unwrap_or_default(),
           "school_name":school_name(person.school),"school_profile_path":person.school.map(|id|path("schools",id)),
        })
    };
    let tournament_columns = columns(
        &[
            "id",
            "host_id",
            "num_rounds",
            "num_teams",
            "num_novice_teams",
            "num_debaters",
            "num_novice_debaters",
            "autoqual_bar",
        ],
        &["qual", "noty", "soty", "toty", "online_qual_points"],
    );
    let tournament_query = format!(
        "SELECT {tournament_columns},name,short_name,manual_name,season,
        CAST(date AS TEXT) AS date FROM core_tournament WHERE season >= $1 ORDER BY season,date,id"
    );
    let mut tournaments = Vec::new();
    for row in sqlx::query(AssertSqlSafe(tournament_query))
        .bind(season)
        .fetch_all(&mut *tx)
        .await?
    {
        let id: i64 = row.try_get("id")?;
        let host = number(&row, "host_id")?;
        school_ids.extend(host);
        let mut speakers = Vec::new();
        for result in speaker_results.remove(&id).unwrap_or_default() {
            let debater: i64 = result.try_get("debater_id")?;
            let person = &people[&debater];
            let kind: i64 = result.try_get("type_of_place")?;
            speakers.push(json!({
                "id":number(&result,"id")?,"debater_id":debater,"debater_name":person.name(),
                "debater_school_id":person.school,"debater_school_name":school_name(person.school),
                "debater_status":person.status,"debater_status_display":person.status(),"debater_profile_path":path("debaters",debater),
                "debater_school_profile_path":person.school.map(|id|path("schools",id)),
                "debater_first_season":person.first_season,"debater_latest_season":person.latest_season,"debater_in_export":included_people.contains(&debater),
                "type_of_place":kind,"type_of_place_display":label(kind,&[(0,"Novice"),(1,"Varsity")]),
                "place":number(&result,"place")?,"tie":yes(&result,"tie")?,"counts_for_points":yes(&result,"counts_for_points")?,
            }));
        }
        let mut awards = Vec::new();
        for result in team_results.remove(&id).unwrap_or_default() {
            let team = &teams[&result.try_get::<i64, _>("team_id")?];
            let kind: i64 = result.try_get("type_of_place")?;
            awards.push(json!({
                "id":number(&result,"id")?,"team_id":team.id,"team_name":team.name,"team_short_name":team.short_name,
                "team_profile_path":path("teams",team.id),"type_of_place":kind,
                "type_of_place_display":label(kind,&[(0,"Novice"),(1,"Varsity")]),"place":number(&result,"place")?,
                "ghost_points":yes(&result,"ghost_points")?,"counts_for_points":yes(&result,"counts_for_points")?,
                "debaters":team.roster.iter().map(&person_detail).collect::<Vec<_>>(),
            }));
        }
        tournaments.push(json!({
            "id":id,"name":text(&row,"name")?,"short_name":text(&row,"short_name")?,"manual_name":text(&row,"manual_name")?,
            "date":text(&row,"date")?,"season":text(&row,"season")?,"host_id":host,"host_name":school_name(host),
            "host_profile_path":host.map(|id|path("schools",id)),
            "num_rounds":number(&row,"num_rounds")?,"num_teams":number(&row,"num_teams")?,"num_novice_teams":number(&row,"num_novice_teams")?,
            "num_debaters":number(&row,"num_debaters")?,"num_novice_debaters":number(&row,"num_novice_debaters")?,
            "qual":yes(&row,"qual")?,"noty":yes(&row,"noty")?,"soty":yes(&row,"soty")?,"toty":yes(&row,"toty")?,
            "online_qual_points":yes(&row,"online_qual_points")?,"autoqual_bar":number(&row,"autoqual_bar")?,
            "profile_path":path("tournaments",id),"speaker_results":speakers,"team_results":awards,
        }));
    }
    let mut quals = Vec::new();
    for (id,debater,season,kind,tournament,tournament_name) in sqlx::query_as::<_,(i64,i64,String,i64,Option<i64>,Option<String>)>(
        "SELECT CAST(q.id AS BIGINT),CAST(q.debater_id AS BIGINT),q.season,CAST(q.qual_type AS BIGINT),CAST(q.tournament_id AS BIGINT),t.name
         FROM core_qual q LEFT JOIN core_tournament t ON t.id=q.tournament_id ORDER BY q.season,q.debater_id,q.qual_type,q.id",
    ).fetch_all(&mut *tx).await? {
        if included_people.contains(&debater) {
            quals.push(json!({"id":id,"debater_id":debater,"season":season,"qual_type":kind,
                "qual_type_display":qualification_label(kind),"tournament_id":tournament,"tournament_name":tournament_name,
                "tournament_profile_path":tournament.map(|id|path("tournaments",id))}));
        }
    }
    let mut school_lookups = Vec::new();
    for (id,server,school) in sqlx::query_as::<_,(i64,String,i64)>(
        "SELECT CAST(id AS BIGINT),server_name,CAST(school_id AS BIGINT) FROM core_schoollookup ORDER BY server_name,id",
    ).fetch_all(&mut *tx).await? {
        if school_ids.contains(&school) {
            school_lookups.push(json!({"id":id,"server_name":server,"school_id":school,"school_name":school_name(Some(school))}));
        }
    }
    let mut alias_groups = Vec::new();
    for (id, label) in sqlx::query_as::<_, (i64, String)>(
        "SELECT CAST(id AS BIGINT),label FROM core_debateraliasgroup ORDER BY id",
    )
    .fetch_all(&mut *tx)
    .await?
    {
        if group_ids.contains(&id) {
            let name = if label.is_empty() {
                format!("Alias Group #{id}")
            } else {
                label.clone()
            };
            alias_groups.push(json!({"id":id,"label":label,"name":name}));
        }
    }
    tx.commit().await?;
    let schools = schools
        .values()
        .filter(|school| school_ids.contains(&school.id) && !school.temporary)
        .map(|school| {
            json!({"id":school.id,"name":school.name,"short_name":school.short_name,
            "included_in_oty":school.included,"profile_path":path("schools",school.id)})
        })
        .collect::<Vec<_>>();
    let debaters = included_people
        .iter()
        .map(&person_detail)
        .collect::<Vec<_>>();
    let teams = teams.values().filter(|team|included_teams.contains(&team.id))
        .map(|team|json!({"id":team.id,"name":team.name,"short_name":team.short_name,"profile_path":path("teams",team.id),
            "debaters":team.roster.iter().map(&person_detail).collect::<Vec<_>>()})).collect::<Vec<_>>();
    Ok(json!({
        "schema_version":2,"exported_at":chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros,false),"starting_season":season,
        "counts":{"tournaments":tournaments.len(),"schools":schools.len(),"school_lookups":school_lookups.len(),
            "debater_alias_groups":alias_groups.len(),"seed_debaters":seed.len(),"debaters":debaters.len(),
            "teams":teams.len(),"quals":quals.len()},
        "schools":schools,"school_lookups":school_lookups,"debater_alias_groups":alias_groups,
        "debaters":debaters,"teams":teams,"quals":quals,"tournaments":tournaments,
    }))
}
