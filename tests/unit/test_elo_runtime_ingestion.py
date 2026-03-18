"""Runtime ingestion/rating regression tests for one-speaker teams and linked debater collapse behavior."""

from datetime import date

import pytest
from django.db import connection
from django.db.models import Prefetch
from django.test.utils import CaptureQueriesContext

from core.models import (
    Debater,
    DebaterAlias,
    DebaterAliasGroup,
    ImportedRoundMetadata,
    Round,
    RoundStats,
    School,
    Team,
    Tournament,
    TournamentImport,
)
from core.utils.elo_runtime_engine.constants import PARTNER_MODE
from core.utils.elo_runtime_engine.debate_contract import debate_source_fields
from core.utils.elo_runtime_engine.ingestion import build_ingested_snapshots_and_debates
from core.utils.elo_runtime_engine.rating import apply_elo


def _team(*debaters):
    team = Team.objects.create(name="Team")
    team.debaters.add(*debaters)
    return team


def _standard_runtime_fixture(*, school_name="Runtime U", tournament_name="Runtime Invitational"):
    school = School.objects.create(name=school_name, short_name="RU")
    gov_one = Debater.objects.create(first_name="Gov", last_name="One", school=school)
    gov_two = Debater.objects.create(first_name="Gov", last_name="Two", school=school)
    opp_one = Debater.objects.create(first_name="Opp", last_name="One", school=school)
    opp_two = Debater.objects.create(first_name="Opp", last_name="Two", school=school)
    return {
        "school": school,
        "gov_one": gov_one,
        "gov_two": gov_two,
        "opp_one": opp_one,
        "opp_two": opp_two,
        "gov_team": _team(gov_one, gov_two),
        "opp_team": _team(opp_one, opp_two),
        "tournament": Tournament.objects.create(
            name=tournament_name,
            host=school,
            season="2025",
            date=date(2025, 1, 12),
            num_teams=16,
            num_rounds=5,
        ),
    }


def _create_round_with_stats(
    *,
    tournament,
    gov_team,
    opp_team,
    round_number,
    victor,
    import_origin="",
    stage=Round.Stage.PRELIM,
):
    gov_members = list(gov_team.debaters.order_by("id"))
    opp_members = list(opp_team.debaters.order_by("id"))
    round_obj = Round.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=round_number,
        round_label=f"P{round_number}",
        stage=stage,
        victor=victor,
        import_origin=import_origin,
    )
    for debater, speaks, ranks, role in [
        (gov_members[0], 28, 1, "PM"),
        (gov_members[1], 27, 2, "MG"),
        (opp_members[0], 26, 3, "LO"),
        (opp_members[1], 25, 4, "MO"),
    ]:
        RoundStats.objects.create(
            round=round_obj,
            debater=debater,
            speaks=speaks,
            ranks=ranks,
            debater_role=role,
        )
    return round_obj


@pytest.mark.django_db
def test_ingestion_collapses_side_to_single_speaker_when_only_one_has_speaks():
    fixture = _standard_runtime_fixture()
    round_obj = Round.objects.create(
        tournament=fixture["tournament"],
        gov=fixture["gov_team"],
        opp=fixture["opp_team"],
        round_number=1,
        victor=Round.GOV,
    )
    RoundStats.objects.create(round=round_obj, debater=fixture["gov_one"], speaks=28, ranks=1, debater_role="PM")
    RoundStats.objects.create(round=round_obj, debater=fixture["gov_two"], speaks=None, ranks=None, debater_role="MG")
    RoundStats.objects.create(round=round_obj, debater=fixture["opp_one"], speaks=27, ranks=2, debater_role="LO")
    RoundStats.objects.create(round=round_obj, debater=fixture["opp_two"], speaks=26, ranks=3, debater_role="MO")

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert len(debates) == 1
    debate = debates[0]
    assert debate.team_a == (fixture["gov_one"].id,)
    assert set(debate.team_b) == {fixture["opp_one"].id, fixture["opp_two"].id}
    assert fixture["gov_two"].id not in debate.participant_names


