mod inlines;
mod tags;
pub(super) mod validation;

use std::collections::{BTreeMap, BTreeSet};

use axum::{
    extract::{DefaultBodyLimit, FromRequest, Multipart, Request},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use chrono::{DateTime, SecondsFormat, Utc};
use minijinja::{Value, context};
use serde_json::{Map, Value as Json, json};
use sqlx::{Any, AnyConnection, AssertSqlSafe, Executor, Row};

use super::{
    AdminRequest, cap_first,
    listing::{Source, display, label},
    model_rules, redirect,
    schema::{Field, Model, Widget, schema},
};
use crate::{
    ApiError, account,
    agent::journal::storage_error,
    flash, login,
    memberships::sync_memberships,
    upload_file::{self, UploadFile},
};
use validation::{RelatedSelections, clean_memberships, cleaned};

type Errors = BTreeMap<String, Vec<String>>;
type Values = BTreeMap<String, Vec<String>>;

const FORM_CONSTRAINT: &str =
    "A record with these values already exists, or a referenced record could not be found.";

fn primitive(value: &Json) -> String {
    match value {
        Json::String(value) => value.clone(),
        Json::Null => String::new(),
        value => value.to_string(),
    }
}

pub(super) fn default(field: &Field) -> Json {
    if let Some(value) = &field.default {
        return value.clone();
    }
    if field.auto_now || field.auto_now_add || field.default_fn.as_deref() == Some("now") {
        return json!(Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true));
    }
    match field.default_fn.as_deref() {
        Some("dict") => json!({}),
        Some("list") => json!([]),
        Some("uuid4") => json!(uuid::Uuid::new_v4().to_string()),
        _ if field.null => Json::Null,
        _ if field.kind == "BooleanField" => json!(false),
        _ if field.blank => json!(""),
        _ => Json::Null,
    }
}

fn selected_fields(values: &Values, name: &str) -> Vec<String> {
    values.get(name).cloned().unwrap_or_default()
}

fn raw<'a>(values: &'a Values, name: &str) -> &'a str {
    values
        .get(name)
        .and_then(|values| values.last())
        .map_or("", String::as_str)
}

fn error(errors: &mut Errors, field: &str, message: &str) {
    errors
        .entry(field.to_owned())
        .or_default()
        .push(message.to_owned());
}

fn related_model(field: &Field) -> Option<&'static Model> {
    let (app, model) = field.related.as_deref()?.split_once('.')?;
    schema().model(app, model)
}

async fn memberships(
    admin: &AdminRequest<'_>,
    model: &Model,
    field: &Field,
    id: i64,
) -> Result<Vec<String>, ApiError> {
    let Some([table, owner, target]) = &field.through else {
        return Ok(Vec::new());
    };
    let generic = field.generic_through.unwrap_or_default();
    let restriction = if generic {
        " AND content_type_id=(SELECT id FROM django_content_type WHERE app_label=$2 AND model=$3)"
    } else {
        ""
    };
    let sql = format!(
        "SELECT CAST({target} AS TEXT) FROM {table} WHERE {owner}=$1{restriction} ORDER BY id"
    );
    let mut query = sqlx::query_scalar::<_, String>(AssertSqlSafe(sql)).bind(id);
    if generic {
        query = query.bind(model.app()).bind(model.slug());
    }
    Ok(query.fetch_all(&admin.state.pool).await?)
}

async fn choices(
    admin: &AdminRequest<'_>,
    model: &Model,
    field: &Field,
    selected: &[String],
    short: bool,
    owner: &str,
) -> Result<Vec<(String, String)>, ApiError> {
    let Some(target) = related_model(field) else {
        let label = |app: &str, model: &str| {
            let key = format!("{app}.{model}");
            schema()
                .content_types
                .get(&key)
                .cloned()
                .unwrap_or_else(|| format!("{app} | {model}"))
        };
        return match field.related.as_deref() {
            Some("contenttypes.contenttype") => Ok(sqlx::query_as::<_, (String,String,String)>(
                "SELECT CAST(id AS TEXT),app_label,model FROM django_content_type ORDER BY id")
                .fetch_all(&admin.state.pool).await?.into_iter().map(|(id,app,model)|(id,label(&app,&model))).collect()),
            Some("auth.permission") => Ok(sqlx::query_as::<_, (String,String,String,String)>(
                "SELECT CAST(p.id AS TEXT),ct.app_label,ct.model,p.name FROM auth_permission p
                 JOIN django_content_type ct ON ct.id=p.content_type_id ORDER BY ct.app_label,ct.model,p.codename")
                .fetch_all(&admin.state.pool).await?.into_iter()
                .map(|(id,app,model,name)|(id,format!("{} | {name}",label(&app,&model)))).collect()),
            _ => Ok(Vec::new()),
        };
    };
    if model.slug() == "totyreaff" && target.slug() == "team" {
        return reaff_team_choices(admin).await;
    }
    let ids = selected
        .iter()
        .filter_map(|value| value.parse::<i64>().ok())
        .collect::<Vec<_>>();
    if short && ids.is_empty() {
        return Ok(Vec::new());
    }
    let mut source = Source::new(target);
    let repr = label(&mut source, target, "");
    let mut filters = Vec::new();
    if short {
        let values = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
        filters.push(format!("t0.id IN ({values})"));
    } else if model.slug() == "teamresult" && field.kind == "ManyToManyField" {
        if let Ok(owner) = owner.parse::<i64>() {
            filters.push(format!(
                "t0.id IN (SELECT debater_id FROM core_team_debaters WHERE team_id={owner})"
            ));
        } else {
            filters.push(
                "NOT EXISTS (SELECT 1 FROM core_team_debaters WHERE debater_id=t0.id)".to_owned(),
            );
        }
    } else {
        match target.slug() {
            "school" => filters.push("NOT t0.temporary".to_owned()),
            "debater" => filters.push("NOT(t0.temporary OR t0.synthetic)".to_owned()),
            _ => {}
        }
    }
    let where_ = if filters.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", filters.join(" AND "))
    };
    let order = if model.slug() == "teamresult" && field.kind == "ManyToManyField" {
        "t0.first_name,t0.last_name,t0.id".to_owned()
    } else {
        super::listing::choice_ordering(&mut source, target)
    };
    let sql = format!(
        "SELECT CAST(t0.id AS TEXT),{repr} AS repr FROM {}{where_} ORDER BY {order}",
        source.sql()
    );
    Ok(sqlx::query_as::<_, (String, String)>(AssertSqlSafe(sql))
        .fetch_all(&admin.state.pool)
        .await?)
}

