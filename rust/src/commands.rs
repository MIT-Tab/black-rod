mod canonical;
mod fixture;
mod shorten;
mod speaker_positions;

use std::{error::Error, fs, path::Path};

use sqlx::{AssertSqlSafe, Row};

use crate::{AppState, agent, claims, delete_relations, points};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const PARTICIPATION: &str = "SELECT td.debater_id,t.season FROM core_teamresult r
    JOIN core_tournament t ON t.id=r.tournament_id
    JOIN core_team_debaters td ON td.team_id=r.team_id
    UNION ALL
    SELECT r.debater_id,t.season FROM core_speakerresult r
    JOIN core_tournament t ON t.id=r.tournament_id";

pub const HELP: &str = "Usage: black-rod [serve | COMMAND]

Commands:
  agent_credential                Issue or revoke a correction API credential
  clean_duplicate_debaters        Delete result-free duplicates with a matching populated identity
  cache_mittab_results            Retain complete MIT-Tab tournament snapshots
  export_canonical_import_data    Export tournament matching data
  create_dev_fixtures             Export sanitized development data
  export_generated_codes          Export generated codes to CSV
  rebuild_synthetic_team_names     Restore native labels for synthetic teams
  generate_speaker_position_amendments  Derive round edits from raw backups
  fix_novice_debaters               Restore first-season status for novice debaters
  reconstruct_historical_point_quals  Restore missing offline point qualifications
  reindex_haystack                Refresh the native debater search index
  redo_rankings                    Recompute qualifications for teams with nine-point debaters
  rerank_current_awards           Refresh the displayed places of current standings
  update_otys                     Refresh points, qualifications and places for the current season
  shorten_names                   Populate school-based tournament and team short names
  seed_qual_bars                   Fill missing offline qualification bars
  seed_season_filters              Update debater season filters from results
  set_old_quals                    Print historical qualification point ranges
";

impl AppState {
    pub async fn run_command(&self, command: &str, args: &[String]) -> Result<()> {
        match command {
            "agent_credential" => self.manage_credential(args).await?,
            "clean_duplicate_debaters" => self.clean_duplicates(args).await?,
            "cache_mittab_results" | "refresh_import_sources" if args.is_empty() => {
                let count = self.refresh_tournament_sources().await;
                let verb = if command == "cache_mittab_results" {
                    "Retained"
                } else {
                    "Refreshed"
                };
                println!("{verb} {count} tournament snapshots.");
            }
            "export_canonical_import_data" => canonical::export(self, args).await?,
            "create_dev_fixtures" => {
                let output = match args {
                    [] => "dev_fixtures.json",
                    [option, path] if option == "--output" => path,
                    [option] if option.starts_with("--output=") => &option["--output=".len()..],
                    _ => return Err("Usage: black-rod create_dev_fixtures [--output PATH]".into()),
                };
                fixture::export(self, Path::new(output)).await?;
            }
            "export_generated_codes" => {
                let output = match args {
                    [] => "generated_codes.csv",
                    [option, path] if option == "--output" => path,
                    [option] if option.starts_with("--output=") => &option["--output=".len()..],
                    _ => {
                        return Err(
                            "Usage: black-rod export_generated_codes [--output PATH]".into()
                        );
                    }
                };
                self.export_generated_codes(Path::new(output)).await?;
            }
            "rebuild_synthetic_team_names" if args.is_empty() || args == ["--dry-run"] => {
                self.rebuild_synthetic_teams(!args.is_empty()).await?;
            }
            "generate_speaker_position_amendments" => {
                speaker_positions::generate(self, args).await?
            }
            "fix_novice_debaters" => self.fix_novices(args).await?,
            "reconstruct_historical_point_quals" => {
                self.reconstruct_historical_qualifications(args).await?
            }
            "shorten_names" if args.is_empty() || args == ["--dry-run"] => {
                shorten::names(self, !args.is_empty()).await?;
            }
            "reindex_haystack" => self.reindex_search(args).await?,
            "redo_rankings" if args.is_empty() => {
                let mut tx = self.pool.begin().await?;
                crate::tournament_import::results::standings::qualifications::redo_nine_point_rankings(
                    &mut tx, &self.current_season.to_string(),
                )
                .await
                .map_err(|error| format!("Could not update qualifications: {error:?}"))?;
                tx.commit().await?;
            }
            "update_otys" | "rerank_current_awards" if args.is_empty() => {
                self.maintain_awards(command == "update_otys").await?;
            }
            "seed_qual_bars" if args.is_empty() => self.seed_qual_bars().await?,
            "seed_season_filters" => self.seed_seasons(args).await?,
            "set_old_quals" if args.is_empty() => self.old_quals().await?,
            _ => {
                return Err(format!("Unknown command or arguments: {command}. Use --help.").into());
            }
        }
        Ok(())
    }

