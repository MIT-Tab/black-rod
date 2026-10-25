from copy import deepcopy
from datetime import date
from unittest.mock import Mock, patch

import pytest
import requests
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from core.import_sources import (
    INDEX_KEY, RETENTION, cached_source, fresh_or_cached_source, load_review,
    refresh_import_sources, save_review, source_index, source_key, store_source,
)
from core.models import COTY, QUAL, Debater, QualPoints, School, Team, TeamResult, Tournament
from core.utils.rankings import rebuild_import_qualifications, redo_rankings
from core.views.import_source_views import match_tournament

URL = "https://example.nu-tab.com"


def payload():
    return {"schema_version": 1, "source_url": URL, "import_ready": True,
            "tournament": {"name": "MIT Fall", "date": "2026-10-24", "season": "2026",
                           "host_school": {"apda_id": 1}, "tournament_type": 0},
            "bundle": {"rounds": [{"import_key": "prelim:1"}]},
            "varsity_team_placements": [], "debater_counts": {"teams": 32}}


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def test_snapshot_overwrite_fallback_and_expiry():
    key = source_key(URL)
    store_source(URL, payload())
    updated = payload()
    updated["debater_counts"]["teams"] = 64
    store_source(URL, updated)
    with patch("core.import_sources.requests.get", side_effect=requests.ConnectionError):
        assert fresh_or_cached_source(key)["debater_counts"]["teams"] == 64
    index = source_index()
    index[key]["missing_since"] = timezone.now().timestamp() - RETENTION - 1
    cache.set(INDEX_KEY, index)
    assert cached_source(key) is None
    with pytest.raises(ValueError, match="expired"):
        fresh_or_cached_source(key)


def test_directory_failure_does_not_evict_but_disappearance_starts_retention():
    store_source(URL, payload())
    with patch("core.import_sources.requests.get", side_effect=requests.ConnectionError):
        refresh_import_sources()
    assert source_index()[source_key(URL)]["missing_since"] is None
    directory = Mock()
    directory.json.return_value = {"tournaments": []}
    with patch("core.import_sources.requests.get", side_effect=[directory, requests.ConnectionError]):
        refresh_import_sources()
    first_missing = source_index()[source_key(URL)]["missing_since"]
    assert first_missing
    with patch("core.import_sources.requests.get", side_effect=[directory, requests.ConnectionError]):
        refresh_import_sources()
    assert source_index()[source_key(URL)]["missing_since"] == first_missing
    assert cached_source(source_key(URL))["bundle"]


def test_review_is_immutable_scoped_and_detects_missing_chunks():
    original = payload()
    token = save_review(original, 1, 2)
    changed = deepcopy(original)
    changed["bundle"] = {"rounds": []}
    store_source(URL, changed)
    assert load_review(token, 1, 2) == original
    with pytest.raises(ValueError):
        load_review(token, 1, 3)
    keys, _, _ = cache.get(f"mittab-review:{token}")
    cache.delete(keys[0])
    with pytest.raises(ValueError):
        load_review(token, 1, 2)


