from unittest.mock import patch
from datetime import date

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from core import live_results
from core.live_results import CACHE_KEY, build_live_standings
from core.utils.rankings import redo_rankings
from core.models import COTY, QUAL, TOTY, Debater, QualPoints, School, Team, Tournament


def _payload(name="MIT Fall", sanctioned=True, place=3):
    return {
        "schema_version": 1,
        "source_url": "https://mit.nu-tab.com",
        "tournament": {
            "name": name,
            "date": "2026-10-19",
            "season": "2026",
            "host_school": {"apda_id": 1, "name": "MIT"},
            "tournament_type": 0,
            "sanctioned": sanctioned,
            "num_teams": 32,
            "num_novice_debaters": 8,
        },
        "team_results": [{
            "place": place,
            "division": "varsity",
            "team": {
                "name": "MIT AB",
                "debaters": [
                    {"apda_id": 101, "name": "Ada A", "school": {"apda_id": 1, "name": "MIT"}},
                    {"apda_id": 102, "name": "Bea B", "school": {"apda_id": 1, "name": "MIT"}},
                ],
            },
        }],
        "speaker_results": [],
    }


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_standings_are_ephemeral_and_cached_for_the_page():
    cache.clear()
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]) as fetch:
        first = build_live_standings()
        second = build_live_standings()

    assert first == second
    fetch.assert_called_once()
    assert first["active_sources"] == ["MIT Fall"]
    assert first["boards"]["toty"][0]["has_live"] is True
    assert first["boards"]["toty"][0]["markers"][0]["placement"] == 3


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_unsanctioned_tournament_does_not_affect_standings():
    cache.clear()
    with patch(
        "core.live_results.fetch_live_sources",
        return_value=[_payload(sanctioned=False)],
    ):
        result = build_live_standings()

    assert result["active_sources"] == []
    assert result["boards"]["toty"] == []


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_page_labels_provisional_results():
    cache.clear()
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        response = Client().get("/live")

    assert response.status_code == 200
    assert b"have not been reviewed by a human" in response.content
    assert b">MIT</a>" in response.content
    assert b"MIT Fall" not in response.content
    assert b"<th scope=\"col\">School</th>" in response.content
    assert b"<th scope=\"col\">Debaters</th>" in response.content
    assert b"<mark class=\"bg-warning\">" in response.content
    assert b"Results counting toward total" not in response.content


@pytest.mark.parametrize("host, name, expected", [
    ({"apda_id": 14, "name": "Tufts"}, "Tufts Fall 2026: Avatar Seven Havens", "Tufts"),
    ({"apda_id": 14, "name": ""}, "Tufts Fall", "Tufts Fall"),
    (None, "Tufts Fall", "Tufts Fall"),
    (None, "", "Live tournament"),
])
def test_live_markers_use_host_school_name_with_fallbacks(host, name, expected):
    marker = live_results._live_marker(2.5, {
        "host_school": host, "name": name,
    }, "https://tufts.nu-tab.com", 5)

    assert marker == {
        "points": 2.5,
        "tournament": expected,
        "url": "https://tufts.nu-tab.com",
        "placement": 5,
        "live": True,
    }


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_already_imported_tournament_is_not_counted_twice():
    cache.clear()
    host = School.objects.create(id=1, name="MIT", short_name="MIT")
    Tournament.objects.create(
        host=host,
        season="2026",
        date=date(2026, 10, 19),
        qual_type=Tournament.POINTS,
        num_teams=32,
        num_novice_teams=0,
        num_debaters=64,
        num_novice_debaters=8,
    )
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        result = build_live_standings()

    assert result["active_sources"] == []
    assert result["boards"]["toty"] == []


