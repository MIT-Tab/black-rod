//! Translate shared Django templates for the MiniJinja renderer.

use std::sync::LazyLock;

use regex::{Captures, Regex};

fn adapt_debater_profile(template: &str) -> String {
    template
        .replace("{% url 'core:school_detail' pk=object.school.id %}", r#""{{ object.school.url }}""#)
        .replace("{% url 'api:debater_detail' object.id %}", "{{ object.api_url }}")
        .replace("{% url 'admin:core_teamresult_changelist' %}", "/admin/core/teamresult/")
        .replace("{% url 'admin:core_speakerresult_changelist' %}", "/admin/core/speakerresult/")
        .replace("{% url 'core:debater_tab_cards_csv' pk=object.id %}", "/core/debaters/{{ object.id }}/tab-cards-csv")
        .replace("{% url 'core:debater_profile_edit' object.pk %}", "/core/debater/{{ object.id }}/edit-profile/")
        .replace("object.user == request.user or request.user.is_superuser", "can_edit_profile")
        .replace(r#"{{ object.first_season }}-{{ object.first_season|add:"1"|stringformat:"s"|slice:"2:4" }}"#, "{{ object.first_season }}")
        .replace(r#"{{ object.latest_season }}-{{ object.latest_season|add:"1"|stringformat:"s"|slice:"2:4" }}"#, "{{ object.latest_season }}")
        .replace("object.show_region", "object.region_display")
        .replace("object.get_region_display", "object.region_display")
        .replace("{{ team|partner_name:object }}", r#"{% if team.partner %}<a href="{{ team.partner.url }}">{{ team.partner.name }}</a>{% else %}NO PARTNER{% endif %}"#)
        .replace("{{ team }}", "{{ team.name }}")
        .replace("round.round|opponent_url:tournament.team", "round.opponent.url")
        .replace("round.round|opponent:tournament.team", "round.opponent.name")
        .replace("round.round|opponent_side:tournament.team", "round.opponent_side")
        .replace("round.round|wl:tournament.team", "round.result")
        .replace("tournament.tournament.date|date:'M j'", "tournament.tournament.date|short_date")
        .replace("{{ tournament.team|partner_display:object }}", r#"{% if tournament.partner %}<a href="{{ tournament.partner.url }}">{{ tournament.partner.name }}</a>{% if tournament.partner.school %} (<a href="{{ tournament.partner.school.url }}">{{ tournament.partner.school.name }}</a>){% endif %}{% else %}NO PARTNER{% endif %}"#)
        .replace("award.get_type_of_place_display", "award.type")
        .replace("award.place|place_as_round_filter:award.ghost_points", "award.place|award_place(award.ghost_points)")
        .replace("{{ award.team }}", "{{ award.team.name }}")
}

fn adapt_profile_editor(template: &str) -> String {
    static FORM: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\{\{ form\.([a-z_]+)(\.label_tag)? \}\}").unwrap());
    FORM.replace_all(template, |capture: &Captures<'_>| {
        let partial = if capture.get(2).is_some() { "profile_label" } else { "profile_field" };
        format!(r#"{{% with field = form.{} %}}{{% include "debater/{partial}.html" %}}{{% endwith %}}"#, &capture[1])
    }).replace("{{ message }}", "{{ message.text }}")
        .replace(r#"|default:"Unknown""#, r#"|default("Unknown", true)"#)
        .replace(r#"|default:"—""#, r#"|default("—", true)"#)
        .replace("object.get_status_display", "object.status")
        .replace(r#"{{ form.instance.DINO|default:"2" }}"#, "2")
        .replace(r#"<form method="post" novalidate>"#, r#"<form method="post" novalidate>
            {% for error in form.non_field_errors %}<div class="alert alert-danger">{{ error }}</div>{% endfor %}"#)
}

fn adapt_scheduler(template: &str) -> String {
    static SETTINGS_INPUT: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\{\{ settings_form\.(\w+) \}\}").unwrap());
    let template = template
        .replace("{{ upload_form.workspace_version }}", r#"<input type="hidden" name="workspace_version" value="{{ version }}" id="id_workspace_version">"#)
        .replace("{{ settings_form.workspace_version }}", r#"<input type="hidden" name="workspace_version" value="{{ version }}" id="id_workspace_version">"#)
        .replace("{{ upload_form.schools_csv }}", r#"<input type="file" name="schools_csv" class="form-control-file" aria-describedby="id_schools_csv_helptext" id="id_schools_csv">"#)
        .replace("{{ upload_form.dates_csv }}", r#"<input type="file" name="dates_csv" class="form-control-file" aria-describedby="id_dates_csv_helptext" id="id_dates_csv">"#)
        .replace("upload_form.schools_csv.errors", "upload_errors.schools_csv")
        .replace("upload_form.dates_csv.errors", "upload_errors.dates_csv")
        .replace("upload_form.non_field_errors", "upload_errors.non_field_errors")
        .replace("settings_form.non_field_errors", "upload_errors.workspace_version")
        .replace("settings_form.errors", "settings_errors")
        .replace("for field in settings_form", "for field in settings_errors")
        .replace(r#"|join:", ""#, r#"|join(", ")"#)
        .replace("|default:'Upload CSVs first'", r#"|default("Upload CSVs first", true)"#);
    SETTINGS_INPUT
        .replace_all(&template, |capture: &Captures<'_>| {
            let field = &capture[1];
            let bounds = if field == "max_workers" { r#" min="1" max="64" aria-describedby="id_max_workers_helptext""# } else { "" };
            format!(r#"<input type="number" name="{field}" class="form-control"{bounds} required id="id_{field}" value="{{{{ settings.{field}.value }}}}">"#)
        })
        .replace("settings_form.", "settings.")
        .replace("<h4 class=\"card-title mb-1\">Scheduling Workspace</h4>", r#"<h4 class="card-title mb-1">Scheduling Workspace</h4>
        {% for message in notices %}<div class="alert alert-{{ message.tags }}" role="alert">{{ message.text }}</div>{% endfor %}
        {% if inline_notice %}<div class="alert alert-danger" role="alert">{{ inline_notice }}</div>{% endif %}"#)
}

pub(super) fn adapt_template(name: &str, template: &str) -> String {
    let adapted;
    let template = match name {
        "socialaccount/login.html" => {
            adapted = [
                "Sign In via APDAOnline",
                "You are about to sign in using your APDAOnline account.",
                "Continue",
                "Back to Login",
            ]
            .into_iter()
            .fold(template.replace("{% load i18n %}", ""), |html, text| {
                html.replace(&format!("{{% trans \"{text}\" %}}"), text)
            });
            &adapted
        }
        "school_admin/merge_debater_request_form.html" | "admin/merge_debater_requests.html" => {
            let mut html = template
                .replace("{{ message }}","{{ message.text }}")
                .replace(r#"|date:"M d, Y H:i""#, "")
                .replace("|default:request.requested_by.username", "")
                .replace("|default:request.processed_by.username", "")
                .replace("request.requested_by.get_full_name", "request.requested_by")
                .replace("request.processed_by.get_full_name", "request.processed_by")
                .replace(r#"|default:"""#, "")
                .replace(r#"|join:" or ""#, r#"|join(" or ")"#)
                .replace("{{ user_pending_requests|length|pluralize }}", "{% if user_pending_requests|length != 1 %}s{% endif %}")
                .replace("{{ form.media }}", r#"{% with media=true %}{% include "school_admin/merge_controls.html" %}{% endwith %}"#)
                .replace("{% for subwidget in form.keep_debater %}", "{% for value, label in choices %}")
                .replace("{{ subwidget.id_for_label }}", "id_keep_debater_{{ loop.index0 }}")
                .replace("{{ subwidget.choice_label }}", "{{ label }}")
                .replace("{{ subwidget.tag }}", r#"<input type="radio" name="keep_debater" value="{{ value }}" class="form-check-input" required id="id_keep_debater_{{ loop.index0 }}"{% if keep == value %} checked{% endif %}>"#);
            for (name, label) in [
                ("school_one", "Debater A School"),
                ("school_two", "Debater B School"),
                ("debater_one", "Debater A"),
                ("debater_two", "Debater B"),
            ] {
                html = html.replace(&format!("{{{{ form.{name}.label_tag }}}}"),&format!(r#"<label for="id_{name}">{label}:</label>"#))
                    .replace(&format!("{{{{ form.{name} }}}}"),&format!(r#"{{% with field="{name}", field_info=form.{name} %}}{{% include "school_admin/merge_controls.html" %}}{{% endwith %}}"#));
            }
            adapted = html;
            &adapted
        }
        "admin/debater_alias_suggestions.html" => {
            adapted = template
                .replace(r#"|default:"?""#, r#"|default("?", true)"#)
                .replace("pair.season_gap is not None", "pair.season_gap is not none")
                .replace("{{ message }}", "{{ message.text }}");
            &adapted
        }
        "admin/merge_suggestions.html" | "admin/synthetic_resolution_suggestions.html" => {
            adapted = template
                .replace(r#"|default:"?""#, r#"|default("?", true)"#)
                .replace(
                    "{% widthratio pair.name_similarity 1 100 %}",
                    "{{ pair.percent_match }}",
                )
                .replace("{{ message }}", "{{ message.text }}");
            &adapted
        }
        "admin/mittab_dashboard.html" => {
            adapted = template.replace(
                "{{ tournaments|length|pluralize }}",
                "{% if tournaments|length != 1 %}s{% endif %}",
            );
            &adapted
        }
        "admin/synthetic_cleanup.html" => {
            adapted = template.replace(
                r#"{{ total_count|pluralize:"y,ies" }}"#,
                "{% if total_count == 1 %}y{% else %}ies{% endif %}",
            );
            &adapted
        }
        "elo/dashboard.html" => {
            static FIELD: LazyLock<Regex> =
                LazyLock::new(|| Regex::new(r"\{\{ form\.(\w+) \}\}").unwrap());
            static LABEL: LazyLock<Regex> =
                LazyLock::new(|| Regex::new(r"\{\{ form\.(\w+)\.label_tag \}\}").unwrap());
            let source = template
                .replace("{% block title %}", r#"{% macro control(field) %}
                    <input type="{{ field.kind }}" name="{{ field.name }}"{% if field.value %} value="{{ field.value }}"{% endif %}
                        {% if field.minimum is not none %} min="{{ field.minimum }}"{% endif %}
                        {% if field.maximum is not none %} max="{{ field.maximum }}"{% endif %}
                        {% if field.step %} step="{{ field.step }}"{% endif %}
                        class="{{ field.classes }}"{% if field.role %} data-slider-role="{{ field.role }}"{% endif %}
                        {% if field.output %} data-slider-output-id="{{ field.output }}" aria-describedby="{{ field.id }}_helptext"{% endif %}
                        {% if field.kind == 'checkbox' %}{% if field.value %} checked{% endif %}{% else %} required{% endif %}
                        {% if field.error %} aria-invalid="true"{% endif %} id="{{ field.id }}">
                    {% endmacro %}
                    {% block title %}"#)
                .replace(".id_for_label", ".id")
                .replace(".help_text", ".help")
                .replace("{% url 'core:debater_detail' pk=row.debater_id %}", "/core/debaters/{{ row.debater_id }}")
                .replace("{% url 'core:school_detail' pk=school.id %}", "/core/schools/{{ school.id }}")
                .replace(r#"row.name|default:"-""#, "row.name or '-'")
                .replace(r#"row.school_name|default:"-""#, "'-'")
                .replace("row.elo|floatformat:2", "row.elo_display");
            let source = FIELD.replace_all(&source, "{{ control(form.$1) }}");
            adapted = LABEL
                .replace_all(
                    &source,
                    r#"<label for="{{ form.$1.id }}">{{ form.$1.label }}:</label>"#,
                )
                .into_owned();
            &adapted
        }
        "admin/tournament_round_ballot.html" => {
            let mut html = template
                .replace(
                    "{{ form.media }}",
                    r#"{% include "autocomplete_media.html" %}{% from "admin/tournament_ballot_control.html" import control %}"#,
                )
                .replace("form.non_field_errors", "errors")
                .replace("form.ballot_rows", "rows");
            for name in [
                "canonical_round_name",
                "source_round_name",
                "stage",
                "outround_stage",
                "round_number",
                "victor",
            ] {
                html = html
                    .replace(
                        &format!("{{{{ form.{name}.id_for_label }}}}"),
                        &format!("id_{name}"),
                    )
                    .replace(
                        &format!("form.{name}.errors"),
                        &format!("fields.{name}.errors"),
                    )
                    .replace(
                        &format!("{{{{ form.{name} }}}}"),
                        &format!("{{{{ control(fields.{name}) }}}}"),
                    );
            }
            for name in ["debater", "role", "source_name", "speaks", "ranks"] {
                html = html.replace(
                    &format!("{{{{ row.{name} }}}}"),
                    &format!("{{{{ control(row.{name}) }}}}"),
                );
            }
            adapted = html;
            &adapted
        }
        "admin/tournament_pipeline_audit.html" | "admin/tournament_audit_detail.html" => {
            adapted = template
                .replace(r#"|join:", ""#, r#"|join(", ")"#)
                .replace(r#"|date:"Y-m-d""#, "")
                .replace(r#"|date:"Y-m-d H:i""#, "")
                .replace(r#"|default:"-""#, r#"|default("-", true)"#)
                .replace("{{ tournament.get_absolute_url }}", "{{ tournament.url }}")
                .replace("{{ import_move_form_media }}", r#"{% include "autocomplete_media.html" %}"#)
                .replace(
                    "{{ row.move_form.tournament_import_id }}",
                    r#"<input type="hidden" name="import-{{ row.id }}-tournament_import_id" value="{{ row.id }}" id="id_import-{{ row.id }}-tournament_import_id">"#,
                )
                .replace(
                    "{{ row.move_form.target_tournament }}",
                    r#"<select name="import-{{ row.id }}-target_tournament" id="id_import-{{ row.id }}-target_tournament"
                            data-autocomplete-light-language="en" data-autocomplete-light-url="/core/tournaments/all_autocomplete" data-autocomplete-light-function="select2" required><option value="" selected>---------</option></select>"#,
                );
            &adapted
        }
        "tournaments/import_sources.html" => {
            adapted = template.replace("sources.items", "sources|items");
            &adapted
        }
        "tournaments/data_entry_setup.html" => {
            adapted = template
                .replace(
                    "{{ form.api_url|as_crispy_field }}",
                    r#"{% include "tournaments/includes/result_setup.html" %}"#,
                )
                .replace("form.non_field_errors", "[]")
                .replace(
                    "{% url 'core:tournament_detail' pk=tournament.id %}",
                    "/core/tournaments/{{ tournament.id }}",
                );
            &adapted
        }
        "tournaments/data_entry.html" => {
            let media =
                Regex::new(r"(?s)\{% for formset in formsets.values %\}.*?\{% endfor %\}").unwrap();
            let panes = Regex::new(r#"(?s)(id="dataEntryTabContent">).*?(</div>\s*<div class="mt-4 pt-3 border-top">)"#).unwrap();
            adapted = panes
                .replace(
                    &media.replace(template, r#"{% include "autocomplete_media.html" %}"#),
                    r#"${1}
                    {% for panel in panels %}
                      {% if (panel.kind == 'school' or panel.kind == 'debater') and not panel.rows and not panel.errors %}
                        <input type="hidden" name="{{ panel.key }}-TOTAL_FORMS" value="0" id="id_{{ panel.key }}-TOTAL_FORMS">
                        <input type="hidden" name="{{ panel.key }}-INITIAL_FORMS" value="0" id="id_{{ panel.key }}-INITIAL_FORMS">
                      {% else %}
                        {% include "tournaments/includes/formset_tab_pane.html" %}
                      {% endif %}
                    {% endfor %}
                    ${2}"#,
                )
                .replace("{{ autoqual_form.media }}", r#"{% include "autocomplete_media.html" %}"#)
                .replace(
                    "{{ autoqual_form.autoqual_debaters|as_crispy_field }}",
                    r#"{% include "tournaments/includes/result_autoqual.html" %}"#,
                )
                .replace(
                    "{% url 'core:tournament_detail' pk=tournament.id %}",
                    "/core/tournaments/{{ tournament.id }}",
                )
                .replace("{{ formsets.schools.management_form }}", "")
                .replace("{{ formsets.debaters.management_form }}", "");
            &adapted
        }
        "tournaments/includes/data_entry_sidebar.html" => {
            adapted = template
                .replace("{% url 'core:school_create' %}", "/core/schools/create")
                .replace(
                    "{{ debater_form.school|as_crispy_field }}",
                    r#"{% from "tournaments/includes/result_widgets.html" import field %}
                    {{ field("", "school", "School", none, "/core/schools/autocomplete", {}, required=true) }}"#,
                );
            &adapted
        }
        "tournaments/includes/formset_tab_pane.html" => {
            adapted = template
                .replace("{% if is_active %}", "{% if active_tab == panel.key %}")
                .replace("{{ tab_id }}", "{{ panel.key }}")
                .replace("{{ item_name }}", "{{ panel.item }}")
                .replace("{{ form_type }}", "{{ panel.kind }}")
                .replace("if has_ghost_points", "if panel.ghost")
                .replace("formset.non_form_errors", "panel.errors")
                .replace("{% url 'core:get_new_team_form' %}", "/core/tournaments/get_new_form")
                .replace("{% for form in formset.forms %}", "{% for row in panel.rows %}")
                .replace(
                    r#"{% include "tournaments/includes/formset_row_new.html" with form=form form_index=forloop.counter0 place_number=forloop.counter form_type=form_type has_ghost_points=has_ghost_points item_name=item_name prefix=tab_id %}"#,
                    r#"{% set form_index = loop.index0 %}{% set place_number = loop.index %}{% set item_name = panel.item %}
                    {% include "tournaments/includes/result_row.html" %}"#,
                )
                .replace(
                    "{{ formset.management_form }}",
                    r#"<input type="hidden" name="{{ panel.key }}-TOTAL_FORMS" value="{{ panel.rows|length }}" id="id_{{ panel.key }}-TOTAL_FORMS">
                    <input type="hidden" name="{{ panel.key }}-INITIAL_FORMS" value="{{ panel.initial_count }}" id="id_{{ panel.key }}-INITIAL_FORMS">
                    <input type="hidden" name="{{ panel.key }}-MIN_NUM_FORMS" value="0" id="id_{{ panel.key }}-MIN_NUM_FORMS">
                    <input type="hidden" name="{{ panel.key }}-MAX_NUM_FORMS" value="150" id="id_{{ panel.key }}-MAX_NUM_FORMS">"#,
                );
            &adapted
        }
        "tournaments/includes/result_row.html" | "tournaments/includes/result_row_ajax.html" => {
            adapted = template.replace(
                r#"{% include "tournaments/includes/form_fields.html" with form=form form_type=form_type has_ghost_points=has_ghost_points %}"#,
                r#"{% include "tournaments/includes/result_fields.html" %}"#,
            );
            &adapted
        }
        "tournaments/mittab_bundle_upload.html" => {
            adapted = template
                .replace(
                    "{{ form.bundle_file|as_crispy_field }}",
                    r#"{% include "tournaments/mittab_bundle_file.html" %}"#,
                )
                .replace(
                    "{% url 'core:tournament_detail' pk=tournament.id %}",
                    "/core/tournaments/{{ tournament.id }}",
                )
                .replace("form.non_field_errors", "[]");
            &adapted
        }
        "tournaments/detail.html" => {
            adapted = template
                .replace("{{ message }}", "{{ message.text }}")
                .replace("stat.debater.get_absolute_url", "stat.debater.url")
                .replace("round.round.round_number", "round.round_number")
                .replace("round.round|opponent_url:team", "round.opponent.url")
                .replace("round.round|opponent_side:team", "round.opponent_side")
                .replace("round.round|opponent:team", "round.opponent.name")
                .replace("round.round|wl:team", "round.result")
                .replace("stat.debater_role|upper", "stat.role");
            &adapted
        }
        "admin/speaks_statistics.html"
        | "admin/includes/speaks_record_distribution.html"
        | "admin/includes/speaks_percentile_chart.html" => {
            adapted = template
                .replace(r#"|floatformat:2|default:"—""#, "|score_decimal(2)")
                .replace("|floatformat:2", "|score_decimal(2)")
                .replace("|floatformat:1", "|score_decimal(1)")
                .replace(
                    "{{ season.round_speaks.tournament_count|pluralize }}",
                    "{% if season.round_speaks.tournament_count != 1 %}s{% endif %}",
                )
                .replace(
                    "{{ metric.tournament_count|pluralize }}",
                    "{% if metric.tournament_count != 1 %}s{% endif %}",
                )
                .replace(
                    "{{ season.round_speaks.correspondence|json_script:season.chart_id }}",
                    r#"<script id="{{ season.chart_id }}" type="application/json">[{% for item in season.round_speaks.correspondence %}{"score": {{ item.score|html_json }}, "sample_size": {{ item.sample_size }}, "cumulative_count": {{ item.cumulative_count }}, "percentile": {{ item.percentile|html_json }}}{% if not loop.last %}, {% endif %}{% endfor %}]</script>"#,
                );
            &adapted
        }
        "school_admin/debater_form.html" => {
            adapted = template
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "school_admin/debater_fields.html" %}"#,
                )
                .replace("form.show_region_field", "show_region")
                .replace("{{ message }}", "{{ message.text }}");
            &adapted
        }
        "school_admin/dashboard.html" | "school_admin/superuser_management.html" => {
            adapted = template
                .replace("schools.count", "schools|length")
                .replace("{{ csrf_token }}", "{{ csrf }}")
                .replace("{% empty %}", "{% else %}")
                .replace("school_admin_roles|dict_item:school.id", "school.role")
                .replace(
                    "current_school_admins|dict_item:school.id",
                    "school.current",
                )
                .replace("school_admins|dict_item:school.id", "school.admins")
                .replace("debaters_data|dict_item:school.id", "school.roster")
                .replace(
                    r#"{% url "core:user_autocomplete" %}"#,
                    "/core/user-autocomplete/",
                )
                .replace(
                    r#"{% url "core:school_admin_add" %}"#,
                    "/core/manage-school-admins/add/",
                )
                .replace(
                    r#"{% url "core:school_admin_remove" %}"#,
                    "/core/manage-school-admins/remove/",
                )
                .replace(
                    r#"{% url "core:school_admin_set_primary" %}"#,
                    "/core/manage-school-admins/primary/",
                )
                .replace(
                    "{% url 'core:debater_detail' debater.id %}",
                    "/core/debaters/{{ debater.id }}",
                );
            &adapted
        }
        "tournaments/update.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace(
                    "{{ form.media }}",
                    r#"{% include "autocomplete_media.html" %}"#,
                )
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "tournaments/fields.html" %}"#,
                );
            &adapted
        }
        "teams/update.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ form.media }}", "")
                .replace("{{ form|crispy }}", r#"{% include "teams/fields.html" %}"#);
            &adapted
        }
        "schools/update.html" | "debaters/update.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ form.media }}", "")
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "core/entity_fields.html" %}"#,
                );
            &adapted
        }
        "videos/create.html" | "videos/update.html" => {
            adapted = template.replace("{{ form.media }}", r#"{% include "django_summernote/widget_media.html" %}"#)
                .replace("{% crispy form %}", r#"{% include "videos/fields.html" %}"#)
                .replace("{{ form|crispy }}", r#"{% include "videos/fields.html" %}"#)
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ debater_form.school|as_crispy_field }}", r#"<div id="div_id_school" class="form-group"><label for="id_school" class="requiredField">School<span class="asteriskField">*</span></label><div><select name="school" id="id_school" class="modelselect2 form-control" data-autocomplete-light-language="en" data-autocomplete-light-url="/core/schools/autocomplete" data-autocomplete-light-function="select2" required><option value="" selected>---------</option></select></div></div>"#);
            &adapted
        }
        "django_summernote/widget_iframe_editor.html" => {
            adapted = template.replace(
                "{% include \"django_summernote/widget_common.html\" with iframe=True %}",
                "{% include \"django_summernote/widget_common.html\" %}",
            );
            &adapted
        }
        "debaters/personal_partnership_stats.html"
        | "debaters/_partnership_stats_results.html"
        | "debaters/_partnership_rankings.html" => {
            adapted = template.replace("{{ form.media }}", r#"{% include "autocomplete_media.html" %}"#)
                .replace("{{ form.debater|as_crispy_field }}", r#"{% with select_name="debater", select_label="Debater" %}{% include "debaters/partnership_person.html" %}{% endwith %}"#)
                .replace(r#"{% include "debaters/_partnership_rankings.html" with rankings=reach_leaders only %}"#,
                    r#"{% with rankings=reach_leaders %}{% include "debaters/_partnership_rankings.html" %}{% endwith %}"#)
                .replace(r#"forloop.counter|add:"1""#, "loop.index + 1")
                .replace("{{ distance|pluralize }}", "{% if distance != 1 %}s{% endif %}")
                .replace("{{ diameter|pluralize }}", "{% if diameter != 1 %}s{% endif %}")
                .replace("{{ forloop.counter|pluralize }}", "{% if loop.index != 1 %}s{% endif %}")
                .replace(".pk }}", ".id }}")
                .replace("{{ debater.get_absolute_url }}", "/core/debaters/{{ debater.id }}")
                .replace("{{ first.get_absolute_url }}", "/core/debaters/{{ first.id }}")
                .replace("{{ second.get_absolute_url }}", "/core/debaters/{{ second.id }}");
            &adapted
        }
        "debaters/partnership_paths.html" => {
            adapted = template.replace("{{ form.media }}", r#"{% include "autocomplete_media.html" %}"#)
                .replace("{{ form.first_debater|as_crispy_field }}", r#"{% with select_name="first_debater", select_label="First debater" %}{% include "debaters/partnership_person.html" %}{% endwith %}"#)
                .replace("{{ form.second_debater|as_crispy_field }}", r#"{% with select_name="second_debater", select_label="Second debater" %}{% include "debaters/partnership_person.html" %}{% endwith %}"#)
                .replace(r#"path|length|add:"-1""#, "(path|length - 1)")
                .replace("{{ hops|pluralize }}", "{% if hops != 1 %}s{% endif %}")
                .replace("{{ debater.get_absolute_url }}", "/core/debaters/{{ debater.id }}")
                .replace("{{ debater.display_name }}", "{{ debater.name }}");
            &adapted
        }
        "debaters/partnership_network.html" => {
            let mut updated = template.replace(
                "{{ form.media }}",
                r#"{% include "autocomplete_media.html" %}"#,
            );
            for (field, label, axis) in [
                ("schools", "Schools", ""),
                ("from_season", "From season", ""),
                ("to_season", "Through season", ""),
                ("min_tournaments", "Minimum tournaments", ""),
                ("qualified", "Nationals qualified", ""),
                ("x_axis", "X axis", "x"),
                ("y_axis", "Y axis", "y"),
            ] {
                updated = updated.replace(&format!("{{{{ form.{field}|as_crispy_field }}}}"),
                    &format!(r#"{{% with field_name="{field}", label="{label}", axis="{axis}" %}}{{% include "debaters/network_filter.html" %}}{{% endwith %}}"#));
            }
            adapted = updated;
            &adapted
        }
        "motions/import.html" => {
            adapted = template
                .replace(r#"|join:", ""#, r#"|join(", ")"#)
                .replace(r#"|join:"; ""#, r#"|join("; ")"#)
                .replace("{{ form|crispy }}", r#"<div id="div_id_spreadsheet" class="form-group"><label for="id_spreadsheet" class="requiredField">Spreadsheet<span class="asteriskField">*</span></label><div><input type="file" name="spreadsheet" class="clearablefileinput form-control-file{% if warning %} is-invalid{% endif %}" required id="id_spreadsheet" aria-describedby="id_spreadsheet_helptext"><small id="hint_id_spreadsheet" class="form-text text-muted">CSV or XLSX. Required column: motion_text. Optional: background_slide, date_set, tournament, tags.</small>{% if warning %}<p id="error_1_id_spreadsheet" class="invalid-feedback"><strong>{{ warning }}</strong></p>{% endif %}</div></div>"#);
            &adapted
        }
        "admin/school_short_name_audit.html" => {
            adapted = template
                .replace("{{ formset.management_form }}", r#"<input type="hidden" name="schools-TOTAL_FORMS" value="{{ total }}" id="id_schools-TOTAL_FORMS"><input type="hidden" name="schools-INITIAL_FORMS" value="{{ total }}" id="id_schools-INITIAL_FORMS"><input type="hidden" name="schools-MIN_NUM_FORMS" value="0" id="id_schools-MIN_NUM_FORMS"><input type="hidden" name="schools-MAX_NUM_FORMS" value="1000" id="id_schools-MAX_NUM_FORMS">"#)
                .replace("{% for form in formset %}", "{% for school in formset %}")
                .replace("{% with school=form.instance %}", "").replace("{% endwith %}", "")
                .replace("{{ form.id }}", r#"<input type="hidden" name="schools-{{ school.index }}-id" value="{{ school.id }}" id="id_schools-{{ school.index }}-id">"#)
                .replace("{{ form.short_name }}", r#"<input type="text" name="schools-{{ school.index }}-short_name" value="{{ school.short_name }}" class="form-control form-control-sm" placeholder="Short name" maxlength="64" id="id_schools-{{ school.index }}-short_name">"#)
                .replace("{% if form.short_name.errors %}", "{% if school.error %}")
                .replace("{% for error in form.short_name.errors %}", "")
                .replace("{{ error }}</div>
                                                    {% endfor %}", "{{ school.error }}</div>");
            &adapted
        }
        "admin/admin_tools.html" => {
            adapted = template
                .replace("{{ user.username }}", "{{ username }}")
                .replace("{{ round_amendment_form.amendment_file.id_for_label }}", "id_amendment_file")
                .replace("{{ round_amendment_form.amendment_file.label }}", "Round amendment JSON")
                .replace("{{ round_amendment_form.amendment_file }}", r#"<input type="file" name="amendment_file" accept=".json,application/json" aria-describedby="id_amendment_file_helptext" required id="id_amendment_file">"#)
                .replace("{% if round_amendment_form.amendment_file.help_text %}", "{% if true %}")
                .replace("round_amendment_form.amendment_file.errors", "round_amendment_errors")
                .replace("{{ round_amendment_form.amendment_file.help_text }}", "Upload a JSON file with synthetic resolutions, round edits, or tournament import moves/deletes.")
                .replace(r#"{% url "core:debater_autocomplete" %}"#, "/core/debaters/autocomplete")
                .replace(r#"{% url "core:school_autocomplete" %}"#, "/core/schools/autocomplete")
                .replace(r#"{% url "core:team_autocomplete" %}"#, "/core/teams/autocomplete");
            &adapted
        }
        "resources/create.html" | "resources/update.html" => {
            adapted = template
                .replace(
                    "{{ form.media }}",
                    r#"{% include "autocomplete_media.html" %}"#,
                )
                .replace(
                    "{% crispy form %}",
                    r#"{% include "resources/fields.html" %}"#,
                )
                .replace(
                    "{{ form|crispy }}",
                    r#"{% include "resources/fields.html" %}"#,
                );
            &adapted
        }
        "base/delete.html" => {
            adapted = template
                .replace("{{ object }}", "{{ object.name }}")
                .replace("{{ object.get_absolute_url }}", "{{ object.url }}");
            &adapted
        }
        "admin/includes/doty_leaderboard.html" => {
            adapted = template
                .replace(
                    "{{ leaderboard.standings|length|pluralize }}",
                    "{% if leaderboard.standings|length != 1 %}s{% endif %}",
                )
                .replace("{% empty %}", "{% else %}")
                .replace(
                    "{{ row.debater.get_absolute_url }}",
                    "/core/debaters/{{ row.debater.id }}",
                )
                .replace(
                    "{{ row.debater.school.get_absolute_url }}",
                    "/core/schools/{{ row.debater.school_id }}",
                )
                .replace("{{ row.debater.school.name }}", "{{ row.debater.school }}")
                .replace(
                    "{{ marker.tournament.get_absolute_url }}",
                    "/core/tournaments/{{ marker.tournament_id }}",
                )
                .replace("|floatformat:-2", "|doty_points");
            &adapted
        }
        "admin/scheduling_dashboard.html" => {
            adapted = adapt_scheduler(template);
            &adapted
        }
        "debaters/detail.html" => {
            adapted = adapt_debater_profile(template);
            &adapted
        }
        "debater/debater_profile_edit.html" => {
            adapted = adapt_profile_editor(template);
            &adapted
        }
        _ => template,
    };
    static DJANGO_TAGS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\{%\s*(load|get_menu|render_bundle|static|url|endblock|cache|endcache)(?:\s+([^%]*?))?\s*%\}").unwrap()
    });
    static INCLUDE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"\{% include "((?:core/stats/[^"]+|base/results/(?:team|speaker)\.html))" with ([^%]+)%\}"#).unwrap()
    });
    static ASSIGN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"[a-z0-9_]+=(?:"[^"]+"|[^ ]+)"#).unwrap());
    let template = INCLUDE.replace_all(template, |captures: &Captures<'_>| {
        let assignments = ASSIGN
            .find_iter(&captures[2])
            .map(|capture| capture.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "{{% with {assignments} %}}{{% include \"{}\" %}}{{% endwith %}}",
            &captures[1]
        )
    });
    let template = if matches!(
        name,
        "debater/my_profile.html"
            | "debater/claim_debater_request_form.html"
            | "admin/claim_debater_requests.html"
    ) {
        std::borrow::Cow::Owned(template
            .replace("{% url 'core:school_detail' debater.school.pk %}", "{{ debater.school.url }}")
            .replace("{% url 'core:debater_profile_edit' debater.pk %}", "/core/debater/{{ debater.id }}/edit-profile/")
            .replace(r#"{{ request.created_at|date:"M d, Y" }}"#, "{{ request.created_at }}")
            .replace(r#"{{ request.created_at|date:"M d, Y H:i" }}"#, "{{ request.created_at_full }}")
            .replace(r#"{{ request.processed_at|date:"M d, Y H:i" }}"#, "{{ request.processed_at }}")
            .replace(r#"|default:"Unknown""#, r#"|default("Unknown", true)"#)
            .replace("debater.get_status_display", "debater.status")
            .replace("request.requested_by.get_full_name|default:request.requested_by.username", "request.requested_by.name")
            .replace("request.processed_by.get_full_name|default:request.processed_by.username", "request.processed_by.name")
            .replace("{{ message }}", "{{ message.text }}")
            .replace("{{ form.media }}", r#"{% include "autocomplete_media.html" %}"#)
            .replace("{{ form.school.label_tag }}", "").replace("{{ form.debater.label_tag }}", "")
            .replace("{{ form.school }}", r#"{% with field="school" %}{% include "debater/claim_selects.html" %}{% endwith %}"#)
            .replace("{{ form.debater }}", r#"{% with field="debater" %}{% include "debater/claim_selects.html" %}{% endwith %}"#)
            .replace("{{ form.school.help_text }}", "First, select the school you debated for")
            .replace("{{ form.debater.help_text }}", "Then search for and select your debater profile")
            .replace("form.school.errors", "errors.school")
            .replace("form.debater.errors", "errors.debater")
            .replace("form.non_field_errors", "errors.non_field_errors"))
    } else {
        template
    };
    let template = if name == "motions/form.html" || name == "base/create.html" {
        std::borrow::Cow::Owned(
            template
                .replace(
                    "{{ form.media }}",
                    r#"{% include "autocomplete_media.html" %}"#,
                )
                .replace("{{ form|crispy }}", "{% include \"motions/fields.html\" %}"),
        )
    } else {
        template
    };
    let rendered = DJANGO_TAGS.replace_all(&template, |capture: &Captures<'_>| {
        let tag = &capture[1];
        let arguments = capture.get(2).map_or("", |value| value.as_str().trim());
        match tag {
            "load" | "get_menu" | "cache" | "endcache" => String::new(),
            "endblock" => "{% endblock %}".into(),
            "render_bundle" => {
                "{% for bundle in bundles %}<script src=\"{{ bundle }}\"></script>{% endfor %}"
                    .into()
            }
            "static" => format!("/static/{}", arguments.trim_matches('\'')),
            "url" => {
                let route = arguments.split(['\'', '"']).nth(1).unwrap_or_default();
                let path = match route {
                    "core:index" => "/",
                    "core:school_create" => "/core/schools/create",
                    "core:admin_tools" => "/core/admin-tools/",
                    "core:school_admin_debater_create" => {
                        "/core/school-admin/{{ school.id }}/debater/create/"
                    }
                    "core:school_admin_debater_update" => {
                        "/core/school-admin/debater/{{ debater.id }}/edit/"
                    }
                    "core:school_admin_dashboard" => "/core/school-admin/",
                    "core:doty" => "/DOTY",
                    "core:partnership_paths" => "/core/debaters/partnership-paths/",
                    "core:partnership_stats" => "/core/debaters/partnership-paths/stats/",
                    "core:partnership_stats_results" => {
                        "/core/debaters/partnership-paths/stats/results/"
                    }
                    "core:clear_partnership_stats_cache" => {
                        "/core/debaters/partnership-paths/stats/clear-cache/"
                    }
                    "core:debater_partnership_stats" => "/core/debaters/partnership-paths/person/",
                    "core:partnership_network" => "/core/debaters/partnership-paths/network/",
                    "core:partnership_network_paths" => {
                        "/core/debaters/partnership-paths/network/paths/"
                    }
                    "core:speaks_statistics" => "/core/admin/speaks-stats/",
                    "core:tournament_dataentry" => "/core/tournaments/data_entry",
                    "core:mittab_dashboard" => "/core/mittab-dashboard/",
                    "core:rankings_recompute" => "/core/rankings-recompute/",
                    "core:scheduling_dashboard" => "/core/admin/scheduling/",
                    "core:tournament_pipeline_audit" => "/core/admin/tournament-pipeline-audit/",
                    "core:school_short_name_audit" => "/core/admin/school-short-names/",
                    "core:debater_autocomplete" => "/core/debaters/autocomplete",
                    "core:school_autocomplete" => "/core/schools/autocomplete",
                    "core:synthetic_resolution_suggestions" => {
                        "/core/admin/synthetic-resolution-suggestions/"
                    }
                    "core:synthetic_cleanup" => "/core/admin/synthetic-cleanup/",
                    "core:invalidate_elo_cache" => "/core/admin/elo-cache/invalidate/",
                    "core:synthetic_resolution" => "/core/admin/synthetic-resolve/",
                    "core:round_amendments_upload" => "/core/admin/round-amendments/",
                    "core:scheduling_download_csv" if arguments.contains("'schools'") => {
                        "/core/admin/scheduling/download/schools/"
                    }
                    "core:scheduling_download_csv" => "/core/admin/scheduling/download/dates/",
                    "core:scheduling_run_output" if arguments.contains("latest_completed_run") => {
                        "/core/admin/scheduling/runs/{{ latest_completed_run.id }}/output/"
                    }
                    "core:scheduling_run_output" => {
                        "/core/admin/scheduling/runs/{{ run.id }}/output/"
                    }
                    "api:season_standings" => "/api/standings/",
                    "api:school_detail" => "/api/schools/{{ object.id }}/detail/",
                    "api:tournament_detail" => "/api/tournaments/{{ object.id }}/detail/",
                    "core:tournament_mittab_bundle_upload" => {
                        "/core/tournaments/mittab_bundle/upload"
                    }
                    "core:tournament_dataentry_setup" => "/core/tournaments/data_entry/setup",
                    "core:resource_list" => "/core/resources/",
                    "core:my_debater_profile" => "/core/my-profile/",
                    "core:claim_debater_request_create" => "/core/claim-debater/request/",
                    "core:motion_status" => "/core/motions/{{ object.id }}/status",
                    "core:tag_detail" => "{{ tag_url(tag.name) }}",
                    "core:team_detail" => "{{ result.team.url }}",
                    "api:team_detail" => "/api/teams/{{ object.id }}/detail/",
                    "core:tournament_detail" if arguments.contains("result.result") => {
                        "{{ result.result.tournament.url }}"
                    }
                    "core:tournament_detail" => "{{ result.tournament.url }}",
                    "core:ai_plugin_manifest" => "/.well-known/ai-plugin.json",
                    "core:openapi_schema" => "/.well-known/openapi.json",
                    "core:standings_replay" => "/standings/replay/",
                    "core:immaculate_grid" => "/immaculate-grid/",
                    "core:immaculate_grid_guess" => "/immaculate-grid/guess/",
                    "core:immaculate_grid_answers" => "/immaculate-grid/answers/",
                    "core:immaculate_grid_search" => "/immaculate-grid/search/",
                    "core:immaculate_grid_infinite" => "/immaculate-grid/infinite/",
                    "core:immaculate_grid_infinite_guess" => "/immaculate-grid/infinite/guess/",
                    "core:immaculate_grid_leaderboard" => "/immaculate-grid/leaderboard/",
                    "core:immaculate_grid_player_board" => {
                        "/immaculate-grid/leaderboard/{{ entry.id }}/"
                    }
                    "core:import_sources" => "/core/tournaments/import/",
                    "core:live_standings" => "/live",
                    "core:debater_detail" if arguments.contains("result.debater") => {
                        "{{ result.debater.url }}"
                    }
                    "core:debater_detail" => "{{ speaker.debater.url }}",
                    "core:school_detail" => "{{ speaker.school.url }}",
                    _ => panic!("Unmapped template route {route}"),
                };
                if let Some((_, alias)) = arguments.split_once(" as ") {
                    format!("{{% set {alias} = '{path}' %}}")
                } else {
                    path.into()
                }
            }
            _ => unreachable!(),
        }
    });
    rendered
        .replace("{{ block.super }}", "{{ super() }}")
        .replace("{% csrf_token %}", r#"<input type="hidden" name="csrfmiddlewaretoken" value="{{ csrf }}">"#)
        .replace("object.authors.all", "object.authors")
        .replace("object.get_resource_type_display", "object.resource_type")
        .replace("object.get_viewing_permission_display", "object.viewing_permission")
        .replace("object.created_at|date:\"F d, Y\"", "object.created_at")
        .replace("object.date_set|default:\"Unknown\"", "object.date_set or 'Unknown'")
        .replace("{{ object.tournament }}", "{{ object.tournament.name }}")
        .replace("object.tournament.get_absolute_url", "object.tournament.url")
        .replace("{% empty %}", "{% else %}")
        .replace("payload is not None", "payload is not none")
        .replace("|date:'Y-m-d'", "")
        .replace(r#"|date:"F j, Y""#, r#"|calendar_date("%B %-d, %Y")"#)
        .replace("|date:'F j'", r#"|calendar_date("%B %-d")"#)
        .replace(r#"|date:"M j, Y""#, r#"|calendar_date("%b %-d, %Y")"#)
        .replace(r#"score_day|date:"M j""#, "score_day|short_date")
        .replace(r#"|date:"D""#, r#"|calendar_date("%a")"#)
        .replace(r#"|default_if_none:"—""#, "|grid_score")
        .replace("latest_guess.score is None", "latest_guess.score is none")
        .replace(r#"not infinite or request.method == "POST""#, "not infinite or practice_post_failed")
        .replace("score is None", "score is none")
        .replace("week_days|length|add:3", "(week_days|length) + 3")
        .replace(r#"|json_script:"immaculate-miss-names""#, "|grid_miss_names")
        .replace("forloop.parentloop.counter", "(cell.index // 3) + 1")
        .replace("forloop.counter", "loop.index")
        .replace("forloop.revcounter", "loop.revindex")
        .replace("forloop.first", "loop.first")
        .replace("forloop.last", "loop.last")
        .replace("loop.index|range_filter:3", "range(loop.index, 3)")
        .replace("{{ object }}", "{{ object.name }}")
        .replace("|floatformat:1", "|one_decimal")
        .replace("message.tags|default:'info'", "message.tags|default('info')")
        .replace("object.autoqual_recipients.exists", "recipients")
        .replace("object.autoqual_recipients.all", "recipients")
        .replace("object.videos.all", "videos")
        .replace("{{ object.date }}", "{{ object.date|long_date }}")
        .replace("|yesno:\"Yes,No\"", "|yesno")
        .replace("round.round|opponent_url:team", "round.opponent.url")
        .replace("round.round|opponent_side:team", "round.opponent_side")
        .replace("round.round|opponent:team", "round.opponent.name")
        .replace("round.round|wl:team", "round.result")
        .replace("loop.index|place_as_round_filter:result.ghost_points", "loop.index|award_place(result.ghost_points)")
        .replace("{{ result.team }}", "{{ result.team.name }}")
        .replace("{{ result.team.debaters_display|safe }}", r#"{% for debater in result.team.debaters %}<a href="{{ debater.url }}">{{ debater.name }}</a>{% if not loop.last %} and {% endif %}{% endfor %}"#)
        .replace("video.get_round_display", "video.round")
        .replace("video.tags.all", "video.tags")
        .replace("object.tags.all", "object.tags")
        .replace("object.get_round_display", "object.round")
        .replace("object.get_permissions_display", "object.permissions")
        .replace("{{ video.pm }}", "{{ video.pm.name }}")
        .replace("{{ video.mg }}", "{{ video.mg.name }}")
        .replace("{{ video.lo }}", "{{ video.lo.name }}")
        .replace("{{ video.mo }}", "{{ video.mo.name }}")
        .replace("item.debaters.all", "item.debater_list")
        .replace("{{ item.debaters_display }}", r#"{% for debater in item.debater_list %}<a href="{{ debater.url }}">{{ debater.name }}</a>{% if not loop.last %} and {% endif %}{% endfor %}"#)
        .replace("object.included_in_oty|yesno:\"Yes,No\"", "'Yes' if object.included_in_oty else 'No'")
        .replace("tournament.date|date:\"M j\"", "tournament.date|short_date")
        .replace(".get_season_display", ".season_display")
        .replace(
            "{% include \"base/results/full_team.html\" with results=team_results %}",
            "{% include \"base/results/full_team.html\" %}",
        )
        .replace("round.round.round_number", "round.round_number")
        .replace("round.round|opponent_url:object.get_absolute_url", "round.opponent.url")
        .replace("round.round|opponent_side:object", "round.opponent_side")
        .replace("round.round|opponent:object", "round.opponent.name")
        .replace("round.round|wl:object", "round.result")
        .replace("stat.debater_role|upper", "stat.role")
        .replace("result.result.place|place_as_round_filter:result.result.ghost_points", "result.result.place|award_place(result.result.ghost_points)")
        .replace("{{ result.result.place|place_as_round_filter }}", "")
        .replace("result.result.get_type_of_place_display", "result.result.type")
        .replace("{{ object.host }}", "")
        .replace("request.GET.season|years_on_team:debater.first_season", "debater.years_on_team")
        .replace("live_sources|join:\", \"", "live_sources|join(\", \")")
        .replace("school.school|relevant_debaters:current_season", "school.breakdown")
        .replace("debater.debater|qual_display:current_season", "debater.auto_quals")
        .replace("debater|qual_contribution:current_season|number", "debater.contribution|number")
        .replace(".get_absolute_url", ".url")
        .replace("speaker.debater.school.name", "speaker.school.name|default('')")
        .replace("{{ team.team.debaters_display|safe }}", r#"{% for debater in team.team.debaters %}<a href="{{ debater.url }}">{{ debater.name }}</a>{% if not loop.last %} and {% endif %}{% endfor %}"#)
}