@pytest.mark.django_db
def test_admin_only_discovery_and_cached_import(client, django_user_model):
    host = School.objects.create(pk=1, name="MIT", short_name="MIT")
    store_source(URL, payload())
    url = reverse("core:import_sources")
    assert client.get(url).status_code == 302
    ordinary = django_user_model.objects.create_user(username="ordinary", is_staff=True)
    client.force_login(ordinary)
    assert client.get(url).status_code == 403
    admin = django_user_model.objects.create_superuser(username="admin", email="a@example.com")
    client.force_login(admin)
    with patch("core.views.import_source_views._directory_urls", return_value=[]):
        assert client.get(url).status_code == 200
        with patch("core.import_sources.requests.get", side_effect=requests.ConnectionError):
            response = client.post(url, {"source": source_key(URL)})
    assert response.status_code == 302
    assert "review=" in response.url
    tournament = Tournament.objects.get(host=host)
    assert tournament.date == date(2026, 10, 24)
    assert match_tournament(payload()).pk == tournament.pk


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026", ONLINE_SEASONS=())
def test_bulk_qualifications_correct_corrections_and_query_budget(django_assert_max_num_queries):
    school = School.objects.create(name="Host", short_name="Host")
    tournament = Tournament.objects.create(host=school, date=date(2026, 10, 24), season="2026", num_teams=32)
    team = Team.objects.create(name="Host AB")
    people = Debater.objects.bulk_create([Debater(pk=i + 1, first_name="Speaker", last_name=str(i), school=school,
                                                latest_season="2026") for i in range(100)])
    team.debaters.add(*people)
    result = TeamResult.objects.create(team=team, tournament=tournament, place=1,
                                      type_of_place=Debater.VARSITY)
    with django_assert_max_num_queries(45):
        rebuild_import_qualifications("2026")
    expected = tournament.get_qual_points(1)
    assert QualPoints.objects.count() == 100
    assert all(row.points == expected for row in QualPoints.objects.all())
    assert COTY.objects.get().points == sum(min(60, expected) + 6 for _ in people)
    result.delete()
    rebuild_import_qualifications("2026")
    assert not QUAL.objects.exists()
    assert not QualPoints.objects.exists()
    assert Team.objects.filter(pk=team.pk).exists()


@pytest.mark.django_db
def test_ranking_ties_have_bounded_queries(django_assert_max_num_queries):
    schools = School.objects.bulk_create([School(pk=i + 1, name=f"School {i}") for i in range(100)])
    COTY.objects.bulk_create([COTY(school=school, season="2026", points=10) for school in schools])
    with django_assert_max_num_queries(6):
        redo_rankings(COTY.objects.filter(season="2026"), "2026", "coty")
    assert set(COTY.objects.values_list("place", "tied")) == {(1, True)}


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026", ONLINE_SEASONS=())
def test_review_save_imports_rounds_and_corrected_identity_without_network(client, django_user_model):
    from core.models import Round, SpeakerResult

    host = School.objects.create(pk=1, name="MIT", short_name="MIT")
    people = [Debater.objects.create(first_name="Known", last_name=str(i), school=host) for i in range(3)]
    tournament = Tournament.objects.create(host=host, season="2026", date=date(2026, 10, 24))
    admin = django_user_model.objects.create_superuser(username="importer", email="importer@example.com")
    client.force_login(admin)
    snapshot = payload()
    refs = [{"apda_id": -1, "tournament_id": 101}] + [
        {"apda_id": person.pk, "tournament_id": 102 + index} for index, person in enumerate(people)
    ]
    snapshot.update(
        new_schools=[],
        new_debater_data=[{"name": "Misspelled Speaker", "novice_status": 0,
                           "school_id": host.pk, "school_name": host.name, "debater_id": 101}],
        varsity_team_placements=[refs[:2]], novice_team_placements=[], non_placing_teams=[refs[2:]],
        varsity_speaker_awards=refs[:2], novice_speaker_awards=[],
    )
    snapshot["bundle"] = {
        "schema_version": 1, "source": "mit_tab_black_rod_bundle", "tournament_name": "MIT Fall",
        "schools": [{"id": 1, "apda_id": 1, "name": "MIT"}],
        "debaters": [{"id": 101, "apda_id": None, "name": "Misspelled Speaker", "school_id": 1}] + [
            {"id": 102 + index, "apda_id": person.pk, "name": person.name, "school_id": 1}
            for index, person in enumerate(people)
        ],
        "rounds": [{"import_key": "prelim:1", "round_number": 1, "label": "Round 1", "stage": "prelim",
                    "division": None, "elim_size": None, "victor": Round.GOV,
                    "gov": {"debater_ids": [101, 102], "source_names": ["Misspelled Speaker", people[0].name]},
                    "opp": {"debater_ids": [103, 104], "source_names": [person.name for person in people[1:]]},
                    "judges": [{"original_name": "Public Judge", "is_chair": True}]}],
    }
    token = save_review(snapshot, tournament.pk, admin.pk)
    url = reverse("core:tournament_dataentry")
    params = {"tournament": tournament.pk, "api_url": URL, "review": token}
    with patch("requests.get", side_effect=AssertionError("No network during review/save")):
        response = client.get(url, params)
        assert response.status_code == 200
        data = dict(params)
        for prefix, formset in response.context["formsets"].items():
            for field in formset.management_form:
                data[field.html_name] = field.value()
            for form in formset:
                for field in form:
                    value = field.value()
                    if value is not None:
                        data[field.html_name] = "on" if value is True else value
        data["debaters-0-first_name"] = "Corrected"
        saved = client.post(url, data)
        assert saved.status_code == 302
    corrected = Debater.objects.get(first_name="Corrected")
    assert not corrected.temporary
    assert SpeakerResult.objects.filter(tournament=tournament, debater=corrected).exists()
    imported_round = Round.objects.get(tournament=tournament)
    assert corrected in imported_round.gov.debaters.all()
    assert not Debater.all_objects.filter(first_name="Misspelled").exists()
    assert not Debater.all_objects.filter(synthetic=True).exclude(first_name="Public", last_name="Judge").exists()


