from datetime import date
import importlib

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from core.models import (
    Debater,
    DebaterAlias,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    Round,
    RoundStats,
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
def test_roundstats_save_tracks_round_stage_for_outrounds():
    round_row = _create_round()
    round_row.stage = Round.Stage.OUTROUND
    round_row.elim_size = 8
    round_row.save(update_fields=["stage", "elim_size"])
    debater = round_row.gov.debaters.order_by("id").first()

    stat = RoundStats.objects.create(
        round=round_row,
        debater=debater,
        speaks="28.5",
        ranks="1",
        debater_role="PM",
    )

    assert stat.stage == Round.Stage.OUTROUND
    assert stat.debater_role is None
    assert stat.speaks is None
    assert stat.ranks is None


@pytest.mark.django_db
def test_round_stage_change_syncs_existing_roundstats():
    round_row = _create_round()
    debater = round_row.gov.debaters.order_by("id").first()
    stat = RoundStats.objects.create(
        round=round_row,
        debater=debater,
        speaks="27.5",
        ranks="1",
        debater_role="PM",
    )

    round_row.stage = Round.Stage.OUTROUND
    round_row.elim_size = 8
    round_row.save(update_fields=["stage", "elim_size"])
    stat.refresh_from_db()

    assert stat.stage == Round.Stage.OUTROUND
    assert stat.debater_role is None
    assert stat.speaks is None
    assert stat.ranks is None


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


@pytest.mark.django_db(transaction=True)
def test_outround_speaker_position_migration_clears_only_outrounds():
    migrate_from = [("core", "0062_schedulerworkspace_schedulingrun")]
    migrate_to = [("core", "0063_outround_roundstats_no_speaker_positions")]

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps

    SchoolModel = old_apps.get_model("core", "School")
    TeamModel = old_apps.get_model("core", "Team")
    DebaterModel = old_apps.get_model("core", "Debater")
    TournamentModel = old_apps.get_model("core", "Tournament")
    RoundModel = old_apps.get_model("core", "Round")
    RoundStatsModel = old_apps.get_model("core", "RoundStats")

    school = SchoolModel.objects.create(name="Trigger Host", short_name="TH")
    tournament = TournamentModel.objects.create(
        name="Trigger Invitational",
        manual_name="Trigger Invitational",
        host=school,
        date=date(2024, 2, 10),
        season="2024",
        num_rounds=5,
    )
    gov_team = TeamModel.objects.create(name="Trigger Gov", short_name="TG")
    opp_team = TeamModel.objects.create(name="Trigger Opp", short_name="TO")
    debater = DebaterModel.objects.create(
        first_name="Alex",
        last_name="Speaker",
        school=school,
    )

    prelim_round = RoundModel.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=1,
        stage="prelim",
        victor=Round.UNKNOWN,
    )
    outround = RoundModel.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=6,
        stage="outround",
        elim_size=8,
        victor=Round.UNKNOWN,
    )
    prelim_stat = RoundStatsModel.objects.create(
        round=prelim_round,
        debater=debater,
        speaks="27.5",
        ranks="1",
        debater_role="PM",
    )
    outround_stat = RoundStatsModel.objects.create(
        round=outround,
        debater=debater,
        score_index=2,
        speaks="28.0",
        ranks="2",
        debater_role="LO",
    )

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_to)
    new_apps = executor.loader.project_state(migrate_to).apps
    MigratedRoundStats = new_apps.get_model("core", "RoundStats")

    migrated_prelim_stat = MigratedRoundStats.objects.get(pk=prelim_stat.pk)
    migrated_outround_stat = MigratedRoundStats.objects.get(pk=outround_stat.pk)

    assert migrated_prelim_stat.stage == "prelim"
    assert migrated_prelim_stat.debater_role == "PM"
    assert str(migrated_prelim_stat.speaks) == "27.5000"
    assert str(migrated_prelim_stat.ranks) == "1.0000"
    assert migrated_outround_stat.stage == "outround"
    assert migrated_outround_stat.debater_role is None
    assert migrated_outround_stat.speaks is None
    assert migrated_outround_stat.ranks is None