async fn reaff_team_choices(admin: &AdminRequest<'_>) -> Result<Vec<(String, String)>, ApiError> {
    let mut db = admin.state.pool.acquire().await?;
    let choices = sqlx::query_as::<_, (i64, String)>(
        "SELECT CAST(t.id AS BIGINT),t.name FROM core_team t
         LEFT JOIN core_team_debaters td ON td.team_id=t.id
         LEFT JOIN core_debater d ON d.id=td.debater_id LEFT JOIN core_school s ON s.id=d.school_id
         ORDER BY s.name,d.first_name,d.last_name",
    )
    .fetch_all(&mut *db)
    .await?;
    let unique = choices
        .iter()
        .map(|(id, _)| *id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let rosters = crate::team_profile::members_for(&mut db, &unique, "http://localhost").await?;
    Ok(choices
        .into_iter()
        .map(|(id, name)| {
            let roster = rosters.get(&id).map(Vec::as_slice).unwrap_or_default();
            let name = match roster {
                [] => name,
                [one, other] => {
                    let school = label_school(one);
                    let other_school = label_school(other);
                    let schools = if school == other_school {
                        school.to_owned()
                    } else {
                        format!("{school} / {other_school}")
                    };
                    format!(
                        "{schools} {} and {}",
                        label_debater(one),
                        label_debater(other)
                    )
                }
                [one, ..] => format!("{} {}", label_school(one), label_debater(one)),
            };
            (id.to_string(), name)
        })
        .collect())
}

fn label_school(person: &Json) -> &str {
    person["school"]["name"].as_str().unwrap_or("Unaffiliated")
}

fn label_debater(person: &Json) -> String {
    format!(
        "{} {}",
        person["first_name"].as_str().unwrap_or_default(),
        person["last_name"].as_str().unwrap_or_default()
    )
}

const GRID_GROUP: &str = "SELECT id FROM core_debater
    WHERE id=$1 OR alias_group_id=(SELECT alias_group_id FROM core_debater WHERE id=$1)";

async fn grid_hidden<'e>(db: impl Executor<'e, Database = Any>, id: i64) -> Result<bool, ApiError> {
    let statement = format!(
        "SELECT CAST(CASE WHEN EXISTS (
        SELECT 1 FROM core_immaculategridhiddendebater WHERE debater_id IN ({GRID_GROUP})
    ) THEN 1 ELSE 0 END AS BIGINT)"
    );
    let hidden = sqlx::query_scalar::<_, i64>(AssertSqlSafe(statement))
        .bind(id)
        .fetch_one(db)
        .await?;
    Ok(hidden != 0)
}

fn checkbox_posted(values: &Values, name: &str) -> bool {
    values
        .get(name)
        .and_then(|items| items.last())
        .is_some_and(|value| !value.is_empty() && !value.eq_ignore_ascii_case("false"))
}

struct Bound<'a> {
    spec: &'a Model,
    loaded: &'a Json,
    id: Option<i64>,
    submitted: Option<&'a Values>,
    errors: &'a Errors,
}

