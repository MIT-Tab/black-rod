use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};

use super::{LABELS, bonus, rank, ranked, year};
use crate::{ApiError, agent::journal::TABLES, delete_relations, points};

const DEBATERS: &str = "
WITH teams AS (
    SELECT id FROM core_team t WHERE EXISTS (
        SELECT 1 FROM core_teamresult r JOIN core_tournament e ON e.id=r.tournament_id
        WHERE r.team_id=t.id AND e.season=$1
    ) OR EXISTS (
        SELECT 1 FROM core_team_debaters m WHERE m.team_id=t.id AND (
            EXISTS(SELECT 1 FROM core_qualpoints p WHERE p.debater_id=m.debater_id AND p.season=$1)
            OR EXISTS(SELECT 1 FROM core_qual q WHERE q.debater_id=m.debater_id AND q.season=$1)
            OR EXISTS(SELECT 1 FROM core_onlinequal o WHERE o.debater_id=m.debater_id AND o.season=$1)
        )
    )
), related AS (
    SELECT m.debater_id,
        MAX(CASE WHEN EXISTS (SELECT 1 FROM core_teamresult r WHERE r.team_id=m.team_id)
            THEN 1 ELSE 0 END) AS has_results,
        MAX(CASE WHEN EXISTS (SELECT 1 FROM core_teamresult r WHERE r.team_id=m.team_id)
            OR EXISTS (SELECT 1 FROM core_round r WHERE r.gov_id=m.team_id OR r.opp_id=m.team_id)
            THEN 1 ELSE 0 END) AS has_online
    FROM teams t JOIN core_team_debaters m ON m.team_id=t.id GROUP BY m.debater_id
)
SELECT CAST(d.id AS BIGINT) AS id, d.latest_season,CAST(d.school_id AS BIGINT) AS school,
    CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included,
    CAST(CASE WHEN COALESCE(related.has_results,0)=1 OR (
        NOT EXISTS (
            SELECT 1 FROM core_team_debaters m JOIN core_teamresult r ON r.team_id=m.team_id
            JOIN core_tournament t ON t.id=r.tournament_id WHERE m.debater_id=d.id AND t.season=$1
        ) AND (
            EXISTS (SELECT 1 FROM core_tournamentautoqual a
                JOIN core_tournament t ON t.id=a.tournament_id WHERE a.debater_id=d.id AND t.season=$1)
            OR EXISTS (SELECT 1 FROM core_qual q WHERE q.debater_id=d.id AND q.season=$1)
            OR EXISTS (SELECT 1 FROM core_qualpoints q WHERE q.debater_id=d.id AND q.season=$1)
        )
    ) THEN 1 ELSE 0 END AS BIGINT) AS recalc,
    CAST(COALESCE(related.has_online,0) AS BIGINT) AS recalc_online
FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
LEFT JOIN related ON related.debater_id=d.id
WHERE NOT (d.temporary OR d.synthetic) ORDER BY d.id";

const RESULTS: &str = "
SELECT CAST(r.id AS BIGINT) AS id, CAST(m.debater_id AS BIGINT) AS person,CAST(t.id AS BIGINT) AS event,
    CAST(t.qual_type AS BIGINT) AS kind,CAST(t.autoqual_bar AS BIGINT) AS bar,CAST(t.num_teams AS BIGINT) AS teams,
    CAST(r.place AS BIGINT) AS place,CAST(CASE WHEN r.counts_for_points THEN 1 ELSE 0 END AS BIGINT) AS scoring,
    CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
    CAST(CASE WHEN t.qual THEN 1 ELSE 0 END AS BIGINT) AS qual,
    CAST(CASE WHEN t.online_qual_points THEN 1 ELSE 0 END AS BIGINT) AS online
FROM core_teamresult r JOIN core_tournament t ON t.id=r.tournament_id
JOIN core_team_debaters m ON m.team_id=r.team_id
WHERE t.season=$1 AND r.type_of_place=1 ORDER BY r.id";

async fn add_qual(
    conn: &mut AnyConnection,
    season: &str,
    debater: i64,
    kind: i64,
    tournament: Option<i64>,
    attach_existing: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO core_qual (season, debater_id, qual_type, tournament_id, place,points,tied)
        VALUES ($1,$2,$3,$4,-1,-1,FALSE) ON CONFLICT(season,debater_id,qual_type) DO NOTHING",
    )
    .bind(season)
    .bind(debater)
    .bind(kind)
    .bind(tournament)
    .execute(&mut *conn)
    .await?;
    if attach_existing && tournament.is_some() {
        sqlx::query("UPDATE core_qual SET tournament_id=$4 WHERE season=$1 AND debater_id=$2 AND qual_type=$3 AND tournament_id IS NULL")
            .bind(season).bind(debater).bind(kind).bind(tournament).execute(conn).await?;
    }
    Ok(())
}

