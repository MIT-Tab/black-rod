from datetime import date
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.urls import reverse

import pytest

from core.models import (
    Debater, DebaterAlias, ImportedRoundJudge, ImportedRoundMetadata, Judge,
    Round, School, Team, TeamResult, Tournament, TournamentImport,
)
from core.utils.mittab_bundle_import import (
    MittabBundleImportError,
    import_mittab_bundle,
)
from core.utils.round_amendments import apply_round_amendments


pytestmark = pytest.mark.django_db


def _make_loaded_bundle(document, name="bundle.json"):
    import hashlib
    import json

    raw_bytes = json.dumps(document).encode("utf-8")
    return type(
        "LoadedBundle",
        (),
        {
            "document": document,
            "original_file_name": name,
            "source_hash": hashlib.sha256(raw_bytes).hexdigest(),
        },
    )()


def _make_tournament(name="Bundle Open", with_results=True):
    host = School.objects.create(name=f"{name} Host", short_name=f"{name[:8]}H")
    tournament = Tournament.objects.create(
        name=name,
        manual_name=name,
        host=host,
        date=date(2024, 2, 1),
        season="2024",
    )
    if with_results:
        result_team = Team.objects.create(name="Result Team", short_name="RT")
        result_team.debaters.set(
            [
                Debater.objects.create(first_name="Result", last_name="One", school=host),
                Debater.objects.create(first_name="Result", last_name="Two", school=host),
            ]
        )
        TeamResult.objects.create(tournament=tournament, team=result_team, place=1)
    return tournament


def _bundle_document(*, debaters, rounds, schools=None):
    return {
        "schema_version": 1,
        "source": "mit_tab_black_rod_bundle",
        "exported_at": "2026-03-19T00:00:00+00:00",
        "tournament_name": "Mit Tab Open",
        "schools": schools
        or [
            {"id": 1, "apda_id": None, "name": "Fallback School"},
        ],
        "debaters": debaters,
        "rounds": rounds,
    }


def _round_payload(import_key="prelim:1", label="Round 1", judges=None):
    return {
        "import_key": import_key,
        "round_number": 1,
        "label": label,
        "stage": "prelim",
        "division": None,
        "elim_size": None,
        "victor": Round.GOV,
        "gov": {
            "debater_ids": [101, 102],
            "source_names": ["Gov One", "Gov Two"],
        },
        "opp": {
            "debater_ids": [201, 202],
            "source_names": ["Opp One", "Opp Two"],
        },
        "judges": judges or [{"original_name": "Judge Prime", "is_chair": True}],
    }