impl Bound<'_> {
    async fn field(
        &self,
        admin: &AdminRequest<'_>,
        name: &str,
        widget: Option<&Widget>,
    ) -> Result<Value, ApiError> {
        let model = self.spec;
        let field = model.fields.get(name);
        let choice_kind = widget.map_or("", |widget| widget.kind.as_str());
        let value = field.map_or(Json::Null, |field| {
            if let Some(column) = &field.column
                && self.id.is_some()
            {
                self.loaded[column].clone()
            } else if field.auto_now || field.auto_now_add {
                Json::Null
            } else {
                default(field)
            }
        });
        let read_only =
            widget.is_none() || self.id.is_some() && !admin.viewer.allows(model, "change");
        let checkbox = !read_only && choice_kind == "CheckboxInput";
        let multiple = matches!(
            choice_kind,
            "SelectMultiple"
                | "AutocompleteSelectMultiple"
                | "FilteredSelectMultiple"
                | "ModelSelect2Multiple"
        );
        let raw_id = matches!(
            choice_kind,
            "ForeignKeyRawIdWidget" | "ManyToManyRawIdWidget"
        );
        let dal = matches!(choice_kind, "ModelSelect2" | "ModelSelect2Multiple");
        let radio = choice_kind == "RadioSelect";
        let multicheck = choice_kind == "CheckboxSelectMultiple";
        let autocomplete = choice_kind.starts_with("AutocompleteSelect");
        let select = matches!(
            choice_kind,
            "Select" | "NullBooleanSelect" | "SelectMultiple"
        ) || autocomplete
            || dal
            || multiple;
        let textarea = matches!(
            choice_kind,
            "Textarea" | "AdminTextareaWidget" | "AdminJSONFieldWidget"
        );
        let datetime = choice_kind == "AdminSplitDateTime";
        let password_status = choice_kind == "ReadOnlyPasswordHashWidget";
        let required = widget.is_some_and(|widget| widget.required)
            && !read_only
            && !(model.slug() == "user"
                && self.id.is_none()
                && matches!(name, "password1" | "password2"));
        let mut caption = widget
            .and_then(|widget| widget.label.clone())
            .or_else(|| field.map(|field| cap_first(&field.label)))
            .unwrap_or_else(|| cap_first(&name.replace('_', " ")));
        if !checkbox {
            caption.push(':');
        }
        let mut attrs = widget
            .map(|widget| widget.attributes.clone())
            .unwrap_or_default();
        if model.slug() == "user" && self.id.is_none() && name == "username" {
            attrs.insert("autofocus".into(), json!(true));
        }
        let text = if let Some(values) = self.submitted {
            raw(values, name).to_owned()
        } else if choice_kind == "TagWidget"
            && let (Some(field), Some(id)) = (field, self.id)
        {
            tags::display(admin, model, field, id).await?
        } else {
            primitive(&value)
        };
        let (date, time) = text.split_once(['T', ' ']).unwrap_or((text.as_str(), ""));
        let checked = if name == "immaculate_hidden" {
            match (self.submitted, self.id) {
                (Some(values), _) => checkbox_posted(values, name),
                (None, Some(id)) => grid_hidden(&admin.state.pool, id).await?,
                (None, None) => false,
            }
        } else {
            self.submitted
                .map_or(value.as_bool().unwrap_or_default(), |values| {
                    checkbox_posted(values, name)
                })
        };
        let widget_help = widget
            .map(|widget| widget.help.as_str())
            .unwrap_or_default();
        let mut help = widget_help.to_owned();
        let kind = match choice_kind {
            "CheckboxInput" => "checkbox",
            "NumberInput" | "AdminIntegerFieldWidget" | "AdminBigIntegerFieldWidget" => "number",
            "AdminEmailInputWidget" | "EmailInput" => "email",
            "PasswordInput" => "password",
            "URLInput" | "AdminURLFieldWidget" => "url",
            "FileInput" | "ClearableFileInput" | "AdminFileWidget" => "file",
            "HiddenInput" => "hidden",
            _ => "text",
        };
        let mut selected = vec![text.clone()];
        if let Some(field) = field
            && field.through.is_some()
        {
            selected = if let Some(values) = self.submitted {
                selected_fields(values, name)
            } else if let Some(id) = self.id {
                memberships(admin, model, field, id).await?
            } else {
                Vec::new()
            };
        }
        if let Some(size) = field.and_then(|field| field.size)
            && !select
            && matches!(kind, "text" | "email" | "url")
            && !attrs.contains_key("maxlength")
        {
            attrs.insert("maxlength".into(), json!(size));
        }
        let mut text_value = text.clone();
        if choice_kind == "ManyToManyRawIdWidget" {
            text_value = selected.join(",");
        }
        let current_url = if choice_kind == "AdminURLFieldWidget" {
            url::Url::parse(&text)
                .ok()
                .filter(|url| matches!(url.scheme(), "http" | "https" | "ftp" | "ftps"))
                .map(|_| text.as_str())
        } else {
            None
        };
        let raw_url = widget.and_then(|widget| widget.related_url.as_deref());
        let raw_caption = if raw_id && choice_kind != "ManyToManyRawIdWidget" {
            if let (Some(target), Ok(id)) = (field.and_then(related_model), text.parse::<i64>()) {
                Some((
                    format!("{}{id}/change/", target.url()),
                    object_label(admin, target, id).await?,
                ))
            } else {
                None
            }
        } else {
            None
        };
        let mut options = widget
            .map(|widget| widget.choices(admin.state.current_season).into_owned())
            .unwrap_or_default();
        let target = field.and_then(related_model);
        if !read_only
            && (select || multicheck)
            && let Some(field) = field
            && field.related.is_some()
        {
            let team = if let Some(values) = self.submitted {
                raw(values, "team").to_owned()
            } else {
                primitive(&self.loaded["team_id"])
            };
            options = choices(admin, model, field, &selected, autocomplete || dal, &team).await?;
            if autocomplete && !required && !multiple {
                options.insert(0, (String::new(), String::new()));
            } else if !autocomplete
                && !multiple
                && !multicheck
                && let Some(empty_label) = widget.and_then(|widget| widget.empty_label.clone())
            {
                options.insert(0, (String::new(), empty_label));
            }
            if multicheck && model.slug() == "teamresult" {
                help = field.help.clone();
                if team.is_empty() {
                    help.push_str(" Save the team first to select members.");
                }
            }
        }
        if autocomplete {
            for (name, value) in [
                ("class", "admin-autocomplete"),
                ("data-ajax--cache", "true"),
                ("data-ajax--delay", "250"),
                ("data-ajax--type", "GET"),
                ("data-ajax--url", "/admin/autocomplete/"),
                ("data-theme", "admin-autocomplete"),
                ("data-allow-clear", if required { "false" } else { "true" }),
                ("data-placeholder", ""),
                ("data-app-label", model.app()),
                ("data-model-name", model.slug()),
                ("data-field-name", name),
                ("lang", "en"),
            ] {
                attrs.insert(name.to_owned(), json!(value));
            }
        }
        if radio && selected == [String::new()] && name == "usable_password" {
            selected = vec!["true".to_owned()];
        }
        let widget_required = required
            && (kind != "file" || value.as_str().is_none_or(str::is_empty))
            && (!select
                || multiple
                || autocomplete
                || dal
                || options.first().is_some_and(|option| option.0.is_empty()));
        let mut found = false;
        let items = options
            .into_iter()
            .map(|(value, label)| {
                let chosen = (!found || multiple || multicheck)
                    && !(autocomplete && value.is_empty())
                    && selected.contains(&value);
                found |= chosen;
                context! {selected=>chosen,value,label}
            })
            .collect::<Vec<_>>();
        let related = target.map(|target| {
            context! {
                slug=>target.slug(),url=>target.url(),singular=>&target.singular,
                add=>admin.viewer.allows(target,"add"),
                change=>!multiple&&!multicheck&&admin.viewer.allows(target,"change"),
                delete=>!multiple&&!multicheck&&widget.is_some_and(|widget|widget.related_delete)&&admin.viewer.allows(target,"delete"),
                view=>!multiple&&!multicheck&&admin.viewer.read(target),
            }
        });
        let related = related.or_else(|| match field?.related.as_deref()? {
            "auth.permission" => Some(context! {slug=>"permission"}),
            "contenttypes.contenttype" => Some(context! {slug=>"contenttype"}),
            _ => None,
        });
        let forward = widget.and_then(|widget| widget.forward.as_deref());
        let mut hash = text.split('$');
        let algorithm = hash.next().unwrap_or_default();
        let iterations = hash.next().unwrap_or_default();
        let mask = |value: &str| {
            value
                .chars()
                .take(6)
                .chain(value.chars().skip(6).map(|_| '*'))
                .collect::<String>()
        };
        let salt = mask(hash.next().unwrap_or_default());
        let digest = mask(hash.next().unwrap_or_default());
        let mut preformatted = false;
        let read_link = if read_only && let (Some(target), Ok(id)) = (target, text.parse::<i64>()) {
            Some(format!("{}{id}/change/", target.url()))
        } else {
            None
        };
        let read_value = if name == "metadata_pretty"
            && matches!(model.slug(), "round" | "roundstats")
        {
            caption = "Metadata JSON:".to_owned();
            let metadata = &self.loaded["metadata"];
            if metadata.is_null() || metadata.as_object().is_some_and(|object| object.is_empty()) {
                Value::from("No metadata")
            } else {
                let formatted = serde_json::to_string_pretty(metadata).map_err(ApiError::from)?;
                preformatted = true;
                Value::from(crate::django_json::ascii(&formatted))
            }
        } else if model.slug() == "round" && name == "stats_count" {
            caption = "Stats:".to_owned();
            match self.id {
                Some(id) => {
                    let count = sqlx::query_scalar::<_, i64>(
                        "SELECT COUNT(*) FROM core_roundstats WHERE round_id=$1",
                    )
                    .bind(id)
                    .fetch_one(&admin.state.pool)
                    .await?;
                    Value::from(count)
                }
                None => Value::from("-"),
            }
        } else if model.slug() == "round" && name == "imported_metadata_summary" {
            caption = "Imported Metadata:".to_owned();
            match self.id {
                Some(id) => Value::from(round_metadata(admin, id).await?),
                None => Value::from("No imported metadata"),
            }
        } else if name == "get_absolute_url" && matches!(model.slug(), "motion" | "resource") {
            let resource = model.slug() == "resource";
            caption = if resource {
                "Resource URL:"
            } else {
                "Public URL:"
            }
            .to_owned();
            Value::from(self.id.map_or_else(
                || "-".to_owned(),
                |id| {
                    format!(
                        "/core/{}/{id}",
                        if resource { "resources" } else { "motions" }
                    )
                },
            ))
        } else if password_status {
            Value::from("No password set.")
        } else if let (Some(_), Some(target), Ok(id)) = (&read_link, target, text.parse::<i64>()) {
            Value::from(object_label(admin, target, id).await?)
        } else if read_only
            && self.id.is_none()
            && field.is_some_and(|field| field.kind == "JSONField")
        {
            Value::from("-")
        } else {
            display(Some(&text), field, false)
        };
        let file_path = (kind == "file")
            .then_some(value.as_str())
            .flatten()
            .filter(|path| {
                path.starts_with("django-summernote/") && !path.split('/').any(|part| part == "..")
            });
        Ok(context! {
            file_path, admin_file=>choice_kind=="AdminFileWidget", name,label=>caption,help,required,widget_required,date_widget=>choice_kind == "AdminDateWidget",datetime,date,time=>time.split(['.','+','Z']).next().unwrap_or_default(),
            checkbox, checked, readonly=>read_only, read_value, read_link, preformatted, wrapped=>widget.is_some_and(|widget|widget.wrapped),
            value=>text_value,type=>kind, attrs=>attrs.iter().collect::<Vec<_>>(),related, select,multiple,radio,multicheck,empty_input_value=>choice_kind=="ManyToManyRawIdWidget",
            raw_url,raw_caption,forward,current_url,hidden_initial=>widget.is_some_and(|widget|widget.hidden_initial),
            textarea,password_status,algorithm,iterations,salt,digest,usable=>!text.starts_with('!'),
            options=>items,error=>self.errors.get(name)
        })
    }
}

