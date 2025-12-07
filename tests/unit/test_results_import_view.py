from types import SimpleNamespace, MethodType

import pytest
from django.http import QueryDict

from core.forms import DebaterCreationFormset, SchoolCreationFormset
from core.models.debater import Debater
from core.models.school import School
from core.views.results_import_views import TournamentDataEntryView


@pytest.mark.django_db
def test_existing_school_temp_ids_are_replaced():
    school = School.objects.create(name="Georgetown University", included_in_oty=True)

    view = TournamentDataEntryView()
    view.has_api_data = MethodType(lambda self: True, view)
    view._api_handler = SimpleNamespace(
        link_tournament_school=lambda server_name, school: None,
        _school_name_map={},
    )

    school_post = {
        "schools-TOTAL_FORMS": "1",
        "schools-INITIAL_FORMS": "1",
        "schools-MIN_NUM_FORMS": "0",
        "schools-MAX_NUM_FORMS": "500",
        "schools-0-name": "Georgetown University",
        "schools-0-short_name": "Georgetown",
        "schools-0-included_in_oty": "on",
        "schools-0-existing_school": str(school.id),
        "schools-0-server_name": "Georgetown University",
    }
    schools_formset = SchoolCreationFormset(school_post, prefix="schools")
    assert schools_formset.is_valid()

    created_schools = view._process_schools(schools_formset)
    temp_map = view._build_temp_school_id_map(created_schools)

    post_data = QueryDict(mutable=True)
    for key, value in school_post.items():
        post_data[key] = value
    post_data.update(
        {
            "debaters-TOTAL_FORMS": "1",
            "debaters-INITIAL_FORMS": "1",
            "debaters-MIN_NUM_FORMS": "0",
            "debaters-MAX_NUM_FORMS": "500",
            "debaters-0-first_name": "New",
            "debaters-0-last_name": "Debater",
        "debaters-0-school": "temp_school_Georgetown_University",
            "debaters-0-alias_group": "",
            "debaters-0-existing_debater": "",
            "debaters-0-tournament_id": "21",
            "debaters-0-school_name": "Georgetown University",
        }
    )

    modified_post = view._replace_temp_ids_in_post(post_data, temp_map)
    debater_formset = DebaterCreationFormset(modified_post, prefix="debaters")

    assert debater_formset.is_valid()
    assert debater_formset.cleaned_data[0]["school"] == school


@pytest.mark.django_db
def test_existing_debater_entries_are_processed():
    school = School.objects.create(name="APDA", included_in_oty=True)
    existing = Debater.objects.create(first_name="Sam", last_name="Existing", school=school)
    links = []

    view = TournamentDataEntryView()
    view.has_api_data = MethodType(lambda self: True, view)
    view._api_handler = SimpleNamespace(
        link_tournament_debater=lambda tid, debater: links.append((tid, debater)),
        _debater_id_map={},
    )

    debater_post = {
        "debaters-TOTAL_FORMS": "1",
        "debaters-INITIAL_FORMS": "1",
        "debaters-MIN_NUM_FORMS": "0",
        "debaters-MAX_NUM_FORMS": "500",
        "debaters-0-first_name": existing.first_name,
        "debaters-0-last_name": existing.last_name,
        "debaters-0-school": str(existing.school_id),
        "debaters-0-alias_group": "",
        "debaters-0-existing_debater": str(existing.id),
        "debaters-0-tournament_id": "33",
        "debaters-0-school_name": existing.school.name,
    }

    debater_formset = DebaterCreationFormset(debater_post, prefix="debaters")
    assert debater_formset.is_valid()
    assert debater_formset.cleaned_data[0]["existing_debater"] == existing

    created = view._process_debaters(debater_formset, created_schools={})

    assert created["tid_33"] == existing
    assert links == [("33", existing)]


@pytest.mark.django_db
def test_temp_school_ids_are_dropped_without_mapping():
    view = TournamentDataEntryView()
    post_data = QueryDict(mutable=True)
    post_data.update(
        {
            "debaters-TOTAL_FORMS": "1",
            "debaters-INITIAL_FORMS": "1",
            "debaters-MIN_NUM_FORMS": "0",
            "debaters-MAX_NUM_FORMS": "500",
            "debaters-0-first_name": "Temp",
            "debaters-0-last_name": "School",
            "debaters-0-school": "temp_school_Smith_College",
            "debaters-0-alias_group": "",
            "debaters-0-existing_debater": "",
            "debaters-0-tournament_id": "42",
            "debaters-0-school_name": "Smith College",
        }
    )

    sanitized = view._replace_temp_ids_in_post(post_data, {})
    assert sanitized.get("debaters-0-school") == ""

    debater_formset = DebaterCreationFormset(sanitized, prefix="debaters")
    assert debater_formset.is_valid() is False
    assert "school" in debater_formset.errors[0]


@pytest.mark.django_db
def test_temp_debater_ids_are_replaced_or_cleared():
    school = School.objects.create(name="Mapped", included_in_oty=True)
    debater = Debater.objects.create(first_name="Mapped", last_name="One", school=school)

    view = TournamentDataEntryView()
    view.has_api_data = MethodType(lambda self: True, view)
    view._api_handler = SimpleNamespace(_debater_id_map={"151": debater.id})

    base_data = {
        "varsity_teams-TOTAL_FORMS": "1",
        "varsity_teams-INITIAL_FORMS": "1",
        "varsity_teams-MIN_NUM_FORMS": "0",
        "varsity_teams-MAX_NUM_FORMS": "150",
        "varsity_teams-0-debater_one": "temp_tid_151",
        "varsity_teams-0-debater_two": "temp_tid_999",
    }

    post = QueryDict(mutable=True)
    post.update(base_data)

    mapped = view._replace_temp_debater_ids_in_post(post, {"temp_tid_151": "7"})
    assert mapped.get("varsity_teams-0-debater_one") == "7"
    assert mapped.get("varsity_teams-0-debater_two") == ""

    post_fresh = QueryDict(mutable=True)
    post_fresh.update(base_data)

    unmapped = view._replace_temp_debater_ids_in_post(post_fresh, {})
    assert unmapped.get("varsity_teams-0-debater_one") == str(debater.id)
    assert unmapped.get("varsity_teams-0-debater_two") == ""
