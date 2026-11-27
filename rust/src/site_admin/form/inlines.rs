use minijinja::{Value, context};
use serde_json::{Map, Value as Json, json};
use sqlx::{AnyConnection, AssertSqlSafe};

use super::{
    AdminRequest, ApiError, Bound, Errors, Values, cap_first, cleaned, default, error,
    object_label, primitive, raw, selected_fields,
};
use crate::delete_relations::{self, DeleteError};

use super::super::schema::{Form, Inline, Model};

fn foreign_key(inline: &Inline, model: &Model) -> String {
    model.fields[&inline.foreign_key]
        .column
        .clone()
        .expect("inline parent column")
}

async fn ids(
    admin: &AdminRequest<'_>,
    spec: &Inline,
    parent: Option<i64>,
) -> Result<Vec<i64>, ApiError> {
    let Some(parent) = parent else {
        return Ok(Vec::new());
    };
    let model = spec.model();
    let fk = foreign_key(spec, model);
    let order = spec
        .ordering
        .iter()
        .filter_map(|name| {
            let field = model.fields.get(name.trim_start_matches('-'))?;
            let column = field.column.as_ref()?;
            Some(format!(
                "{column} {}",
                if name.starts_with('-') { "DESC" } else { "ASC" }
            ))
        })
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!(
        "SELECT CAST(id AS BIGINT) FROM {} WHERE {fk}=$1 ORDER BY {order}",
        model.table
    );
    Ok(sqlx::query_scalar(AssertSqlSafe(sql))
        .bind(parent)
        .fetch_all(&admin.state.pool)
        .await?)
}

fn row_values(values: &Values, prefix: &str, index: usize) -> Values {
    let path = format!("{prefix}-{index}-");
    values
        .iter()
        .filter_map(|(name, values)| Some((name.strip_prefix(&path)?.to_owned(), values.clone())))
        .collect()
}

fn count(values: &Values, spec: &Inline) -> Option<usize> {
    raw(values, &format!("{}-TOTAL_FORMS", spec.prefix))
        .parse::<usize>()
        .ok()
        .filter(|count| *count <= spec.absolute_max)
}

struct RenderInput<'a> {
    admin: &'a AdminRequest<'a>,
    spec: &'a Inline,
    parent: Option<i64>,
    submitted: Option<&'a Values>,
    errors: &'a Errors,
}

impl RenderInput<'_> {
    async fn row(&self, index: usize, id: Option<i64>, empty: bool) -> Result<Value, ApiError> {
        let model = self.spec.model();
        let data = if let Some(id) = id {
            model
                .storage
                .get(&mut *self.admin.state.pool.acquire().await?, id)
                .await?
        } else {
            Json::Null
        };
        let values = self
            .submitted
            .filter(|_| !empty)
            .map(|values| row_values(values, &self.spec.prefix, index));
        let segment = if empty {
            "__prefix__".to_owned()
        } else {
            index.to_string()
        };
        let prefix = format!("{}-{segment}", self.spec.prefix);
        let row_errors = self
            .errors
            .iter()
            .filter_map(|(name, errors)| {
                Some((
                    name.strip_prefix(&format!("{prefix}-"))?.to_owned(),
                    errors.clone(),
                ))
            })
            .collect();
        let bound = Bound {
            spec: model,
            loaded: &data,
            id,
            submitted: values.as_ref(),
            errors: &row_errors,
        };
        let mut fields = Vec::new();
        for name in &self.spec.fields {
            let row = bound
                .field(self.admin, name, self.spec.widgets.get(name))
                .await?;
            let required = row.get_attr("required").is_ok_and(|value| value.is_true());
            fields.push(
                context! {name=>format!("{prefix}-{name}"),field_name=>name,widget_required=>false,
                label_required=>required,..row},
            );
        }
        let label = if let Some(id) = id {
            Some(object_label(self.admin, model, id).await?)
        } else {
            None
        };
        let change_url = id
            .filter(|_| self.spec.show_change_link)
            .map(|id| format!("{}{id}/change/", model.url()));
        let suffix = if empty {
            "empty".to_owned()
        } else {
            index.to_string()
        };
        let deleting = values
            .as_ref()
            .is_some_and(|values| values.contains_key("DELETE"));
        Ok(
            context! {dom_id=>format!("{}-{suffix}",self.spec.prefix),prefix,id, label, fields, empty,
            number=>index+1, change_url, deleting,parent=>self.parent,foreign_key=>&self.spec.foreign_key,
            errors=>row_errors.get("form")},
        )
    }
}