async fn round_metadata(admin: &AdminRequest<'_>, id: i64) -> Result<String, ApiError> {
    let row = sqlx::query(
        "SELECT m.raw_result_code,m.raw_outcome_text,
            g1.source_name AS g1,g2.source_name AS g2,o1.source_name AS o1,o2.source_name AS o2
         FROM core_importedroundmetadata m
         LEFT JOIN core_debateralias g1 ON g1.id=m.gov_1_alias_id
         LEFT JOIN core_debateralias g2 ON g2.id=m.gov_2_alias_id
         LEFT JOIN core_debateralias o1 ON o1.id=m.opp_1_alias_id
         LEFT JOIN core_debateralias o2 ON o2.id=m.opp_2_alias_id
         WHERE m.round_id=$1",
    )
    .bind(id)
    .fetch_optional(&admin.state.pool)
    .await?;
    let Some(row) = row else {
        return Ok("No imported metadata".to_owned());
    };
    let mut summary = Vec::new();
    let code = row.try_get::<&str, _>("raw_result_code")?;
    if !code.is_empty() {
        summary.push(format!("Result code: {code}"));
    }
    for (side, keys) in [("Gov", ["g1", "g2"]), ("Opp", ["o1", "o2"])] {
        let aliases = keys
            .into_iter()
            .map(|name| row.try_get::<Option<&str>, _>(name))
            .collect::<Result<Vec<_>, _>>()?
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        if !aliases.is_empty() {
            summary.push(format!("{side} aliases: {}", aliases.join(", ")));
        }
    }
    let outcome = row.try_get::<&str, _>("raw_outcome_text")?;
    if !outcome.is_empty() {
        summary.push(outcome.to_owned());
    }
    Ok(if summary.is_empty() {
        "Imported metadata present".to_owned()
    } else {
        summary.join(" | ")
    })
}

