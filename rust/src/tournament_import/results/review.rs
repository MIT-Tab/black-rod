use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

use ring::digest;
use serde_json::{Value, json};
use sqlx::{AnyConnection, Row};

use super::forms::{self, DEBATER, Panel, ResultRow, SCHOOL};
use crate::{
    ApiError, AppState, claims, delete_relations, django_json,
    tournament_import::{
        self, Error, as_id, bundle, invalid,
        source::{self, Mappings},
        text,
    },
};

pub(super) struct Review {
    pub(super) token: String,
    pub(super) url: String,
    pub(super) data: Arc<Value>,
    pub(super) mappings: Mappings,
}

fn reference_id(row: &Value) -> String {
    ["tournament_id", "debater_id", "id"]
        .into_iter()
        .map(|key| text(&row[key]))
        .find(|value| !value.is_empty())
        .unwrap_or_default()
}

pub(super) const RESULT_KEYS: [&str; 5] = [
    "varsity_team_placements",
    "varsity_speaker_awards",
    "novice_team_placements",
    "novice_speaker_awards",
    "non_placing_teams",
];

async fn school_by_name(conn: &mut AnyConnection, name: &str) -> Result<Option<i64>, ApiError> {
    Ok(sqlx::query_scalar::<_,i64>("SELECT CAST(id AS BIGINT) FROM core_school WHERE LOWER(name)=LOWER($1) ORDER BY id LIMIT 1")
        .bind(name).fetch_optional(conn).await?)
}

async fn school_lookup(conn: &mut AnyConnection, name: &str) -> Result<Option<i64>, ApiError> {
    Ok(sqlx::query_scalar::<_, i64>(
        "SELECT CAST(school_id AS BIGINT) FROM core_schoollookup WHERE server_name=$1",
    )
    .bind(name)
    .fetch_optional(conn)
    .await?)
}

