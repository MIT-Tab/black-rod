from datetime import date

import pytest

from core.models import (
    Debater,
    ImportedRoundMetadata,
    Round,
    RoundStats,
    School,
    SyntheticResolutionLog,
    Team,
    Tournament,
    TournamentImport,
)
from core.utils.round_amendments import apply_round_amendments


def _create_tournament(name):
    school = School.objects.create(name=f"{name} Host", short_name=f"{name[:8]}H")
    return Tournament.objects.create(
        host=school,
        date=date(2024, 2, 10),
        season="2024",
        manual_name=name,
        num_rounds=5,
    )


def _create_debater(school, first_name, last_name):
    return Debater.objects.create(
        first_name=first_name,
        last_name=last_name,
        school=school,
    )


def _create_round_fixture(tournament, label="Existing Round", import_key="existing-round"):
    gov = Team.objects.create(name=f"{label} Gov", short_name=f"{label} Gov")
    opp = Team.objects.create(name=f"{label} Opp", short_name=f"{label} Opp")
    gov_one = _create_debater(tournament.host, "Gov", "One")
    gov_two = _create_debater(tournament.host, "Gov", "Two")
    opp_one = _create_debater(tournament.host, "Opp", "One")
    opp_two = _create_debater(tournament.host, "Opp", "Two")
    gov.debaters.add(gov_one, gov_two)
    opp.debaters.add(opp_one, opp_two)
    round_obj = Round.objects.create(
        tournament=tournament,
        gov=gov,
        opp=opp,
        round_number=1,
        round_label=label,
        stage=Round.Stage.PRELIM,
        import_origin="file_backup",
        import_key=import_key,
        victor=Round.GOV,
    )
    return round_obj, (gov_one, gov_two, opp_one, opp_two)


@pytest.mark.django_db
def test_round_amendments_create_update_and_delete_round_with_related_data():
    tournament = _create_tournament("Round Amendment Open")
    school = tournament.host
    gov_one = _create_debater(school, "Alice", "Gov")
    gov_two = _create_debater(school, "Blair", "Gov")
    opp_one = _create_debater(school, "Casey", "Opp")
    opp_two = _create_debater(school, "Drew", "Opp")
    import_row = TournamentImport.objects.create(
        tournament=tournament,
        import_type=TournamentImport.ImportType.FILE_BACKUP,
        original_file_name="rounds.json",
        source_hash="rounds-hash",
    )

    create_summary = apply_round_amendments(
        {
            "rounds": {
                "create": [
                    {
                        "tournament_id": tournament.id,
                        "gov_debater_ids": [gov_one.id, gov_two.id],
                        "opp_debater_ids": [opp_one.id, opp_two.id],
                        "round_number": 4,
                        "stage": Round.Stage.PRELIM,
                        "division": Round.Division.VARSITY,
                        "round_label": "Prelim 4",
                        "victor": Round.GOV,
                        "is_rated": True,
                        "weight": 1.25,
                        "import_origin": "file_backup",
                        "import_key": "round-amendment-1",
                        "metadata": {"source_round_name": "R4"},
                        "stats": [
                            {"debater_id": gov_one.id, "debater_role": "PM", "speaks": "28.5", "ranks": "1"},
                            {"debater_id": gov_two.id, "debater_role": "MG", "speaks": "28.0", "ranks": "2"},
                            {"debater_id": opp_one.id, "debater_role": "LO", "speaks": "27.5", "ranks": "3"},
                            {"debater_id": opp_two.id, "debater_role": "MO", "speaks": "27.0", "ranks": "4"},
                        ],
                        "imported_metadata": {
                            "gov_1": {"debater_id": gov_one.id, "source_name": "Alice G", "role": "PM"},
                            "gov_2": {"debater_id": gov_two.id, "source_name": "Blair G", "role": "MG"},
                            "opp_1": {"debater_id": opp_one.id, "source_name": "Casey O", "role": "LO"},
                            "opp_2": {"debater_id": opp_two.id, "source_name": "Drew O", "role": "MO"},
                            "raw_result_code": "3-0",
                            "source_import_ids": [import_row.id],
                            "judges": [
                                {"original_name": "Chair Judge", "is_chair": True},
                                {"original_name": "Wing Judge", "is_chair": False},
                            ],
                        },
                    }
                ]
            }
        }
    )

    assert create_summary["rounds_created"] == 1
    round_obj = Round.objects.get(tournament=tournament, import_key="round-amendment-1")
    assert round_obj.round_label == "Prelim 4"
    assert round_obj.stats.count() == 4
    assert round_obj.imported_metadata.raw_result_code == "3-0"
    assert round_obj.imported_metadata.sources.count() == 1
    assert round_obj.imported_metadata.judges.count() == 2

    update_summary = apply_round_amendments(
        {
            "actions": [
                {
                    "type": "update_round",
                    "tournament_id": tournament.id,
                    "import_key": "round-amendment-1",
                    "round_label": "Prelim 4 Revised",
                    "weight": 1.5,
                    "stats": [
                        {"debater_id": gov_one.id, "debater_role": "PM", "speaks": "29.0", "ranks": "1"},
                        {"debater_id": gov_two.id, "debater_role": "MG", "speaks": "28.0", "ranks": "2"},
                        {"debater_id": opp_one.id, "debater_role": "LO", "speaks": "27.5", "ranks": "3"},
                        {"debater_id": opp_two.id, "debater_role": "MO", "speaks": "27.0", "ranks": "4"},
                    ],
                    "imported_metadata": {
                        "raw_outcome_text": "Government won comfortably.",
                        "judges": [{"original_name": "Replacement Chair", "is_chair": True}],
                    },
                }
            ]
        }
    )

    assert update_summary["rounds_updated"] == 1
    round_obj.refresh_from_db()
    assert round_obj.round_label == "Prelim 4 Revised"
    assert float(round_obj.weight) == 1.5
    pm_stat = round_obj.stats.get(debater=gov_one)
    assert float(pm_stat.speaks) == 29.0
    assert round_obj.imported_metadata.raw_result_code == "3-0"
    assert round_obj.imported_metadata.raw_outcome_text == "Government won comfortably."
    judges = list(round_obj.imported_metadata.judges.order_by("id"))
    assert len(judges) == 1
    assert judges[0].original_name == "Replacement Chair"

    delete_summary = apply_round_amendments(
        {
            "rounds": {
                "delete": [
                    {
                        "tournament_id": tournament.id,
                        "import_key": "round-amendment-1",
                    }
                ]
            }
        }
    )

    assert delete_summary["rounds_deleted"] == 1
    assert not Round.objects.filter(pk=round_obj.pk).exists()
    assert not ImportedRoundMetadata.objects.filter(round_id=round_obj.pk).exists()
    assert not RoundStats.objects.filter(round_id=round_obj.pk).exists()


