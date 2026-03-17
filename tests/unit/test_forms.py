"""Targeted validation for core forms."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core.forms import (
    RoundAmendmentUploadForm,
    TeamChoiceField,
    TeamForm,
    TournamentCreateForm,
    TournamentImportForm,
    TournamentResultsImportOptionsForm,
)
from core.models import Debater, School, Team, Tournament


@pytest.mark.django_db
def test_team_form_requires_exactly_two_debaters():
    school = School.objects.create(name="Form School", included_in_oty=True)
    one = Debater.objects.create(first_name="A", last_name="One", school=school)
    two = Debater.objects.create(first_name="B", last_name="Two", school=school)
    three = Debater.objects.create(first_name="C", last_name="Three", school=school)

    valid_form = TeamForm(
        data={"debaters": [str(one.pk), str(two.pk)], "short_name": "AB"}
    )
    assert valid_form.is_valid()

    invalid_form = TeamForm(data={"debaters": [str(one.pk)], "short_name": "A"})
    assert not invalid_form.is_valid()
    assert "debaters" in invalid_form.errors or "__all__" in invalid_form.errors

    too_many_form = TeamForm(
        data={
            "debaters": [str(one.pk), str(two.pk), str(three.pk)],
            "short_name": "ABC",
        }
    )
    assert not too_many_form.is_valid()


@pytest.mark.django_db
def test_team_choice_field_uses_team_long_name():
    school = School.objects.create(name="Label School", included_in_oty=True)
    first = Debater.objects.create(first_name="Alex", last_name="Smith", school=school)
    second = Debater.objects.create(first_name="Billie", last_name="Jones", school=school)
    team = Team.objects.create(name="Temp Team")
    team.debaters.set([first, second])

    field = TeamChoiceField(queryset=Team.objects.all())

    label = field.label_from_instance(team)

    assert first.last_name in label
    assert second.last_name in label
    assert school.name in label


@pytest.mark.django_db
def test_tournament_import_form_validates_url():
    form = TournamentImportForm(data={"url": "invalid-url"})
    assert not form.is_valid()
    assert "url" in form.errors

    valid = TournamentImportForm(data={"url": "https://nu-tab.com"})
    assert valid.is_valid()


def test_round_amendment_upload_form_requires_json_extension():
    invalid = RoundAmendmentUploadForm(
        files={"amendment_file": SimpleUploadedFile("rounds.txt", b"{}", content_type="text/plain")}
    )
    assert not invalid.is_valid()
    assert "amendment_file" in invalid.errors

    valid = RoundAmendmentUploadForm(
        files={
            "amendment_file": SimpleUploadedFile(
                "rounds.json",
                b"{}",
                content_type="application/json",
            )
        }
    )
    assert valid.is_valid()


@pytest.mark.django_db
def test_tournament_create_form_has_no_api_url_field():
    school = School.objects.create(name="Host School", included_in_oty=True)
    form = TournamentCreateForm(
        data={
            "host": str(school.pk),
            "season": "2024",
            "date": "2024-01-01",
            "num_teams": "16",
            "num_novice_debaters": "0",
            "qual_type": str(Tournament.POINTS),
            "name_suffix": str(Tournament.NONE),
            "manual_name": "",
        }
    )

    assert form.is_valid()
    assert "api_url" not in form.fields


def test_results_import_options_require_url_when_categories_selected():
    form = TournamentResultsImportOptionsForm(
        data={
            "api_url": "",
            "import_varsity_teams": "on",
        }
    )

    assert not form.is_valid()
    assert "__all__" in form.errors


def test_results_import_options_allow_all_db_mode():
    form = TournamentResultsImportOptionsForm(
        data={
            "api_url": "",
        }
    )

    assert form.is_valid()
