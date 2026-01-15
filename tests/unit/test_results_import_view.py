from types import SimpleNamespace

import pytest

from core.models.debater import Debater
from core.models.school import School, SchoolLookup
from core.models.team import Team
from core.views.results_import_views import (
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