async fn seed_school(conn: &mut AnyConnection, name: &str) -> Result<i64, ApiError> {
    if let Some(id) = school_by_name(conn, name).await? {
        return Ok(id);
    }
    let id=sqlx::query_scalar::<_,i64>("INSERT INTO core_school (name,short_name,included_in_oty,temporary,synthetic)
        VALUES($1,$2,TRUE,TRUE,FALSE) ON CONFLICT (name) DO UPDATE SET name=excluded.name RETURNING CAST(id AS BIGINT)")
        .bind(name).bind(name.chars().take(64).collect::<String>()).fetch_one(&mut *conn).await?;
    sqlx::query(
        "INSERT INTO core_schoollookup (server_name,school_id) VALUES($1,$2)
        ON CONFLICT(server_name) DO UPDATE SET school_id=excluded.school_id",
    )
    .bind(name)
    .bind(id)
    .execute(conn)
    .await?;
    Ok(id)
}

async fn same_person(
    conn: &mut AnyConnection,
    first: &str,
    last: &str,
    school: Option<i64>,
    season: i32,
) -> Result<Option<i64>, ApiError> {
    let rows=sqlx::query("SELECT CAST(id AS BIGINT) AS id,latest_season,
        CAST(CASE WHEN synthetic THEN 1 ELSE 0 END AS BIGINT) AS synthetic
        FROM core_debater WHERE LOWER(first_name)=LOWER($1) AND LOWER(last_name)=LOWER($2)
        AND (school_id=$3 OR (school_id IS NULL AND CAST($3 AS BIGINT) IS NULL)) ORDER BY temporary,id")
        .bind(first).bind(last).bind(school).fetch_all(conn).await?;
    for row in &rows {
        let recent = row
            .try_get::<Option<&str>, _>("latest_season")?
            .and_then(|season| season.split('-').next()?.parse::<i32>().ok())
            .is_some_and(|year| year >= season - 2);
        if school.is_some() && recent {
            return Ok(Some(row.try_get("id")?));
        }
    }
    for row in rows {
        if row.try_get::<i64, _>("synthetic")? == 0 {
            return Ok(Some(row.try_get("id")?));
        }
    }
    Ok(None)
}

async fn insert_person(
    conn: &mut AnyConnection,
    first: &str,
    last: &str,
    school: Option<i64>,
    season: i32,
    temporary: bool,
) -> Result<i64, ApiError> {
    let first = claims::edit::sanitize_name(first, "Removed");
    let last = claims::edit::sanitize_name(last, "");
    Ok(sqlx::query_scalar::<_,i64>(
        "INSERT INTO core_debater (first_name,last_name,school_id,first_season,latest_season,status,region,
            dino_to_contact_opt_in,dino_judge_contact_opt_in,elo_manual_opt,temporary,synthetic)
        VALUES ($1,$2,$3,$4,$4,$5,'[]',FALSE,FALSE,'',$6,FALSE) RETURNING CAST(id AS BIGINT)")
        .bind(first).bind(last).bind(school).bind(season.to_string())
        .bind(i64::from(!temporary)).bind(temporary).fetch_one(conn).await?)
}

async fn lookup_person(
    conn: &mut AnyConnection,
    reference: &Value,
    mappings: &Mappings,
) -> Result<Option<i64>, ApiError> {
    if let Some(id) = as_id(&reference["apda_id"]).filter(|id| *id > 0) {
        let existing =
            sqlx::query_scalar::<_, i64>("SELECT CAST(id AS BIGINT) FROM core_debater WHERE id=$1")
                .bind(id)
                .fetch_optional(conn)
                .await?;
        if existing.is_some() {
            return Ok(existing);
        }
    }
    Ok(mappings.debaters.get(&reference_id(reference)).copied())
}

pub(super) async fn seed(
    state: &AppState,
    review: &mut Review,
    event: i64,
) -> Result<Vec<Panel>, Error> {
    let mut tx = state.pool.begin().await?;
    sqlx::query("UPDATE core_tournament SET id=id WHERE id=$1")
        .bind(event)
        .execute(&mut *tx)
        .await?;
    let data = &review.data;
    let mut offered_schools = BTreeSet::new();
    let mut created_debaters = BTreeSet::new();
    for row in data["new_schools"].as_array().into_iter().flatten() {
        let name = text(row).trim().to_owned();
        if !name.is_empty() {
            let exact = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM core_school WHERE name=$1",
            )
            .bind(&name)
            .fetch_optional(&mut *tx)
            .await?;
            let id = match exact {
                Some(id) => id,
                None => {
                    let id = seed_school(&mut tx, &name).await?;
                    offered_schools.insert(id);
                    id
                }
            };
            review.mappings.schools.insert(name, id);
        }
    }
    for row in data["new_debater_data"].as_array().into_iter().flatten() {
        let name = text(&row["name"]);
        let mut words = name.split_whitespace();
        let Some(first) = words.next() else { continue };
        let last = words.collect::<Vec<_>>().join(" ");
        if last.is_empty() {
            continue;
        }
        let school = match as_id(&row["school_id"]) {
            Some(-1) => {
                let name = text(&row["school_name"]);
                if let Some(id) = school_lookup(&mut tx, name.trim()).await? {
                    Some(id)
                } else if let Some(&id) = review.mappings.schools.get(name.trim()) {
                    Some(id)
                } else {
                    school_by_name(&mut tx, name.trim()).await?
                }
            }
            Some(id) => {
                sqlx::query_scalar::<_, i64>(
                    "SELECT CAST(id AS BIGINT) FROM core_school WHERE id=$1",
                )
                .bind(id)
                .fetch_optional(&mut *tx)
                .await?
            }
            _ => None,
        };
        let key = reference_id(row);
        let id = if let Some(id) =
            same_person(&mut tx, first, &last, school, state.current_season).await?
        {
            id
        } else {
            let id =
                insert_person(&mut tx, first, &last, school, state.current_season, true).await?;
            created_debaters.insert(id);
            id
        };
        if !key.is_empty() {
            review.mappings.debaters.insert(key, id);
        }
    }
    let mut schools = Panel::new(SCHOOL);
    for row in sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id,name,
         CAST(CASE WHEN included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included,
         CAST(CASE WHEN temporary THEN 1 ELSE 0 END AS BIGINT) AS temporary
         FROM core_school ORDER BY name",
    )
    .fetch_all(&mut *tx)
    .await?
    {
        let id = row.try_get("id")?;
        let eligible = if offered_schools.is_empty() {
            row.try_get::<i64, _>("temporary")? == 1
        } else {
            offered_schools.contains(&id)
        };
        if eligible {
            schools.rows.push(ResultRow {
                record: Some(id),
                name: row.try_get("name")?,
                included: row.try_get::<i64, _>("included")? == 1,
                ..Default::default()
            });
        }
    }
    schools.initial_count = schools.rows.len();
    let mut debaters = Panel::new(DEBATER);
    for row in sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id,first_name,last_name,CAST(school_id AS BIGINT) AS school
         FROM core_debater WHERE temporary ORDER BY id",
    )
    .fetch_all(&mut *tx)
    .await?
    {
        let id = row.try_get("id")?;
        if created_debaters.is_empty() || created_debaters.contains(&id) {
            debaters.rows.push(ResultRow {
                record: Some(id),
                name: row.try_get("first_name")?,
                last_name: row.try_get("last_name")?,
                school: row.try_get("school")?,
                ..Default::default()
            });
        }
    }
    debaters.initial_count = debaters.rows.len();
    let mut panels = Vec::from([schools, debaters]);
    for (tab, key) in forms::TABS.into_iter().zip(RESULT_KEYS) {
        let mut panel = Panel::new(tab);
        for (index, row) in data[key].as_array().into_iter().flatten().enumerate() {
            if tab.kind == "team" {
                let Some([left, right]) = row.as_array().map(Vec::as_slice) else {
                    continue;
                };
                let first = lookup_person(&mut tx, left, &review.mappings).await?;
                let second = lookup_person(&mut tx, right, &review.mappings).await?;
                if first.is_some() && second.is_some() {
                    panel.rows.push(ResultRow {
                        first,
                        second,
                        scoring: true,
                        order: index as i64 + 1,
                        ..Default::default()
                    });
                }
            } else {
                let first = lookup_person(&mut tx, row, &review.mappings).await?;
                if first.is_some() {
                    panel.rows.push(ResultRow {
                        first,
                        scoring: true,
                        order: index as i64 + 1,
                        ..Default::default()
                    });
                }
            }
        }
        panel.initial_count = panel.rows.len();
        panels.push(panel);
    }
    tx.commit().await?;
    source::save_mappings(state, &review.token, review.mappings.clone()).await?;
    Ok(panels)
}

