use chrono::{SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};
use unicode_casefold::UnicodeCaseFold;

use super::{Error, as_id, invalid, text};
use crate::{agent::journal::TABLES, claims};

pub(super) fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

fn split(name: &str) -> Result<(&str, String), Error> {
    let mut words = name.split_whitespace();
    let first = words
        .next()
        .ok_or_else(|| invalid("Encountered a blank participant name in the Mit-Tab bundle."))?;
    Ok((first, words.collect::<Vec<_>>().join(" ")))
}

#[derive(Clone)]
pub(super) struct Person {
    pub(super) id: i64,
    pub(super) name: String,
}

struct Match {
    person: Person,
    synthetic: bool,
    awarded: bool,
}

async fn exact(conn: &mut AnyConnection, name: &str, tournament: i64) -> Result<Vec<Match>, Error> {
    let (first, last) = split(name)?;
    let rows=sqlx::query("SELECT CAST(d.id AS BIGINT) AS id,
        TRIM(d.first_name || ' ' || d.last_name) AS name,
        CAST(CASE WHEN d.synthetic THEN 1 ELSE 0 END AS BIGINT) AS synthetic,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM core_team_debaters m JOIN core_teamresult r ON r.team_id=m.team_id
            WHERE m.debater_id=d.id AND r.tournament_id=$3) THEN 1 ELSE 0 END AS BIGINT) AS awarded
        FROM core_debater d WHERE LOWER(d.first_name)=LOWER($1) AND LOWER(d.last_name)=LOWER($2) ORDER BY d.id")
        .bind(first).bind(last).bind(tournament).fetch_all(conn).await?;
    rows.into_iter()
        .map(|row| {
            Ok(Match {
                person: Person {
                    id: row.try_get("id")?,
                    name: row.try_get("name")?,
                },
                synthetic: row.try_get::<i64, _>("synthetic")? == 1,
                awarded: row.try_get::<i64, _>("awarded")? == 1,
            })
        })
        .collect()
}

async fn school(conn: &mut AnyConnection, payload: &Value) -> Result<Option<i64>, Error> {
    if let Some(apda) = as_id(&payload["apda_id"])
        && let Some(id) =
            sqlx::query_scalar::<_, i64>("SELECT CAST(id AS BIGINT) FROM core_school WHERE id=$1")
                .bind(apda)
                .fetch_optional(&mut *conn)
                .await?
    {
        return Ok(Some(id));
    }
    let name = text(&payload["name"]).trim().to_owned();
    if name.is_empty() {
        return Ok(None);
    }
    if let Some(id)=sqlx::query_scalar::<_,i64>("SELECT CAST(id AS BIGINT) FROM core_school WHERE LOWER(name)=LOWER($1) ORDER BY id LIMIT 1")
        .bind(&name).fetch_optional(&mut *conn).await? {
        return Ok(Some(id));
    }
    let short_name = name.chars().take(64).collect::<String>();
    let fields = Map::from_iter([
        ("name".to_owned(), json!(name)),
        ("short_name".to_owned(), json!(short_name)),
        ("temporary".to_owned(), json!(true)),
        ("synthetic".to_owned(), json!(false)),
        ("included_in_oty".to_owned(), json!(true)),
    ]);
    Ok(Some(
        TABLES["core.School"].write(conn, &fields, None).await?,
    ))
}

async fn synthetic(
    conn: &mut AnyConnection,
    name: &str,
    school: Option<i64>,
    season: i32,
) -> Result<Person, Error> {
    let (first, last) = split(name)?;
    let first = claims::edit::sanitize_name(first, "Removed");
    let last = claims::edit::sanitize_name(&last, "");
    let values = Map::from_iter([
        ("first_name".to_owned(), json!(first)),
        ("last_name".to_owned(), json!(last)),
        ("school_id".to_owned(), json!(school)),
        ("alias_group_id".to_owned(), Value::Null),
        ("user_id".to_owned(), Value::Null),
        ("email".to_owned(), Value::Null),
        ("paradigm".to_owned(), Value::Null),
        ("first_season".to_owned(), json!(season.to_string())),
        ("latest_season".to_owned(), json!(season.to_string())),
        ("status".to_owned(), json!(1)),
        ("region".to_owned(), json!([])),
        ("dino_to_contact_opt_in".to_owned(), json!(false)),
        ("dino_judge_contact_opt_in".to_owned(), json!(false)),
        ("elo_manual_opt".to_owned(), json!("")),
        ("temporary".to_owned(), json!(false)),
        ("synthetic".to_owned(), json!(true)),
    ]);
    let id = TABLES["core.Debater"].write(conn, &values, None).await?;
    Ok(Person {
        id,
        name: format!("{first} {last}").trim().to_owned(),
    })
}

