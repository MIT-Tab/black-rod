use serde_json::{Map, Value, json};
use sqlx::AnyConnection;

use super::schema::Model;
use crate::ApiError;

pub(super) async fn before_save(
    conn: &mut AnyConnection,
    model: &Model,
    season: i32,
    id: Option<i64>,
    columns: &mut Map<String, Value>,
) -> Result<(), ApiError> {
    use crate::tournament_profile::edit;
    if model.app() != "core" {
        return Ok(());
    }
    if model.slug() == "debater" {
        let text = |key: &str| columns.get(key).and_then(Value::as_str).unwrap_or_default();
        let first = crate::claims::edit::sanitize_name(text("first_name"), "Removed");
        let last = crate::claims::edit::sanitize_name(text("last_name"), "");
        columns.insert("first_name".to_owned(), json!(first));
        columns.insert("last_name".to_owned(), json!(last));
        if id.is_none() {
            for name in ["first_season", "latest_season"] {
                if columns
                    .get(name)
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
                {
                    columns.insert(name.to_owned(), json!(season.to_string()));
                }
            }
        }
        let dino = columns.get("status").and_then(Value::as_i64) == Some(2);
        if !dino {
            columns.insert("dino_judge_contact_opt_in".to_owned(), json!(false));
        }
        if !["dino_judge_contact_opt_in", "dino_to_contact_opt_in"]
            .iter()
            .any(|key| columns.get(*key) == Some(&json!(true)))
        {
            columns.insert("region".to_owned(), json!([]));
        } else if let Some(region) = columns.get("region").and_then(Value::as_str) {
            let region = if region.is_empty() {
                json!([])
            } else {
                json!([region])
            };
            columns.insert("region".to_owned(), region);
        }
    } else if model.slug() == "tournament"
        && let Some(host) = columns.get("host_id").and_then(Value::as_i64)
    {
        let text = |name: &str| {
            columns
                .get(name)
                .and_then(Value::as_str)
                .unwrap_or_default()
        };
        let date = text("date");
        let season = text("season");
        let code = columns
            .get("qual_type")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        let suffix = columns
            .get("name_suffix")
            .and_then(Value::as_i64)
            .unwrap_or_default()
            .to_string();
        let (name, short_name) = edit::names(
            conn,
            host,
            code,
            id,
            edit::Naming {
                season,
                date,
                name_suffix: &suffix,
                manual_name: text("manual_name"),
                short_name: text("short_name"),
            },
        )
        .await?;
        let scoring = edit::scoring(code, season);
        for (key, value) in [
            ("name", json!(name)),
            ("short_name", json!(short_name)),
            ("toty", json!(scoring.toty)),
            ("soty", json!(scoring.soty)),
            ("qual", json!(scoring.qual)),
            ("noty", json!(false)),
        ] {
            columns.insert(key.to_owned(), value);
        }
        if let Some(online) = scoring.online {
            columns.insert("online_qual_points".to_owned(), json!(online));
        }
        if let Some(bar) = scoring.bar {
            columns.insert("autoqual_bar".to_owned(), json!(bar));
        }
    } else if model.slug() == "roundstats"
        && let Some(round) = columns.get("round_id").and_then(Value::as_i64)
        && let Some(stage) =
            sqlx::query_scalar::<_, String>("SELECT stage FROM core_round WHERE id=$1")
                .bind(round)
                .fetch_optional(conn)
                .await?
    {
        if stage == "outround" {
            for field in ["speaks", "ranks", "debater_role"] {
                columns.insert(field.to_owned(), Value::Null);
            }
        }
        columns.insert("stage".to_owned(), json!(stage));
    }
    Ok(())
}