def test_successful_refresh_replaces_entire_snapshot_and_resets_disappearance():
    initial = payload()
    for key in ("novice_team_placements", "non_placing_teams", "varsity_speaker_awards",
                "novice_speaker_awards", "new_schools", "new_debater_data"):
        initial[key] = []
    store_source(URL, initial)
    index = source_index()
    index[source_key(URL)]["missing_since"] = timezone.now().timestamp()
    cache.set(INDEX_KEY, index)
    updated = deepcopy(initial)
    updated["debater_counts"]["teams"] = 64
    directory = Mock()
    directory.json.return_value = {"tournaments": [{"url": URL}]}
    source = Mock()
    source.json.return_value = updated
    with patch("core.import_sources.requests.get", side_effect=[directory, source]):
        refresh_import_sources()
    assert cached_source(source_key(URL)) == updated
    assert source_index()[source_key(URL)]["missing_since"] is None


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026", ONLINE_SEASONS=())
def test_bulk_rebuild_preserves_standalone_manual_qualification():
    host = School.objects.create(name="Host", short_name="Host")
    person = Debater.objects.create(first_name="Manually", last_name="Qualified", school=host)
    QUAL.objects.create(season="2026", debater=person, qual_type=QUAL.EXPANSION)
    rebuild_import_qualifications("2026")
    assert QUAL.objects.filter(debater=person, qual_type=QUAL.EXPANSION).exists()
    assert COTY.objects.get(school=host).points == 6


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026", ONLINE_SEASONS=())
def test_import_persistence_query_budget_for_32_teams(django_assert_max_num_queries):
    from time import perf_counter
    from types import SimpleNamespace
    from django.test import RequestFactory
    from core.views.results_import_views import RESULT_FORMSET_CONFIGS, TournamentDataEntryView

    host = School.objects.create(name="MIT", short_name="MIT")
    tournament = Tournament.objects.create(host=host, season="2026", date=date(2026, 10, 24), num_teams=32)
    people = Debater.objects.bulk_create([
        Debater(pk=index + 1, first_name="Speaker", last_name=str(index), school=host,
                first_season="2026", latest_season="2026") for index in range(64)
    ])
    view = TournamentDataEntryView()
    view.request = RequestFactory().post("/", {"tournament": tournament.pk})
    formsets = {config.key: SimpleNamespace(cleaned_data=[]) for config in RESULT_FORMSET_CONFIGS}
    formsets["varsity_teams"].cleaned_data = [
        {"debater_one": people[index * 2], "debater_two": people[index * 2 + 1], "ORDER": index + 1}
        for index in range(32)
    ]
    started = perf_counter()
    with django_assert_max_num_queries(1200) as queries:
        view.persist_results(tournament, formsets, use_api=False)
    assert TeamResult.objects.filter(tournament=tournament).count() == 32
    print(f"32-team persistence: {len(queries)} SQL queries in {perf_counter() - started:.2f}s")