pub(super) async fn competitor(
    conn: &mut AnyConnection,
    tournament: i64,
    payload: &Value,
    school_payload: Result<Option<&Value>, Error>,
    season: i32,
) -> Result<Person, Error> {
    if let Some(apda) = as_id(&payload["apda_id"])
        && let Some(name) = sqlx::query_scalar::<_, String>(
            "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1",
        )
        .bind(apda)
        .fetch_optional(&mut *conn)
        .await?
    {
        return Ok(Person { id: apda, name });
    }
    let name = text(&payload["name"]);
    let mut choices = exact(conn, &name, tournament).await?;
    if choices.len() > 1 && choices.iter().any(|candidate| candidate.awarded) {
        choices.retain(|candidate| candidate.awarded);
    }
    if let Some(selected) = choices
        .iter()
        .find(|candidate| !candidate.synthetic)
        .or_else(|| choices.first())
    {
        return Ok(selected.person.clone());
    }
    let school = if let Some(payload) = school_payload? {
        school(conn, payload).await?
    } else {
        None
    };
    synthetic(conn, &name, school, season).await
}

pub(super) async fn judge(
    conn: &mut AnyConnection,
    tournament: i64,
    name: &str,
    season: i32,
) -> Result<i64, Error> {
    let people = exact(conn, name, tournament).await?;
    if let [person] = people.as_slice() {
        return Ok(person.person.id);
    }
    let aliases = sqlx::query_scalar::<_, i64>(
        "SELECT DISTINCT CAST(d.id AS BIGINT)
        FROM core_debateralias a JOIN core_debater d ON d.id=a.debater_id
        WHERE d.synthetic AND LOWER(a.source_name)=LOWER($1) LIMIT 2",
    )
    .bind(name)
    .fetch_all(&mut *conn)
    .await?;
    if let [person] = aliases.as_slice() {
        return Ok(*person);
    }
    Ok(synthetic(conn, name, None, season).await?.id)
}

pub(super) async fn alias(conn: &mut AnyConnection, person: i64, name: &str) -> Result<i64, Error> {
    if name.is_empty() {
        return Err(invalid(
            "Alias payloads require a source_name or a debater with a name.",
        ));
    }
    let normalized = name.case_fold().collect::<String>();
    let existing = sqlx::query(
        "SELECT CAST(id AS BIGINT) AS id,normalized_name FROM core_debateralias
        WHERE debater_id=$1 AND source_name=$2",
    )
    .bind(person)
    .bind(name)
    .fetch_optional(&mut *conn)
    .await?;
    let instant = now();
    if let Some(row) = existing {
        let id = row.try_get("id")?;
        if row.try_get::<&str, _>("normalized_name")? != normalized {
            TABLES["core.DebaterAlias"]
                .write(
                    conn,
                    &Map::from_iter([
                        ("normalized_name".to_owned(), json!(normalized)),
                        ("updated_at".to_owned(), json!(instant)),
                    ]),
                    Some(id),
                )
                .await?;
        }
        return Ok(id);
    }
    let fields = Map::from_iter([
        ("debater_id".to_owned(), json!(person)),
        ("source_name".to_owned(), json!(name)),
        ("normalized_name".to_owned(), json!(normalized)),
        ("created_at".to_owned(), json!(instant)),
        ("updated_at".to_owned(), json!(instant)),
    ]);
    Ok(TABLES["core.DebaterAlias"]
        .write(conn, &fields, None)
        .await?)
}

pub(super) async fn team(conn: &mut AnyConnection, roster: &[i64]) -> Result<i64, Error> {
    let query=match roster {
        [_] => "SELECT CAST(m.team_id AS BIGINT) FROM core_team_debaters m
            WHERE m.debater_id=$1 AND (SELECT COUNT(*) FROM core_team_debaters t WHERE t.team_id=m.team_id)=1
            ORDER BY m.team_id LIMIT 1",
        [_,_]=>"SELECT CAST(a.team_id AS BIGINT) FROM core_team_debaters a JOIN core_team_debaters b ON a.team_id=b.team_id
            WHERE a.debater_id=$1 AND b.debater_id=$2 ORDER BY a.team_id LIMIT 1",
        _=>return Err(invalid("A round requires one or two debater IDs per side.")),
    };
    let mut query = sqlx::query_scalar::<_, i64>(query).bind(roster[0]);
    if let Some(second) = roster.get(1) {
        query = query.bind(second);
    }
    if let Some(id) = query.fetch_optional(&mut *conn).await? {
        return Ok(id);
    }
    let id=sqlx::query_scalar::<_,i64>("INSERT INTO core_team (name,short_name,synthetic) VALUES ('','',FALSE) RETURNING CAST(id AS BIGINT)")
        .fetch_one(&mut *conn).await?;
    for (index, &member) in roster.iter().enumerate() {
        if index == 0 || member != roster[0] {
            sqlx::query("INSERT INTO core_team_debaters (team_id,debater_id) VALUES ($1,$2)")
                .bind(id)
                .bind(member)
                .execute(&mut *conn)
                .await?;
        }
    }
    claims::edit::team_name(conn, id).await?;
    Ok(id)
}
