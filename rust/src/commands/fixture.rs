use std::{collections::BTreeMap, path::Path};

use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, AssertSqlSafe};

use crate::{ApiError, AppState, agent::journal::Table};

use super::Result;

#[derive(Deserialize)]
struct Field {
    column: String,
    related: Option<String>,
    primary: bool,
}

#[derive(Deserialize)]
struct Membership {
    through: [String; 3],
    related: String,
}

#[derive(Deserialize)]
struct Model {
    model: String,
    title: String,
    natural: bool,
    fields: BTreeMap<String, Field>,
    memberships: BTreeMap<String, Membership>,
    ordering: Vec<(String, bool)>,
    storage: Table,
}

#[derive(Deserialize)]
struct Examples {
    first_names: Vec<String>,
    fake_cases: Vec<String>,
    fake_descriptions: Vec<String>,
    fake_youtube_ids: Vec<String>,
}

type Keys = BTreeMap<String, BTreeMap<i64, Value>>;

fn natural_key(model: &str, row: &Map<String, Value>) -> Option<Value> {
    Some(match model {
        "contenttypes.contenttype" => json!([row["app_label"], row["model"]]),
        "auth.permission" => {
            let content = row["content_type"].as_array()?;
            json!([row["codename"], content.first()?, content.get(1)?])
        }
        "auth.group" | "taggit.tag" => json!([row["name"]]),
        "taggit.taggeditem" => json!([row["object_id"]]),
        "core.user" => json!([row["username"]]),
        _ => return None,
    })
}

fn relation(keys: &Keys, model: &str, value: Value) -> Value {
    value
        .as_i64()
        .and_then(|id| keys.get(model)?.get(&id))
        .cloned()
        .unwrap_or(value)
}

async fn memberships(
    conn: &mut AnyConnection,
    keys: &Keys,
    model: &Model,
) -> std::result::Result<BTreeMap<i64, Map<String, Value>>, ApiError> {
    let mut result = BTreeMap::<i64, Map<String, Value>>::new();
    for (field, membership) in &model.memberships {
        let [table, owner, target] = &membership.through;
        let statement = format!(
            r#"SELECT CAST("{owner}" AS BIGINT),CAST("{target}" AS BIGINT)
               FROM "{table}" ORDER BY "{owner}","{target}""#
        );
        let rows = sqlx::query_as::<_, (i64, i64)>(AssertSqlSafe(statement))
            .fetch_all(&mut *conn)
            .await?;
        let mut members = BTreeMap::<i64, Vec<Value>>::new();
        for (owner, target) in rows {
            members.entry(owner).or_default().push(relation(
                keys,
                &membership.related,
                json!(target),
            ));
        }
        for (owner, mut members) in members {
            if model.model == "core.user" {
                members.sort_by_cached_key(Value::to_string);
            } else if membership.related == "auth.permission" {
                members.sort_by(|left, right| {
                    let key = |value: &Value, index: usize| {
                        value[index].as_str().unwrap_or_default().to_owned()
                    };
                    [1, 2, 0]
                        .into_iter()
                        .map(|index| key(left, index))
                        .cmp([1, 2, 0].into_iter().map(|index| key(right, index)))
                });
            }
            result
                .entry(owner)
                .or_default()
                .insert(field.clone(), json!(members));
        }
    }
    Ok(result)
}

fn sanitize(
    model: &str,
    id: i64,
    index: usize,
    length: usize,
    fields: &mut Map<String, Value>,
    examples: &Examples,
) {
    match model {
        "core.user" => {
            let names = &examples.first_names;
            fields.insert("first_name".to_owned(), json!(names[index % names.len()]));
            fields.insert(
                "last_name".to_owned(),
                json!(names[(length - index - 1) % names.len()]),
            );
            fields.insert("username".to_owned(), json!(format!("user_{id}")));
            fields.insert("email".to_owned(), json!(format!("user_{id}@example.com")));
            fields.insert(
                "password".to_owned(),
                json!("pbkdf2_sha256$260000$fake$fakehashfordev"),
            );
        }
        "core.video" => {
            let select = |values: &[String]| json!(values[index % values.len()]);
            let youtube = &examples.fake_youtube_ids[index % examples.fake_youtube_ids.len()];
            fields.insert("case".to_owned(), select(&examples.fake_cases));
            fields.insert(
                "description".to_owned(),
                select(&examples.fake_descriptions),
            );
            fields.insert(
                "link".to_owned(),
                json!(format!("https://www.youtube.com/watch?v={youtube}")),
            );
            fields.insert("password".to_owned(), json!(""));
        }
        "core.generatedcode" => {
            fields.insert("code".to_owned(), json!(format!("{id:012X}")));
        }
        "core.agentcredential" => {
            fields.insert("token_hash".to_owned(), json!(format!("{id:064x}")));
            fields.insert("revoked_at".to_owned(), json!("2000-01-01T00:00:00Z"));
        }
        "core.agentchangeset" => {
            fields.insert("reason".to_owned(), json!("Fixture change"));
            fields.insert("operations".to_owned(), json!([]));
            fields.insert("changes".to_owned(), json!([]));
            fields.insert("results".to_owned(), json!([]));
        }
        _ => (),
    }
}

pub(super) async fn export(state: &AppState, output: &Path) -> Result<()> {
    let models = serde_json::from_str::<Vec<Model>>(include_str!("../../schema/fixture.json"))?;
    let examples =
        serde_json::from_str::<Examples>(include_str!("../../schema/fixture_examples.json"))?;
    let mut tx = crate::team_profile::read_transaction(&state.pool).await?;
    let mut objects = Vec::new();
    let mut keys = Keys::new();
    for model in models {
        let rows = model
            .storage
            .rows(&mut tx, &model.ordering)
            .await
            .map_err(|error| format!("Could not read {}: {error:?}", model.model))?;
        let length = rows.len();
        match model.model.as_str() {
            "core.user" => println!("Simulating {length} users"),
            "core.video" => println!("Simulating {length} videos"),
            _ => println!("Processing {}: {length} objects", model.title),
        }
        let mut linked = memberships(&mut tx, &keys, &model)
            .await
            .map_err(|error| format!("Could not read {} relationships: {error:?}", model.model))?;
        for (index, mut row) in rows.into_iter().enumerate() {
            let id = row
                .get("id")
                .and_then(Value::as_i64)
                .ok_or("Fixture row lacks an ID")?;
            let mut fields = Map::new();
            for (name, field) in &model.fields {
                if field.primary {
                    continue;
                }
                let raw = row.remove(&field.column).unwrap_or(Value::Null);
                let value = match &field.related {
                    Some(related) => relation(&keys, related, raw),
                    None => raw,
                };
                fields.insert(name.clone(), value);
            }
            let mut linked_fields = linked.remove(&id).unwrap_or_default();
            for name in model.memberships.keys() {
                fields.insert(
                    name.clone(),
                    linked_fields.remove(name).unwrap_or_else(|| json!([])),
                );
            }
            sanitize(&model.model, id, index, length, &mut fields, &examples);
            let key = natural_key(&model.model, &fields);
            let mut record = json!({"model":model.model,"fields":fields});
            if model.natural {
                if let Some(key) = key {
                    keys.entry(model.model.clone()).or_default().insert(id, key);
                }
            } else {
                record["pk"] = json!(id);
            }
            objects.push(record);
        }
    }
    tx.commit().await?;
    std::fs::write(output, crate::django_json::encode(&objects)?)?;
    println!(
        "Successfully created {} with {} objects",
        output.display(),
        objects.len()
    );
    Ok(())
}