    async fn maintain_awards(&self, refresh: bool) -> Result<()> {
        use crate::tournament_import::results::standings;
        let mut tx = self.pool.begin().await?;
        if refresh {
            standings::refresh_current_awards(&mut tx, self.current_season).await
        } else {
            standings::rerank_current_awards(&mut tx, self.current_season).await
        }
        .map_err(|reason| format!("Could not refresh standings: {reason:?}"))?;
        tx.commit().await?;
        let seasons = if refresh {
            self.current_season..=self.current_season
        } else {
            2004..=self.current_season
        };
        for season in seasons {
            let boards = if refresh {
                &["toty", "noty", "coty", "soty"][..]
            } else {
                &["toty", "coty", "soty"][..]
            };
            crate::standings::invalidate(self, &season.to_string(), boards).await;
        }
        Ok(())
    }

    async fn reindex_search(&self, args: &[String]) -> Result<()> {
        let mut args = args.iter();
        while let Some(value) = args.next() {
            match value.as_str() {
                "--remove-old" | "--using=default" => (),
                "--using" if args.next().is_some_and(|value| value == "default") => (),
                "--verbosity"
                    if args
                        .next()
                        .is_some_and(|value| matches!(value.as_str(), "0" | "1" | "2" | "3")) => {}
                _ => return Err(format!("Unknown search index option: {value}").into()),
            }
        }
        let count = crate::search::reindex(self)
            .await
            .map_err(|_| "Could not rebuild the debater search index.")?;
        println!("Indexed {count} debaters.");
        Ok(())
    }