pub(super) async fn render(
    admin: &AdminRequest<'_>,
    form: &Form,
    parent: Option<i64>,
    submitted: Option<&Values>,
    errors: &Errors,
) -> Result<Vec<Value>, ApiError> {
    let mut groups = Vec::new();
    for spec in &form.inlines {
        let model = spec.model();
        let adding = admin.viewer.allows(model, "add");
        if !adding && !admin.viewer.read(model) {
            continue;
        }
        let existing = ids(admin, spec, parent).await?;
        let initial = existing.len();
        let total = submitted
            .and_then(|values| count(values, spec))
            .unwrap_or_else(|| initial.max(spec.min) + spec.extra);
        let input = RenderInput {
            admin,
            spec,
            parent,
            submitted,
            errors,
        };
        let mut rows = Vec::new();
        for index in 0..total {
            let id = submitted.map_or_else(
                || existing.get(index).copied(),
                |values| {
                    raw(&row_values(values, &spec.prefix, index), "id")
                        .parse::<i64>()
                        .ok()
                        .filter(|id| existing.contains(id))
                },
            );
            rows.push(input.row(index, id, false).await?);
        }
        if adding {
            rows.push(input.row(total, None, true).await?);
        }
        let columns = spec.fields.iter().map(|name| {
            let widget = &spec.widgets[name];
            let label = widget.label.clone().unwrap_or_else(|| cap_first(&model.fields[name].label));
            context! {name,label=>cap_first(&label),required=>widget.required,help=>&widget.help}
        }).collect::<Vec<_>>();
        let singular = cap_first(&model.singular);
        let heading = if spec.max == 1 {
            singular.clone()
        } else {
            cap_first(&model.plural)
        };
        groups.push(context! {kind=>&spec.kind,prefix=>&spec.prefix,data=>&spec.data,classes=>&spec.classes,
            rows,columns,singular,heading,initial,total,min=>spec.min,max=>if adding{spec.max}else{0},
            can_delete=>admin.viewer.allows(model,"delete"),can_change=>admin.viewer.allows(model,"change"),
            errors=>errors.get(&spec.prefix)});
    }
    Ok(groups)
}

pub(super) struct Change<'a> {
    spec: &'a Inline,
    id: Option<i64>,
    remove: bool,
    fields: Map<String, Json>,
    values: Values,
    changed: Vec<String>,
}

