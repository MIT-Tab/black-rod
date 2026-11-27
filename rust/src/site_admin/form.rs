use std::collections::{BTreeMap, BTreeSet};

use axum::{
    body::{Body, to_bytes},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, SecondsFormat, Utc};
use minijinja::{Value, context};
use ring::{
    digest,
    rand::{SecureRandom, SystemRandom},
};
use serde_json::{Map, Value as Json, json};
use sqlx::{AnyConnection, AssertSqlSafe};

use super::{
    AdminRequest, cap_first,
    listing::{Source, display, label},
    redirect,
    schema::{Field, Model, Widget, schema},
};
use crate::{ApiError, account, agent::journal::storage_error, flash, login};

type Errors = BTreeMap<String, Vec<String>>;
type Values = BTreeMap<String, Vec<String>>;

fn primitive(value: &Json) -> String {
    match value {
        Json::String(value) => value.clone(),
        Json::Null => String::new(),
        value => value.to_string(),
    }
}

fn default(field: &Field) -> Json {
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
        if field.related.as_deref() == Some("contenttypes.contenttype") {
            return Ok(sqlx::query_as::<_, (String, String)>(
                "SELECT CAST(id AS TEXT),app_label || ' | ' || model
                FROM django_content_type ORDER BY app_label,model",
            )
            .fetch_all(&admin.state.pool)
            .await?);
        }
        return Ok(Vec::new());
    };
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
        let owner = owner.parse::<i64>().unwrap_or(0);
        filters.push(format!(
            "t0.id IN (SELECT debater_id FROM core_team_debaters WHERE team_id={owner})"
        ));
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
    let order = match target.slug() {
        "debater" => "t0.first_name,t0.last_name,t0.id",
        "school" | "team" | "tournament" | "group" | "site" => "repr,t0.id",
        "user" => "t0.username",
        _ => "t0.id",
    };
    let sql = format!(
        "SELECT CAST(t0.id AS TEXT),{repr} AS repr FROM {}{where_} ORDER BY {order}",
        source.sql()
    );
    Ok(sqlx::query_as::<_, (String, String)>(AssertSqlSafe(sql))
        .fetch_all(&admin.state.pool)
        .await?)
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
            } else {
                default(field)
            }
        });
        let read_only =
            widget.is_none() || self.id.is_some() && !admin.viewer.allows(model, "change");
        let checkbox = !read_only && choice_kind == "CheckboxInput";
        let multiple = matches!(choice_kind, "SelectMultiple" | "AutocompleteSelectMultiple");
        let radio = choice_kind == "RadioSelect";
        let multicheck = choice_kind == "CheckboxSelectMultiple";
        let autocomplete = choice_kind.starts_with("AutocompleteSelect");
        let select = matches!(
            choice_kind,
            "Select" | "NullBooleanSelect" | "SelectMultiple"
        ) || autocomplete;
        let textarea = matches!(
            choice_kind,
            "Textarea" | "AdminTextareaWidget" | "AdminJSONFieldWidget"
        );
        let datetime = choice_kind == "AdminSplitDateTime";
        let password_status = choice_kind == "ReadOnlyPasswordHashWidget";
        let required = widget.is_some_and(|widget| widget.required) && !read_only;
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
        let text = if let Some(values) = self.submitted {
            raw(values, name).to_owned()
        } else {
            primitive(&value)
        };
        let (date, time) = text.split_once(['T', ' ']).unwrap_or((text.as_str(), ""));
        let checked = self
            .submitted
            .map_or(value.as_bool().unwrap_or_default(), |values| {
                values.contains_key(name)
            });
        let widget_help = widget
            .map(|widget| widget.help.as_str())
            .unwrap_or_default();
        let mut help = widget_help.to_owned();
        let kind = match choice_kind {
            "CheckboxInput" => "checkbox",
            "NumberInput" => "number",
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
            && matches!(kind, "text" | "email" | "url")
            && !attrs.contains_key("maxlength")
        {
            attrs.insert("maxlength".into(), json!(size));
        }
        let mut options = widget
            .map(|widget| widget.choices.clone())
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
            options = choices(admin, model, field, &selected, autocomplete, &team).await?;
            if !multiple && !multicheck && (!required || value.is_null() || autocomplete) {
                options.insert(0, (String::new(), "---------".to_owned()));
            }
            if multicheck && team.is_empty() {
                help.push_str(" Save the team first to select members.");
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
                ("data-ajax--data-type", "json"),
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
        let items = options
            .into_iter()
            .map(|(value, label)| context! {selected=>selected.contains(&value),value,label})
            .collect::<Vec<_>>();
        let related = target.map(|target| {
            context! {
                slug=>target.slug(),url=>target.url(),singular=>&target.singular,
                add=>admin.viewer.allows(target,"add"),
                change=>!multiple&&admin.viewer.allows(target,"change"),
                delete=>!multiple&&!required&&admin.viewer.allows(target,"delete"),
                view=>!multiple&&admin.viewer.read(target),
            }
        });
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
        let read_value = if password_status {
            Value::from("No password set.")
        } else {
            display(Some(&text), field, false)
        };
        Ok(context! {
            name,label=>caption,help,required,datetime,date,time=>time.split(['.','+','Z']).next().unwrap_or_default(),
            checkbox, checked, readonly=>read_only, read_value, wrapped=>widget.is_some_and(|widget|widget.wrapped),
            value=>&text,type=>kind, attrs=>attrs.iter().collect::<Vec<_>>(),related, select,multiple,radio,multicheck,
            textarea,password_status,algorithm,iterations,salt,digest,usable=>!text.starts_with('!'),
            options=>items,error=>self.errors.get(name)
        })
    }
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
    let app = schema().app(model.app()).expect("admin app metadata");
    let original = if let Some(id) = id {
        object_label(admin, model, id).await?
    } else {
        String::new()
    };
    let url = model.url();
    let object_url = id.map(|id| format!("{url}{id}/")).unwrap_or_default();
    let assets = |a: &[String], b: &[String]| {
        let mut seen = BTreeSet::new();
        a.iter()
            .chain(b)
            .filter(|name| seen.insert(name.as_str()))
            .cloned()
            .collect::<Vec<_>>()
    };
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
        model_name=>model.slug(),model_url=>url,object_url, fieldsets,add=>adding,errors,
        css=>assets(&model.list_media.css,&form.media.css),
        js=>assets(&model.list_media.js,&form.media.js), can_view=>admin.viewer.read(model),
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
    let mut source = Source::new(model);
    let repr = label(&mut source, model, "");
    let sql = format!("SELECT {repr} FROM {} WHERE t0.id=$1", source.sql());
    Ok(sqlx::query_scalar::<_, Option<String>>(AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(&admin.state.pool)
        .await?
        .flatten()
        .unwrap_or_else(|| id.to_string()))
}

fn datetime(values: &Values, name: &str) -> Option<Json> {
    let date = raw(values, &format!("{name}_0"));
    let time = raw(values, &format!("{name}_1"));
    if !date.is_empty() || !time.is_empty() {
        let date = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
        let time = NaiveTime::parse_from_str(time, "%H:%M:%S")
            .or_else(|_| NaiveTime::parse_from_str(time, "%H:%M"))
            .ok()?;
        return Some(json!(
            date.and_time(time)
                .and_utc()
                .to_rfc3339_opts(SecondsFormat::Secs, true)
        ));
    }
    let value = raw(values, name);
    if value.is_empty() {
        return Some(Json::Null);
    }
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|datetime| json!(datetime.to_rfc3339()))
        .or_else(|| {
            NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S")
                .ok()
                .map(|datetime| json!(datetime.and_utc().to_rfc3339()))
        })
}

