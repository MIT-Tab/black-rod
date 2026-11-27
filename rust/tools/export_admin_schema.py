"""Describe the installed Django admin's model metadata for the native admin."""
from pathlib import Path
import json

from django import setup
from django.core.serializers.json import DjangoJSONEncoder


def field_config(field):
    from django.core.exceptions import FieldDoesNotExist
    from django.db.models import NOT_PROVIDED

    related = getattr(field, "related_model", None)
    config = {
        "name": field.name,
        "column": field.column,
        "kind": field.get_internal_type(),
        "label": str(field.verbose_name),
        "help": str(field.help_text),
        "null": field.null,
        "blank": field.blank,
        "auto_now": getattr(field, "auto_now", False),
        "auto_now_add": getattr(field, "auto_now_add", False),
        "primary": field.primary_key,
        "unique": field.unique,
        "size": getattr(field, "max_length", None),
        "scale": getattr(field, "decimal_places", None),
        "choices": [(key, str(label)) for key, label in field.flatchoices],
        "related": related._meta.label_lower if related else None,
    }
    if hasattr(field, "max_digits"):
        config["precision"] = field.max_digits
    if field.default is not NOT_PROVIDED:
        if callable(field.default):
            config["default_fn"] = field.default.__name__
        else:
            config["default"] = field.default
    if field.many_to_many:
        middle = field.remote_field.through._meta
        if hasattr(field, "m2m_field_name"):
            config["through"] = [
                middle.db_table, middle.get_field(field.m2m_field_name()).column,
                middle.get_field(field.m2m_reverse_field_name()).column,
            ]
        else:
            try:
                parent = middle.get_field("content_object").column
            except FieldDoesNotExist:
                parent = None
            if parent is None:
                parent = middle.get_field("object_id").column
                config["generic_through"] = True
            config["through"] = [middle.db_table, parent, "tag_id"]
            config["kind"] = "Tags"
    return config