@pytest.mark.django_db
def test_debate_source_fields_uses_prefetched_import_sources_without_extra_queries():
    fixture = _standard_runtime_fixture()
    round_obj = _create_round_with_stats(
        tournament=fixture["tournament"],
        gov_team=fixture["gov_team"],
        opp_team=fixture["opp_team"],
        round_number=1,
        victor=Round.GOV,
        import_origin="file_backup",
    )
    imported_metadata = ImportedRoundMetadata.objects.create(round=round_obj)
    source = TournamentImport.objects.create(
        tournament=fixture["tournament"],
        import_type=TournamentImport.ImportType.FILE_BACKUP,
        original_file_name="runtime-fixture.json",
    )
    imported_metadata.sources.add(source)

    prefetched_round = (
        Round.objects.select_related("imported_metadata")
        .prefetch_related(
            Prefetch(
                "imported_metadata__sources",
                queryset=TournamentImport.objects.order_by("id"),
                to_attr="ordered_sources",
            )
        )
        .get(pk=round_obj.pk)
    )

    with CaptureQueriesContext(connection) as ctx:
        source_kind, source_label = debate_source_fields(prefetched_round)

    assert len(ctx) == 0
    assert source_kind == TournamentImport.ImportType.FILE_BACKUP
    assert source_label == "runtime-fixture.json"


@pytest.mark.django_db
def test_partner_mode_updates_only_scored_partner_when_teammate_missing_speaks():
    fixture = _standard_runtime_fixture()
    round_obj = Round.objects.create(
        tournament=fixture["tournament"],
        gov=fixture["gov_team"],
        opp=fixture["opp_team"],
        round_number=1,
        victor=Round.GOV,
    )
    RoundStats.objects.create(round=round_obj, debater=fixture["gov_one"], speaks=28, ranks=1, debater_role="PM")
    RoundStats.objects.create(round=round_obj, debater=fixture["gov_two"], speaks=None, ranks=None, debater_role="MG")
    RoundStats.objects.create(round=round_obj, debater=fixture["opp_one"], speaks=27, ranks=2, debater_role="LO")
    RoundStats.objects.create(round=round_obj, debater=fixture["opp_two"], speaks=26, ranks=3, debater_role="MO")

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )
    stats, processed = apply_elo(
        debates=debates,
        initial_rating=1500.0,
        k_max=40.0,
        k_min=40.0,
        k_decay_scale=1.0,
        mode=PARTNER_MODE,
        higher_elo_win_share=0.5,
        higher_elo_loss_share=0.5,
        debates_sorted=True,
    )

    assert processed == 1
    assert fixture["gov_two"].id not in stats
    assert stats[fixture["gov_one"].id].rounds == 1
    assert stats[fixture["opp_one"].id].rounds == 1
    assert stats[fixture["opp_two"].id].rounds == 1


@pytest.mark.django_db
def test_ingestion_collapses_linked_debater_ids_before_rating():
    school = School.objects.create(name="Linked Runtime U", short_name="LRU")
    alias_group = DebaterAliasGroup.objects.create(label="Linked Debater")
    andrew_old = Debater.objects.create(
        first_name="Andrew",
        last_name="Monteith",
        school=school,
        alias_group=alias_group,
        first_season="2020",
        latest_season="2023",
    )
    andrew_new = Debater.objects.create(
        first_name="Andrew",
        last_name="Monteith",
        school=school,
        alias_group=alias_group,
        first_season="2024",
        latest_season="2025",
    )
    opp_one = Debater.objects.create(first_name="Opp", last_name="One", school=school)
    opp_two = Debater.objects.create(first_name="Opp", last_name="Two", school=school)

    gov_team = _team(andrew_old, andrew_new)
    opp_team = _team(opp_one, opp_two)
    tournament = Tournament.objects.create(
        name="Linked Runtime Invitational",
        host=school,
        season="2025",
        date=date(2025, 2, 1),
        num_teams=16,
        num_rounds=5,
    )
    round_obj = Round.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=1,
        victor=Round.GOV,
        metadata={
            "team_a_ids": [andrew_old.id, andrew_new.id],
            "team_a_names": ["Andrew Monteith", "Andrew Monteith"],
            "team_b_ids": [opp_one.id, opp_two.id],
            "team_b_names": ["Opp One", "Opp Two"],
        },
    )
    RoundStats.objects.create(
        round=round_obj,
        debater=andrew_new,
        speaks=29,
        ranks=1,
        debater_role="PM",
    )
    RoundStats.objects.create(round=round_obj, debater=opp_one, speaks=27, ranks=2, debater_role="LO")
    RoundStats.objects.create(round=round_obj, debater=opp_two, speaks=26, ranks=3, debater_role="MO")

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert len(debates) == 1
    debate = debates[0]
    assert debate.team_a == (andrew_old.id,)
    assert debate.participant_names[andrew_old.id] == "Andrew Monteith"

    stats, processed = apply_elo(
        debates=debates,
        initial_rating=1500.0,
        k_max=40.0,
        k_min=40.0,
        k_decay_scale=1.0,
        mode=PARTNER_MODE,
        higher_elo_win_share=0.5,
        higher_elo_loss_share=0.5,
        debates_sorted=True,
    )

    assert processed == 1
    assert andrew_old.id in stats
    assert andrew_new.id not in stats
    assert stats[andrew_old.id].rounds == 1


