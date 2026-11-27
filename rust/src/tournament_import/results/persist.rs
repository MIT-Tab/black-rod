use std::collections::{BTreeMap, BTreeSet};

use sqlx::{AnyConnection, Row};

use super::{
    forms::Panel,
    review::{self, Review},
    standings::{self, Award},
};
use crate::{
    AppState, delete_relations,
    tournament_import::{Error, identity, invalid},
};

type Teams = BTreeMap<(i64, i64), BTreeSet<i64>>;

async fn hidden(
    conn: &mut AnyConnection,
    event: i64,
) -> Result<(Teams, BTreeSet<(i64, i64)>), Error> {
    let mut teams = Teams::new();
    for row in sqlx::query("SELECT CAST(r.team_id AS BIGINT) AS team,CAST(r.type_of_place AS BIGINT) AS kind,
        CAST(h.debater_id AS BIGINT) AS person FROM core_teamresult r
        JOIN core_teamresult_hidden_from_debaters h ON h.teamresult_id=r.id JOIN core_debater d ON d.id=h.debater_id
        WHERE r.tournament_id=$1 AND NOT (d.synthetic OR d.temporary)")
        .bind(event).fetch_all(&mut *conn).await? {
        teams.entry((row.try_get("team")?,row.try_get("kind")?)).or_default().insert(row.try_get("person")?);
    }
    let mut speakers = BTreeSet::new();
    for row in sqlx::query(
        "SELECT CAST(debater_id AS BIGINT) AS person,CAST(type_of_place AS BIGINT) AS kind
        FROM core_speakerresult WHERE tournament_id=$1 AND hidden_from_profile",
    )
    .bind(event)
    .fetch_all(conn)
    .await?
    {
        speakers.insert((row.try_get("person")?, row.try_get("kind")?));
    }
    Ok((teams, speakers))
}

async fn recipients(
    conn: &mut AnyConnection,
    event: i64,
    ids: &[i64],
) -> Result<BTreeSet<i64>, Error> {
    let old = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(debater_id AS BIGINT) FROM core_tournamentautoqual WHERE tournament_id=$1",
    )
    .bind(event)
    .fetch_all(&mut *conn)
    .await?;
    let changed = old
        .iter()
        .copied()
        .chain(ids.iter().copied())
        .collect::<BTreeSet<_>>();
    for id in old.iter().filter(|id| !ids.contains(id)) {
        sqlx::query("DELETE FROM core_tournamentautoqual WHERE tournament_id=$1 AND debater_id=$2")
            .bind(event)
            .bind(id)
            .execute(&mut *conn)
            .await?;
    }
    for id in ids.iter().filter(|id| !old.contains(id)) {
        sqlx::query("INSERT INTO core_tournamentautoqual(tournament_id,debater_id) VALUES($1,$2)")
            .bind(event)
            .bind(id)
            .execute(&mut *conn)
            .await?;
    }
    Ok(changed)
}

