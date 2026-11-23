from datetime import date
import pytest

from core.models import Debater, ImportedRoundJudge, Round, School, Team, TeamResult, Tournament, TournamentImport
from core.utils.mittab_bundle_import import (
    MittabBundleImportError,
    import_mittab_bundle,
)


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


def test_import_creates_synthetic_debaters_and_alias_backed_judges():
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
    assert {"Gov Two", "Opp One", "Opp Two", "Judge Prime"} <= synthetic_names

    imported_judge = ImportedRoundJudge.objects.get(round_metadata=created_round.imported_metadata)
    assert imported_judge.original_name == "Judge Prime"
    assert imported_judge.debater_alias is not None
    assert imported_judge.debater_alias.debater.synthetic is True
    assert imported_judge.debater_alias.debater.temporary is False


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


@pytest.mark.parametrize("has_valid_round", [True, False])
def test_import_preserves_existing_round_with_incomplete_roster(has_valid_round):
    tournament = _make_tournament()
    debaters = [
        {"id": person_id, "name": name}
        for person_id, name in [(101, "Gov One"), (102, "Gov Two"), (201, "Opp One"), (202, "Opp Two")]
    ]
    original = _round_payload(import_key="prelim:27")
    valid = _round_payload()
    import_mittab_bundle(
        _make_loaded_bundle(_bundle_document(debaters=debaters, rounds=[original, valid])),
        tournament,
    )
    broken = {
        **original, "gov": {"debater_ids": [], "source_names": []},
        "victor": Round.OPP,
    }
    rounds = [broken, valid] if has_valid_round else [broken]

    summary = import_mittab_bundle(
        _make_loaded_bundle(_bundle_document(debaters=debaters, rounds=rounds)),
        tournament,
    )

    assert summary["rounds_updated"] == int(has_valid_round)
    assert summary["rounds_deleted"] == 0
    assert summary["skipped_rounds"] == [
        "Round 'prelim:27' side 'gov' needs 1 or 2 debater IDs, but the export lists 0."
    ]
    assert tournament.rounds.get(import_key="prelim:27").victor == Round.GOV
    assert tournament.rounds.filter(import_key="prelim:1").exists()


@pytest.mark.parametrize("side", ["gov", "opp"])
def test_reimport_updates_a_round_to_a_solo_side_and_reuses_the_team(side):
    tournament = _make_tournament()
    debaters = []
    for person_id, first_name, last_name in (
        (101, "Gov", "One"), (102, "Gov", "Two"), (201, "Opp", "One"), (202, "Opp", "Two"),
    ):
        debater = Debater.objects.create(first_name=first_name, last_name=last_name)
        debaters.append({"id": person_id, "name": debater.name, "apda_id": debater.pk})
    round_payload = _round_payload()
    document = _bundle_document(debaters=debaters, rounds=[round_payload])
    import_mittab_bundle(_make_loaded_bundle(document), tournament)
    original_round = tournament.rounds.get()
    original_team = getattr(original_round, side)

    round_payload[side]["debater_ids"] = round_payload[side]["debater_ids"][:1]
    round_payload[side]["source_names"] = round_payload[side]["source_names"][:1]
    summary = import_mittab_bundle(_make_loaded_bundle(document), tournament)

    original_round.refresh_from_db()
    solo_team = getattr(original_round, side)
    assert summary["rounds_updated"] == 1
    assert summary["skipped_rounds"] == []
    assert solo_team.pk != original_team.pk
    assert original_team.debaters.count() == 2
    assert set(solo_team.debaters.values_list("first_name", "last_name")) == {
        (side.title(), "One")
    }
    metadata = original_round.imported_metadata
    assert getattr(metadata, f"{side}_1_alias").debater in solo_team.debaters.all()
    assert getattr(metadata, f"{side}_2_alias_id") is None

    round_payload["label"] = "Round 1 Revised"
    import_mittab_bundle(_make_loaded_bundle(document), tournament)
    original_round.refresh_from_db()
    assert getattr(original_round, f"{side}_id") == solo_team.pk


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