pub(super) async fn school_labels(
    conn: &mut AnyConnection,
    panels: &[Panel],
) -> Result<BTreeMap<String, String>, ApiError> {
    let ids = panels
        .iter()
        .flat_map(|panel| {
            panel.rows.iter().flat_map(move |row| {
                [
                    row.school,
                    if panel.tab.kind == "school" {
                        row.first
                    } else {
                        None
                    },
                ]
            })
        })
        .flatten()
        .collect::<BTreeSet<_>>();
    let mut schools = BTreeMap::new();
    for id in ids {
        let name = sqlx::query_scalar::<_, String>("SELECT name FROM core_school WHERE id=$1")
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
        if let Some(name) = name {
            schools.insert(id.to_string(), name);
        }
    }
    Ok(schools)
}

type Resolution = BTreeMap<i64, Option<i64>>;

async fn erase(conn: &mut AnyConnection, table: &'static str, id: i64) -> Result<(), Error> {
    delete_relations::delete(conn, table, id)
        .await
        .map_err(|error| match error {
            delete_relations::DeleteError::Database(error) => error.into(),
            delete_relations::DeleteError::Absent => {
                invalid("The import review expired. Open it again.")
            }
            delete_relations::DeleteError::Protected => {
                invalid("This record still has linked results.")
            }
        })
}

