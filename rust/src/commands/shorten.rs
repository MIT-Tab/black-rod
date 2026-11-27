use std::{collections::BTreeMap, sync::LazyLock};

use md5::{Digest, Md5};
use regex::Regex;
use sqlx::Row;

use crate::{AppState, tournament_profile::edit};

use super::Result;

pub(super) async fn names(state: &AppState, dry_run: bool) -> Result<()> {
    if dry_run {
        println!("DRY RUN MODE - No changes will be saved");
    }
    println!("\nProcessing tournaments...");
    static PARENTHESES: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\s*\([^)]*\)\s*").expect("parentheses"));

    let mut tx = state.pool.begin().await?;
    let mut tournaments_changed = 0;
    let tournaments = sqlx::query(
        "SELECT CAST(t.id AS BIGINT) AS id, t.name,t.short_name,t.manual_name,t.season,
          CAST(t.qual_type AS BIGINT) AS qual_type,CAST(t.name_suffix AS TEXT) AS name_suffix,
          CAST(t.date AS TEXT) AS date,CAST(t.host_id AS BIGINT) AS host_id,
          s.name AS school,s.short_name AS school_short
         FROM core_tournament t LEFT JOIN core_school s ON s.id=t.host_id ORDER BY t.date,t.id",
    )
    .fetch_all(&mut *tx)
    .await?;
    for row in tournaments {
        let name: &str = row.try_get("name")?;
        let old_short: &str = row.try_get("short_name")?;
        let school: Option<&str> = row.try_get("school")?;
        let school_short: Option<&str> = row.try_get("school_short")?;
        let mut short = name.to_owned();
        if let (Some(school), Some(replacement)) = (
            school.filter(|s| !s.is_empty()),
            school_short.filter(|s| !s.is_empty()),
        ) {
            short = short.replace(school, replacement);
        }
        let short = PARENTHESES.replace_all(&short, " ");
        let short = short.trim();
        if name == short && old_short == short {
            continue;
        }
        if !dry_run {
            let id = row.try_get::<i64, _>("id")?;
            let season = row.try_get::<&str, _>("season")?;
            let code = row.try_get::<i64, _>("qual_type")?;
            let host = row
                .try_get::<Option<i64>, _>("host_id")?
                .ok_or("The tournament is missing a host.")?;
            let (new_name, new_short) = edit::names(
                &mut tx,
                host,
                code,
                Some(id),
                edit::Naming {
                    season,
                    date: row.try_get("date")?,
                    name_suffix: row.try_get("name_suffix")?,
                    manual_name: row.try_get("manual_name")?,
                    short_name: short,
                },
            )
            .await
            .map_err(|_| "Could not rebuild the tournament name.")?;
            let edit::Scoring {
                toty,
                soty,
                qual,
                online,
                bar,
            } = edit::scoring(code, season);
            sqlx::query(
                "UPDATE core_tournament SET name=$1,short_name=$2,
              toty=$3,soty=$4,qual=$5,noty=FALSE,online_qual_points=COALESCE($6,online_qual_points),
              autoqual_bar=COALESCE($7,autoqual_bar) WHERE id=$8",
            )
            .bind(new_name)
            .bind(new_short)
            .bind(toty)
            .bind(soty)
            .bind(qual)
            .bind(online)
            .bind(bar)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        }
        if name != short {
            println!("  Tournament: \"{name}\" -> \"{short}\"");
            tournaments_changed += 1;
        }
    }

    println!("\nProcessing teams...");
    let mut affiliations = BTreeMap::<i64, Vec<(String, String)>>::new();
    for (team, name, short) in sqlx::query_as::<_, (i64, String, String)>(
        "SELECT DISTINCT CAST(m.team_id AS BIGINT),s.name,s.short_name FROM core_team_debaters m
         JOIN core_debater d ON d.id=m.debater_id JOIN core_school s ON s.id=d.school_id
         WHERE NOT d.temporary AND NOT d.synthetic AND s.name<>'' AND s.short_name<>''
         ORDER BY 1,2",
    )
    .fetch_all(&mut *tx)
    .await?
    {
        affiliations.entry(team).or_default().push((name, short));
    }
    let mut teams_changed = 0;
    for (id, name, old_short) in sqlx::query_as::<_, (i64, String, String)>(
        "SELECT CAST(id AS BIGINT),name,short_name FROM core_team ORDER BY id",
    )
    .fetch_all(&mut *tx)
    .await?
    {
        let mut short = name.clone();
        for (name, replacement) in affiliations.remove(&id).unwrap_or_default() {
            short = short.replace(&name, &replacement);
        }
        if !dry_run && old_short != short {
            sqlx::query("UPDATE core_team SET short_name=$1 WHERE id=$2")
                .bind(&short)
                .bind(id)
                .execute(&mut *tx)
                .await?;
        }
        if name != short {
            println!("  Team: \"{name}\" -> \"{short}\"");
            teams_changed += 1;
        }
    }
    tx.commit().await?;

    println!("\n{}", "=".repeat(60));
    println!(
        "{}",
        if dry_run {
            "DRY RUN COMPLETE - No changes saved"
        } else {
            "COMPLETE - Changes saved"
        }
    );
    println!("Tournaments shortened: {tournaments_changed}");
    println!("Teams shortened: {teams_changed}");
    if !dry_run {
        println!("\nClearing OTY caches...");
        for cache_type in ["toty", "soty", "noty", "coty", "online_quals"] {
            for year in (2004..=state.current_season).rev() {
                if let Some(cache) = &state.memcached {
                    let digest = hex::encode(Md5::digest(format!("{year}:")));
                    let key = format!(":1:template.cache.{cache_type}.{digest}");
                    cache.delete_raw(&key).await;
                }
                println!("  Cleared {cache_type} cache for season {year}");
            }
        }
        println!("All OTY caches cleared successfully");
    }
    Ok(())
}