    async fn manage_credential(&self, values: &[String]) -> Result<()> {
        let mut admin = None;
        let mut name = None;
        let mut days = 7;
        let mut write = false;
        let mut revoke = None;
        let mut values = values.iter();
        while let Some(value) = values.next() {
            let (key, inline) = value
                .split_once('=')
                .map_or((value.as_str(), None), |(key, value)| (key, Some(value)));
            let mut argument = || {
                inline
                    .or_else(|| values.next().map(String::as_str))
                    .ok_or_else(|| format!("Supply a value for {key}"))
            };
            match key {
                "--admin" => admin = Some(argument()?),
                "--name" => name = Some(argument()?),
                "--days" => days = argument()?.parse::<i64>()?,
                "--write" if inline.is_none() => write = true,
                "--revoke" => revoke = Some(argument()?.parse::<i64>()?),
                _ => return Err(format!("Unknown agent_credential argument: {key}").into()),
            }
        }
        let admin = admin.ok_or("Supply --admin with an active superuser username.")?;
        let mut tx = self.pool.begin().await?;
        let id = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_user WHERE username=$1 AND is_active AND is_superuser",
        ).bind(admin).fetch_optional(&mut *tx).await?.ok_or("An active superuser is required.")?;
        if let Some(credential) = revoke {
            let updated = sqlx::query(
                "UPDATE core_agentcredential SET revoked_at=CURRENT_TIMESTAMP WHERE id=$1",
            )
            .bind(credential)
            .execute(&mut *tx)
            .await?
            .rows_affected();
            if updated == 0 {
                return Err("Credential not found.".into());
            }
            tx.commit().await?;
            println!("Credential revoked.");
            return Ok(());
        }
        let name = name
            .filter(|name| {
                !name.is_empty() && name.chars().count() <= 128 && (1..=90).contains(&days)
            })
            .ok_or("Provide --name and --days between 1 and 90.")?;
        let (token, hash) =
            agent::new_credential().map_err(|_| "Could not generate a credential token.")?;
        let expiration = (chrono::Utc::now() + chrono::Duration::days(days)).to_rfc3339();
        let expires_at = if self.pool.connect_options().database_url.scheme() == "sqlite" {
            "$4"
        } else {
            "CAST(CAST($4 AS TEXT) AS TIMESTAMPTZ)"
        };
        let sql = format!(
            "INSERT INTO core_agentcredential
            (name,token_hash,created_by_id,expires_at,can_write,created_at)
            VALUES ($1,$2,$3,{expires_at},$5,CURRENT_TIMESTAMP) RETURNING CAST(id AS BIGINT)"
        );
        let record = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
            .bind(name)
            .bind(hash)
            .bind(id)
            .bind(expiration)
            .bind(write)
            .fetch_one(&mut *tx)
            .await?;
        tx.commit().await?;
        println!("Credential {record}: copy this token now; only its hash is stored.");
        println!("{token}");
        Ok(())
    }

    async fn clean_duplicates(&self, values: &[String]) -> Result<()> {
        let mut min_id = None;
        let mut dry_run = false;
        let mut confirm = false;
        let mut args = values.iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--dry-run" => dry_run = true,
                "--confirm" => confirm = true,
                "--min-id" => {
                    min_id = Some(
                        args.next()
                            .ok_or("Supply an ID after --min-id.")?
                            .parse::<i64>()?,
                    )
                }
                value if value.starts_with("--min-id=") => {
                    min_id = Some(value["--min-id=".len()..].parse::<i64>()?)
                }
                _ => return Err(format!("Unknown clean_duplicate_debaters option: {arg}").into()),
            }
        }
        let min_id = min_id.ok_or("--min-id is required.")?;
        if dry_run == confirm {
            if dry_run {
                println!(
                    "Error: Cannot use both --dry-run and --confirm together. Use --dry-run first to see what will be deleted, then run again with --confirm to actually delete."
                );
            } else {
                println!(
                    "Error: Must specify either --dry-run or --confirm.\nFirst run with --dry-run to see what will be deleted:\n  black-rod clean_duplicate_debaters --min-id {min_id} --dry-run\nThen run with --confirm to actually delete:\n  black-rod clean_duplicate_debaters --min-id {min_id} --confirm"
                );
            }
            return Ok(());
        }
        println!("Checking for duplicate debaters with ID >= {min_id}");
        let mut tx = self.pool.begin().await?;
        let rows = sqlx::query(
            "WITH candidates AS (
                 SELECT d.id,d.first_name,d.last_name,d.school_id,s.name AS school,
                     CASE WHEN EXISTS(SELECT 1 FROM core_team_debaters m WHERE m.debater_id=d.id)
                       OR EXISTS(SELECT 1 FROM core_speakerresult r WHERE r.debater_id=d.id)
                     THEN 1 ELSE 0 END AS result
                 FROM core_debater d LEFT JOIN core_school s ON s.id=d.school_id
                 WHERE d.id>=$1 AND NOT d.temporary AND NOT d.synthetic
             ), grouped AS (
                 SELECT candidates.*,
                     MIN(result) OVER(PARTITION BY first_name,last_name,school_id) AS minimum,
                     MAX(result) OVER(PARTITION BY first_name,last_name,school_id) AS maximum
                 FROM candidates
             )
             SELECT CAST(id AS BIGINT) AS id,first_name,last_name,school,CAST(result AS BIGINT) AS result
             FROM grouped WHERE minimum=0 AND maximum=1
             ORDER BY first_name,last_name,school_id,result,id",
        ).bind(min_id).fetch_all(&mut *tx).await?;
        let mut planned = 0;
        let mut deleted = 0;
        for row in rows {
            let id: i64 = row.try_get("id")?;
            let first: &str = row.try_get("first_name")?;
            let last: &str = row.try_get("last_name")?;
            let school: Option<&str> = row.try_get("school")?;
            let has_results = row.try_get::<i64, _>("result")? == 1;
            let (change, status) = if has_results {
                ("Keeping", "HAS RESULTS")
            } else {
                ("Found duplicate", "NO RESULTS")
            };
            println!(
                "  {change}: {} (ID: {id}) from {} - {status}",
                format!("{first} {last}").trim(),
                school.unwrap_or("No School")
            );
            if has_results {
                continue;
            }
            planned += 1;
            if dry_run {
                continue;
            }
            match delete_relations::plan(&mut tx, "core_debater", id).await {
                Ok(plan) => {
                    deleted += plan.removed().count();
                    plan.apply(&mut tx).await.map_err(deletion_failure)?;
                }
                Err(delete_relations::DeleteError::Absent) => (),
                Err(error) => return Err(deletion_failure(error)),
            }
        }
        tx.commit().await?;
        if planned == 0 {
            println!("\nNo duplicate debaters found with ID >= {min_id}");
        } else if dry_run {
            println!("\nDRY RUN: Would delete {planned} duplicate debater(s)");
        } else {
            println!("\nSuccessfully deleted {deleted} duplicate debater(s)");
        }
        Ok(())
    }

    async fn export_generated_codes(&self, output: &Path) -> Result<()> {
        if let Some(parent) = output.parent().filter(|path| !path.as_os_str().is_empty()) {
            fs::create_dir_all(parent)?;
        }
        let rows = sqlx::query_as::<_, (String, String, String)>(
            "SELECT g.code,u.username,u.email FROM core_generatedcode g
             JOIN core_user u ON g.user_id=u.id ORDER BY g.code",
        )
        .fetch_all(&self.pool)
        .await?;
        let mut writer = csv::WriterBuilder::new()
            .terminator(csv::Terminator::CRLF)
            .from_path(output)?;
        writer.write_record(["code", "username", "email"])?;
        for (code, username, email) in &rows {
            writer.write_record([code, username, email])?;
        }
        writer.flush()?;
        println!(
            "Exported {} generated codes to {}",
            rows.len(),
            output.display()
        );
        Ok(())
    }

    async fn seed_qual_bars(&self) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        for year in (2004..=self.current_season)
            .rev()
            .filter(|year| !matches!(year, 2020 | 2021))
        {
            let season = year.to_string();
            let exists =
                sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM core_qualbar WHERE season=$1")
                    .bind(&season)
                    .fetch_one(&mut *tx)
                    .await?;
            if exists > 0 {
                continue;
            }
            let lowest = sqlx::query_scalar::<_, Option<f64>>(
                "SELECT CAST(MIN(p.points) AS DOUBLE PRECISION) FROM core_qualpoints p
                 WHERE p.season=$1 AND EXISTS (
                     SELECT 1 FROM core_qual q WHERE q.debater_id=p.debater_id
                       AND q.season=p.season AND q.qual_type=0
                 )",
            )
            .bind(&season)
            .fetch_one(&mut *tx)
            .await?;
            println!("Creating QualBar for season: {season}");
            let report = lowest.map_or_else(|| "inf".to_owned(), python_float);
            println!("Minimum points qualified for season {season}: {report}");
            if let Some(points) = lowest.filter(|points| *points > 0.0 && points.is_finite()) {
                sqlx::query("INSERT INTO core_qualbar (season,points) VALUES ($1,CAST($2 AS DOUBLE PRECISION))
                     ON CONFLICT (season) DO NOTHING")
                    .bind(&season)
                    .bind(points)
                    .execute(&mut *tx)
                    .await?;
            }
        }
        tx.commit().await?;
        Ok(())
    }

    async fn seed_seasons(&self, args: &[String]) -> Result<()> {
        let mut first = true;
        let mut latest = true;
        let mut active = None;
        let mut args = args.iter();
        while let Some(argument) = args.next() {
            match argument.as_str() {
                "--first-only" => latest = false,
                "--latest-only" => first = false,
                "--active-in-season" => {
                    active = args
                        .next()
                        .map(String::as_str)
                        .ok_or("Supply a season after --active-in-season.")?
                        .into()
                }
                option if option.starts_with("--active-in-season=") => {
                    active = Some(&option["--active-in-season=".len()..])
                }
                _ => return Err(format!("Unknown seed_season_filters option: {argument}").into()),
            }
        }
        let set = match (first, latest) {
            (true, true) => "first_season=seasons.first_season,latest_season=seasons.latest_season",
            (true, false) => "first_season=seasons.first_season",
            (false, true) => "latest_season=seasons.latest_season",
            (false, false) => {
                println!("Debater season filters seeded successfully:  updated for 0 debaters.");
                return Ok(());
            }
        };
        let active = active.filter(|season| !season.is_empty());
        let query = format!(
            "WITH participation AS ({PARTICIPATION}), seasons AS (
                 SELECT d.id, MIN(p.season) AS first_season, MAX(p.season) AS latest_season
                 FROM core_debater d LEFT JOIN participation p ON p.debater_id=d.id
                 WHERE NOT d.temporary AND NOT d.synthetic
                 GROUP BY d.id HAVING COUNT(CASE WHEN p.season=$1 THEN 1 END)>0 OR $1 IS NULL
             )
             UPDATE core_debater SET {set} FROM seasons WHERE core_debater.id=seasons.id"
        );
        let updated = sqlx::query(AssertSqlSafe(query))
            .bind(active)
            .execute(&self.pool)
            .await?
            .rows_affected();
        let fields = [
            first.then_some("first_season"),
            latest.then_some("latest_season"),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join(", ");
        let suffix = active.map_or_else(String::new, |season| {
            format!(" for debaters active in season {season}")
        });
        println!(
            "Debater season filters seeded successfully: {fields} updated for {updated} debaters{suffix}."
        );
        Ok(())
    }

    async fn rebuild_synthetic_teams(&self, dry_run: bool) -> Result<()> {
        if dry_run {
            println!("DRY RUN MODE - No changes will be saved");
        }
        let mut tx = self.pool.begin().await?;
        let teams = sqlx::query_as::<_, (i64, String, String)>(
            "SELECT CAST(id AS BIGINT), name, short_name FROM core_team WHERE synthetic ORDER BY id",
        ).fetch_all(&mut *tx).await?;
        let mut updated = 0;
        let mut skipped = 0;
        for (id, name, short_name) in &teams {
            let Some((new, short)) = claims::edit::native_team_labels(&mut tx, *id).await? else {
                println!("Skipping synthetic team {id}: no debaters available for native naming.");
                skipped += 1;
                continue;
            };
            if name == &new && short_name == &short {
                continue;
            }
            println!("Team {id}: \"{name}\" / \"{short_name}\" -> \"{new}\" / \"{short}\"");
            updated += 1;
            if !dry_run {
                sqlx::query("UPDATE core_team SET name=$1,short_name=$2 WHERE id=$3")
                    .bind(&new)
                    .bind(&short)
                    .bind(id)
                    .execute(&mut *tx)
                    .await?;
            }
        }
        tx.commit().await?;
        println!(
            "Processed {} synthetic teams; updated {updated}, skipped {skipped}.",
            teams.len()
        );
        Ok(())
    }

    async fn fix_novices(&self, values: &[String]) -> Result<()> {
        let mut season = self.current_season.to_string();
        let mut dry_run = false;
        let mut confirm = false;
        let mut args = values.iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--dry-run" => dry_run = true,
                "--confirm" => confirm = true,
                "--season" => {
                    season = args
                        .next()
                        .ok_or("Supply a season after --season.")?
                        .clone()
                }
                value if value.starts_with("--season=") => {
                    season = value["--season=".len()..].to_owned()
                }
                _ => return Err(format!("Unknown fix_novice_debaters option: {arg}").into()),
            }
        }
        match (dry_run, confirm) {
            (true, true) => {
                println!(
                    "Error: Cannot use both --dry-run and --confirm together. Use --dry-run first to see what will be updated, then run again with --confirm to actually update."
                );
                return Ok(());
            }
            (false, false) => {
                println!(
                    "Error: Must specify either --dry-run or --confirm.\nFirst run with --dry-run to see what will be updated:\n  black-rod fix_novice_debaters --dry-run\nThen run with --confirm to actually update:\n  black-rod fix_novice_debaters --confirm"
                );
                return Ok(());
            }
            _ => (),
        }
        if season.is_empty() {
            season = self.current_season.to_string();
        }
        println!("Checking for debaters with first_season=None who only competed in {season}");
        let mut tx = self.pool.begin().await?;
        let sql = format!(
            "WITH participation AS ({PARTICIPATION}), candidates AS (
               SELECT debater_id FROM participation WHERE season<>'' GROUP BY debater_id
               HAVING MIN(season)=$1 AND MAX(season)=$1
             )
             SELECT CAST(d.id AS BIGINT) AS id,d.first_name,d.last_name,s.name AS school
             FROM core_debater d JOIN candidates c ON c.debater_id=d.id
             LEFT JOIN core_school s ON s.id=d.school_id
             WHERE NOT d.temporary AND NOT d.synthetic AND COALESCE(d.first_season,'')=''
             ORDER BY d.id"
        );
        let people = sqlx::query(AssertSqlSafe(sql))
            .bind(&season)
            .fetch_all(&mut *tx)
            .await?;
        if people.is_empty() {
            println!("\nNo debaters found with first_season=None who only competed in {season}");
            return Ok(());
        }
        let sqlite = self.pool.connect_options().database_url.scheme() == "sqlite";
        let region = if sqlite { "'[]'" } else { "'[]'::jsonb" };
        let update = format!(
            "UPDATE core_debater SET first_name=$1,last_name=$2,first_season=$3,latest_season=$3,
               status=0,dino_judge_contact_opt_in=FALSE,
               region=CASE WHEN dino_to_contact_opt_in THEN region ELSE {region} END
             WHERE id=$4 AND COALESCE(first_season,'')=''"
        );
        let mut changed = 0;
        for person in &people {
            let id: i64 = person.try_get("id")?;
            let first: &str = person.try_get("first_name")?;
            let last: &str = person.try_get("last_name")?;
            let school: Option<&str> = person.try_get("school")?;
            println!(
                "  Found: {} (ID: {id}) from {} - only competed in {season}",
                format!("{first} {last}").trim(),
                school.unwrap_or("No School")
            );
            if confirm {
                let result = sqlx::query(AssertSqlSafe(update.as_str()))
                    .bind(claims::edit::sanitize_name(first, "Removed"))
                    .bind(claims::edit::sanitize_name(last, ""))
                    .bind(&season)
                    .bind(id)
                    .execute(&mut *tx)
                    .await?;
                if result.rows_affected() > 0 {
                    claims::edit::team_names(&mut tx, id).await?;
                    changed += 1;
                }
            }
        }
        tx.commit().await?;
        if dry_run {
            println!("\nDRY RUN: Would update {} debater(s) to:", people.len());
        } else {
            println!("\nSuccessfully updated {changed} debater(s) to:");
        }
        println!("  - first_season: {season}\n  - latest_season: {season}\n  - status: NOVICE (0)");
        Ok(())
    }

    async fn reconstruct_historical_qualifications(&self, values: &[String]) -> Result<()> {
        let mut seasons = Vec::new();
        let mut dry_run = false;
        let mut all = false;
        let mut args = values.iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--all" => all = true,
                "--dry-run" => dry_run = true,
                "--season" => seasons.push(
                    args.next()
                        .ok_or("Supply a season after --season.")?
                        .to_owned(),
                ),
                value if value.starts_with("--season=") => {
                    seasons.push(value["--season=".len()..].to_owned())
                }
                _ => {
                    return Err(format!(
                        "Unknown reconstruct_historical_point_quals option: {arg}"
                    )
                    .into());
                }
            }
        }
        if all || seasons.is_empty() {
            seasons = (2004..=2025)
                .filter(|year| !all || *year < self.current_season)
                .map(|year| year.to_string())
                .filter(|season| points::historical_qualification_bar(season).is_some())
                .collect();
        }
        if seasons.is_empty() {
            println!("No seasons selected. Nothing to do.");
            return Ok(());
        }
        if dry_run {
            println!("DRY RUN: no rows will be written");
        }
        let mut tx = self.pool.begin().await?;
        let mut attempted = 0;
        for season in seasons {
            if matches!(season.as_str(), "2020" | "2021") {
                println!("Skipping online season {season}");
                continue;
            }
            if season.trim().parse::<i32>().is_err()
                || season.trim().parse::<i32>()? >= self.current_season
            {
                println!("Skipping non-historical season {season}");
                continue;
            }
            let Some(bar) = points::historical_qualification_bar(&season) else {
                println!("Skipping {season}: no HISTORICAL_QUAL_BARS entry");
                continue;
            };
            let found = sqlx::query_as::<_, (i64, i64)>(
                "SELECT DISTINCT CAST(qp.debater_id AS BIGINT), CAST(CASE WHEN EXISTS (
                   SELECT 1 FROM core_qual q WHERE q.debater_id=qp.debater_id AND q.season=$1
                 ) THEN 1 ELSE 0 END AS BIGINT)
                 FROM core_qualpoints qp JOIN core_debater d ON d.id=qp.debater_id
                 JOIN core_school s ON s.id=d.school_id
                 WHERE s.included_in_oty AND qp.season=$1 AND qp.points>=$2
                 ORDER BY 1",
            )
            .bind(&season)
            .bind(bar)
            .fetch_all(&mut *tx)
            .await?;
            let missing = found
                .iter()
                .filter(|(_, exists)| *exists == 0)
                .map(|(id, _)| *id)
                .collect::<Vec<_>>();
            attempted += missing.len();
            println!(
                "Season {season}: bar={}, candidates={}, already_qualified={}, missing={}",
                python_float(bar),
                found.len(),
                found.len() - missing.len(),
                missing.len()
            );
            if !dry_run {
                for id in missing {
                    sqlx::query(
                        "INSERT INTO core_qual (season,debater_id,qual_type,place,points,tied,tournament_id)
                         VALUES ($1,$2,0,-1,-1,FALSE,NULL) ON CONFLICT (season,debater_id,qual_type) DO NOTHING"
                    ).bind(&season).bind(id).execute(&mut *tx).await?;
                }
            }
        }
        tx.commit().await?;
        if dry_run {
            println!("Dry run complete. Would attempt to create up to {attempted} rows.");
        } else {
            println!("Reconstruction complete. Attempted to create {attempted} rows.");
        }
        Ok(())
    }

    async fn old_quals(&self) -> Result<()> {
        for year in (2019..=2024.min(self.current_season)).rev() {
            let values = sqlx::query_as::<_, (Option<f64>, Option<f64>)>(
                "WITH points AS (
                     SELECT p.points, EXISTS (
                         SELECT 1 FROM core_qual q WHERE q.debater_id=p.debater_id
                           AND q.season=p.season AND q.qual_type=0
                     ) AS qualified FROM core_qualpoints p WHERE p.season=$1
                 )
                 SELECT CAST(MIN(CASE WHEN qualified THEN points END) AS DOUBLE PRECISION),
                        CAST(MAX(CASE WHEN NOT qualified THEN points END) AS DOUBLE PRECISION)
                 FROM points",
            )
            .bind(year.to_string())
            .fetch_one(&self.pool)
            .await?;
            let smallest = values
                .0
                .filter(|value| *value < 100000.0)
                .map_or_else(|| "100000".to_owned(), python_float);
            let largest = values
                .1
                .filter(|value| *value > 0.0)
                .map_or_else(|| "0".to_owned(), python_float);
            println!("Season {year} - Min Qualled: {smallest}, Max Unqualled: {largest}");
        }
        Ok(())
    }
}

fn python_float(value: f64) -> String {
    format!("{value:?}")
}

fn deletion_failure(error: delete_relations::DeleteError) -> Box<dyn Error> {
    match error {
        delete_relations::DeleteError::Absent => "The record no longer exists.".into(),
        delete_relations::DeleteError::Protected => {
            "Related records protect this record from deletion.".into()
        }
        delete_relations::DeleteError::Database(error) => error.into(),
    }
}