def _reviewed_team(season="2026"):
    school = School.objects.create(name="Yale", short_name="Yale")
    first = Debater.objects.create(first_name="Ada", last_name="A", school=school)
    second = Debater.objects.create(first_name="Bea", last_name="B", school=school)
    team = Team.objects.create(name="Yale AB")
    team.debaters.add(first, second)
    reviewed = Tournament.objects.create(
        host=school,
        season=season,
        date=date(2026, 9, 20),
        qual_type=Tournament.POINTS,
        num_teams=32,
        num_novice_teams=0,
        num_debaters=64,
        num_novice_debaters=0,
    )
    TOTY.objects.create(
        season=season, team=team, points=12, marker_one=12, tournament_one=reviewed,
    )
    return school, first, second


def _live_team(payload, first, second, school, place):
    payload["team_results"] = [{
        "place": place,
        "division": "varsity",
        "team": {
            "name": "Yale AB",
            "debaters": [
                {"apda_id": first.id, "name": first.name, "school": {"apda_id": school.id}},
                {"apda_id": second.id, "name": second.name, "school": {"apda_id": school.id}},
            ],
        },
    }]
    return payload


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_markers_sort_with_reviewed_markers_by_points():
    cache.clear()
    school, first, second = _reviewed_team()
    payload = _live_team(_payload(), first, second, school, place=1)
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        row = build_live_standings()["boards"]["toty"][0]

    assert [(marker["points"], marker["live"]) for marker in row["markers"]] == [
        (14.0, True), (12.0, False),
    ]
    assert row["marker_slots"][0]["live"] is True
    assert row["points"] == 26


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_auto_qual_tournaments_award_coty_without_points():
    cache.clear()
    school, first, second = _reviewed_team()
    payload = _live_team(_payload(), first, second, school, place=3)
    payload["tournament"]["tournament_type"] = Tournament.BRANDEIS
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        boards = build_live_standings()["boards"]

    coty = boards["coty"][0]
    assert coty["key"] == f"school:{school.id}"
    assert coty["points"] == 12
    assert [
        (debater["points"], debater["contribution"], debater["qualled_live"], debater["quals"])
        for debater in coty["debaters"]
    ] == [(0.0, 6.0, True, [{"label": "Brandeis IV", "live": True}])] * 2
    assert all(not marker["live"] for row in boards["toty"] for marker in row["markers"])


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_existing_qualification_is_not_counted_twice():
    cache.clear()
    school, first, second = _reviewed_team()
    QUAL.objects.create(season="2026", debater=first, qual_type=QUAL.POINTS)
    QualPoints.objects.create(season="2026", debater=first, points=58)
    payload = _live_team(_payload(), first, second, school, place=1)
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        coty = build_live_standings()["boards"]["coty"][0]

    # First debater is capped at 60 qual points and already qualified; the
    # second earns 14 points and crosses the bar for a new qualification.
    breakdown = {debater["name"]: debater for debater in coty["debaters"]}
    assert breakdown[first.name]["points"] == 72
    assert breakdown[first.name]["contribution"] == 66
    assert breakdown[first.name]["qualled_live"] is False
    assert breakdown[second.name]["contribution"] == 20
    assert breakdown[second.name]["qualled_live"] is True
    assert coty["points"] == 86
    assert [debater["name"] for debater in coty["debaters"]] == [first.name, second.name]


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_coty_matches_reviewed_breakdown_without_live_results():
    cache.clear()
    school, first, second = _reviewed_team()
    COTY.objects.create(season="2026", school=school, points=26)
    QualPoints.objects.create(season="2026", debater=first, points=14)
    QUAL.objects.create(season="2026", debater=first, qual_type=QUAL.POINTS)
    QUAL.objects.create(season="2026", debater=second, qual_type=QUAL.YALE)
    with patch("core.live_results.fetch_live_sources", return_value=[]):
        coty = build_live_standings()["boards"]["coty"]

    assert len(coty) == 1
    assert coty[0]["points"] == 26
    assert coty[0]["has_live"] is False
    assert [
        (debater["name"], debater["points"], debater["contribution"], debater["quals"])
        for debater in coty[0]["debaters"]
    ] == [
        (first.name, 14.0, 20.0, []),
        (second.name, 0.0, 6.0, [{"label": "Yale IV", "live": False}]),
    ]


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_page_shows_coty_debater_breakdown():
    cache.clear()
    school, first, second = _reviewed_team()
    payload = _live_team(_payload(), first, second, school, place=1)
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        response = Client().get("/live")

    content = response.content.decode()
    assert 'id="expandAll"' in content
    assert 'data-newtarget="#coty_1"' in content
    assert "Points (Contribution)" in content
    assert content.count('id="coty"') == 1
    assert content.count('id="toty"') == 1
    assert first.name in content and second.name in content


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_hybrid_teams_do_not_earn_live_toty():
    cache.clear()
    school, first, second = _reviewed_team()
    second.school = School.objects.create(name="Brown", short_name="Brown")
    second.save()
    payload = _live_team(_payload(), first, second, school, place=1)
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        toty = build_live_standings()["boards"]["toty"]

    assert not any(row["has_live"] for row in toty)


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_new_live_toty_rows_use_school_and_debater_initials():
    cache.clear()
    payload = _payload()
    payload["team_results"][0]["team"]["name"] = "fun tab nickname"
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        result = build_live_standings()

    row = result["boards"]["toty"][0]
    assert row["name"] == "MIT AB"
    assert row["detail"] == "Ada A and Bea B"
    assert row["points"] > 0
    assert row["name"] != payload["team_results"][0]["team"]["name"]


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
@pytest.mark.parametrize("flags", [{}, {"temporary": True}, {"synthetic": True}])
def test_explicit_debater_ids_preserve_unaffiliated_status(flags):
    cache.clear()
    school = School.objects.create(name="Amherst")
    julia = Debater.all_objects.create(
        id=19492, first_name="Julia", last_name="Rubas", school=None, **flags,
    )
    partner = Debater.objects.create(first_name="Raina", last_name="Batra", school=school)
    payload = _live_team(_payload(), julia, partner, school, place=5)
    # The tab site assigns Julia to the team's school. Black Rod must use her ID.
    person = payload["team_results"][0]["team"]["debaters"][0]
    person["apda_id"] = str(julia.id)
    person["name"] = "Tab site nickname"
    payload["speaker_results"] = [{"place": 1, "division": "varsity", "debater": person}]
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        boards = build_live_standings()["boards"]

    assert boards["toty"] == []
    speaker = boards["soty"][0]
    assert speaker["key"] == f"debater:{julia.id}"
    assert speaker["name"] == "Julia Rubas"
    assert speaker["detail"] == "Unaffiliated"
    assert all(
        debater["key"] != f"debater:{julia.id}"
        for row in boards["coty"] for debater in row["debaters"]
    )


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_new_live_team_uses_canonical_affiliation_and_last_names():
    cache.clear()
    school = School.objects.create(name="Yale")
    first = Debater.all_objects.create(
        first_name="Alexandra", last_name="Uy-Tioco", school=school, temporary=True,
    )
    second = Debater.objects.create(first_name="Grace", last_name="Lyles", school=school)
    payload = _live_team(_payload(), first, second, school, place=3)
    payload["team_results"][0]["team"]["name"] = "fun tab nickname"
    for person in payload["team_results"][0]["team"]["debaters"]:
        person["name"] = "Tab nickname"
        person["school"] = {"apda_id": 9999, "name": "Wrong school"}
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        boards = build_live_standings()["boards"]

    assert boards["toty"][0]["name"] == "Yale UL"
    assert boards["toty"][0]["detail"] == "Alexandra Uy-Tioco and Grace Lyles"
    assert boards["coty"][0]["name"] == "Yale"


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_tournament_without_host_is_ignored():
    cache.clear()
    payload = _payload()
    payload["tournament"]["host_school"] = {"apda_id": None, "name": ""}
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        result = build_live_standings()

    assert result["active_sources"] == []


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_import_dated_days_apart_still_deduplicates():
    cache.clear()
    host = School.objects.create(id=1, name="MIT", short_name="MIT")
    Tournament.objects.create(
        host=host,
        season="2026",
        date=date(2026, 10, 17),
        qual_type=Tournament.POINTS,
        num_teams=32,
        num_novice_teams=0,
        num_debaters=64,
        num_novice_debaters=8,
    )
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        result = build_live_standings()

    assert result["active_sources"] == []


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_build_only_reads_the_database():
    cache.clear()
    school, first, second = _reviewed_team()
    payload = _live_team(_payload(), first, second, school, place=1)
    with patch("core.live_results.fetch_live_sources", return_value=[payload]):
        with CaptureQueriesContext(connection) as queries:
            build_live_standings()

    writes = [
        query["sql"] for query in queries
        if not query["sql"].lstrip().upper().startswith("SELECT")
    ]
    assert writes == []


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_standings_query_count_does_not_grow_with_rows():
    cache.clear()
    _reviewed_team()
    with patch("core.live_results.fetch_live_sources", return_value=[]):
        with CaptureQueriesContext(connection) as small:
            build_live_standings()
    cache.clear()
    for index in range(5):
        school = School.objects.create(name=f"School {index}", short_name=f"S{index}")
        team = Team.objects.create(name=f"Team {index}")
        team.debaters.add(
            Debater.objects.create(first_name="A", last_name=str(index), school=school),
            Debater.objects.create(first_name="B", last_name=str(index), school=school),
        )
        TOTY.objects.create(
            season="2026", team=team, points=4, marker_one=4,
            tournament_one=Tournament.objects.first(),
        )
    with patch("core.live_results.fetch_live_sources", return_value=[]):
        with CaptureQueriesContext(connection) as large:
            build_live_standings()

    assert len(large) == len(small)


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_concurrent_refresh_serves_stale_standings():
    cache.clear()
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        first = build_live_standings()
    cache.delete(f"{CACHE_KEY}:2026")
    cache.add(f"{CACHE_KEY}:2026:lock", 1)
    with patch("core.live_results.fetch_live_sources") as fetch:
        assert build_live_standings() == first
    fetch.assert_not_called()


