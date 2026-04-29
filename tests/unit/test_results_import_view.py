from datetime import date
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.db import IntegrityError
from django.test import RequestFactory

from core.models.debater import Debater
from core.models.school import School, SchoolLookup
from core.models.team import Team
from core.models.tournament import Tournament
from core.views.results_import_views import (
    SchoolImportFormset,
    TournamentDataEntryView,
    build_api_initial,
    build_speaker_initial,
    build_team_initial,
    cleanup_temporary_debaters,
    cleanup_temporary_schools,
    seed_temporary_debaters,
    seed_temporary_schools,
)


@pytest.mark.django_db
def test_cleanup_temporary_debaters_removes_only_unreferenced():
    school = School.all_objects.create(
        name="Temp U", short_name="Temp U", included_in_oty=True, temporary=True
    )
    unused = Debater.all_objects.create(
        first_name="Unused", last_name="Debater", school=school, temporary=True
    )
    kept = Debater.all_objects.create(
        first_name="Keeps", last_name="Team", school=school, temporary=True
    )
    team = Team.objects.create(name="Temp Team", short_name="Temp Team")
    team.debaters.add(kept)

    deleted = cleanup_temporary_debaters(max_delete=10)

    assert deleted == 1
    assert not Debater.all_objects.filter(id=unused.id).exists()
    assert Debater.all_objects.filter(id=kept.id).exists()


@pytest.mark.django_db
def test_cleanup_temporary_debaters_aborts_on_large_delete():
    school = School.all_objects.create(
        name="Temp Big", short_name="Temp Big", included_in_oty=True, temporary=True
    )
    Debater.all_objects.create(
        first_name="One", last_name="Tmp", school=school, temporary=True
    )
    Debater.all_objects.create(
        first_name="Two", last_name="Tmp", school=school, temporary=True
    )

    with pytest.raises(RuntimeError):
        cleanup_temporary_debaters(max_delete=1)


@pytest.mark.django_db
def test_cleanup_temporary_schools_respects_debaters():
    to_delete = School.all_objects.create(
        name="Orphan School", short_name="Orphan School", temporary=True
    )
    keep = School.all_objects.create(
        name="Has Debater", short_name="Has Debater", temporary=True
    )
    Debater.all_objects.create(
        first_name="Attached", last_name="One", school=keep, temporary=True
    )

    deleted = cleanup_temporary_schools(max_delete=10)

    assert deleted == 1
    assert not School.all_objects.filter(id=to_delete.id).exists()
    assert School.all_objects.filter(id=keep.id).exists()


@pytest.mark.django_db
def test_cleanup_temporary_schools_aborts_on_large_delete():
    School.all_objects.create(name="Temp1", short_name="Temp1", temporary=True)
    School.all_objects.create(name="Temp2", short_name="Temp2", temporary=True)
    with pytest.raises(RuntimeError):
        cleanup_temporary_schools(max_delete=1)


@pytest.mark.django_db
def test_seed_temporary_schools_creates_and_links():
    linked = []

    def link(server_name, school):
        linked.append((server_name, school.id if hasattr(school, "id") else school))

    handler = SimpleNamespace(
        get_new_schools_from_api=lambda: [
            {"name": "New School", "server_name": "new-school", "included_in_oty": True}
        ],
        link_tournament_school=link,
    )

    schools_by_server = seed_temporary_schools(handler)

    assert "new-school" in schools_by_server
    school = schools_by_server["new-school"]
    assert school.temporary is True
    assert SchoolLookup.objects.filter(server_name="new-school", school=school).exists()
    assert linked == [("new-school", school.id)]


@pytest.mark.django_db
def test_seed_temporary_schools_reuses_existing():
    existing = School.all_objects.create(
        name="Existing School", short_name="Existing", temporary=False
    )
    handler = SimpleNamespace(
        get_new_schools_from_api=lambda: [
            {"name": "Existing School", "server_name": "existing-school"}
        ],
        link_tournament_school=lambda server, school: None,
    )

    schools_by_server = seed_temporary_schools(handler)

    assert schools_by_server["existing-school"].id == existing.id
    assert schools_by_server["existing-school"].temporary is False


