use std::collections::BTreeSet;

use sqlx::{AnyConnection, Row};

use super::{Error, Summary, erase, invalid};

async fn linked_rounds(conn: &mut AnyConnection, source: i64) -> Result<Vec<i64>, Error> {
    Ok(sqlx::query_scalar::<_,i64>("SELECT DISTINCT CAST(m.round_id AS BIGINT) FROM core_importedroundmetadata_sources s
        JOIN core_importedroundmetadata m ON m.id=s.importedroundmetadata_id WHERE s.tournamentimport_id=$1 ORDER BY 1")
        .bind(source).fetch_all(conn).await?)
}

async fn tournament(conn: &mut AnyConnection, source: i64) -> Result<i64, Error> {
    sqlx::query_scalar::<_, i64>(
        "SELECT CAST(tournament_id AS BIGINT) FROM core_tournamentimport WHERE id=$1",
    )
    .bind(source)
    .fetch_optional(conn)
    .await?
    .ok_or_else(|| invalid("TournamentImport matching query does not exist."))
}

pub(super) async fn delete(
    conn: &mut AnyConnection,
    source: i64,
    summary: &mut Summary,
) -> Result<(), Error> {
    tournament(conn, source).await?;
    let rounds = linked_rounds(conn, source).await?;
    for id in rounds {
        erase(conn, "core_round", id).await?;
        summary.rounds_deleted += 1;
    }
    erase(conn, "core_tournamentimport", source).await?;
    summary.tournament_imports_deleted += 1;
    Ok(())
}

pub(super) async fn move_to(
    conn: &mut AnyConnection,
    source: i64,
    destination: i64,
    summary: &mut Summary,
) -> Result<(), Error> {
    let current = tournament(conn, source).await?;
    let exists = sqlx::query_scalar::<_, i64>(
        "UPDATE core_tournament SET id=id WHERE id=$1 RETURNING CAST(id AS BIGINT)",
    )
    .bind(destination)
    .fetch_optional(&mut *conn)
    .await?;
    if exists.is_none() {
        return Err(invalid("Tournament matching query does not exist."));
    }
    let rounds = linked_rounds(conn, source).await?;
    let mut sources = BTreeSet::from([source]);
    for &id in &rounds {
        let counts = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_round r
            JOIN core_round dest ON dest.tournament_id=$1 AND dest.import_key=r.import_key
            WHERE r.id=$2 AND r.import_key<>'' AND dest.id<>r.id",
        )
        .bind(destination)
        .bind(id)
        .fetch_one(&mut *conn)
        .await?;
        if counts > 0 {
            return Err(invalid(
                "The target tournament already has a round with the same import key.",
            ));
        }
        sources.extend(sqlx::query_scalar::<_,i64>("SELECT CAST(i.id AS BIGINT) FROM core_importedroundmetadata_sources s
            JOIN core_importedroundmetadata m ON m.id=s.importedroundmetadata_id JOIN core_tournamentimport i ON i.id=s.tournamentimport_id
            WHERE m.round_id=$1 AND i.tournament_id=$2")
            .bind(id).bind(current).fetch_all(&mut *conn).await?);
    }
    for &id in &sources {
        let row =
            sqlx::query("SELECT import_type,source_hash FROM core_tournamentimport WHERE id=$1")
                .bind(id)
                .fetch_one(&mut *conn)
                .await?;
        let hash = row.try_get::<&str, _>("source_hash")?;
        if !hash.is_empty() {
            let conflict = sqlx::query_scalar::<_, i64>(
                "SELECT CAST(id AS BIGINT) FROM core_tournamentimport
                WHERE tournament_id=$1 AND import_type=$2 AND source_hash=$3 AND id<>$4 LIMIT 1",
            )
            .bind(destination)
            .bind(row.try_get::<&str, _>("import_type")?)
            .bind(hash)
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?;
            if conflict.is_some() {
                return Err(invalid(
                    "The target tournament already has an import with the same source hash.",
                ));
            }
        }
    }
    for id in rounds {
        sqlx::query("UPDATE core_round SET tournament_id=$1 WHERE id=$2")
            .bind(destination)
            .bind(id)
            .execute(&mut *conn)
            .await?;
        summary.rounds_moved += 1;
    }
    for id in sources {
        sqlx::query("UPDATE core_tournamentimport SET tournament_id=$1 WHERE id=$2")
            .bind(destination)
            .bind(id)
            .execute(&mut *conn)
            .await?;
        summary.linked_source_imports_moved += 1;
    }
    summary.tournament_imports_moved += 1;
    Ok(())
}
