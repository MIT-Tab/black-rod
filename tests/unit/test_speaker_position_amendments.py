import json
from datetime import date

import pytest

from core.models import Debater, ImportedRoundMetadata, Round, RoundStats, School, Team, Tournament, TournamentImport
from core.utils.speaker_position_amendments import generate_speaker_position_amendment_document


def _create_debater(*, school, first_name, last_name, alias_name):
    debater = Debater.objects.create(
        first_name=first_name,
        last_name=last_name,
        school=school,
    )
    debater.aliases.create(
        source_name=alias_name,
        normalized_name=alias_name.casefold(),
    )
    return debater


@pytest.mark.django_db
def test_generate_speaker_position_amendment_document_from_raw_backup(tmp_path):
    school = School.objects.create(name="Host", short_name="Host")
    tournament = Tournament.objects.create(
        host=school,
        date=date(2021, 1, 8),
        season="2021",
        manual_name="CMU (Online)",
        num_rounds=5,
    )
    gov_one = _create_debater(school=school, first_name="Roza", last_name="Kavak", alias_name="Roza Kavak")
    gov_two = _create_debater(school=school, first_name="Sai", last_name="Karnati", alias_name="Sai Karnati")
    opp_one = _create_debater(school=school, first_name="Gabbi", last_name="Shilcusky", alias_name="Gabbi Shilcusky")
    opp_two = _create_debater(school=school, first_name="Kavya", last_name="Gopinath", alias_name="Kavya Gopinath")

    gov_team = Team.objects.create(name="Gov", short_name="Gov")
    gov_team.debaters.add(gov_one, gov_two)
    opp_team = Team.objects.create(name="Opp", short_name="Opp")
    opp_team.debaters.add(opp_one, opp_two)

    round_obj = Round.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=1,
        round_label="P1",
        stage=Round.Stage.PRELIM,
        import_origin="file_backup",
        import_key="round-key-1",
        victor=Round.GOV,
    )
    metadata = ImportedRoundMetadata.objects.create(
        round=round_obj,
        gov_1_alias=gov_one.aliases.first(),
        gov_2_alias=gov_two.aliases.first(),
        opp_1_alias=opp_one.aliases.first(),
        opp_2_alias=opp_two.aliases.first(),
    )
    import_row = TournamentImport.objects.create(
        tournament=tournament,
        import_type=TournamentImport.ImportType.FILE_BACKUP,
        original_file_name="hopkinsmit-2020-12-1609170497-1609170497.json",
        source_hash="source-hash",
    )
    metadata.sources.add(import_row)

    RoundStats.objects.create(round=round_obj, debater=gov_one, speaks="27.0", ranks="3", debater_role="PM")
    RoundStats.objects.create(round=round_obj, debater=gov_two, speaks="27.0", ranks="4", debater_role="MG")
    RoundStats.objects.create(round=round_obj, debater=opp_one, speaks="28.0", ranks="2", debater_role="LO")
    RoundStats.objects.create(round=round_obj, debater=opp_two, speaks="30.0", ranks="1", debater_role="MO")

    backup_dir = tmp_path / "hopkinsmit-2020-12-1609170497" / "1609170497"
    backup_dir.mkdir(parents=True)
    backup_payload = [
        {"model": "tab.debater", "pk": 1, "fields": {"name": "Roza Kavak"}},
        {"model": "tab.debater", "pk": 2, "fields": {"name": "Sai Karnati"}},
        {"model": "tab.debater", "pk": 3, "fields": {"name": "Gabbi Shilcusky"}},
        {"model": "tab.debater", "pk": 4, "fields": {"name": "Kavya Gopinath"}},
        {"model": "tab.team", "pk": 11, "fields": {"debaters": [1, 2]}},
        {"model": "tab.team", "pk": 12, "fields": {"debaters": [3, 4]}},
        {
            "model": "tab.round",
            "pk": 21,
            "fields": {
                "round_number": 1,
                "gov_team": 11,
                "opp_team": 12,
                "victor": 1,
            },
        },
        {"model": "tab.roundstats", "pk": 31, "fields": {"debater": 2, "round": 21, "debater_role": "pm"}},
        {"model": "tab.roundstats", "pk": 32, "fields": {"debater": 1, "round": 21, "debater_role": "mg"}},
        {"model": "tab.roundstats", "pk": 33, "fields": {"debater": 3, "round": 21, "debater_role": "lo"}},
        {"model": "tab.roundstats", "pk": 34, "fields": {"debater": 4, "round": 21, "debater_role": "mo"}},
    ]
    (backup_dir / "final-backup.json").write_text(json.dumps(backup_payload), encoding="utf-8")

    document, report = generate_speaker_position_amendment_document(
        source_root=tmp_path,
        round_ids=[round_obj.id],
    )

    assert report["summary"]["target_rounds"] == 1
    assert report["summary"]["actions_written"] == 1
    action = document["actions"][0]
    assert action["type"] == "update_round"
    assert action["import_key"] == "round-key-1"
    assert action["imported_metadata"]["gov_1"]["debater_id"] == gov_two.id
    assert action["imported_metadata"]["gov_2"]["debater_id"] == gov_one.id
    stat_roles = {row["debater_id"]: row["debater_role"] for row in action["stats"]}
    assert stat_roles[gov_one.id] == "MG"
    assert stat_roles[gov_two.id] == "PM"
    speaks = {row["debater_id"]: row["speaks"] for row in action["stats"]}
    assert speaks[gov_one.id] == "27.0000"
    assert speaks[gov_two.id] == "27.0000"