@pytest.mark.django_db(transaction=True)
def test_outround_speaker_position_rule_blocks_future_positions():
    migrate_to = [("core", "0063_outround_roundstats_no_speaker_positions")]

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_to)
    apps = executor.loader.project_state(migrate_to).apps

    SchoolModel = apps.get_model("core", "School")
    TeamModel = apps.get_model("core", "Team")
    DebaterModel = apps.get_model("core", "Debater")
    TournamentModel = apps.get_model("core", "Tournament")
    RoundModel = apps.get_model("core", "Round")
    RoundStatsModel = apps.get_model("core", "RoundStats")

    school = SchoolModel.objects.create(name="Rule Host", short_name="RH")
    tournament = TournamentModel.objects.create(
        name="Rule Invitational",
        manual_name="Rule Invitational",
        host=school,
        date=date(2024, 2, 10),
        season="2024",
        num_rounds=5,
    )
    gov_team = TeamModel.objects.create(name="Rule Gov", short_name="RG")
    opp_team = TeamModel.objects.create(name="Rule Opp", short_name="RO")
    debater = DebaterModel.objects.create(
        first_name="Taylor",
        last_name="Debater",
        school=school,
    )

    outround = RoundModel.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=6,
        stage="outround",
        elim_size=8,
        victor=Round.UNKNOWN,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RoundStatsModel.objects.create(
                round=outround,
                debater=debater,
                stage="outround",
                debater_role="PM",
            )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RoundStatsModel.objects.create(
                round=outround,
                debater=debater,
                score_index=2,
                stage="outround",
                speaks="28.5",
                ranks="1",
            )

    prelim_round = RoundModel.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=2,
        stage="prelim",
        victor=Round.UNKNOWN,
    )
    prelim_stat = RoundStatsModel.objects.create(
        round=prelim_round,
        debater=debater,
        score_index=2,
        stage="prelim",
        speaks="27.0",
        ranks="2",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            prelim_stat.stage = "outround"
            prelim_stat.save(update_fields=["stage"])

    refreshed_prelim_stat = RoundStatsModel.objects.get(pk=prelim_stat.pk)
    assert refreshed_prelim_stat.stage == "prelim"
    assert refreshed_prelim_stat.debater_role is None
    assert str(refreshed_prelim_stat.speaks) == "27.0000"
    assert str(refreshed_prelim_stat.ranks) == "2.0000"


@pytest.mark.django_db(transaction=True)
def test_imported_metadata_role_migration_backfills_only_blank_roundstats():
    migrate_from = [("core", "0062_schedulerworkspace_schedulingrun")]
    migrate_to = [("core", "0063_outround_roundstats_no_speaker_positions")]

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps

    SchoolModel = old_apps.get_model("core", "School")
    TeamModel = old_apps.get_model("core", "Team")
    DebaterModel = old_apps.get_model("core", "Debater")
    DebaterAliasModel = old_apps.get_model("core", "DebaterAlias")
    TournamentModel = old_apps.get_model("core", "Tournament")
    RoundModel = old_apps.get_model("core", "Round")
    RoundStatsModel = old_apps.get_model("core", "RoundStats")
    ImportedRoundMetadataModel = old_apps.get_model("core", "ImportedRoundMetadata")

    school = SchoolModel.objects.create(name="Role Migration Host", short_name="RMH")
    tournament = TournamentModel.objects.create(
        name="Role Migration Invitational",
        manual_name="Role Migration Invitational",
        host=school,
        date=date(2024, 2, 10),
        season="2024",
        num_rounds=5,
    )
    gov_team = TeamModel.objects.create(name="Role Gov", short_name="RG")
    opp_team = TeamModel.objects.create(name="Role Opp", short_name="RO")
    blank_debater = DebaterModel.objects.create(first_name="Blank", last_name="Role", school=school)
    mismatch_debater = DebaterModel.objects.create(first_name="Mismatch", last_name="Role", school=school)
    gov_team.debaters.add(blank_debater)
    opp_team.debaters.add(mismatch_debater)

    round_row = RoundModel.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=1,
        stage="prelim",
        victor=Round.UNKNOWN,
    )
    blank_alias = DebaterAliasModel.objects.create(
        debater=blank_debater,
        source_name="Blank Role",
        normalized_name="blank role",
    )
    mismatch_alias = DebaterAliasModel.objects.create(
        debater=mismatch_debater,
        source_name="Mismatch Role",
        normalized_name="mismatch role",
    )
    blank_stat = RoundStatsModel.objects.create(
        round=round_row,
        debater=blank_debater,
        debater_role=None,
    )
    mismatch_stat = RoundStatsModel.objects.create(
        round=round_row,
        debater=mismatch_debater,
        score_index=2,
        debater_role="MO",
    )
    metadata_row = ImportedRoundMetadataModel.objects.create(
        round=round_row,
        gov_1_alias=blank_alias,
        gov_1_role="PM",
        opp_1_alias=mismatch_alias,
        opp_1_role="LO",
    )

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_to)
    new_apps = executor.loader.project_state(migrate_to).apps
    MigratedRoundStats = new_apps.get_model("core", "RoundStats")
    MigratedImportedRoundMetadata = new_apps.get_model("core", "ImportedRoundMetadata")

    assert MigratedRoundStats.objects.get(pk=blank_stat.pk).stage == "prelim"
    assert MigratedRoundStats.objects.get(pk=blank_stat.pk).debater_role == "PM"
    assert MigratedRoundStats.objects.get(pk=mismatch_stat.pk).stage == "prelim"
    assert MigratedRoundStats.objects.get(pk=mismatch_stat.pk).debater_role == "MO"
    migrated_metadata = MigratedImportedRoundMetadata.objects.get(pk=metadata_row.pk)
    assert not hasattr(migrated_metadata, "gov_1_role")