async fn resolve_schools(
    conn: &mut AnyConnection,
    rows: &[ResultRow],
) -> Result<(Resolution, Vec<i64>), Error> {
    let mut map = Resolution::new();
    let mut cleanup = Vec::new();
    for row in rows {
        let source = row.record;
        let name = if row.name.trim().is_empty() {
            row.first_source.trim()
        } else {
            row.name.trim()
        };
        let short_name = if row.short_name.is_empty() {
            name
        } else {
            row.short_name.as_str()
        };
        if source.is_none() && row.first.is_none() && name.is_empty() {
            continue;
        }
        let status = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(CASE WHEN temporary THEN 1 ELSE 0 END AS BIGINT)
            FROM core_school WHERE id=$1",
        )
        .bind(source)
        .fetch_optional(&mut *conn)
        .await?;
        if row.deleted {
            if let Some(id) = source.filter(|_| status == Some(1)) {
                cleanup.push(id);
                map.insert(id, None);
            }
            continue;
        }
        if let Some(target) = row.first {
            if let Some(id) = source {
                sqlx::query("UPDATE core_schoollookup SET school_id=$1 WHERE school_id=$2")
                    .bind(target)
                    .bind(id)
                    .execute(&mut *conn)
                    .await?;
                if status == Some(1) && id != target {
                    cleanup.push(id);
                }
                map.insert(id, Some(target));
            }
            continue;
        }
        let old = if source.is_some() {
            source
        } else {
            school_by_name(conn, name).await?
        };
        let id = if let Some(id) = old {
            sqlx::query("UPDATE core_school SET name=$1,short_name=$2,included_in_oty=$3,temporary=FALSE WHERE id=$4")
                .bind(name).bind(short_name).bind(row.included).bind(id).execute(&mut *conn).await?;
            id
        } else {
            sqlx::query_scalar::<_, i64>(
                "INSERT INTO core_school (name,short_name,included_in_oty,temporary,synthetic)
                VALUES($1,$2,$3,FALSE,FALSE) RETURNING CAST(id AS BIGINT)",
            )
            .bind(name)
            .bind(short_name)
            .bind(row.included)
            .fetch_one(&mut *conn)
            .await?
        };
        map.insert(source.unwrap_or(id), Some(id));
    }
    Ok((map, cleanup))
}

async fn resolve_people(
    conn: &mut AnyConnection,
    rows: &[ResultRow],
    schools: &Resolution,
    season: i32,
) -> Result<Resolution, Error> {
    let mut map = Resolution::new();
    for row in rows {
        let old = row.record;
        let temporary = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(CASE WHEN temporary THEN 1 ELSE 0 END AS BIGINT)
            FROM core_debater WHERE id=$1",
        )
        .bind(old)
        .fetch_optional(&mut *conn)
        .await?
            == Some(1);
        if row.deleted || row.first.is_some() {
            if let Some(id) = old {
                if temporary && row.first != Some(id) {
                    erase(conn, "core_debater", id).await?;
                }
                map.insert(id, row.first);
            }
            continue;
        }
        let first = claims::edit::sanitize_name(&row.name, "Removed");
        let last = claims::edit::sanitize_name(&row.last_name, "");
        if old.is_none() && row.name.trim().is_empty() {
            continue;
        }
        let school = row
            .school
            .and_then(|id| schools.get(&id).copied().unwrap_or(Some(id)));
        let matched = if old.is_none() {
            same_person(conn, &first, &last, school, season).await?
        } else {
            old
        };
        let id = match matched {
            Some(id) => id,
            None => insert_person(conn, &first, &last, school, season, false).await?,
        };
        sqlx::query("UPDATE core_debater SET first_name=$1,last_name=$2,school_id=$3,temporary=FALSE WHERE id=$4")
            .bind(first).bind(last).bind(school).bind(id).execute(&mut *conn).await?;
        claims::edit::team_names(conn, id).await?;
        map.insert(old.unwrap_or(id), Some(id));
    }
    Ok(map)
}

