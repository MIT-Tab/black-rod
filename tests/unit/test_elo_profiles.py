"""Tests dashboard profile row construction, ranking order, and manual opt-in/opt-out behavior in the runtime profile filtering layer."""


import pytest

from core.models import Debater, DebaterAliasGroup, QUAL, School
from core.utils.elo_runtime_engine.models import PlayerStats
from core.utils.elo_runtime_engine.profiles import build_dashboard_payload


def _build_stats(name, season, rating=1600.0):
    player_stats = PlayerStats()
    player_stats.rating = rating
    player_stats.rounds = 4.0
    player_stats.prelim_rounds = 4.0
    player_stats.outround_rounds = 0.0
    player_stats.yearly_results[str(season)] = [3.0, 1.0]
    player_stats.name_hints[name] = 1.0
    return player_stats


@pytest.mark.django_db
def test_build_dashboard_payload_returns_core_row_fields():
    school = School.objects.create(name="Opt School")
    debater = Debater.objects.create(
        first_name="Manual",
        last_name="Out",
        school=school,
        first_season="2025",
        latest_season="2025",
    )

    rows, excluded_count, qual_data_available = build_dashboard_payload(
        stats={debater.id: _build_stats("Manual Out", 2025)},
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=["2025"],
        exclude_dino_rounds=False,
    )

    assert qual_data_available is False
    assert excluded_count == 0
    assert len(rows) == 1
    assert rows[0].name == "Manual Out"
    assert rows[0].rounds == 4
    assert rows[0].prelim_rounds == 4
    assert rows[0].outround_rounds == 0
    assert rows[0].elo == pytest.approx(1600.0)


@pytest.mark.django_db
def test_build_dashboard_payload_sorts_by_elo_desc():
    school = School.objects.create(name="Sort School")
    first = Debater.objects.create(
        first_name="High",
        last_name="Elo",
        school=school,
        first_season="2025",
        latest_season="2025",
    )
    second = Debater.objects.create(
        first_name="Low",
        last_name="Elo",
        school=school,
        first_season="2025",
        latest_season="2025",
    )

    rows, _excluded_count, _qual_data_available = build_dashboard_payload(
        stats={
            first.id: _build_stats("High Elo", 2025, rating=1700.0),
            second.id: _build_stats("Low Elo", 2025, rating=1500.0),
        },
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=["2025"],
        exclude_dino_rounds=False,
    )

    assert [row.name for row in rows] == ["High Elo", "Low Elo"]
    assert [row.rank for row in rows] == [1, 2]


@pytest.mark.django_db
def test_build_dashboard_payload_respects_manual_opt_out():
    school = School.objects.create(name="Opt School")
    debater = Debater.objects.create(
        first_name="Manual",
        last_name="Out",
        school=school,
        first_season="2025",
        latest_season="2025",
    )
    debater.elo_manual_opt = Debater.EloManualOpt.OPT_OUT
    debater.save(update_fields=["elo_manual_opt"])

    rows, excluded_count, qual_data_available = build_dashboard_payload(
        stats={debater.id: _build_stats("Manual Out", 2025)},
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=["2025"],
        exclude_dino_rounds=False,
    )

    assert qual_data_available is False
    assert excluded_count == 1
    assert rows == []


@pytest.mark.django_db
def test_build_dashboard_payload_respects_manual_opt_in():
    school = School.objects.create(name="Opt-In School")
    debater = Debater.objects.create(
        first_name="Manual",
        last_name="In",
        school=school,
        first_season="2025",
        latest_season="2025",
    )
    other = Debater.objects.create(
        first_name="Qual",
        last_name="Marker",
        school=school,
        first_season="2024",
        latest_season="2025",
    )
    QUAL.objects.create(
        debater=other,
        season="2025",
        qual_type=QUAL.POINTS,
    )
    debater.elo_manual_opt = Debater.EloManualOpt.OPT_IN
    debater.save(update_fields=["elo_manual_opt"])

    rows, excluded_count, qual_data_available = build_dashboard_payload(
        stats={debater.id: _build_stats("Manual In", 2025)},
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=["2025"],
        exclude_dino_rounds=False,
    )

    assert qual_data_available is True
    assert excluded_count == 0
    assert len(rows) == 1