async fn render_form(
    admin: &AdminRequest<'_>,
    model: &Model,
    id: Option<i64>,
    loaded: &Json,
    submitted: Option<&Values>,
    errors: &Errors,
) -> Result<Response, ApiError> {
    let adding = id.is_none();
    if adding && matches!(model.key.as_str(), "core.motion" | "core.resource") {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    }
    let sets = if adding {
        &model.fieldsets_add
    } else {
        &model.fieldsets_change
    };
    let form = if adding {
        &model.form_add
    } else {
        &model.form_change
    };
    let bound = Bound {
        spec: model,
        loaded,
        id,
        submitted,
        errors,
    };
    let mut fieldsets = Vec::new();
    for set in sets {
        let mut rows = Vec::new();
        for line in &set.fields {
            let mut cells = Vec::new();
            for name in line.names() {
                cells.push(bound.field(admin, name, form.widgets.get(name)).await?);
            }
            rows.push(cells);
        }
        fieldsets.push(
            context! {title=>&set.title,classes=>&set.classes,description=>&set.description,
            collapsible=>set.classes.iter().any(|class|class=="collapse"),lines=>rows},
        );
    }
    let inlines = inlines::render(admin, form, id, submitted, errors).await?;
    let app = schema().app(model.app()).expect("admin app metadata");
    let original = if let Some(id) = id {
        object_label(admin, model, id).await?
    } else {
        String::new()
    };
    let url = model.url();
    let object_url = id.map(|id| format!("{url}{id}/")).unwrap_or_default();
    let history_url = admin.preserved_url(format!("{object_url}history/"), false);
    let delete_url = admin.preserved_url(format!("{object_url}delete/"), false);
    let close_url = admin.preserved_url(url.clone(), true);
    let site_object = if let Some(id) = id.filter(|_| {
        matches!(
            model.slug(),
            "school" | "debater" | "tournament" | "team" | "motion" | "resource" | "video"
        )
    }) {
        let content = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM django_content_type WHERE app_label=$1 AND model=$2",
        )
        .bind(model.app())
        .bind(model.slug())
        .fetch_optional(&admin.state.pool)
        .await?;
        content.map(|content| format!("/admin/r/{content}/{id}/"))
    } else {
        None
    };
    admin.view("site_admin/change_form.html",context!{
        title=>format!("{} {}",if adding{"Add"}else if admin.viewer.allows(model,"change"){"Change"}else{"View"},model.singular),
        subtitle=>id.map(|_|&original), original,
        plural=>cap_first(&model.plural), app_label=>model.app(),app_name=>&app.name,app_url=>app.url(),
        model_name=>model.slug(),model_url=>url,history_url,delete_url,close_url,
        fieldsets,inlines,save_on_top=>model.save_on_top,add=>adding,errors,
        css=>&form.media().css, js=>&form.media().js, multipart=>model.key=="django_summernote.attachment",
        add_user=>adding && model.slug()=="user", can_view=>admin.viewer.read(model),
        can_add=>admin.viewer.allows(model,"add"),can_change=>admin.viewer.allows(model,"change"),
        can_delete=>admin.viewer.allows(model,"delete"),save=>if adding{admin.viewer.allows(model,"add")}else{admin.viewer.allows(model,"change")},
        site_object,
        to_field=>login::form_values(admin.uri.query().unwrap_or_default().as_bytes()).remove("_to_field"),
    })
}