def model_config(request, model, model_admin):
    from django.contrib.admin.utils import label_for_field
    from django.contrib.admin.views.main import ChangeList
    from django.db.models import Model

    def column(name):
        label, attr = label_for_field(name, model, model_admin, return_attr=True)
        ordering = getattr(attr, "admin_order_field", None) if attr else name
        return {
            "name": name,
            "label": str(label),
            "order_by": ordering if isinstance(ordering, str) else None,
            "boolean": bool(getattr(attr, "boolean", False)),
        }

    def fieldsets(obj, change):
        return [
            {"title": str(title) if title else None, "fields": list(options["fields"]),
             "classes": list(options.get("classes", ())), "description": str(options.get("description", ""))}
            for title, options in model_admin.get_fieldsets(request, obj)
        ] if not change or model_admin.has_view_permission(request) else []

    def widgets_for(fields):
        widgets = {}
        for name, field in fields.items():
            widget = field.widget
            wrapped = type(widget).__name__ == "RelatedFieldWidgetWrapper"
            actual = widget.widget if wrapped else widget
            kind = type(actual).__name__
            attributes = actual.build_attrs(actual.attrs)
            related_url = None
            forward = None
            if kind in ("ForeignKeyRawIdWidget", "ManyToManyRawIdWidget", "FilteredSelectMultiple"):
                context = actual.get_context(name, None, {"id": f"id_{name}"})
                attributes = context["widget"]["attrs"]
                related_url = context.get("related_url")
            if hasattr(actual, "_make_forward_dict") and actual.forward:
                forward = json.dumps([actual._make_forward_dict(field) for field in actual.forward])
            attributes = {key: value for key, value in attributes.items() if key not in ("id", "multiple")}
            widgets[name] = {
                "kind": kind,
                "related_url": related_url,
                "forward": forward,
                "label": str(field.label) if field.label else None,
                "help": str(field.help_text),
                "required": field.required,
                "hidden_initial": field.show_hidden_initial,
                "attributes": {k: str(v) if not isinstance(v, (bool, int)) else v for k, v in attributes.items()},
                "label_suffix": field.label_suffix,
                "wrapped": wrapped,
                "related_delete": wrapped and widget.can_delete_related,
                "empty_label": getattr(field, "empty_label", None),
            }
            if type(actual).__name__ in ("RadioSelect", "Select") and hasattr(field, "_choices"):
                widgets[name]["choices"] = [(str(value), str(label)) for value, label in field.choices]
        return widgets

    def form_meta(obj, change):
        form = model_admin.get_form(request, obj, change=change)
        instance = form()
        media = model_admin.media + instance.media
        inlines = []
        for formset, inline in model_admin.get_formsets_with_inlines(request, obj):
            prefix = formset.get_default_prefix()
            fields = list(inline.get_fields(request, obj))
            verbose_name = str(inline.opts.verbose_name)
            media += inline.media + formset().media
            inlines.append({
                "model": inline.opts.label_lower,
                "kind": "tabular" if inline.template.endswith("tabular.html") else "stacked",
                "prefix": prefix,
                "foreign_key": formset.fk.name,
                "min": formset.min_num,
                "max": formset.max_num,
                "absolute_max": formset.absolute_max,
                "extra": formset.extra,
                "classes": list(inline.classes or []),
                "fields": fields,
                "widgets": widgets_for(formset.form.base_fields),
                "ordering": inline.get_ordering(request) or inline.opts.ordering or ["id"],
                "show_change_link": inline.show_change_link,
                "data": json.dumps({
                    "name": f"#{prefix}",
                    "options": {"prefix": prefix, "addText": f"Add another {verbose_name[:1].upper()}{verbose_name[1:]}",
                                "deleteText": "Remove"},
                }),
            })
        return {"widgets": widgets_for(instance.fields), "inlines": inlines,
                "media": {"css": [(section, path) for section, paths in sorted(media._css.items()) for path in paths], "js": media._js}}

    def display_filter(entry):
        if isinstance(entry, str):
            return entry
        if isinstance(entry, (tuple, list)):
            return entry[0]
        return getattr(entry, "parameter_name", entry.__name__)

    opts = model._meta
    model_admin_options = {
        "plural": str(opts.verbose_name_plural),
        "singular": str(opts.verbose_name),
        "table": opts.db_table,
        "key": opts.label_lower,
        "object_name": opts.object_name,
        "default_repr": model.__str__ is Model.__str__,
        "allowed": model_admin.get_model_perms(request),
        "restricted": opts.model_name in ("agentcredential", "agentchangeset"),
        "list_display": [column(name) for name in model_admin.get_list_display(request)],
        "list_links": model_admin.get_list_display_links(request, model_admin.get_list_display(request)),
        "ordering": model_admin.get_ordering(request) or opts.ordering or ["-pk"],
        "choice_ordering": model_admin.get_ordering(request) or opts.ordering or ["pk"],
        "model_ordering": opts.ordering,
        "filter": [display_filter(value) for value in model_admin.get_list_filter(request)],
        "search": model_admin.get_search_fields(request),
        "per_page": model_admin.list_per_page,
        "max_show_all": model_admin.list_max_show_all,
        "autocomplete": model_admin.get_autocomplete_fields(request),
        "readonly_add": list(model_admin.get_readonly_fields(request)),
        "readonly_change": list(model_admin.get_readonly_fields(request, model())),
        "fieldsets_add": fieldsets(None, False),
        "fieldsets_change": fieldsets(model(), True),
        "form_add": form_meta(None, False),
        "form_change": form_meta(model(), True),
        "actions": [list(entry[1:]) for entry in model_admin.get_actions(request).values()],
        "import_export": hasattr(model_admin, "get_import_formats"),
        "save_on_top": model_admin.save_on_top,
        "list_media": {"css": [(section, path) for section, paths in sorted(model_admin.media._css.items()) for path in paths],
                       "js": model_admin.media._js},
    }
    if model_admin_options["import_export"]:
        resource = model_admin.get_export_resource_class()()
        import_media = model_admin.media + model_admin.get_import_form()(model_admin.get_import_formats()).media
        model_admin_options["exchange"] = {
            "headers": resource.get_export_headers(),
            "import_media": import_media._js,
            "fields": [field.column_name for field in resource.get_user_visible_fields()],
            "import_formats": [format().get_title() for format in model_admin.get_import_formats()],
            "export_formats": [format().get_title() for format in model_admin.get_export_formats()],
        }
        if resource._meta.model != model:
            model_admin_options["exchange"]["resource_model"] = resource._meta.model._meta.label_lower
    assert model_admin.get_changelist(request) is not None
    model_admin_options["standard_list"] = model_admin.get_changelist(request) is ChangeList
    model_admin_options["fields"] = {field.name: field_config(field) for field in (*opts.fields, *opts.many_to_many)}
    model_admin_options["storage"] = {
        "table": opts.db_table,
        "fields": {
            field.column: {"column": field.column, "exposed": False,
                           "scale": getattr(field, "decimal_places", None),
                           "schema": {"type": field.get_internal_type()}}
            for field in opts.concrete_fields
        },
    }
    return model_admin_options