@pytest.mark.django_db
def test_seed_temporary_debaters_creates_and_links():
    school = School.all_objects.create(
        name="Temp U", short_name="Temp U", included_in_oty=True, temporary=True
    )
    schools_by_server = {"Temp U": school}
    links = []
    handler = SimpleNamespace(
        get_new_debaters_from_api=lambda: [
            {
                "first_name": "New",
                "last_name": "Person",
                "school_name": "Temp U",
                "tournament_id": "77",
            }
        ],
        link_tournament_debater=lambda tid, debater: links.append((tid, debater.id)),
    )

    created = seed_temporary_debaters(handler, schools_by_server)

    assert len(created) == 1
    debater = created[0]
    assert debater.temporary is True
    assert debater.school == school
    assert links == [("77", debater.id)]


@pytest.mark.django_db
def test_seed_temporary_debaters_reuses_existing_match():
    school = School.all_objects.create(
        name="Existing School", short_name="Existing School", temporary=False
    )
    existing = Debater.all_objects.create(
        first_name="Sam", last_name="Existing", school=school, temporary=False
    )
    handler = SimpleNamespace(
        get_new_debaters_from_api=lambda: [
            {
                "first_name": "Sam",
                "last_name": "Existing",
                "school": school,
                "tournament_id": "88",
            }
        ],
        link_tournament_debater=lambda tid, debater: None,
    )

    created = seed_temporary_debaters(handler, {})

    assert created == []
    assert Debater.objects.filter(id=existing.id).exists()


def test_build_team_initial_skips_incomplete_entries():
    handler = SimpleNamespace(
        get_teams_from_api=lambda endpoint: [
            {"debater_one": "A", "debater_two": "B"},
            {"debater_one": "OnlyOne"},
        ]
    )

    initial = build_team_initial(handler, "varsity-team-placements")

    assert len(initial) == 1
    assert initial[0]["debater_one"] == "A"
    assert initial[0]["counts_for_points"] is True
    assert initial[0]["ORDER"] == 1


def test_build_speaker_initial_skips_missing_speakers():
    handler = SimpleNamespace(
        get_speakers_from_api=lambda endpoint: [
            {"speaker": "X", "tie": True},
            {"speaker": None},
        ]
    )

    initial = build_speaker_initial(handler, "varsity-speaker-awards")

    assert len(initial) == 1
    assert initial[0]["speaker"] == "X"
    assert initial[0]["tie"] is True
    assert initial[0]["counts_for_points"] is True


def test_build_api_initial_only_uses_selected_tabs():
    handler = SimpleNamespace(
        get_teams_from_api=lambda endpoint: [{"debater_one": endpoint, "debater_two": "B"}],
        get_speakers_from_api=lambda endpoint: [{"speaker": endpoint, "tie": False}],
    )

    initial = build_api_initial(handler, ["varsity_teams", "novice_speakers"])

    assert set(initial.keys()) == {"varsity_teams", "novice_speakers"}
    assert initial["varsity_teams"][0]["debater_one"] == "varsity-team-placements"
    assert initial["novice_speakers"][0]["speaker"] == "novice-speaker-awards"


@pytest.mark.django_db
def test_update_tournament_counts_from_api_updates_expected_fields():
    school = School.objects.create(name="Count School", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Count Tournament",
        host=school,
        date=date(2024, 1, 1),
        season=settings.CURRENT_SEASON,
        num_teams=10,
        num_novice_debaters=5,
    )

    request = RequestFactory().get(
        "/core/tournaments/data_entry",
        data={"tournament": tournament.id, "api_url": "https://api.example", "import_counts": "1"},
    )
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)

    view = TournamentDataEntryView()
    view.setup(request)
    view._api_handler = SimpleNamespace(
        get_debater_counts_from_api=lambda: {"teams": 24, "novice": 26}
    )

    view.update_tournament_counts_from_api(tournament)

    tournament.refresh_from_db()
    assert tournament.num_teams == 24
    assert tournament.num_novice_debaters == 26