@pytest.mark.django_db(transaction=True)
def test_debater_name_sanitization_migration_cleans_existing_payloads():
    migrate_from = [("core", "0066_user_can_view_debug_tab_cards")]
    migrate_to = [("core", "0067_sanitize_debater_names")]

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps

    SchoolModel = old_apps.get_model("core", "School")
    DebaterModel = old_apps.get_model("core", "Debater")

    school = SchoolModel.objects.create(name="Cleanup Host", short_name="CH")
    payload_only = DebaterModel.objects.create(
        first_name="<script src=//x.js>",
        last_name="Partner",
        school=school,
    )
    mixed = DebaterModel.objects.create(
        first_name="Alex<script>alert(1)</script>",
        last_name="<b>Smith</b>",
        school=school,
    )

    executor = MigrationExecutor(connection)
    executor.migrate(migrate_to)
    new_apps = executor.loader.project_state(migrate_to).apps
    MigratedDebater = new_apps.get_model("core", "Debater")

    cleaned_payload_only = MigratedDebater.objects.get(pk=payload_only.pk)
    cleaned_mixed = MigratedDebater.objects.get(pk=mixed.pk)

    assert cleaned_payload_only.first_name == "Removed"
    assert cleaned_payload_only.last_name == "Partner"
    assert cleaned_mixed.first_name == "Alexalert(1)"
    assert cleaned_mixed.last_name == "Smith"


@pytest.mark.django_db
def test_debater_name_sanitization_migration_logs_fallback_hits(monkeypatch):
    migration = importlib.import_module("core.migrations.0067_sanitize_debater_names")
    school = School.objects.create(name="Fallback Host", short_name="FH")
    debater = Debater.objects.create(
        first_name="Alex",
        last_name="Partner",
        school=school,
    )
    Debater.all_objects.filter(pk=debater.pk).update(
        first_name="<script src=//attacker/x.js>",
        last_name="Partner",
    )

    captured_messages = []
    monkeypatch.setattr("builtins.print", lambda *parts, **kwargs: captured_messages.append(" ".join(map(str, parts))))

    migration.sanitize_debater_names(apps=django_apps, schema_editor=None)

    debater.refresh_from_db()
    assert debater.first_name == "Removed"
    assert debater.last_name == "Partner"
    assert any("fallback applied to debater id=" in message for message in captured_messages)
    assert any("original_first_name='<script src=//attacker/x.js>'" in message for message in captured_messages)
    assert any("fallback applied to 1 debater name(s)" in message for message in captured_messages)