pub(super) async fn object_label(
    admin: &AdminRequest<'_>,
    model: &Model,
    id: i64,
) -> Result<String, ApiError> {
    let mut conn = admin.state.pool.acquire().await?;
    loaded_label(&mut conn, model, id).await
}

pub(super) async fn loaded_label(
    conn: &mut AnyConnection,
    model: &Model,
    id: i64,
) -> Result<String, ApiError> {
    let mut source = Source::new(model);
    let repr = label(&mut source, model, "");
    let sql = format!("SELECT {repr} FROM {} WHERE t0.id=$1", source.sql());
    Ok(sqlx::query_scalar::<_, Option<String>>(AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(conn)
        .await?
        .flatten()
        .unwrap_or_else(|| id.to_string()))
}

pub(super) async fn log(
    conn: &mut AnyConnection,
    model: &Model,
    user: i64,
    id: i64,
    name: &str,
    action: i64,
    message: Json,
) -> Result<(), ApiError> {
    let text = name.chars().take(200).collect::<String>();
    let change = message
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| message.to_string());
    sqlx::query("INSERT INTO django_admin_log (action_time,object_id,object_repr,action_flag,change_message,content_type_id,user_id)
        SELECT CURRENT_TIMESTAMP,$1,$2,$3,$4,id,$5 FROM django_content_type WHERE app_label=$6 AND model=$7")
        .bind(id.to_string()).bind(text).bind(action).bind(change).bind(user).bind(model.app()).bind(model.slug())
        .execute(conn).await?;
    Ok(())
}

async fn save_memberships(
    conn: &mut AnyConnection,
    model: &Model,
    widgets: &BTreeMap<String, Widget>,
    record: i64,
    values: &Values,
    selections: &RelatedSelections,
) -> Result<(), ApiError> {
    for (name, field) in &model.fields {
        if field.kind == "Tags" && widgets.contains_key(name) {
            tags::save(conn, model, field, record, raw(values, name)).await?;
            continue;
        }
        if let (Some([table, current, target]), Some(new)) = (&field.through, selections.get(name))
        {
            sync_memberships(
                conn,
                [table, current, target],
                record,
                new,
                field.related.as_deref() == Some("core.debater"),
            )
            .await?;
        }
    }
    Ok(())
}

async fn save(
    admin: &AdminRequest<'_>,
    model: &Model,
    id: Option<i64>,
    loaded: &Json,
    values: &Values,
    errors: &mut Errors,
) -> Result<Option<Response>, ApiError> {
    let form = if id.is_some() {
        &model.form_change
    } else {
        &model.form_add
    };
    let mut columns = Map::new();
    let mut changed = Vec::new();
    for (name, field) in &model.fields {
        let Some(column) = field
            .column
            .as_ref()
            .filter(|_| !field.primary && field.through.is_none())
        else {
            continue;
        };
        if let Some(widget) = form
            .widgets
            .get(name)
            .filter(|widget| widget.kind != "ReadOnlyPasswordHashWidget")
        {
            if let Some(value) = cleaned(admin, model, field, widget, name, values, errors).await? {
                if loaded[column] != value {
                    changed.push(cap_first(&field.label));
                }
                columns.insert(column.clone(), value);
            }
        } else if id.is_none() || field.auto_now {
            columns.insert(column.clone(), default(field));
        }
    }
    if model.slug() == "user" && id.is_none() {
        let enabled = !matches!(raw(values, "usable_password"), "false" | "False" | "0");
        if enabled {
            let fields = values
                .iter()
                .filter_map(|(name, values)| Some((name.clone(), values.last()?.clone())))
                .collect();
            let validation =
                account::validate_password(&fields, [raw(values, "username"), "", "", ""]);
            for (name, messages) in validation {
                errors.insert(name.to_owned(), messages);
            }
            if errors.is_empty() {
                columns.insert(
                    "password".to_owned(),
                    json!(account::new_password(&fields["password1"]).await?),
                );
            }
        } else {
            let random = crate::session::random_token(
                b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                40,
            )
            .map_err(|_| ApiError::Serialization)?;
            columns.insert("password".to_owned(), json!(format!("!{random}")));
        }
    }
    if model.slug() == "teamresult"
        && let Some(team) = columns.get("team_id").and_then(Json::as_i64)
    {
        let allowed = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(debater_id AS BIGINT) FROM core_team_debaters WHERE team_id=$1",
        )
        .bind(team)
        .fetch_all(&admin.state.pool)
        .await?;
        for selected in selected_fields(values, "hidden_from_debaters") {
            if selected
                .parse::<i64>()
                .ok()
                .is_none_or(|id| !allowed.contains(&id))
            {
                error(
                    errors,
                    "hidden_from_debaters",
                    &format!(
                        "Select a valid choice. {selected} is not one of the available choices."
                    ),
                );
                break;
            }
        }
    }
    if model.slug() == "agentcredential"
        && id.is_none()
        && columns
            .get("expires_at")
            .and_then(Json::as_str)
            .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
            .is_some_and(|expires| expires <= Utc::now())
    {
        error(errors, "expires_at", "Expiration must be in the future.");
    }
    let selections = clean_memberships(admin, model, &form.widgets, values, errors).await?;
    let inline_changes = inlines::prepare(admin, form, id, values, errors).await?;
    if !errors.is_empty() {
        return Ok(None);
    }
    let actor = admin.viewer.user.id.ok_or(ApiError::Serialization)?;
    let mut generated_token = None;
    if model.slug() == "agentcredential" && id.is_none() {
        let (token, hash) = crate::agent::new_credential()?;
        columns.insert("token_hash".to_owned(), json!(hash));
        columns.insert("created_by_id".to_owned(), json!(actor));
        generated_token = Some(token);
    }
    if model.slug() == "agentcredential" && id.is_some() {
        return Ok(Some(redirect(&model.url())));
    }
    let mut tx = admin.state.pool.begin().await?;
    if let Some(id) = id
        && !model.lock_visible_school(&mut tx, id).await?
    {
        return Ok(Some(admin.missing_record(model, id)));
    }
    let mut was_grid_hidden = false;
    let hide_from_grid =
        model.key == "core.debater" && checkbox_posted(values, "immaculate_hidden");
    if model.key == "core.debater"
        && let Some(id) = id
    {
        was_grid_hidden = grid_hidden(&mut *tx, id).await?;
        if was_grid_hidden != hide_from_grid {
            changed.push("Hide from Immaculate Grid clues".to_owned());
        }
    }
    model_rules::before_save(&mut tx, model, admin.state.current_season, id, &mut columns).await?;
    let record = if columns.is_empty() && id.is_some() {
        id.unwrap_or_default()
    } else {
        match model.storage.write(&mut tx, &columns, id).await {
            Ok(record) => record,
            Err(ApiError::Constraint) => {
                error(errors, "form", FORM_CONSTRAINT);
                return Ok(None);
            }
            Err(error) => return Err(error),
        }
    };
    save_memberships(&mut tx, model, &form.widgets, record, values, &selections).await?;
    if model.slug() == "round"
        && id.is_some()
        && let Some(stage) = columns
            .get("stage")
            .and_then(Json::as_str)
            .filter(|stage| loaded["stage"] != *stage)
    {
        sqlx::query(
            "UPDATE core_roundstats SET stage=$1,
            speaks=CASE WHEN $1='outround' THEN NULL ELSE speaks END,
            ranks=CASE WHEN $1='outround' THEN NULL ELSE ranks END,
            debater_role=CASE WHEN $1='outround' THEN NULL ELSE debater_role END WHERE round_id=$2",
        )
        .bind(stage)
        .bind(record)
        .execute(&mut *tx)
        .await?;
    }
    let inline_changes = match inlines::apply(
        &mut tx,
        record,
        admin.state.current_season,
        inline_changes,
    )
    .await
    {
        Ok(changes) => changes,
        Err(ApiError::Constraint) => {
            error(
                errors,
                "form",
                "An inline record conflicts with existing data or references an invalid record.",
            );
            return Ok(None);
        }
        Err(reason) => return Err(reason),
    };
    if model.slug() == "debater" {
        crate::claims::edit::team_names(&mut tx, record).await?;
        if hide_from_grid {
            sqlx::query(
                "INSERT INTO core_immaculategridhiddendebater (debater_id)
                VALUES ($1) ON CONFLICT(debater_id) DO NOTHING",
            )
            .bind(record)
            .execute(&mut *tx)
            .await?;
        } else if was_grid_hidden {
            let statement = format!(
                "DELETE FROM core_immaculategridhiddendebater WHERE debater_id IN ({GRID_GROUP})"
            );
            sqlx::query(AssertSqlSafe(statement))
                .bind(record)
                .execute(&mut *tx)
                .await?;
        }
    }
    let name = loaded_label(&mut tx, model, record).await?;
    let mut message = Vec::new();
    if id.is_none() {
        message.push(json!({"added":{}}));
    } else if !changed.is_empty() {
        message.push(json!({"changed":{"fields":changed}}));
    }
    message.extend(inline_changes);
    log(
        &mut tx,
        model,
        actor,
        record,
        &name,
        if id.is_none() { 1 } else { 2 },
        Json::Array(message),
    )
    .await?;
    if let Err(failure) = tx.commit().await {
        match storage_error(failure) {
            ApiError::Constraint => {
                error(errors, "form", FORM_CONSTRAINT);
                return Ok(None);
            }
            failure => return Err(failure),
        }
    }
    if matches!(model.key.as_str(), "core.debater" | "core.school") {
        crate::search::invalidate(admin.state).await;
    }
    if model.table == "core_tournament" {
        crate::recent_results::invalidate(admin.state).await;
    }
    if let Some(token) = generated_token {
        let mut response=admin.view("site_admin/agent_credential_issued.html",context!{
            title=>"Agent credential issued",token,credential=>context!{
                name,expires_at=>super::listing::display(columns["expires_at"].as_str(),model.fields.get("expires_at"),false),
                can_write=>columns["can_write"]==true }
        })?;
        response
            .headers_mut()
            .insert(header::CACHE_CONTROL, "no-store, private".parse().unwrap());
        response
            .headers_mut()
            .insert(header::REFERRER_POLICY, "no-referrer".parse().unwrap());
        return Ok(Some(response));
    }
    if values.contains_key("_popup") {
        let value = record.to_string();
        let data = if id.is_some() {
            json!({"action":"change","value":value,"obj":name,"new_value":value})
        } else {
            json!({"value":value,"obj":name})
        };
        return Ok(Some(admin.popup_response(data)?));
    }
    let model_url = model.url();
    let location = if values.contains_key("_continue") {
        admin.preserved_url(format!("{model_url}{record}/change/"), false)
    } else if values.contains_key("_addanother") {
        admin.preserved_url(format!("{model_url}add/"), false)
    } else if admin.viewer.read(model) {
        admin.preserved_url(model_url, true)
    } else {
        "/admin/".to_owned()
    };
    let mut response = redirect(&location);
    if let Some(secret) = admin.state.secret.as_deref() {
        let message = format!(
            "The {} “{name}” was {} successfully.",
            model.singular,
            if id.is_none() { "added" } else { "changed" }
        );
        flash::push(
            &mut response,
            admin.headers,
            secret,
            25,
            &message,
            admin.secure(),
        );
    }
    Ok(Some(response))
}