def test_import_creates_synthetic_competitors_and_separate_judges():
    tournament = _make_tournament()
    school = School.objects.create(name="Existing School", short_name="ES")
    canonical = Debater.objects.create(first_name="Gov", last_name="One", school=school)

    document = _bundle_document(
        schools=[{"id": 1, "apda_id": school.id, "name": "Existing School"}],
        debaters=[
            {"id": 101, "apda_id": canonical.id, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[_round_payload()],
    )

    summary = import_mittab_bundle(_make_loaded_bundle(document), tournament)

    assert summary["rounds_created"] == 1
    created_round = Round.objects.get(tournament=tournament, import_key="prelim:1")
    synthetic_names = {
        debater.name for debater in Debater.all_objects.filter(synthetic=True)
    }
    assert synthetic_names == {"Gov Two", "Opp One", "Opp Two"}

    imported_judge = ImportedRoundJudge.objects.get(round_metadata=created_round.imported_metadata)
    assert imported_judge.original_name == "Judge Prime"
    assert imported_judge.debater_alias is None
    assert imported_judge.judge.name == "Judge Prime"
    assert imported_judge.judge.source_tournament == tournament
    assert imported_judge.is_chair


def test_import_reuses_unique_exact_name_match():
    tournament = _make_tournament()
    school = School.objects.create(name="Fallback School", short_name="FS")
    matched = Debater.objects.create(first_name="Gov", last_name="Two", school=school)

    document = _bundle_document(
        schools=[{"id": 1, "apda_id": school.id, "name": "Fallback School"}],
        debaters=[
            {"id": 101, "apda_id": None, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": matched.name, "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[_round_payload()],
    )

    import_mittab_bundle(_make_loaded_bundle(document), tournament)

    created_round = Round.objects.get(tournament=tournament, import_key="prelim:1")
    gov_debaters = list(created_round.gov.debaters.order_by("id"))
    assert matched in gov_debaters


def test_import_prefers_competitor_with_existing_tournament_result():
    tournament = _make_tournament()
    school = School.objects.create(name="Fallback School", short_name="FS")
    matched = Debater.objects.create(first_name="Gov", last_name="One", school=school)
    other = Debater.objects.create(first_name="Gov", last_name="One", school=None)
    result_team = Team.objects.create(name="Existing Tournament Team", short_name="ETT")
    partner = Debater.objects.create(first_name="Partner", last_name="Prime", school=school)
    result_team.debaters.set([matched, partner])
    TeamResult.objects.create(tournament=tournament, team=result_team, place=-1)

    document = _bundle_document(
        schools=[{"id": 1, "apda_id": school.id, "name": "Fallback School"}],
        debaters=[
            {"id": 101, "apda_id": None, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[_round_payload()],
    )

    import_mittab_bundle(_make_loaded_bundle(document), tournament)

    created_round = Round.objects.get(tournament=tournament, import_key="prelim:1")
    gov_debaters = list(created_round.gov.debaters.order_by("id"))
    assert matched in gov_debaters
    assert other not in gov_debaters


def test_import_filters_out_synthetic_competitor_matches():
    tournament = _make_tournament()
    school = School.objects.create(name="Fallback School", short_name="FS")
    synthetic = Debater.all_objects.create(
        first_name="Gov",
        last_name="One",
        school=school,
        synthetic=True,
        temporary=False,
    )
    matched = Debater.objects.create(first_name="Gov", last_name="One", school=school)

    document = _bundle_document(
        schools=[{"id": 1, "apda_id": school.id, "name": "Fallback School"}],
        debaters=[
            {"id": 101, "apda_id": None, "name": matched.name, "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[_round_payload()],
    )

    import_mittab_bundle(_make_loaded_bundle(document), tournament)

    created_round = Round.objects.get(tournament=tournament, import_key="prelim:1")
    gov_debaters = list(created_round.gov.debaters.order_by("id"))
    assert matched in gov_debaters
    assert synthetic not in gov_debaters


def test_import_resolves_minus_one_exact_name_match_by_existing_tournament_result():
    tournament = _make_tournament()
    host = tournament.host
    other_school = School.objects.create(name="Other School", short_name="OS")
    matched = Debater.objects.create(first_name="Gov", last_name="One", school=host)
    Debater.objects.create(first_name="Gov", last_name="One", school=other_school)
    result_team = Team.objects.create(name="Existing Tournament Team", short_name="ETT")
    partner = Debater.objects.create(first_name="Partner", last_name="Prime", school=host)
    result_team.debaters.set([matched, partner])
    TeamResult.objects.create(tournament=tournament, team=result_team, place=-1)

    document = _bundle_document(
        schools=[{"id": 1, "apda_id": None, "name": "Unmatched School"}],
        debaters=[
            {"id": -1, "apda_id": None, "name": matched.name, "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[
            _round_payload().copy()
            | {
                "gov": {
                    "debater_ids": [-1, 102],
                    "source_names": [matched.name, "Gov Two"],
                }
            }
        ],
    )

    import_mittab_bundle(_make_loaded_bundle(document), tournament)

    created_round = Round.objects.get(tournament=tournament, import_key="prelim:1")
    gov_debaters = list(created_round.gov.debaters.order_by("id"))
    assert matched in gov_debaters


def test_reupload_replaces_prior_bundle_rounds_missing_from_new_file():
    tournament = _make_tournament()

    schools = [{"id": 1, "apda_id": None, "name": "Fallback School"}]
    debaters = [
        {"id": 101, "apda_id": None, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
        {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
        {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
        {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
    ]
    first_document = _bundle_document(
        schools=schools,
        debaters=debaters,
        rounds=[
            _round_payload(import_key="prelim:1", label="Round 1"),
            _round_payload(import_key="prelim:2", label="Round 2"),
        ],
    )
    second_document = _bundle_document(
        schools=schools,
        debaters=debaters,
        rounds=[_round_payload(import_key="prelim:1", label="Round 1 Revised")],
    )

    import_mittab_bundle(_make_loaded_bundle(first_document, name="first.json"), tournament)
    summary = import_mittab_bundle(_make_loaded_bundle(second_document, name="second.json"), tournament)

    assert summary["rounds_updated"] == 1
    assert summary["rounds_deleted"] == 1
    assert Round.objects.filter(tournament=tournament, import_key="prelim:1").exists()
    assert not Round.objects.filter(tournament=tournament, import_key="prelim:2").exists()
    assert TournamentImport.objects.filter(
        tournament=tournament,
        import_type=TournamentImport.ImportType.MITTAB_BUNDLE,
    ).count() == 2


def test_duplicate_bundle_hash_is_rejected():
    tournament = _make_tournament()
    document = _bundle_document(
        debaters=[
            {"id": 101, "apda_id": None, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[_round_payload()],
    )
    loaded = _make_loaded_bundle(document)

    import_mittab_bundle(loaded, tournament)
    with pytest.raises(MittabBundleImportError) as excinfo:
        import_mittab_bundle(loaded, tournament)

    assert "already uploaded" in str(excinfo.value)


def test_import_requires_existing_results():
    tournament = _make_tournament(with_results=False)
    document = _bundle_document(
        debaters=[
            {"id": 101, "apda_id": None, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        rounds=[_round_payload()],
    )

    with pytest.raises(MittabBundleImportError) as excinfo:
        import_mittab_bundle(_make_loaded_bundle(document), tournament)

    assert "Results must be imported" in str(excinfo.value)


@pytest.fixture
def card_bundle():
    debaters = [
        {"id": ref, "name": name} for ref, name in
        ((101, "Gov One"), (102, "Gov Two"), (201, "Opp One"), (202, "Opp Two"))
    ]
    cards = [{
        "speakers": [{"name": "Gov One", "status": "V"}, {"name": "Gov Two", "status": "N"}],
        "rounds": [
            {
                "side": "G", "result": "WF", "opponents": ["Opp One", "Opp Two"],
                "judges": "Judge Prime", "scores": [[28.5, 1.0], [27.0, 2.0], [55.5, 3.0]],
            },
            {"side": "BYE", "result": "", "opponents": [], "judges": "", "scores": [None, None, [55.5, 3.0]]},
        ],
        "totals": [[28.5, 1.0], [27.0, 2.0], [55.5, 3.0]],
    }]
    return _bundle_document(debaters=debaters, rounds=[_round_payload()]) | {"tab_cards": cards}


def test_tournament_pages_show_latest_published_tab_cards(client, card_bundle):
    tournament = _make_tournament()
    url = reverse("core:tournament_tab_cards", args=[tournament.pk])
    detail_url = reverse("core:tournament_detail", args=[tournament.pk])
    assert client.get(url).status_code == 404

    import_mittab_bundle(_make_loaded_bundle(card_bundle), tournament)
    assert url in client.get(detail_url).content.decode()
    response = client.get(url)
    assert response.status_code == 200
    html = response.content.decode()
    for expected in ("Gov Two (N)", "Opp One &amp; Opp Two", "Judge Prime", "WF", "BYE", "(55.50,3.00)"):
        assert expected in html
    assert "Result Team" not in html

    import_mittab_bundle(_make_loaded_bundle(card_bundle | {"tab_cards": None}), tournament)
    assert client.get(url).status_code == 404
    assert url not in client.get(detail_url).content.decode()

    latest = tournament.tournament_imports.first()
    apply_round_amendments({"actions": [{"type": "delete_tournament_import", "id": latest.pk}]})
    assert client.get(url).status_code == 404


def test_legacy_bundle_shows_only_recoverable_round_data(client, card_bundle):
    tournament = _make_tournament()
    card_bundle.pop("tab_cards")
    import_mittab_bundle(_make_loaded_bundle(card_bundle), tournament)
    response = client.get(reverse("core:tournament_tab_cards", args=[tournament.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    for expected in ("Opp One &amp; Opp Two", "Judge Prime", "Some details may be missing"):
        assert expected in html
    assert "(0.00,0.00)" not in html


def test_malformed_tab_cards_do_not_replace_an_existing_import(card_bundle):
    tournament = _make_tournament()
    import_mittab_bundle(_make_loaded_bundle(card_bundle), tournament)
    with pytest.raises(MittabBundleImportError, match="invalid tab card"):
        import_mittab_bundle(_make_loaded_bundle(card_bundle | {"tab_cards": [{}]}), tournament)
    assert tournament.tournament_imports.count() == 1
    assert tournament.tournament_imports.get().tab_cards["cards"] == card_bundle["tab_cards"]


def test_ironman_import_and_same_name_judges(card_bundle):
    tournament = _make_tournament()
    speaker = Debater.objects.create(first_name="Gov", last_name="One")
    partner = Debater.objects.create(first_name="Gov", last_name="Two")
    existing_team = Team.objects.create(name="Existing partnership")
    existing_team.debaters.add(speaker, partner)
    judge_rows = [
        {"id": 1, "original_name": "Same Name", "is_chair": True},
        {"id": 2, "original_name": "Same Name", "is_chair": False},
    ]
    round_payload = _round_payload(judges=judge_rows) | {
        "gov": {"debater_ids": [101], "source_names": ["Gov One"]},
    }
    card_bundle["rounds"] = [round_payload, round_payload | {
        "import_key": "prelim:2", "round_number": 2, "judges": judge_rows[1:],
    }]
    import_mittab_bundle(_make_loaded_bundle(card_bundle), tournament)
    round_obj = tournament.rounds.get(import_key="prelim:1")
    assert list(round_obj.gov.debaters.all()) == [speaker]
    assert round_obj.imported_metadata.gov_2_alias is None
    judges = Judge.objects.filter(source_tournament=tournament, name="Same Name")
    assert judges.count() == 2
    assert judges.get(source_key="id:2").round_assignments.count() == 2


def test_judge_migration_removes_only_mittab_judge_only_synthetic_debaters():
    tournament = _make_tournament()
    team = tournament.team_results.first().team
    won = Round.objects.create(tournament=tournament, gov=team, opp=team, import_origin="mittab_bundle")
    metadata = ImportedRoundMetadata.objects.create(round=won)
    names = ("Judge Only", "Judge Competitor", "Canonical Judge", "Unattached Synthetic", "Other Source Judge")
    people = {}
    for name in names:
        first, last = name.split(maxsplit=1)
        people[name] = Debater.all_objects.create(first_name=first, last_name=last, synthetic=name != "Canonical Judge")
    team.debaters.add(people["Judge Competitor"])
    other_round = Round.objects.create(tournament=tournament, gov=team, opp=team, import_origin="forum_post")
    other_metadata = ImportedRoundMetadata.objects.create(round=other_round)
    for name in names:
        alias = DebaterAlias.objects.create(debater=people[name], source_name=name, normalized_name=name.lower())
        if name != "Unattached Synthetic":
            ImportedRoundJudge.objects.create(
                original_name=name, debater_alias=alias,
                round_metadata=other_metadata if name == "Other Source Judge" else metadata,
            )

    migration = import_module("core.migrations.0078_migrate_imported_judges")
    migration.migrate_mittab_judges(apps, SimpleNamespace(connection=connection))

    assert not Debater.all_objects.filter(pk=people["Judge Only"].pk).exists()
    assignment = metadata.judges.get(original_name="Judge Only")
    assert assignment.judge.name == "Judge Only"
    assert assignment.debater_alias is None
    for name in names[1:]:
        assert Debater.all_objects.filter(pk=people[name].pk).exists()
    assert other_metadata.judges.get().judge is None