@pytest.mark.django_db
def test_round_amendments_clear_outround_stat_fields():
    tournament = _create_tournament("Outround Amendment Open")
    school = tournament.host
    gov_one = _create_debater(school, "Alice", "Gov")
    gov_two = _create_debater(school, "Blair", "Gov")
    opp_one = _create_debater(school, "Casey", "Opp")
    opp_two = _create_debater(school, "Drew", "Opp")

    summary = apply_round_amendments(
        {
            "rounds": {
                "create": [
                    {
                        "tournament_id": tournament.id,
                        "gov_debater_ids": [gov_one.id, gov_two.id],
                        "opp_debater_ids": [opp_one.id, opp_two.id],
                        "round_number": 6,
                        "stage": Round.Stage.OUTROUND,
                        "elim_size": 8,
                        "round_label": "Quarterfinal",
                        "victor": Round.GOV,
                        "import_key": "outround-amendment-1",
                        "stats": [
                            {"debater_id": gov_one.id, "debater_role": "PM", "speaks": "28.5", "ranks": "1"},
                            {"debater_id": gov_two.id, "debater_role": "MG", "speaks": "28.0", "ranks": "2"},
                            {"debater_id": opp_one.id, "debater_role": "LO", "speaks": "27.5", "ranks": "3"},
                            {"debater_id": opp_two.id, "debater_role": "MO", "speaks": "27.0", "ranks": "4"},
                        ],
                    }
                ]
            }
        }
    )

    assert summary["rounds_created"] == 1
    round_obj = Round.objects.get(tournament=tournament, import_key="outround-amendment-1")
    assert round_obj.stage == Round.Stage.OUTROUND

    for stat in round_obj.stats.order_by("id"):
        assert stat.debater_role is None
        assert stat.speaks is None
        assert stat.ranks is None


@pytest.mark.django_db
def test_round_amendments_delete_tournament_import_removes_linked_rounds():
    tournament = _create_tournament("Delete Import Open")
    round_obj, (gov_one, _, _, _) = _create_round_fixture(tournament, import_key="delete-import-round")
    import_row = TournamentImport.objects.create(
        tournament=tournament,
        import_type=TournamentImport.ImportType.FILE_BACKUP,
        original_file_name="delete.json",
    )
    metadata = ImportedRoundMetadata.objects.create(round=round_obj)
    metadata.sources.add(import_row)
    RoundStats.objects.create(
        round=round_obj,
        debater=gov_one,
        debater_role="PM",
        speaks="28.1",
        ranks="1",
    )

    summary = apply_round_amendments(
        {
            "tournament_imports": {
                "delete": [{"id": import_row.id}]
            }
        }
    )

    assert summary["tournament_imports_deleted"] == 1
    assert summary["rounds_deleted"] == 1
    assert not TournamentImport.objects.filter(pk=import_row.pk).exists()
    assert not Round.objects.filter(pk=round_obj.pk).exists()
    assert not ImportedRoundMetadata.objects.filter(round_id=round_obj.pk).exists()
    assert not RoundStats.objects.filter(round_id=round_obj.pk).exists()