@pytest.mark.django_db
def test_generate_speaker_position_amendment_document_from_sql_dump(tmp_path):
    school = School.objects.create(name="Harvard", short_name="Harvard")
    tournament = Tournament.objects.create(
        host=school,
        date=date(2022, 10, 8),
        season="2023",
        manual_name="Harvard (APDA Meeting)",
        num_rounds=5,
    )
    gov_one = _create_debater(school=school, first_name="Ahmad", last_name="Howard", alias_name="Ahmad Howard")
    gov_two = _create_debater(school=school, first_name="Roy", last_name="Tiefer", alias_name="Roy Tiefer")
    opp_one = _create_debater(school=school, first_name="Mandy", last_name="Feuerman", alias_name="Mandy Feuerman")
    opp_two = _create_debater(school=school, first_name="Thabang", last_name="Matona", alias_name="Thabang Matona")

    gov_team = Team.objects.create(name="Gov", short_name="Gov")
    gov_team.debaters.add(gov_one, gov_two)
    opp_team = Team.objects.create(name="Opp", short_name="Opp")
    opp_team.debaters.add(opp_one, opp_two)

    round_obj = Round.objects.create(
        tournament=tournament,
        gov=gov_team,
        opp=opp_team,
        round_number=3,
        round_label="P3",
        stage=Round.Stage.PRELIM,
        import_origin="db_inference",
        import_key="round-key-sql-1",
        victor=Round.GOV,
    )
    metadata = ImportedRoundMetadata.objects.create(
        round=round_obj,
        gov_1_alias=gov_one.aliases.first(),
        gov_2_alias=gov_two.aliases.first(),
        opp_1_alias=opp_one.aliases.first(),
        opp_2_alias=opp_two.aliases.first(),
    )
    import_row = TournamentImport.objects.create(
        tournament=tournament,
        import_type=TournamentImport.ImportType.DB_INFERENCE,
        original_file_name="1665094650",
        source_hash="db-source-hash",
    )
    metadata.sources.add(import_row)

    RoundStats.objects.create(round=round_obj, debater=gov_one, speaks="29.0", ranks="1", debater_role="PM")
    RoundStats.objects.create(round=round_obj, debater=gov_two, speaks="28.0", ranks="2", debater_role="MG")
    RoundStats.objects.create(round=round_obj, debater=opp_one, speaks="27.0", ranks="3", debater_role="LO")
    RoundStats.objects.create(round=round_obj, debater=opp_two, speaks="26.0", ranks="4", debater_role="MO")

    backup_dir = tmp_path / "harvardapda" / "1665094650"
    backup_dir.mkdir(parents=True)
    dump_sql = """
CREATE TABLE `tab_debater` (
  `id` int NOT NULL,
  `name` varchar(128) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE `tab_team_debaters` (
  `debater_id` int NOT NULL,
  `id` int NOT NULL,
  `team_id` int NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE `tab_round` (
  `chair_id` int DEFAULT NULL,
  `gov_team_id` int NOT NULL,
  `id` int NOT NULL,
  `opp_team_id` int NOT NULL,
  `pullup` tinyint(1) NOT NULL,
  `room_id` int DEFAULT NULL,
  `round_number` int NOT NULL,
  `victor` int DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE `tab_roundstats` (
  `debater_id` int NOT NULL,
  `debater_role` varchar(2) NOT NULL,
  `id` int NOT NULL,
  `ranks` decimal(5,4) NOT NULL,
  `round_id` int NOT NULL,
  `speaks` decimal(5,4) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `tab_debater` (`id`, `name`) VALUES (255,'Ahmad Howard'),(256,'Roy Tiefer'),(333,'Mandy Feuerman'),(334,'Thabang Matona');
INSERT INTO `tab_team_debaters` (`debater_id`, `id`, `team_id`) VALUES (255,1,122),(256,2,122),(333,3,123),(334,4,123);
INSERT INTO `tab_round` (`chair_id`, `gov_team_id`, `id`, `opp_team_id`, `pullup`, `room_id`, `round_number`, `victor`) VALUES (1,122,85,123,0,1,3,1);
INSERT INTO `tab_roundstats` (`debater_id`, `debater_role`, `id`, `ranks`, `round_id`, `speaks`) VALUES (256,'pm',473,2.0000,85,30.0000),(255,'mg',474,1.0000,85,30.0000),(333,'lo',475,3.0000,85,29.0000),(334,'mo',476,4.0000,85,29.0000);
""".strip()
    (backup_dir / "before_pairing_2.0_0.dump.sql").write_text(dump_sql + "\n", encoding="utf-8")

    document, report = generate_speaker_position_amendment_document(
        source_root=tmp_path,
        round_ids=[round_obj.id],
    )

    assert report["summary"]["target_rounds"] == 1
    assert report["summary"]["actions_written"] == 1
    action = document["actions"][0]
    assert action["import_key"] == "round-key-sql-1"
    assert action["imported_metadata"]["gov_1"]["debater_id"] == gov_two.id
    assert action["imported_metadata"]["gov_2"]["debater_id"] == gov_one.id
    stat_roles = {row["debater_id"]: row["debater_role"] for row in action["stats"]}
    assert stat_roles[gov_one.id] == "MG"
    assert stat_roles[gov_two.id] == "PM"