#[derive(Default)]
pub(super) struct Resolved {
    pub(super) schools: Resolution,
    pub(super) people: Resolution,
}

pub(super) async fn resolve(
    conn: &mut AnyConnection,
    panels: &mut [Panel],
    season: i32,
) -> Result<Resolved, Error> {
    let mut resolved = Resolved::default();
    let mut cleanup = Vec::new();
    for panel in panels {
        match panel.tab.kind {
            "school" => {
                (resolved.schools, cleanup) = resolve_schools(conn, &panel.rows).await?;
            }
            "debater" => {
                resolved.people =
                    resolve_people(conn, &panel.rows, &resolved.schools, season).await?;
            }
            _ => {
                for row in &mut panel.rows {
                    row.first = row
                        .first
                        .and_then(|id| resolved.people.get(&id).copied().unwrap_or(Some(id)));
                    row.second = row
                        .second
                        .and_then(|id| resolved.people.get(&id).copied().unwrap_or(Some(id)));
                }
            }
        }
    }
    for id in cleanup {
        let found = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_debater WHERE school_id=$1 LIMIT 1",
        )
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?;
        if found.is_none() {
            erase(conn, "core_school", id).await?;
        }
    }
    Ok(resolved)
}

pub(super) async fn counts(
    conn: &mut AnyConnection,
    event: i64,
    review: &Review,
) -> Result<(), Error> {
    let counts = &review.data["debater_counts"];
    sqlx::query("UPDATE core_tournament SET num_teams=COALESCE($1,num_teams),num_novice_debaters=COALESCE($2,num_novice_debaters)
        WHERE id=$3")
        .bind(as_id(&counts["teams"]).filter(|count|*count>=0))
        .bind(as_id(&counts["novice"]).filter(|count|*count>=0))
        .bind(event).execute(conn).await?;
    Ok(())
}

pub(super) async fn rounds(
    conn: &mut AnyConnection,
    event: i64,
    season: i32,
    review: &Review,
    resolved: &Resolved,
) -> Result<bundle::Summary, Error> {
    let mut document = review.data["bundle"].clone();
    if !tournament_import::truthy(&document) {
        return Ok(bundle::Summary::default());
    }
    if let Some(rows) = document["debaters"].as_array_mut() {
        for row in rows {
            let id = review
                .mappings
                .debaters
                .get(&text(&row["id"]))
                .copied()
                .or_else(|| as_id(&row["apda_id"]))
                .filter(|id| *id > 0);
            if let Some(id) =
                id.and_then(|id| resolved.people.get(&id).copied().unwrap_or(Some(id)))
            {
                let valid=sqlx::query_scalar::<_,i64>("SELECT CAST(id AS BIGINT) FROM core_debater WHERE id=$1 AND NOT(temporary OR synthetic)")
                    .bind(id).fetch_optional(&mut *conn).await?;
                if valid.is_some() {
                    row["apda_id"] = json!(id);
                }
            }
        }
    }
    if let Some(rows) = document["schools"].as_array_mut() {
        for row in rows {
            let id = review
                .mappings
                .schools
                .get(&text(&row["name"]))
                .copied()
                .or_else(|| as_id(&row["apda_id"]));
            if let Some(id) = id.and_then(|id| resolved.schools.get(&id).copied().flatten()) {
                row["apda_id"] = json!(id);
            }
        }
    }
    let payload = django_json::encode(&document).map_err(|_| ApiError::Serialization)?;
    let hash = hex::encode(digest::digest(&digest::SHA256, payload.as_bytes()));
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_tournamentimport
        WHERE tournament_id=$1 AND source_hash=$2",
    )
    .bind(event)
    .bind(&hash)
    .fetch_optional(&mut *conn)
    .await?;
    if existing.is_some() {
        return Ok(bundle::Summary::default());
    }
    bundle::within(conn, event, season, &document, "api-bundle.json", &hash).await
}