@pytest.mark.django_db
def test_build_dashboard_payload_uses_affiliated_canonical_seasons_for_active_filter():
    affiliated_school = School.objects.create(name="Affiliated School")
    unaffiliated_school = School.objects.create(name="Unaffiliated")
    alias_group = DebaterAliasGroup.objects.create()
    affiliated = Debater.objects.create(
        first_name="Active",
        last_name="Alum",
        school=affiliated_school,
        alias_group=alias_group,
        status=Debater.DINO,
        first_season="2021",
        latest_season="2024",
    )
    Debater.objects.create(
        first_name="Active",
        last_name="Alum",
        school=unaffiliated_school,
        alias_group=alias_group,
        status=Debater.DINO,
        first_season="2025",
        latest_season="2025",
    )

    rows, excluded_count, qual_data_available = build_dashboard_payload(
        stats={affiliated.id: _build_stats("Active Alum", 2025)},
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=["2025"],
        exclude_dino_rounds=False,
    )

    assert qual_data_available is False
    assert excluded_count == 0
    assert rows == []


@pytest.mark.django_db
def test_build_dashboard_payload_shows_all_alias_group_schools_except_unaffiliated():
    first_school = School.objects.create(name="Alpha College")
    second_school = School.objects.create(name="Beta University")
    unaffiliated_school = School.objects.create(name="Unaffiliated")
    alias_group = DebaterAliasGroup.objects.create()
    debater = Debater.objects.create(
        first_name="School",
        last_name="List",
        school=second_school,
        alias_group=alias_group,
        first_season="2024",
        latest_season="2025",
    )
    Debater.objects.create(
        first_name="School",
        last_name="List",
        school=first_school,
        alias_group=alias_group,
        first_season="2023",
        latest_season="2024",
    )
    Debater.objects.create(
        first_name="School",
        last_name="List",
        school=unaffiliated_school,
        alias_group=alias_group,
        first_season="2025",
        latest_season="2025",
    )

    rows, excluded_count, qual_data_available = build_dashboard_payload(
        stats={debater.id: _build_stats("School List", 2025)},
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=["2025"],
        exclude_dino_rounds=False,
    )

    assert qual_data_available is False
    assert excluded_count == 0
    assert len(rows) == 1
    assert rows[0].school_name == "Alpha College, Beta University"
    assert rows[0].schools == [
        {"id": first_school.id, "name": "Alpha College"},
        {"id": second_school.id, "name": "Beta University"},
    ]


@pytest.mark.django_db
def test_build_dashboard_payload_uses_end_of_affiliation_snapshot_when_excluding_dino_rounds():
    affiliated_school = School.objects.create(name="Affiliated School")
    unaffiliated_school = School.objects.create(name="Unaffiliated")
    alias_group = DebaterAliasGroup.objects.create()
    affiliated = Debater.objects.create(
        first_name="Dino",
        last_name="Row",
        school=affiliated_school,
        alias_group=alias_group,
        status=Debater.DINO,
        first_season="2021",
        latest_season="2024",
    )
    Debater.objects.create(
        first_name="Dino",
        last_name="Row",
        school=unaffiliated_school,
        alias_group=alias_group,
        status=Debater.DINO,
        first_season="2025",
        latest_season="2025",
    )
    player_stats = _build_stats("Dino Row", 2025, rating=1700.0)
    player_stats.rounds = 10.0
    player_stats.prelim_rounds = 8.0
    player_stats.outround_rounds = 2.0
    player_stats.season_snapshots["2024"] = {
        "elo": 1612.0,
        "rounds": 7,
        "prelim_rounds": 6,
        "outround_rounds": 1,
    }

    rows, excluded_count, qual_data_available = build_dashboard_payload(
        stats={affiliated.id: player_stats},
        min_rounds=1,
        min_outrounds=0,
        output_limit=50,
        active_seasons=[],
        exclude_dino_rounds=True,
    )

    assert qual_data_available is False
    assert excluded_count == 0
    assert len(rows) == 1
    assert rows[0].elo == pytest.approx(1612.0)
    assert rows[0].rounds == 7
    assert rows[0].prelim_rounds == 6
    assert rows[0].outround_rounds == 1