pub(super) async fn recompute(
    conn: &mut AnyConnection,
    season: &str,
    current: bool,
) -> Result<(), ApiError> {
    recompute_for(conn, season, current, None).await
}

async fn recompute_for(
    conn: &mut AnyConnection,
    season: &str,
    current: bool,
    selected_people: Option<&BTreeSet<i64>>,
) -> Result<(), ApiError> {
    let online = matches!(year(season), Some(2020 | 2021));
    let mut results = BTreeMap::<i64, Vec<sqlx::any::AnyRow>>::new();
    for row in sqlx::query(RESULTS)
        .bind(season)
        .fetch_all(&mut *conn)
        .await?
    {
        results.entry(row.try_get("person")?).or_default().push(row);
    }
    let mut manual = BTreeMap::<i64, Vec<(i64, i64)>>::new();
    for row in sqlx::query_as::<_, (i64, i64, i64)>(
        "SELECT CAST(a.debater_id AS BIGINT),CAST(t.qual_type AS BIGINT),CAST(t.id AS BIGINT)
        FROM core_tournamentautoqual a JOIN core_tournament t ON t.id=a.tournament_id
        WHERE t.season=$1 ORDER BY a.id",
    )
    .bind(season)
    .fetch_all(&mut *conn)
    .await?
    {
        manual.entry(row.0).or_default().push((row.1, row.2));
    }
    let mut cleared_schools = BTreeSet::new();
    let mut selected_schools = BTreeSet::new();
    let people = sqlx::query(DEBATERS)
        .bind(season)
        .fetch_all(&mut *conn)
        .await?;
    for row in people {
        let id: i64 = row.try_get("id")?;
        let included = row.try_get::<i64, _>("included")? == 1;
        let recalc = match selected_people {
            Some(ids) => ids.contains(&id),
            None => row.try_get::<i64, _>("recalc")? == 1,
        };
        let recalc_online =
            selected_people.is_none() && row.try_get::<i64, _>("recalc_online")? == 1;
        if !recalc && !recalc_online {
            continue;
        }
        if selected_people.is_some() {
            selected_schools.insert(row.try_get::<Option<i64>, _>("school")?);
        }
        let rounds = results.get(&id).map_or(&[][..], Vec::as_slice);
        if recalc
            && !included
            && current
            && cleared_schools.insert(row.try_get::<Option<i64>, _>("school")?)
        {
            let school = row.try_get::<Option<i64>, _>("school")?;
            for table in ["core_qual", "core_qualpoints"] {
                let command = format!("DELETE FROM {table} WHERE season=$1 AND debater_id IN (
                    SELECT id FROM core_debater WHERE school_id=$2 OR (school_id IS NULL AND CAST($2 AS BIGINT) IS NULL))");
                sqlx::query(sqlx::AssertSqlSafe(command))
                    .bind(season)
                    .bind(school)
                    .execute(&mut *conn)
                    .await?;
            }
        }
        if recalc && included {
            if current {
                sqlx::query("DELETE FROM core_qual WHERE season=$1 AND debater_id=$2")
                    .bind(season)
                    .bind(id)
                    .execute(&mut *conn)
                    .await?;
            }
            let recipients = manual.get(&id).map_or(&[][..], Vec::as_slice);
            for &(kind, event) in recipients {
                if matches!(kind, 1..=10 | 15 | 16) {
                    add_qual(conn, season, id, kind, Some(event), false).await?;
                }
            }
            let latest = row
                .try_get::<Option<&str>, _>("latest_season")?
                .and_then(year);
            let target = year(season);
            if (!rounds.is_empty() || !recipients.is_empty()) && latest < target {
                sqlx::query("UPDATE core_debater SET latest_season=$1,
                    dino_judge_contact_opt_in=CASE WHEN status=2 THEN dino_judge_contact_opt_in ELSE FALSE END
                    WHERE id=$2")
                    .bind(target.map(|year| year.to_string()))
                    .bind(id)
                    .execute(&mut *conn)
                    .await?;
                crate::claims::edit::team_names(conn, id).await?;
            }
            let mut total = 0.0;
            for entry in rounds {
                if entry.try_get::<i64, _>("scoring")? != 1 {
                    continue;
                }
                let (place, kind, event) = (
                    entry.try_get::<i64, _>("place")?,
                    entry.try_get::<i64, _>("kind")?,
                    entry.try_get("event")?,
                );
                if kind != 8 && place > 0 && place <= entry.try_get::<i64, _>("bar")? {
                    add_qual(conn, season, id, kind, Some(event), true).await?;
                }
                if entry.try_get::<i64, _>("qual")? == 1 {
                    total += points::team_points_for_size(
                        entry.try_get("teams")?,
                        place,
                        entry.try_get::<i64, _>("ghost")? == 1,
                    );
                }
            }
            let previous = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM core_qualpoints
                WHERE season=$1 AND debater_id=$2 ORDER BY id LIMIT 1",
            )
            .bind(season)
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
            match (total > 0.0, previous) {
                (true, Some(previous)) => {
                    sqlx::query("UPDATE core_qualpoints SET points=$1 WHERE id=$2")
                        .bind(total)
                        .bind(previous)
                        .execute(&mut *conn)
                        .await?;
                }
                (true, None) => {
                    sqlx::query(
                        "INSERT INTO core_qualpoints (season,debater_id,points) VALUES ($1,$2,$3)",
                    )
                    .bind(season)
                    .bind(id)
                    .bind(total)
                    .execute(&mut *conn)
                    .await?;
                }
                (false, Some(previous)) if current => {
                    sqlx::query("DELETE FROM core_qualpoints WHERE id=$1")
                        .bind(previous)
                        .execute(&mut *conn)
                        .await?;
                }
                _ => {}
            }
            if !online && total >= 12.0 {
                add_qual(conn, season, id, 0, None, false).await?;
            }
        }
        if recalc_online {
            let mut markers = Vec::new();
            if included {
                for entry in rounds {
                    if entry.try_get::<i64, _>("scoring")? == 1 {
                        let value = if entry.try_get::<i64, _>("online")? == 1 {
                            points::online_points(entry.try_get("place")?)
                        } else {
                            0.0
                        };
                        markers.push((value, entry.try_get::<i64, _>("event")?));
                    }
                }
            }
            if markers.is_empty() {
                sqlx::query("DELETE FROM core_onlinequal WHERE season=$1 AND debater_id=$2")
                    .bind(season)
                    .bind(id)
                    .execute(&mut *conn)
                    .await?;
                continue;
            }
            markers.sort_by(|left, right| right.0.total_cmp(&left.0));
            let existing = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM core_onlinequal WHERE season=$1 AND debater_id=$2",
            )
            .bind(season)
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
            let total = markers.iter().take(6).map(|(value, _)| value).sum::<f64>();
            let mut fields = if existing.is_none() {
                ranked(season, "debater_id", id, total)
            } else {
                Map::from_iter([("points".to_owned(), json!(total))])
            };
            for (index, label) in LABELS.iter().enumerate() {
                if let Some((value, event)) = markers.get(index) {
                    fields.insert(format!("marker_{label}"), json!(value));
                    fields.insert(format!("tournament_{label}_id"), json!(event));
                } else if existing.is_none() {
                    fields.insert(format!("marker_{label}"), json!(0));
                    fields.insert(format!("tournament_{label}_id"), Value::Null);
                }
            }
            TABLES["core.OnlineQUAL"]
                .write(conn, &fields, existing)
                .await?;
            if online && total >= 10.0 {
                add_qual(conn, season, id, 0, None, false).await?;
            }
        }
    }
    if selected_people.is_some() {
        for school in selected_schools.into_iter().flatten() {
            refresh_school(conn, season, online, school).await?;
        }
        return Ok(());
    }
    if online {
        sqlx::query("DELETE FROM core_coty WHERE season=$1")
            .bind(season)
            .execute(&mut *conn)
            .await?;
    } else {
        let mut totals: BTreeMap<i64, f64> = BTreeMap::new();
        let mut awarded = BTreeSet::new();
        for (school, id, kind) in sqlx::query_as::<_,(i64,i64,i64)>("SELECT CAST(s.id AS BIGINT),CAST(q.debater_id AS BIGINT),CAST(q.qual_type AS BIGINT)
            FROM core_qual q JOIN core_debater d ON d.id=q.debater_id JOIN core_school s ON d.school_id=s.id
            WHERE q.season=$1 AND s.included_in_oty AND NOT s.temporary").bind(season).fetch_all(&mut *conn).await? {
            if bonus(kind, season) && awarded.insert(id) {
                *totals.entry(school).or_default() += 6.0;
            }
        }
        for (school, value) in sqlx::query_as::<_,(i64,f64)>("SELECT CAST(s.id AS BIGINT),CAST(p.points AS DOUBLE PRECISION)
            FROM core_qualpoints p JOIN core_debater d ON d.id=p.debater_id JOIN core_school s ON d.school_id=s.id
            WHERE p.season=$1 AND s.included_in_oty AND NOT s.temporary").bind(season).fetch_all(&mut *conn).await? {
            *totals.entry(school).or_default() += value.min(60.0);
        }
        for school in sqlx::query_scalar::<_, i64>("SELECT CAST(c.school_id AS BIGINT) FROM core_coty c JOIN core_school s ON s.id=c.school_id
            WHERE c.season=$1 AND NOT s.temporary").bind(season).fetch_all(&mut *conn).await? {
            if !totals.contains_key(&school) {
                sqlx::query("DELETE FROM core_coty WHERE season=$1 AND school_id=$2").bind(season).bind(school).execute(&mut *conn).await?;
            }
        }
        for (school, total) in totals {
            sqlx::query("INSERT INTO core_coty (season,school_id,points,place,tied) VALUES ($1,$2,$3,-1,FALSE)
                ON CONFLICT (season,school_id) DO UPDATE SET points=excluded.points")
                .bind(season).bind(school).bind(total).execute(&mut *conn).await?;
        }
    }
    rank(conn, "core_coty", season).await?;
    rank(conn, "core_onlinequal", season).await
}

