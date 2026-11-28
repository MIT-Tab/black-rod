use std::{borrow::Cow, collections::BTreeMap, sync::LazyLock};

use serde::Deserialize;

#[derive(Deserialize)]
pub(super) struct Schema {
    pub(super) title: String,
    pub(super) header: String,
    pub(super) index: String,
    pub(super) content_types: BTreeMap<String, String>,
    pub(super) apps: Vec<Application>,
}

#[derive(Deserialize)]
pub(super) struct Application {
    pub(super) label: String,
    pub(super) name: String,
    pub(super) models: Vec<Model>,
}

#[derive(Deserialize)]
pub(super) struct Column {
    pub(super) name: String,
    pub(super) label: String,
    pub(super) order_by: Option<String>,
    pub(super) boolean: bool,
}

#[derive(Deserialize)]
pub(super) struct Field {
    pub(super) column: Option<String>,
    pub(super) kind: String,
    pub(super) label: String,
    pub(super) help: String,
    pub(super) choices: Vec<(serde_json::Value, String)>,
    pub(super) null: bool,
    pub(super) blank: bool,
    pub(super) auto_now: bool,
    pub(super) auto_now_add: bool,
    pub(super) primary: bool,
    pub(super) size: Option<usize>,
    pub(super) scale: Option<usize>,
    pub(super) precision: Option<usize>,
    pub(super) default: Option<serde_json::Value>,
    pub(super) default_fn: Option<String>,
    pub(super) through: Option<[String; 3]>,
    pub(super) generic_through: Option<bool>,
    pub(super) related: Option<String>,
}

#[derive(Deserialize)]
pub(super) struct Media {
    pub(super) css: Vec<(String, String)>,
    pub(super) js: Vec<String>,
}

#[derive(Deserialize)]
pub(super) struct Widget {
    pub(super) kind: String,
    pub(super) label: Option<String>,
    pub(super) help: String,
    pub(super) required: bool,
    pub(super) hidden_initial: bool,
    pub(super) attributes: BTreeMap<String, serde_json::Value>,
    pub(super) wrapped: bool,
    pub(super) related_delete: bool,
    pub(super) empty_label: Option<String>,
    pub(super) related_url: Option<String>,
    pub(super) forward: Option<String>,
    pub(super) cleaning: Option<String>,
    pub(super) assumed_url_scheme: Option<String>,
    #[serde(default)]
    choices: Vec<(String, String)>,
    #[serde(default)]
    season_choices: bool,
}

impl Widget {
    pub(super) fn choices(&self, current_season: i32) -> Cow<'_, [(String, String)]> {
        if self.season_choices {
            Cow::Owned(
                (2004..=current_season)
                    .rev()
                    .map(|year| (year.to_string(), crate::schedule::season_display(year)))
                    .collect(),
            )
        } else {
            Cow::Borrowed(&self.choices)
        }
    }
}

#[derive(Deserialize)]
pub(super) struct Form {
    pub(super) widgets: BTreeMap<String, Widget>,
    pub(super) media: Media,
    pub(super) production_media: Option<Media>,
    pub(super) inlines: Vec<Inline>,
}

#[derive(Deserialize)]
pub(super) struct Inline {
    pub(super) model: String,
    pub(super) kind: String,
    pub(super) prefix: String,
    pub(super) foreign_key: String,
    pub(super) min: usize,
    pub(super) max: usize,
    pub(super) absolute_max: usize,
    pub(super) extra: usize,
    pub(super) classes: Vec<String>,
    pub(super) fields: Vec<String>,
    pub(super) widgets: BTreeMap<String, Widget>,
    pub(super) ordering: Vec<String>,
    pub(super) show_change_link: bool,
    pub(super) data: String,
}

impl Inline {
    pub(super) fn model(&self) -> &'static Model {
        let (app, slug) = self.model.split_once('.').expect("inline model key");
        schema().model(app, slug).expect("registered inline model")
    }
}

#[derive(Deserialize)]
pub(super) struct Fieldset {
    pub(super) title: Option<String>,
    pub(super) fields: Vec<FieldLine>,
    pub(super) classes: Vec<String>,
    pub(super) description: String,
}

#[derive(Deserialize)]
#[serde(untagged)]
pub(super) enum FieldLine {
    One(String),
    Group(Vec<String>),
}

impl FieldLine {
    pub(super) fn names(&self) -> &[String] {
        match self {
            Self::One(name) => std::slice::from_ref(name),
            Self::Group(names) => names,
        }
    }
}

#[derive(Deserialize)]
pub(super) struct Exchange {
    resource_model: Option<String>,
    pub(super) headers: Vec<String>,
    pub(super) fields: Vec<String>,
    pub(super) import_formats: Vec<String>,
    pub(super) import_media: Vec<String>,
    pub(super) production_import_media: Option<Vec<String>>,
    pub(super) export_formats: Vec<String>,
}