fn cleaned(
    field: &Field,
    widget: &Widget,
    name: &str,
    values: &Values,
    errors: &mut Errors,
) -> Option<Json> {
    let raw = raw(values, name);
    let date_value = if field.kind == "DateTimeField" {
        datetime(values, name)
    } else {
        None
    };
    if widget.required
        && raw.trim().is_empty()
        && date_value.as_ref().is_none_or(Json::is_null)
        && field.kind != "BooleanField"
    {
        error(errors, name, "This field is required.");
        return None;
    }
    if field.size.is_some_and(|max| raw.chars().count() > max) {
        error(
            errors,
            name,
            &format!(
                "Ensure this value has at most {} characters (it has {}).",
                field.size.unwrap_or_default(),
                raw.chars().count()
            ),
        );
    }
    if !field.choices.is_empty()
        && !raw.is_empty()
        && !field.choices.iter().any(|(key, _)| primitive(key) == raw)
    {
        error(
            errors,
            name,
            &format!("Select a valid choice. {raw} is not one of the available choices."),
        );
    }
    let value = match field.kind.as_str() {
        "BooleanField" => Some(json!(values.contains_key(name))),
        "ForeignKey"
        | "OneToOneField"
        | "IntegerField"
        | "SmallIntegerField"
        | "BigIntegerField"
        | "PositiveIntegerField"
        | "PositiveSmallIntegerField"
        | "PositiveBigIntegerField"
            if !raw.is_empty() =>
        {
            raw.parse::<i64>()
                .ok()
                .filter(|value| !field.kind.starts_with("Positive") || *value >= 0)
                .map(|value| json!(value))
        }
        "FloatField" | "DecimalField" if !raw.is_empty() => raw
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .map(|_| json!(raw)),
        "DateTimeField" => date_value,
        "DateField" if !raw.is_empty() => NaiveDate::parse_from_str(raw, "%Y-%m-%d")
            .ok()
            .map(|date| json!(date.to_string())),
        "JSONField" if !raw.is_empty() => serde_json::from_str(raw).ok(),
        _ if raw.is_empty() && field.null => Some(Json::Null),
        _ => Some(json!(if field.kind == "TextField" {
            raw
        } else {
            raw.trim()
        })),
    };
    if value.is_none() {
        error(
            errors,
            name,
            match field.kind.as_str() {
                "DateTimeField" | "DateField" => "Enter a valid date.",
                "JSONField" => "Enter a valid JSON.",
                "FloatField" | "DecimalField" => "Enter a number.",
                _ => "Enter a whole number.",
            },
        );
    }
    value
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
        let Some(column) = field.column.as_ref().filter(|_| !field.primary) else {
            continue;
        };
        if let Some(widget) = form
            .widgets
            .get(name)
            .filter(|widget| widget.kind != "ReadOnlyPasswordHashWidget")
        {
            if let Some(value) = cleaned(field, widget, name, values, errors) {
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
    if !errors.is_empty() {
        return Ok(None);
    }
    let actor = admin.viewer.user.id.ok_or(ApiError::Serialization)?;
    let mut generated_token = None;
    if model.slug() == "agentcredential" && id.is_none() {
        let mut bytes = [0; 32];
        SystemRandom::new()
            .fill(&mut bytes)
            .map_err(|_| ApiError::Serialization)?;
        let token = format!("br_agent_{}", URL_SAFE_NO_PAD.encode(bytes));
        columns.insert(
            "token_hash".to_owned(),
            json!(hex::encode(digest::digest(
                &digest::SHA256,
                token.as_bytes()
            ))),
        );
        columns.insert("created_by_id".to_owned(), json!(actor));
        generated_token = Some(token);
    }
    if model.slug() == "agentcredential" && id.is_some() {
        return Ok(Some(redirect(&model.url())));
    }
    let mut tx = admin.state.pool.begin().await?;
    let record = if columns.is_empty() && id.is_some() {
        id.unwrap_or_default()
    } else {
        match model.storage.write(&mut tx, &columns, id).await {
            Ok(record) => record,
            Err(ApiError::Constraint) => {
                error(
                    errors,
                    "form",
                    "A record with these values already exists, or a referenced record could not be found.",
                );
                return Ok(None);
            }
            Err(error) => return Err(error),
        }
    };
    for (name, field) in &model.fields {
        let Some([table, current, target]) = field
            .through
            .as_ref()
            .filter(|_| form.widgets.contains_key(name) && field.kind != "Tags")
        else {
            continue;
        };
        if field.generic_through.unwrap_or_default() {
            continue;
        }
        let new = selected_fields(values, name)
            .into_iter()
            .filter_map(|value| value.parse::<i64>().ok())
            .collect::<BTreeSet<_>>();
        let statement = format!("DELETE FROM {table} WHERE {current}=$1");
        sqlx::query(AssertSqlSafe(statement))
            .bind(record)
            .execute(&mut *tx)
            .await?;
        let insert = format!("INSERT INTO {table} ({current},{target}) VALUES ($1,$2)");
        for related in new {
            sqlx::query(AssertSqlSafe(insert.clone()))
                .bind(record)
                .bind(related)
                .execute(&mut *tx)
                .await
                .map_err(storage_error)?;
        }
    }
    if model.slug() == "debater" {
        crate::claims::edit::team_names(&mut tx, record).await?;
        if values.contains_key("immaculate_hidden") {
            sqlx::query(
                "INSERT INTO core_immaculategridhiddendebater (debater_id,created_at)
                VALUES ($1,CURRENT_TIMESTAMP) ON CONFLICT(debater_id) DO NOTHING",
            )
            .bind(record)
            .execute(&mut *tx)
            .await?;
        } else {
            sqlx::query("DELETE FROM core_immaculategridhiddendebater WHERE debater_id IN (
                SELECT id FROM core_debater WHERE id=$1 OR alias_group_id=(SELECT alias_group_id FROM core_debater WHERE id=$1))")
                .bind(record).execute(&mut *tx).await?;
        }
    }
    let mut source = Source::new(model);
    let repr = label(&mut source, model, "");
    let query = format!("SELECT {repr} FROM {} WHERE t0.id=$1", source.sql());
    let name = sqlx::query_scalar::<_, Option<String>>(AssertSqlSafe(query))
        .bind(record)
        .fetch_one(&mut *tx)
        .await?
        .unwrap_or_else(|| record.to_string());
    let message = if id.is_none() {
        json!([{"added":{}}])
    } else if !changed.is_empty() {
        json!([{"changed":{"fields":changed}}])
    } else {
        json!([])
    };
    log(
        &mut tx,
        model,
        actor,
        record,
        &name,
        if id.is_none() { 1 } else { 2 },
        message,
    )
    .await?;
    tx.commit().await?;
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
    let model_url = model.url();
    let location = if values.contains_key("_continue") {
        format!("{model_url}{record}/change/")
    } else if values.contains_key("_addanother") {
        format!("{model_url}add/")
    } else {
        model_url.clone()
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
    method: Method,
    body: Body,
) -> Result<Response, ApiError> {
    if id.is_none() && !admin.viewer.allows(model, "add")
        || id.is_some() && !admin.viewer.read(model)
    {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let loaded = if let Some(id) = id {
        let entry = model
            .storage
            .get(&mut *admin.state.pool.acquire().await?, id)
            .await?;
        if entry.is_null() {
            return Ok(redirect("/admin/"));
        }
        entry
    } else {
        Json::Null
    };
    if method != Method::POST {
        return render_form(admin, model, id, &loaded, None, &Errors::new()).await;
    }
    if id.is_some() && !admin.viewer.allows(model, "change") {
        return Ok(StatusCode::FORBIDDEN.into_response());
    }
    let Ok(data) = to_bytes(body, 2_621_440).await else {
        return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response());
    };
    let mut parsed = Values::new();
    for (name, value) in url::form_urlencoded::parse(&data) {
        parsed
            .entry(name.into_owned())
            .or_default()
            .push(value.into_owned());
    }
    let Some(origin) = admin.origin() else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
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
    let mut errors = Errors::new();
    if let Some(response) = save(admin, model, id, &loaded, &parsed, &mut errors).await? {
        return Ok(response);
    }
    render_form(admin, model, id, &loaded, Some(&parsed), &errors).await
}