@pytest.mark.django_db
def test_update_tournament_counts_from_api_ignores_negative_values():
    school = School.objects.create(name="Negative Count School", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Negative Count Tournament",
        host=school,
        date=date(2024, 1, 1),
        season=settings.CURRENT_SEASON,
        num_teams=10,
        num_novice_debaters=5,
    )

    request = RequestFactory().get(
        "/core/tournaments/data_entry",
        data={"tournament": tournament.id, "api_url": "https://api.example", "import_counts": "1"},
    )
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)

    view = TournamentDataEntryView()
    view.setup(request)
    view._api_handler = SimpleNamespace(
        get_debater_counts_from_api=lambda: {"teams": -24, "novice": -26}
    )

    view.update_tournament_counts_from_api(tournament)

    tournament.refresh_from_db()
    assert tournament.num_teams == 10
    assert tournament.num_novice_debaters == 5


@pytest.mark.django_db
def test_forms_valid_rejects_api_mode_post_without_school_debater_management_forms():
    school = School.objects.create(name="Guard School", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Guard Tournament",
        host=school,
        date=date(2024, 1, 1),
        season=settings.CURRENT_SEASON,
    )

    post_data = {
        "tournament": str(tournament.id),
        "api_url": "https://api.example",
        "import_varsity_teams": "1",
    }
    for prefix in [
        "varsity_teams",
        "varsity_speakers",
        "novice_teams",
        "novice_speakers",
        "unplaced_teams",
    ]:
        post_data[f"{prefix}-TOTAL_FORMS"] = "0"
        post_data[f"{prefix}-INITIAL_FORMS"] = "0"

    request = RequestFactory().post("/core/tournaments/data_entry", data=post_data)
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)

    view = TournamentDataEntryView()
    view.setup(request)
    formsets = view.build_formsets(
        tournament=tournament,
        api_state=None,
        use_api=True,
        data=request.POST,
    )

    assert "schools" in formsets
    assert "debaters" in formsets
    assert not view.forms_valid(formsets, use_api=True)


@pytest.mark.django_db
def test_resolve_schools_skips_blank_import_rows():
    School.all_objects.create(name="", short_name="", temporary=False)
    formset = SchoolImportFormset(
        data={
            "schools-TOTAL_FORMS": "1",
            "schools-INITIAL_FORMS": "0",
            "schools-MIN_NUM_FORMS": "0",
            "schools-MAX_NUM_FORMS": "500",
            "schools-0-name": "",
            "schools-0-short_name": "",
            "schools-0-existing_school": "",
            "schools-0-server_name": "",
            "schools-0-included_in_oty": "on",
        },
        queryset=School.objects.none(),
        prefix="schools",
        form_kwargs={"allow_blank_name": True},
    )

    assert formset.is_valid(), formset.errors

    view = TournamentDataEntryView()
    try:
        resolution = view.resolve_schools(formset)
    except IntegrityError as exc:  # pragma: no cover - documents prior failure mode
        pytest.fail(f"blank school rows should be ignored, but save hit IntegrityError: {exc}")

    assert resolution == {}
    assert School.all_objects.filter(name="").count() == 1


@pytest.mark.django_db
def test_resolve_schools_uses_server_name_fallback_for_blank_name():
    formset = SchoolImportFormset(
        data={
            "schools-TOTAL_FORMS": "1",
            "schools-INITIAL_FORMS": "0",
            "schools-MIN_NUM_FORMS": "0",
            "schools-MAX_NUM_FORMS": "500",
            "schools-0-name": "",
            "schools-0-short_name": "",
            "schools-0-existing_school": "",
            "schools-0-server_name": "Hopkins",
            "schools-0-included_in_oty": "on",
        },
        queryset=School.objects.none(),
        prefix="schools",
        form_kwargs={"allow_blank_name": True},
    )

    assert formset.is_valid(), formset.errors

    view = TournamentDataEntryView()
    resolution = view.resolve_schools(formset)

    school = School.objects.get(name="Hopkins")
    assert school.short_name == "Hopkins"
    assert resolution == {school.pk: school}