@pytest.mark.django_db
def test_round_amendments_move_import_moves_linked_rounds_and_sources():
    source_tournament = _create_tournament("Source Tournament")
    target_tournament = _create_tournament("Target Tournament")
    round_obj, _ = _create_round_fixture(source_tournament, import_key="move-import-round")
    primary_import = TournamentImport.objects.create(
        tournament=source_tournament,
        import_type=TournamentImport.ImportType.FILE_BACKUP,
        original_file_name="primary.json",
        source_hash="primary-hash",
    )
    secondary_import = TournamentImport.objects.create(
        tournament=source_tournament,
        import_type=TournamentImport.ImportType.FORUM_POST,
        original_file_name="secondary.json",
        source_hash="secondary-hash",
    )
    metadata = ImportedRoundMetadata.objects.create(round=round_obj)
    metadata.sources.add(primary_import, secondary_import)

    summary = apply_round_amendments(
        {
            "tournament_imports": {
                "move": [
                    {
                        "id": primary_import.id,
                        "target_tournament_id": target_tournament.id,
                    }
                ]
            }
        }
    )

    assert summary["tournament_imports_moved"] == 1
    assert summary["linked_source_imports_moved"] == 2
    assert summary["rounds_moved"] == 1
    round_obj.refresh_from_db()
    primary_import.refresh_from_db()
    secondary_import.refresh_from_db()
    assert round_obj.tournament == target_tournament
    assert primary_import.tournament == target_tournament
    assert secondary_import.tournament == target_tournament


@pytest.mark.django_db
def test_round_amendments_apply_synthetic_resolution():
    tournament = _create_tournament("Synthetic Amendment Open")
    canonical = _create_debater(tournament.host, "Canonical", "Debater")
    synthetic = Debater.all_objects.create(
        first_name="Synthetic",
        last_name="Debater",
        school=tournament.host,
        temporary=True,
        synthetic=True,
    )

    summary = apply_round_amendments(
        {
            "synthetic_resolutions": [
                {
                    "entity_type": "debater",
                    "synthetic_id": synthetic.id,
                    "target_id": canonical.id,
                    "reason": "bulk amendment",
                }
            ]
        }
    )

    assert summary["synthetic_resolutions"] == 1
    assert not Debater.all_objects.filter(pk=synthetic.pk).exists()
    assert SyntheticResolutionLog.objects.filter(
        entity_type=SyntheticResolutionLog.EntityType.DEBATER,
        synthetic_id=synthetic.id,
        resolved_to_id=canonical.id,
    ).exists()


@pytest.mark.django_db
def test_round_amendments_update_round_can_create_synthetic_alias_and_slot_ref_stats():
    tournament = _create_tournament("Synthetic Alias Round Open")
    round_obj, (gov_one, gov_two, opp_one, opp_two) = _create_round_fixture(
        tournament,
        import_key="synthetic-alias-round",
    )
    ImportedRoundMetadata.objects.create(round=round_obj)

    summary = apply_round_amendments(
        {
            "actions": [
                {
                    "type": "update_round",
                    "round_id": round_obj.id,
                    "imported_metadata": {
                        "gov_1": {"debater_id": gov_one.id, "source_name": gov_one.name, "role": "PM"},
                        "gov_2": {
                            "source_name": "Elaine Zhang",
                            "create_synthetic": True,
                            "role": "MG",
                        },
                        "opp_1": {"debater_id": opp_one.id, "source_name": opp_one.name, "role": "LO"},
                        "opp_2": {"debater_id": opp_two.id, "source_name": opp_two.name, "role": "MO"},
                    },
                    "stats": [
                        {
                            "debater_id": gov_one.id,
                            "debater_role": "PM",
                            "speaks": "29.0",
                            "ranks": "1",
                        },
                        {
                            "slot_ref": "gov_2",
                            "debater_role": "MG",
                            "speaks": "28.0",
                            "ranks": "2",
                            "metadata": {"source": "tab_card", "speaker_name": "Elaine Zhang"},
                        },
                        {
                            "debater_id": opp_one.id,
                            "debater_role": "LO",
                            "speaks": "27.0",
                            "ranks": "3",
                        },
                        {
                            "debater_id": opp_two.id,
                            "debater_role": "MO",
                            "speaks": "26.0",
                            "ranks": "4",
                        },
                    ],
                }
            ]
        }
    )

    assert summary["rounds_updated"] == 1
    round_obj.refresh_from_db()
    metadata = round_obj.imported_metadata
    synthetic_alias = metadata.gov_2_alias
    assert synthetic_alias is not None
    assert synthetic_alias.source_name == "Elaine Zhang"
    assert synthetic_alias.debater.synthetic is True
    assert synthetic_alias.debater.temporary is False
    assert synthetic_alias.debater.first_name == "Elaine"
    assert synthetic_alias.debater.last_name == "Zhang"
    gov_team_member_ids = sorted(
        round_obj.gov.debaters.through.objects.filter(team_id=round_obj.gov_id).values_list(
            "debater_id",
            flat=True,
        )
    )
    assert gov_team_member_ids == sorted([gov_one.id, synthetic_alias.debater_id])
    synthetic_stat = round_obj.stats.get(debater=synthetic_alias.debater)
    assert synthetic_stat.debater_role == "MG"
    assert float(synthetic_stat.speaks) == 28.0