BANNER = b"are not in these standings yet"


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_index_hides_banner_without_live_results():
    cache.clear()
    with patch("core.live_results._start_refresh") as refresh, patch(
        "core.live_results.fetch_live_sources"
    ) as fetch:
        response = Client().get("/")

    fetch.assert_not_called()
    refresh.assert_called_once_with("2026")
    assert BANNER not in response.content
    assert b'href="/live"' in response.content


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_index_banner_appears_after_background_refresh():
    cache.clear()
    with patch("core.live_results._start_refresh") as refresh, patch(
        "core.live_results.fetch_live_sources", return_value=[_payload()]
    ):
        # The first visit never waits on remote tournaments.
        first = Client().get("/")
        live_results._refresh(*refresh.call_args.args)
        second = Client().get("/")

    assert BANNER not in first.content
    assert BANNER in second.content
    assert b"MIT Fall" in second.content


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_index_hides_banner_when_no_tournament_is_live():
    cache.clear()
    with patch("core.live_results._start_refresh", side_effect=live_results._refresh), patch(
        "core.live_results.fetch_live_sources", return_value=[_payload(sanctioned=False)]
    ):
        Client().get("/")
        response = Client().get("/")

    assert BANNER not in response.content


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_reviewed_rankings_update_clears_live_standings():
    cache.clear()
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        build_live_standings()

    with patch("core.utils.rankings.Client"):
        redo_rankings(TOTY.objects.filter(season="2026"), season="2026")

    assert cache.get(f"{CACHE_KEY}:2026") is None
    assert cache.get(f"{CACHE_KEY}:2026:stale") is None