pub(super) async fn page(
    admin: &AdminRequest<'_>,
    model: &Model,
    id: Option<i64>,
    request: Request,
) -> Result<Response, ApiError> {
    if id.is_none() && model.key == "core.user" && !admin.viewer.allows(model, "change") {
        let status = if admin.viewer.allows(model, "add") && crate::web::debug_assets() {
            StatusCode::NOT_FOUND
        } else {
            StatusCode::FORBIDDEN
        };
        return Ok(status.into_response());
    }
    let allowed = match (id, request.method()) {
        (None, _) => admin.viewer.allows(model, "add"),
        (Some(_), &Method::POST) => admin.viewer.allows(model, "change"),
        (Some(_), _) => admin.viewer.read(model),
    };
    if !allowed {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let loaded = if let Some(id) = id {
        let entry = model
            .visible_record(&mut *admin.state.pool.acquire().await?, id)
            .await?;
        if entry.is_null() {
            return Ok(admin.missing_record(model, id));
        }
        entry
    } else {
        Json::Null
    };
    if *request.method() != Method::POST {
        return render_form(admin, model, id, &loaded, None, &Errors::new()).await;
    }
    let Some(origin) = admin.origin() else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let (mut parsed, uploaded) = if model.key == "django_summernote.attachment" {
        if login::csrf_request_secret(admin.headers, &origin).is_err() {
            return Ok(login::csrf_failure());
        }
        match attachment_form(request).await {
            Ok(values) => values,
            Err(response) => return Ok(*response),
        }
    } else {
        let (parts, body) = request.into_parts();
        let fields = match login::form_fields(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let mut parsed = Values::new();
        for (name, value) in fields {
            parsed.entry(name).or_default().push(value);
        }
        (parsed, None)
    };
    if !login::csrf_valid(
        admin.headers,
        parsed
            .get("csrfmiddlewaretoken")
            .and_then(|values| values.last())
            .map(String::as_str),
        &origin,
    ) {
        return Ok(login::csrf_failure());
    }
    let mut staged = None;
    if let Some((name, upload)) = uploaded {
        if upload.empty {
            let mut errors = Errors::new();
            error(&mut errors, "file", "The submitted file is empty.");
            return render_form(admin, model, id, &loaded, Some(&parsed), &errors).await;
        }
        let file = upload.into_reader().await.map_err(|_| ApiError::Database)?;
        let (stored, path) =
            crate::summernote::write_attachment_from(admin.state, &name, None, file)
                .await
                .map_err(|_| ApiError::Database)?;
        staged = Some(path);
        parsed.insert("file".to_owned(), vec![stored]);
        if raw(&parsed, "name").is_empty() {
            parsed.insert("name".to_owned(), vec![name]);
        }
    } else if model.key == "django_summernote.attachment"
        && let Some(path) = loaded["file"].as_str()
    {
        parsed.insert("file".to_owned(), vec![path.to_owned()]);
        if raw(&parsed, "name").is_empty() {
            parsed.insert("name".to_owned(), vec![path.to_owned()]);
        }
    }
    let mut errors = Errors::new();
    let saved = save(admin, model, id, &loaded, &parsed, &mut errors).await;
    if !matches!(&saved, Ok(Some(_)))
        && let Some(path) = staged
    {
        let _ = tokio::fs::remove_file(path).await;
    }
    if let Some(response) = saved? {
        return Ok(response);
    }
    render_form(admin, model, id, &loaded, Some(&parsed), &errors).await
}

type AttachmentForm = (Values, Option<(String, UploadFile)>);

async fn attachment_form(mut request: Request) -> Result<AttachmentForm, Box<Response>> {
    DefaultBodyLimit::disable().apply(&mut request);
    let mut multipart = Multipart::from_request(request, &())
        .await
        .map_err(|error| Box::new(error.into_response()))?;
    let mut values = Values::new();
    let mut uploaded = None;
    let mut remaining = upload_file::MAX_FORM_DATA;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|error| Box::new(error.into_response()))?
    {
        let key = field.name().unwrap_or_default().to_owned();
        if key == "file" {
            let name = field
                .file_name()
                .unwrap_or_default()
                .rsplit(['/', '\\'])
                .next()
                .unwrap_or_default()
                .chars()
                .take(255)
                .collect::<String>();
            if !name.is_empty() {
                let file = UploadFile::stage(field)
                    .await
                    .map_err(|status| Box::new(status.into_response()))?;
                uploaded = Some((name, file));
            }
        } else {
            let value = upload_file::text(field)
                .await
                .map_err(|status| Box::new(status.into_response()))?;
            remaining = remaining
                .checked_sub(key.len() + value.len())
                .ok_or_else(|| Box::new(StatusCode::BAD_REQUEST.into_response()))?;
            values.entry(key).or_default().push(value);
        }
    }
    Ok((values, uploaded))
}