async fn refresh_school(
    conn: &mut AnyConnection,
    season: &str,
    online: bool,
    school: i64,
) -> Result<(), ApiError> {
    let included = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(CASE WHEN included_in_oty THEN 1 ELSE 0 END AS BIGINT)
         FROM core_school WHERE id=$1",
    )
    .bind(school)
    .fetch_one(&mut *conn)
    .await?
        == 1;
    if online || !included {
        sqlx::query("DELETE FROM core_coty WHERE season=$1 AND school_id=$2")
            .bind(season)
            .bind(school)
            .execute(conn)
            .await?;
        return Ok(());
    }
    let points = sqlx::query_scalar::<_, f64>(
        "SELECT CAST(p.points AS DOUBLE PRECISION) FROM core_qualpoints p
         JOIN core_debater d ON d.id=p.debater_id WHERE p.season=$1 AND d.school_id=$2",
    )
    .bind(season)
    .bind(school)
    .fetch_all(&mut *conn)
    .await?;
    let people = sqlx::query_as::<_, (i64, i64)>(
        "SELECT CAST(q.debater_id AS BIGINT),CAST(q.qual_type AS BIGINT) FROM core_qual q
         JOIN core_debater d ON d.id=q.debater_id WHERE q.season=$1 AND d.school_id=$2",
    )
    .bind(season)
    .bind(school)
    .fetch_all(&mut *conn)
    .await?;
    let qualified = people
        .into_iter()
        .filter_map(|(debater, kind)| bonus(kind, season).then_some(debater))
        .collect::<BTreeSet<_>>();
    let total =
        points.into_iter().map(|value| value.min(60.0)).sum::<f64>() + 6.0 * qualified.len() as f64;
    sqlx::query(
        "INSERT INTO core_coty (season,school_id,points,place,tied) VALUES ($1,$2,$3,-1,FALSE)
         ON CONFLICT (season,school_id) DO UPDATE SET points=excluded.points",
    )
    .bind(season)
    .bind(school)
    .bind(total)
    .execute(conn)
    .await?;
    Ok(())
}

