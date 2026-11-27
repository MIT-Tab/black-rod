use std::collections::{BTreeMap, BTreeSet};

use axum::{
    body::{Body, to_bytes},
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use minijinja::{Value, context};
use serde_json::Value as Json;
use sqlx::Row;

use super::{
    AdminRequest, cap_first, form, listing, redirect,
    schema::{Model, schema},
};
use crate::{
    ApiError,
    delete_relations::{self, DeleteError},
    flash, login,
};

fn common(model: &Model, id: i64, original: &str) -> Value {
    let app = schema().app(model.app()).expect("admin app");
    context! {
        app_url=>app.url(),app_name=>&app.name,app_label=>&app.label,model_name=>model.slug(),
        model_url=>model.url(),object_url=>format!("{}{id}/",model.url()),
        singular=>&model.singular,plural=>cap_first(&model.plural),original,
        css=>&model.list_media.css,js=>&model.list_media.js,
    }
}

fn translate_change(source: &str) -> String {
    if !source.starts_with('[') {
        return source.to_owned();
    }
    let Ok(Json::Array(rows)) = serde_json::from_str(source) else {
        return source.to_owned();
    };
    let mut result = Vec::new();
    for row in rows {
        if let Some(added) = row.get("added") {
            if let Some(name) = added["name"].as_str() {
                result.push(format!(
                    "Added {name} “{}”.",
                    added["object"].as_str().unwrap_or_default()
                ));
            } else {
                result.push("Added.".to_owned());
            }
        } else if let Some(changed) = row.get("changed") {
            let fields = changed["fields"]
                .as_array()
                .map(|fields| fields.iter().filter_map(Json::as_str).collect::<Vec<_>>())
                .unwrap_or_default();
            if let Some((last, former)) = fields.split_last() {
                let joined = if former.is_empty() {
                    last.to_string()
                } else {
                    format!("{} and {last}", former.join(", "))
                };
                let suffix = changed["name"].as_str().map_or(String::new(), |name| {
                    format!(
                        " for {name} “{}”",
                        changed["object"].as_str().unwrap_or_default()
                    )
                });
                result.push(format!("Changed {joined}{suffix}."));
            }
        } else if let Some(deleted) = row.get("deleted")
            && let Some(name) = deleted["name"].as_str()
        {
            result.push(format!(
                "Deleted {name} “{}”.",
                deleted["object"].as_str().unwrap_or_default()
            ));
        }
    }
    if result.is_empty() {
        "No fields changed.".to_owned()
    } else {
        result.join(" ")
    }
}

pub(super) async fn history(
    admin: &AdminRequest<'_>,
    model: &Model,
    id: i64,
) -> Result<Response, ApiError> {
    if !admin.viewer.read(model) {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let original = form::object_label(admin, model, id).await?;
    let query = login::form_values(admin.uri.query().unwrap_or_default().as_bytes());
    let page = query
        .get("p")
        .and_then(|page| page.parse::<i64>().ok())
        .unwrap_or(1)
        .clamp(1, i64::MAX / 100);
    let count:i64=sqlx::query_scalar("SELECT COUNT(*) FROM django_admin_log l JOIN django_content_type ct ON ct.id=l.content_type_id
        WHERE l.object_id=$1 AND ct.app_label=$2 AND ct.model=$3")
        .bind(id.to_string()).bind(model.app()).bind(model.slug()).fetch_one(&admin.state.pool).await?;
    let last = (count + 99).div_euclid(100).max(1);
    let page = page.min(last);
    let pages = (1..=last)
        .map(|number| context! {number,active=>page==number})
        .collect::<Vec<_>>();
    let mut actions = Vec::new();
    for row in sqlx::query(
        "SELECT CAST(l.action_time AS TEXT) AS stamp,l.change_message,u.username,
        TRIM(u.first_name || ' ' || u.last_name) AS full_name
        FROM django_admin_log l JOIN core_user u ON u.id=l.user_id
        JOIN django_content_type ct ON ct.id=l.content_type_id
        WHERE l.object_id=$1 AND ct.app_label=$2 AND ct.model=$3 ORDER BY l.action_time,l.id
        LIMIT 100 OFFSET $4",
    )
    .bind(id.to_string())
    .bind(model.app())
    .bind(model.slug())
    .bind((page - 1) * 100)
    .fetch_all(&admin.state.pool)
    .await?
    {
        actions.push(context!{
            date=>listing::date(row.try_get("stamp")?),user=>row.try_get::<&str,_>("username")?,
            full_name=>row.try_get::<&str,_>("full_name")?,change=>translate_change(row.try_get("change_message")?)
        });
    }
    let base = common(model, id, &original);
    admin.view(
        "site_admin/object_history.html",
        context! {title=>format!("Change history: {original}"),
        actions,pages,total=>count,..base},
    )
}

pub(super) async fn delete_entry(
    admin: &AdminRequest<'_>,
    model: &'static Model,
    id: i64,
    method: Method,
    body: Body,
) -> Result<Response, ApiError> {
    if !admin.viewer.allows(model, "delete") {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let confirmed = if method == Method::POST {
        let Ok(contents) = to_bytes(body, 128 * 1024).await else {
            return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
        };
        let values = login::form_values(&contents);
        let Some(origin) = admin.origin() else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            admin.headers,
            values.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        values.contains_key("post")
    } else {
        false
    };
    delete(admin, model, &[id], false, confirmed).await
}

pub(super) async fn delete(
    admin: &AdminRequest<'_>,
    model: &'static Model,
    ids: &[i64],
    bulk: bool,
    confirmed: bool,
) -> Result<Response, ApiError> {
    if !admin.viewer.allows(model, "delete") {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let Some(&first) = ids.first() else {
        return Ok(redirect(&model.url()));
    };
    let original = form::object_label(admin, model, first).await?;
    let mut tx = admin.state.pool.begin().await?;
    let mut plans = Vec::new();
    let mut lacking = BTreeSet::new();
    let mut protected = Vec::new();
    let mut removed = BTreeSet::new();
    let mut tree = Vec::new();
    for &id in ids {
        let plan = if confirmed {
            delete_relations::plan(&mut tx, &model.table, id).await
        } else {
            delete_relations::preview(&mut tx, &model.table, id).await
        };
        match plan {
            Err(DeleteError::Absent) => continue,
            Err(DeleteError::Protected) => {
                protected.push(model.singular.clone());
                continue;
            }
            Err(DeleteError::Database(error)) => return Err(error.into()),
            Ok(plan) => {
                let mut children = Vec::new();
                for (table, pk) in plan.removed() {
                    removed.insert((table, pk));
                    if table == model.table && pk == id {
                        continue;
                    }
                    if let Some(child) = schema()
                        .apps
                        .iter()
                        .flat_map(|app| &app.models)
                        .find(|model| model.table == table)
                    {
                        if !admin.viewer.allows(child, "delete") {
                            lacking.insert(child.singular.clone());
                        }
                        children.push(context!{kind=>cap_first(&child.singular),label=>form::object_label(admin,child,pk).await?,
                            url=>format!("{}{pk}/change/",child.url())});
                    }
                }
                let name = form::object_label(admin, model, id).await?;
                tree.push(context!{kind=>cap_first(&model.singular),label=>&name,url=>format!("{}{id}/change/",model.url()),children});
                plans.push((id, name, plan));
            }
        }
    }
    if confirmed && !lacking.is_empty() {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    if confirmed && protected.is_empty() {
        let actor = admin.viewer.user.id.ok_or(ApiError::Serialization)?;
        let count = plans.len();
        for (id, name, plan) in plans {
            form::log(
                &mut tx,
                model,
                actor,
                id,
                &name,
                3,
                Json::String(String::new()),
            )
            .await?;
            plan.apply(&mut tx).await.map_err(|error| match error {
                DeleteError::Database(error) => error.into(),
                _ => ApiError::Constraint,
            })?;
        }
        tx.commit().await?;
        admin.state.search.invalidate_all();
        let mut response = redirect(&model.url());
        if let Some(secret) = admin.state.secret.as_deref() {
            let message = if bulk {
                format!("Successfully deleted {count} {}.", model.plural)
            } else {
                format!(
                    "The {} “{original}” was deleted successfully.",
                    model.singular
                )
            };
            flash::push(
                &mut response,
                admin.headers,
                secret,
                25,
                &message,
                admin.secure(),
            );
        }
        return Ok(response);
    }
    let mut summary = BTreeMap::<String, usize>::new();
    for (table, _) in removed {
        if let Some(model) = schema()
            .apps
            .iter()
            .flat_map(|app| &app.models)
            .find(|model| model.table == table)
        {
            *summary.entry(cap_first(&model.plural)).or_default() += 1;
        }
    }
    let title = match (lacking.is_empty() && protected.is_empty(), bulk) {
        (true, false) => "Delete".to_owned(),
        (true, true) => "Are you sure?".to_owned(),
        (false, false) => format!("Cannot delete {}", model.singular),
        (false, true) => format!("Cannot delete {}", model.plural),
    };
    let base = common(model, first, &original);
    admin.view(
        "site_admin/delete_confirmation.html",
        context! {
            title, bulk, ids,tree,protected,lacking=>lacking.into_iter().collect::<Vec<_>>(),
            summary=>summary.into_iter().collect::<Vec<_>>(), ..base
        },
    )
}