async fn sync_manual(
    conn: &mut AnyConnection,
    season: &str,
    people: &BTreeSet<i64>,
) -> Result<(), Error> {
    for &person in people {
        sqlx::query("INSERT INTO core_qual (season,debater_id,qual_type,tournament_id,place,points,tied)
            SELECT t.season,a.debater_id,t.qual_type,t.id,-1,-1,FALSE
            FROM core_tournamentautoqual a JOIN core_tournament t ON t.id=a.tournament_id
            JOIN core_debater d ON d.id=a.debater_id JOIN core_school s ON s.id=d.school_id
            WHERE t.season=$1 AND a.debater_id=$2 AND s.included_in_oty AND t.qual_type IN (1,2,3,4,5,6,7,8,9,10,15,16) ORDER BY a.id
            ON CONFLICT (season,debater_id,qual_type) DO NOTHING")
            .bind(season).bind(person).execute(&mut *conn).await?;
    }
    Ok(())
}

async fn historical_coty(
    conn: &mut AnyConnection,
    season: &str,
    people: &BTreeSet<i64>,
) -> Result<(), Error> {
    let no_bp = season
        .split('-')
        .next()
        .and_then(|part| part.parse::<i32>().ok())
        .unwrap_or_default()
        >= 2026;
    for &person in people {
        sqlx::query("INSERT INTO core_coty (season,school_id,place,points,tied)
            SELECT $1,s.id,-1,
            COALESCE((SELECT SUM(CASE WHEN q.points>60 THEN 60 ELSE q.points END)
                FROM core_qualpoints q JOIN core_debater d ON d.id=q.debater_id
                WHERE q.season=$1 AND d.school_id=s.id),0)
             + 6 * (SELECT COUNT(DISTINCT q.debater_id) FROM core_qual q JOIN core_debater d ON d.id=q.debater_id
                WHERE q.season=$1 AND d.school_id=s.id AND (NOT $3 OR q.qual_type NOT IN (1,2,3,5,6,16))),
            FALSE FROM core_school s JOIN core_debater d ON d.school_id=s.id
            WHERE d.id=$2 AND NOT s.temporary AND s.included_in_oty
            ON CONFLICT (season,school_id) DO UPDATE SET points=excluded.points")
            .bind(season).bind(person).bind(no_bp).execute(&mut *conn).await?;
        sqlx::query("DELETE FROM core_coty WHERE season=$1 AND school_id IN (SELECT s.id FROM core_school s
            JOIN core_debater d ON s.id=d.school_id WHERE d.id=$2 AND NOT s.temporary AND NOT s.included_in_oty)")
            .bind(season).bind(person).execute(&mut *conn).await?;
    }
    standings::rank(conn, "core_coty", season).await?;
    Ok(())
}

pub(super) async fn store(
    state: &AppState,
    event: i64,
    panels: &mut [Panel],
    autoquals: &[i64],
    review: Option<&Review>,
) -> Result<Vec<String>, Error> {
    let mut tx = state.pool.begin().await?;
    let Some(season) = sqlx::query_scalar::<_, String>(
        "UPDATE core_tournament SET id=id WHERE id=$1 RETURNING season",
    )
    .bind(event)
    .fetch_optional(&mut *tx)
    .await?
    else {
        return Err(invalid("Tournament not found."));
    };
    let resolved = if let Some(review) = review {
        let rows = review::resolve(&mut tx, panels, state.current_season).await?;
        review::counts(&mut tx, event, review).await?;
        Some(rows)
    } else {
        None
    };
    let (hidden_teams, hidden_speakers) = hidden(&mut tx, event).await?;
    let mut teams = BTreeSet::new();
    let mut speakers = [BTreeSet::new(), BTreeSet::new()];
    let old=sqlx::query("SELECT CAST(id AS BIGINT) AS id,CAST(team_id AS BIGINT) AS team FROM core_teamresult WHERE tournament_id=$1")
        .bind(event).fetch_all(&mut *tx).await?;
    for row in old {
        teams.insert(row.try_get("team")?);
        delete_relations::delete(&mut tx, "core_teamresult", row.try_get("id")?)
            .await
            .map_err(|error| match error {
                delete_relations::DeleteError::Database(error) => Error::from(error),
                _ => invalid("Could not replace prior team results."),
            })?;
    }
    for row in sqlx::query(
        "SELECT CAST(debater_id AS BIGINT) AS person,CAST(type_of_place AS BIGINT) AS kind
        FROM core_speakerresult WHERE tournament_id=$1",
    )
    .bind(event)
    .fetch_all(&mut *tx)
    .await?
    {
        speakers[usize::from(row.try_get::<i64, _>("kind")? != 1)].insert(row.try_get("person")?);
    }
    sqlx::query("DELETE FROM core_speakerresult WHERE tournament_id=$1")
        .bind(event)
        .execute(&mut *tx)
        .await?;
    sqlx::query("DELETE FROM core_qual WHERE tournament_id=$1")
        .bind(event)
        .execute(&mut *tx)
        .await?;
    let affected = recipients(&mut tx, event, autoquals).await?;
    sync_manual(&mut tx, &season, &affected).await?;
    for panel in panels
        .iter()
        .filter(|panel| matches!(panel.tab.kind, "speaker" | "team"))
    {
        let tab = panel.tab;
        for row in panel.rows.iter().filter(|row| !row.deleted) {
            let Some(first) = row.first else { continue };
            if tab.kind == "team" {
                let Some(second) = row.second else { continue };
                let first_school=sqlx::query_scalar::<_,i64>("SELECT CAST(school_id AS BIGINT) FROM core_debater WHERE id=$1 AND school_id IS NOT NULL")
                    .bind(first).fetch_optional(&mut *tx).await?;
                let second_school=sqlx::query_scalar::<_,i64>("SELECT CAST(school_id AS BIGINT) FROM core_debater WHERE id=$1 AND school_id IS NOT NULL")
                    .bind(second).fetch_optional(&mut *tx).await?;
                if first_school.is_none() || second_school.is_none() {
                    continue;
                }
                let id = identity::team(&mut tx, &[first, second]).await?;
                teams.insert(id);
                let place = if tab.key == "unplaced_teams" {
                    -1
                } else {
                    row.order
                };
                let result = sqlx::query_scalar::<_, i64>(
                    "INSERT INTO core_teamresult
                    (tournament_id,team_id,type_of_place,place,ghost_points,counts_for_points)
                    VALUES ($1,$2,$3,$4,$5,$6) RETURNING CAST(id AS BIGINT)",
                )
                .bind(event)
                .bind(id)
                .bind(tab.division)
                .bind(place)
                .bind(row.ghost)
                .bind(row.scoring)
                .fetch_one(&mut *tx)
                .await?;
                if let Some(people) = hidden_teams.get(&(id, tab.division)) {
                    for person in people {
                        sqlx::query("INSERT INTO core_teamresult_hidden_from_debaters (teamresult_id,debater_id) VALUES ($1,$2)")
                            .bind(result).bind(person).execute(&mut *tx).await?;
                    }
                }
            } else {
                speakers[usize::from(tab.division != 1)].insert(first);
                sqlx::query("INSERT INTO core_speakerresult (tournament_id,debater_id,type_of_place,place,tie,counts_for_points,hidden_from_profile)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)")
                    .bind(event).bind(first).bind(tab.division).bind(row.order).bind(row.tie).bind(row.scoring)
                    .bind(hidden_speakers.contains(&(first,tab.division))).execute(&mut *tx).await?;
            }
        }
    }
    let skipped = if let Some((review, resolved)) = review.zip(resolved.as_ref()) {
        review::rounds(&mut tx, event, state.current_season, review, resolved).await?
    } else {
        Vec::new()
    };
    if season == state.current_season.to_string() {
        standings::awards(&mut tx, &season, Award::Team, &teams).await?;
        standings::awards(&mut tx, &season, Award::Speaker, &speakers[0]).await?;
        standings::awards(&mut tx, &season, Award::Novice, &speakers[1]).await?;
        standings::rebuild(&mut tx, &season).await?;
    } else if !affected.is_empty() {
        historical_coty(&mut tx, &season, &affected).await?;
    }
    tx.commit().await?;
    state.search.invalidate_all();
    let boards = if season == state.current_season.to_string() {
        crate::standings::STORED_BOARDS
    } else if !affected.is_empty() {
        &["coty"][..]
    } else {
        &[][..]
    };
    tokio::join!(
        crate::standings::invalidate(state, &season, boards),
        crate::recent_results::invalidate(state),
    );
    Ok(skipped)
}