pub(crate) async fn redo_nine_point_rankings(
    conn: &mut AnyConnection,
    season: &str,
) -> Result<(), ApiError> {
    let teams = sqlx::query_as::<_, (i64, String, i64)>(
        "SELECT CAST(t.id AS BIGINT), t.name,
            CAST(CASE WHEN EXISTS(SELECT 1 FROM core_teamresult r WHERE r.team_id=t.id)
                 THEN 1 ELSE 0 END AS BIGINT)
         FROM core_team t JOIN core_team_debaters m ON m.team_id=t.id
         WHERE EXISTS(SELECT 1 FROM core_qualpoints p WHERE p.debater_id=m.debater_id
            AND p.season=$1 AND p.points=9)
         GROUP BY t.id,t.name ORDER BY MIN(m.id)",
    )
    .bind(season)
    .fetch_all(&mut *conn)
    .await?;
    let mut people = BTreeSet::new();
    for (id, name, has_results) in teams {
        println!("Updating {name}");
        if has_results == 0 {
            delete_relations::delete(conn, "core_team", id)
                .await
                .map_err(|_| ApiError::Constraint)?;
        } else {
            people.extend(
                sqlx::query_scalar::<_, i64>(
                    "SELECT CAST(debater_id AS BIGINT) FROM core_team_debaters WHERE team_id=$1",
                )
                .bind(id)
                .fetch_all(&mut *conn)
                .await?,
            );
        }
    }
    if people.is_empty() {
        return Ok(());
    }
    recompute_for(conn, season, true, Some(&people)).await
}