@pytest.mark.django_db
def test_ingestion_uses_imported_alias_names_and_sources():
    fixture = _standard_runtime_fixture(tournament_name="Imported Runtime Invitational")
    round_obj = _create_round_with_stats(
        tournament=fixture["tournament"],
        gov_team=fixture["gov_team"],
        opp_team=fixture["opp_team"],
        round_number=2,
        victor=Round.OPP,
    )
    import_row = TournamentImport.objects.create(
        tournament=fixture["tournament"],
        import_type=TournamentImport.ImportType.FILE_BACKUP,
        original_file_name="runtime-source.json",
    )
    imported_metadata = ImportedRoundMetadata.objects.create(
        round=round_obj,
        gov_1_alias=DebaterAlias.objects.create(
            source_name="Gov Imported One",
            normalized_name="gov imported one",
            debater=fixture["gov_one"],
        ),
        gov_2_alias=DebaterAlias.objects.create(
            source_name="Gov Imported Two",
            normalized_name="gov imported two",
            debater=fixture["gov_two"],
        ),
        opp_1_alias=DebaterAlias.objects.create(
            source_name="Opp Imported One",
            normalized_name="opp imported one",
            debater=fixture["opp_one"],
        ),
        opp_2_alias=DebaterAlias.objects.create(
            source_name="Opp Imported Two",
            normalized_name="opp imported two",
            debater=fixture["opp_two"],
        ),
    )
    imported_metadata.sources.add(import_row)

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert len(debates) == 1
    assert debates[0].participant_names[fixture["gov_one"].id] == "Gov Imported One"
    assert debates[0].participant_names[fixture["opp_two"].id] == "Opp Imported Two"
    assert debates[0].source_kind == TournamentImport.ImportType.FILE_BACKUP
    assert debates[0].source_label == "runtime-source.json"


@pytest.mark.django_db
def test_ingestion_marks_outrounds_from_round_stage():
    fixture = _standard_runtime_fixture(tournament_name="Outround Runtime Invitational")
    _create_round_with_stats(
        tournament=fixture["tournament"],
        gov_team=fixture["gov_team"],
        opp_team=fixture["opp_team"],
        round_number=6,
        victor=Round.OPP,
        stage=Round.Stage.OUTROUND,
    )

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert len(debates) == 1
    assert debates[0].stage == "outround"
    assert debates[0].winner == "b"
    assert debates[0].round_label == "P6"


@pytest.mark.django_db
def test_ingestion_excludes_novice_tournament_by_default():
    fixture = _standard_runtime_fixture(tournament_name="Novice Runtime Invitational")
    fixture["tournament"].qual_type = Tournament.NOVICE
    fixture["tournament"].save(update_fields=["qual_type"])
    _create_round_with_stats(
        tournament=fixture["tournament"],
        gov_team=fixture["gov_team"],
        opp_team=fixture["opp_team"],
        round_number=1,
        victor=Round.GOV,
    )

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert debates == []