#[derive(Deserialize)]
pub(super) struct Model {
    pub(super) key: String,
    pub(super) object_name: String,
    pub(super) default_repr: bool,
    pub(super) singular: String,
    pub(super) plural: String,
    pub(super) table: String,
    pub(super) allowed: BTreeMap<String, bool>,
    pub(super) list_display: Vec<Column>,
    pub(super) list_links: Option<Vec<String>>,
    pub(super) search: Vec<String>,
    pub(super) choice_ordering: Vec<String>,
    pub(super) model_ordering: Vec<String>,
    pub(super) ordering: Vec<String>,
    pub(super) filter: Vec<String>,
    pub(super) fields: BTreeMap<String, Field>,
    pub(super) restricted: bool,
    pub(super) per_page: usize,
    pub(super) max_show_all: usize,
    pub(super) import_export: bool,
    pub(super) exchange: Option<Exchange>,
    pub(super) save_on_top: bool,
    pub(super) list_media: Media,
    pub(super) production_list_media: Option<Media>,
    pub(super) actions: Vec<(String, String)>,
    pub(super) fieldsets_add: Vec<Fieldset>,
    pub(super) fieldsets_change: Vec<Fieldset>,
    pub(super) form_add: Form,
    pub(super) form_change: Form,
    pub(super) storage: crate::agent::journal::Table,
}

impl Schema {
    pub(super) fn app(&self, label: &str) -> Option<&Application> {
        self.apps.iter().find(|app| app.label == label)
    }
    pub(super) fn model(&self, label: &str, slug: &str) -> Option<&Model> {
        self.app(label)?
            .models
            .iter()
            .find(|model| model.slug() == slug)
    }
}

impl Application {
    pub(super) fn url(&self) -> String {
        format!("/admin/{}/", self.label)
    }
}

impl Form {
    pub(super) fn media(&self) -> &Media {
        if !crate::web::debug_assets()
            && let Some(media) = &self.production_media
        {
            return media;
        }
        &self.media
    }
}

impl Exchange {
    pub(super) fn resource<'a>(&self, model: &'a Model) -> Result<&'a Model, crate::ApiError> {
        let Some(key) = &self.resource_model else {
            return Ok(model);
        };
        let (app, slug) = key.split_once('.').ok_or(crate::ApiError::Serialization)?;
        schema()
            .model(app, slug)
            .ok_or(crate::ApiError::Serialization)
    }

    pub(super) fn import_media(&self) -> &[String] {
        if !crate::web::debug_assets()
            && let Some(media) = &self.production_import_media
        {
            return media;
        }
        &self.import_media
    }
}

impl Model {
    pub(super) fn list_media(&self) -> &Media {
        if !crate::web::debug_assets()
            && let Some(media) = &self.production_list_media
        {
            return media;
        }
        &self.list_media
    }

    pub(super) fn app(&self) -> &str {
        self.key.split_once('.').expect("admin model key").0
    }
    pub(super) fn slug(&self) -> &str {
        self.key.split_once('.').expect("admin model key").1
    }
    pub(super) fn url(&self) -> String {
        format!("/admin/{}/{}/", self.app(), self.slug())
    }
}

pub(super) fn schema() -> &'static Schema {
    static SCHEMA: LazyLock<Schema> = LazyLock::new(|| {
        serde_json::from_str(include_str!("../../schema/admin.json")).expect("Django admin schema")
    });
    &SCHEMA
}

pub(super) fn quote(pk: &str) -> String {
    let mut escaped = String::new();
    for ch in pk.chars() {
        if ":/_#?;@&=+$,\"<>%\\\n\t".contains(ch) {
            escaped.push_str(&format!("_{:02X}", u32::from(ch)));
        } else {
            escaped.push(ch);
        }
    }
    escaped
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn admin_season_choices_follow_the_running_season() {
        for slug in [
            "coty",
            "noty",
            "qual",
            "qualpoints",
            "reaff",
            "soty",
            "totyreaff",
        ] {
            let model = schema().model("core", slug).unwrap();
            for form in [&model.form_add, &model.form_change] {
                let widget = &form.widgets["season"];
                let choices = widget.choices(2027);
                assert_eq!(choices[0].0, "2027");
                assert_eq!(choices[0].1, "2027-28");
                assert_eq!(choices[23].0, "2004");
                assert_eq!(choices[23].1, "2004-05");
                assert_eq!(choices.len(), 24);
                assert_eq!(widget.choices(2025).first().unwrap().0, "2025");
            }
        }
        assert!(
            schema()
                .model("core", "tournament")
                .unwrap()
                .form_add
                .widgets["season"]
                .choices(2027)
                .is_empty()
        );
    }
}
