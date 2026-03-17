from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from core.models import (
    Debater,
    DebaterAlias,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    Round,
    School,
    Team,
    Tournament,
)


def _create_tournament(name="Import Test"):
    school = School.objects.create(name=f"{name} Host", short_name=f"{name[:8]}H")
    return Tournament.objects.create(
        name=name,
        manual_name=name,
        host=school,
        date=date(2024, 2, 10),
        season="2024",
        num_rounds=5,
    )


def _create_round(tournament=None):
    tournament = tournament or _create_tournament()
    school = tournament.host
    gov_team = Team.objects.create(name="Gov Team", short_name="Gov Team")
    opp_team = Team.objects.create(name="Opp Team", short_name="Opp Team")
    gov_team.debaters.add(
        Debater.objects.create(first_name="Gov", last_name="One", school=school),
        Debater.objects.create(first_name="Gov", last_name="Two", school=school),
    )
    opp_team.debaters.add(
        Debater.objects.create(first_name="Opp", last_name="One", school=school),
        Debater.objects.create(first_name="Opp", last_name="Two", school=school),
    )
    return Round.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=1,
        victor=Round.GOV,
    )


@pytest.mark.django_db
def test_round_allows_nullable_division():
    round_row = _create_round()

    assert round_row.division is None


@pytest.mark.django_db
def test_round_rejects_elim_size_on_prelim_stage():
    round_row = _create_round()
    round_row.stage = Round.Stage.PRELIM
    round_row.elim_size = 8

    with pytest.raises(IntegrityError):
        round_row.save()


@pytest.mark.django_db
def test_round_rejects_small_elim_size():
    round_row = _create_round()
    round_row.stage = Round.Stage.OUTROUND
    round_row.elim_size = 1

    with pytest.raises(IntegrityError):
        round_row.save()


@pytest.mark.django_db
def test_debater_alias_requires_debater():
    with pytest.raises(IntegrityError):
        DebaterAlias.objects.create(
            source_name="Alex Smith",
            normalized_name="alex smith",
        )


@pytest.mark.django_db
def test_imported_round_metadata_is_one_to_one_with_round():
    round_row = _create_round()
    metadata = ImportedRoundMetadata.objects.create(round=round_row)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ImportedRoundMetadata.objects.create(round=round_row)

    assert ImportedRoundMetadata.objects.get(pk=metadata.id).round_id == round_row.id


@pytest.mark.django_db
def test_imported_round_judge_allows_only_one_chair_per_round():
    round_row = _create_round()
    metadata = ImportedRoundMetadata.objects.create(round=round_row)
    ImportedRoundJudge.objects.create(
        round_metadata=metadata,
        original_name="Chair One",
        is_chair=True,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ImportedRoundJudge.objects.create(
                round_metadata=metadata,
                original_name="Chair Two",
                is_chair=True,
            )


@pytest.mark.django_db
def test_imported_round_metadata_rejects_wrong_side_roles():
    round_row = _create_round()
    metadata = ImportedRoundMetadata(
        round=round_row,
        gov_1_role=ImportedRoundMetadata.SpeakerRole.LO,
        opp_1_role=ImportedRoundMetadata.SpeakerRole.PM,
    )

    with pytest.raises(ValidationError):
        metadata.full_clean()


@pytest.mark.django_db(transaction=True)
def test_round_import_migration_preserves_trusted_manual_rounds():
    migrate_from = [("core", "0059_result_counts_for_points")]
    migrate_to = [("core", "0061_round_import_and_synthetic_models")]

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps

    SchoolModel = old_apps.get_model("core", "School")
    TeamModel = old_apps.get_model("core", "Team")
    TournamentModel = old_apps.get_model("core", "Tournament")
    RoundModel = old_apps.get_model("core", "Round")

    school = SchoolModel.objects.create(name="Legacy Host", short_name="LH")
    tournament = TournamentModel.objects.create(
        name="Legacy Manual Tournament",
        manual_name="Legacy Manual Tournament",
        host=school,
        date=date(2024, 2, 10),
        season="2024",
        num_rounds=5,
    )
    gov_team = TeamModel.objects.create(name="Legacy Gov", short_name="LG")
    opp_team = TeamModel.objects.create(name="Legacy Opp", short_name="LO")
    round_row = RoundModel.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=7,
    )

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_to)
    new_apps = executor.loader.project_state(migrate_to).apps
    MigratedRound = new_apps.get_model("core", "Round")

    migrated = MigratedRound.objects.get(pk=round_row.pk)

    assert migrated.import_origin == "manual"
    assert migrated.stage == "outround"
    assert migrated.metadata["record_origin"] == "trusted_pre_import_manual_round"
    assert migrated.metadata["preserved_by_round_import_migration"] is True