@pytest.mark.django_db
def test_ingestion_includes_novice_tournament_when_requested():
    fixture = _standard_runtime_fixture(tournament_name="Novice Runtime Included")
    fixture["tournament"].qual_type = Tournament.NOVICE
    fixture["tournament"].save(update_fields=["qual_type"])
    _create_round_with_stats(
        tournament=fixture["tournament"],
        gov_team=fixture["gov_team"],
        opp_team=fixture["opp_team"],
        round_number=1,
        victor=Round.GOV,
    )

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=True,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert len(debates) == 1


@pytest.mark.django_db
def test_ingestion_marks_proam_partnerships_from_first_year_status_across_linked_profiles():
    school = School.objects.create(name="Runtime U", short_name="RU")
    tournament = Tournament.objects.create(
        name="Runtime Invitational",
        host=school,
        season="2025",
        date=date(2025, 2, 15),
        num_teams=16,
        num_rounds=5,
    )

    novice_group = DebaterAliasGroup.objects.create(label="Linked Novice")
    novice_old = Debater.objects.create(
        first_name="Novice",
        last_name="Old",
        school=school,
        alias_group=novice_group,
        first_season="2025",
        latest_season="2025",
    )
    novice_current = Debater.objects.create(
        first_name="Novice",
        last_name="Current",
        school=school,
        alias_group=novice_group,
        first_season="2025",
        latest_season="2025",
    )
    veteran_group = DebaterAliasGroup.objects.create(label="Linked Veteran")
    veteran_old = Debater.objects.create(
        first_name="Veteran",
        last_name="Old",
        school=school,
        alias_group=veteran_group,
        first_season="2024",
        latest_season="2024",
    )
    veteran_current = Debater.objects.create(
        first_name="Veteran",
        last_name="Current",
        school=school,
        alias_group=veteran_group,
        first_season="2025",
        latest_season="2025",
    )
    opp_one = Debater.objects.create(
        first_name="Opp",
        last_name="One",
        school=school,
        first_season="2025",
        latest_season="2025",
    )
    opp_two = Debater.objects.create(
        first_name="Opp",
        last_name="Two",
        school=school,
        first_season="2024",
        latest_season="2025",
    )

    gov_team = _team(novice_current, veteran_current)
    opp_team = _team(opp_one, opp_two)
    round_obj = Round.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=1,
        round_label="P1",
        victor=Round.GOV,
        metadata={
            "team_a_ids": [novice_current.id, veteran_current.id],
            "team_a_names": ["Novice Current", "Veteran Current"],
            "team_b_ids": [opp_one.id, opp_two.id],
            "team_b_names": ["Opp One", "Opp Two"],
        },
    )
    for debater, speaks, ranks, role in [
        (novice_current, 28, 1, "PM"),
        (veteran_current, 27, 2, "MG"),
        (opp_one, 26, 3, "LO"),
        (opp_two, 25, 4, "MO"),
    ]:
        RoundStats.objects.create(
            round=round_obj,
            debater=debater,
            speaks=speaks,
            ranks=ranks,
            debater_role=role,
        )

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert len(debates) == 1
    assert debates[0].is_proam_partnership is True


@pytest.mark.django_db
def test_ingestion_skips_bye_rounds_even_when_marked_rated():
    fixture = _standard_runtime_fixture(tournament_name="Bye Runtime Invitational")
    round_obj = Round.objects.create(
        tournament=fixture["tournament"],
        gov=fixture["gov_team"],
        opp=fixture["opp_team"],
        round_number=1,
        victor=Round.BYE,
    )
    RoundStats.objects.create(round=round_obj, debater=fixture["gov_one"], speaks=28, ranks=1, debater_role="PM")

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert debates == []


@pytest.mark.django_db
def test_ingestion_skips_non_debated_administrative_advances():
    fixture = _standard_runtime_fixture(tournament_name="Forfeit Runtime Invitational")
    _create_round_with_stats(
        tournament=fixture["tournament"],
        gov_team=fixture["gov_team"],
        opp_team=fixture["opp_team"],
        round_number=1,
        victor=Round.GOV_VIA_FORFEIT,
    )

    _snapshots, debates = build_ingested_snapshots_and_debates(
        allowed_seasons={"2025"},
        include_novice=False,
        include_proam=False,
        completed_only=False,
        max_date=None,
    )

    assert debates == []