pub(super) async fn prepare<'a>(
    admin: &AdminRequest<'_>,
    form: &'a Form,
    parent: Option<i64>,
    values: &Values,
    errors: &mut Errors,
) -> Result<Vec<Change<'a>>, ApiError> {
    let mut changes = Vec::new();
    for spec in &form.inlines {
        let model = spec.model();
        if !admin.viewer.allows(model, "add") && !admin.viewer.read(model) {
            continue;
        }
        let Some(total) = count(values, spec) else {
            error(
                errors,
                &spec.prefix,
                &format!(
                    "ManagementForm data is missing or has been tampered with. Missing fields: {}-TOTAL_FORMS, {}-INITIAL_FORMS. You may need to file a bug report if the issue persists.",
                    spec.prefix, spec.prefix
                ),
            );
            continue;
        };
        let existing = ids(admin, spec, parent).await?;
        let mut seen = std::collections::BTreeSet::new();
        for index in 0..total {
            let values = row_values(values, &spec.prefix, index);
            let id = raw(&values, "id").parse::<i64>().ok();
            let prefix = format!("{}-{index}", spec.prefix);
            if id.is_some_and(|id| !existing.contains(&id) || !seen.insert(id)) {
                error(
                    errors,
                    &format!("{prefix}-form"),
                    "Select a valid choice. That choice is not one of the available choices.",
                );
                continue;
            }
            let remove = values.contains_key("DELETE");
            let may_write = admin.viewer.allows(
                model,
                if remove {
                    "delete"
                } else if id.is_some() {
                    "change"
                } else {
                    "add"
                },
            );
            if !may_write || (remove && id.is_none()) {
                continue;
            }
            let data = if let Some(id) = id {
                model
                    .storage
                    .get(&mut *admin.state.pool.acquire().await?, id)
                    .await?
            } else {
                Json::Null
            };
            if id.is_none()
                && spec.fields.iter().all(|name| {
                    let text = raw(&values, name);
                    text.is_empty() || text == primitive(&default(&model.fields[name]))
                })
            {
                continue;
            }
            let mut fields = Map::new();
            let mut changed = Vec::new();
            if !remove {
                let mut invalid = Errors::new();
                for (name, field) in &model.fields {
                    let Some(column) = field.column.as_ref().filter(|_| {
                        !field.primary && field.through.is_none() && *name != spec.foreign_key
                    }) else {
                        continue;
                    };
                    if let Some(widget) = spec.widgets.get(name) {
                        if let Some(value) = cleaned(field, widget, name, &values, &mut invalid) {
                            if data[column] != value {
                                changed.push(cap_first(&field.label));
                            }
                            fields.insert(column.clone(), value);
                        }
                    } else if id.is_none() || field.auto_now {
                        fields.insert(column.clone(), default(field));
                    }
                }
                for name in &spec.fields {
                    let field = &model.fields[name];
                    if field.through.is_some()
                        && id.is_some()
                        && super::memberships(admin, model, field, id.unwrap_or_default()).await?
                            != selected_fields(&values, name)
                    {
                        changed.push(cap_first(&field.label));
                    }
                }
                errors.extend(
                    invalid
                        .into_iter()
                        .map(|(name, errors)| (format!("{prefix}-{name}"), errors)),
                );
            }
            if remove || id.is_none() || !changed.is_empty() {
                changes.push(Change {
                    spec,
                    id,
                    remove,
                    fields,
                    values,
                    changed,
                });
            }
        }
    }
    Ok(changes)
}

pub(super) async fn apply(
    conn: &mut AnyConnection,
    parent: i64,
    changes: Vec<Change<'_>>,
) -> Result<Vec<Json>, ApiError> {
    let mut messages = Vec::new();
    for Change {
        spec,
        id,
        remove,
        mut fields,
        values,
        changed,
    } in changes
    {
        let model = spec.model();
        if remove {
            let id = id.expect("existing inline");
            let name = super::loaded_label(conn, model, id).await?;
            let plan = delete_relations::plan(conn, &model.table, id)
                .await
                .map_err(delete_error)?;
            plan.apply(conn).await.map_err(delete_error)?;
            messages.push(json!({"deleted":{"name":model.singular,"object":name}}));
            continue;
        }
        fields.insert(foreign_key(spec, model), json!(parent));
        super::record_rules(conn, model, &mut fields).await?;
        let record = model.storage.write(conn, &fields, id).await?;
        super::save_memberships(conn, model, &spec.widgets, record, &values).await?;
        let name = super::loaded_label(conn, model, record).await?;
        if id.is_none() {
            messages.push(json!({"added":{"name":model.singular,"object":name}}));
        } else {
            messages
                .push(json!({"changed":{"name":model.singular,"object":name,"fields":changed}}));
        }
    }
    Ok(messages)
}

fn delete_error(error: DeleteError) -> ApiError {
    match error {
        DeleteError::Database(error) => error.into(),
        _ => ApiError::Constraint,
    }
}