def configured_model(request, model, admin):
    from django.test import override_settings

    config = model_config(request, model, admin)
    with override_settings(DEBUG=False):
        production = model_config(request, model, admin)
    if config["list_media"] != production["list_media"]:
        config["production_list_media"] = production["list_media"]
    for key in ("form_add", "form_change"):
        if config[key]["media"] != production[key]["media"]:
            config[key]["production_media"] = production[key]["media"]
    if "exchange" in config and config["exchange"]["import_media"] != production["exchange"]["import_media"]:
        config["exchange"]["production_import_media"] = production["exchange"]["import_media"]
    return config


def main():
    setup()
    from django.contrib import admin
    from django.test import RequestFactory
    from django.contrib.auth import get_user_model

    request = RequestFactory().get("/admin/")
    request.user = get_user_model()(username="fixture-admin", is_active=True, is_staff=True, is_superuser=True)
    site = admin.site
    from django.apps import apps
    content_types = {
        model._meta.label_lower: f"{config.verbose_name} | {model._meta.verbose_name}"
        for config in apps.get_app_configs()
        for model in config.get_models()
    }
    schema = {
        "title": str(site.site_title),
        "header": str(site.site_header),
        "index": str(site.index_title),
        "content_types": content_types,
        "apps": [
            {"label": app["app_label"], "name": str(app["name"]),
             "models": [configured_model(request, entry["model"], site._registry[entry["model"]]) for entry in app["models"]]}
            for app in site.get_app_list(request)
        ],
    }
    output = Path(__file__).resolve().parents[1] / "schema" / "admin.json"
    compact = lambda value: json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False, separators=(",", ":"))
    metadata = {key: value for key, value in schema.items() if key != "apps"}
    apps = []
    for app in schema["apps"]:
        label = {key: value for key, value in app.items() if key != "models"}
        models = ",\n".join(f"      {compact(model)}" for model in app["models"])
        apps.append("    " + compact(label)[:-1] + ', "models": [\n' + models + "\n    ]}")
    output.write_text(compact(metadata)[:-1] + ', "apps": [\n' + ",\n".join(apps) + "\n]}\n", encoding="utf-8")
    from django.test import RequestFactory
    from django.views.i18n import JavaScriptCatalog
    script = JavaScriptCatalog.as_view(packages=["django.contrib.admin"])(RequestFactory().get("/admin/jsi18n/"))
    lines = script.content.decode("utf-8").strip().splitlines()
    output.with_name("admin_jsi18n.js").write_text(
        "\n".join(line.rstrip() for line in lines) + "\n", encoding="utf-8",
    )


if __name__ == "__main__":
    main()
