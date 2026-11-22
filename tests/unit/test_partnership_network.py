from datetime import date

import pytest
from django.urls import reverse

from core.models import Debater, DebaterAliasGroup, QUAL, Round, School, SpeakerResult, Team, TeamResult, Tournament
from core.utils.partnership_network import build_partnership_network

pytestmark = pytest.mark.django_db


def test_network_deduplicates_tournaments_and_combines_filters():
    school = School.objects.create(name="School")
    other_school = School.objects.create(name="Other School")
    group = DebaterAliasGroup.objects.create()
    first = Debater.objects.create(first_name="First", school=school, alias_group=group)
    alias = Debater.objects.create(first_name="Alias", school=other_school, alias_group=group)
    second = Debater.objects.create(first_name="Second", school=school)
    isolated = Debater.objects.create(first_name="Isolated", school=school)
    temporary = Debater.all_objects.create(first_name="Temporary", temporary=True)
    synthetic = Debater.all_objects.create(first_name="Synthetic", synthetic=True)
    team = Team.objects.create(name="Partners")
    team.debaters.add(alias, second)
    opponent = Team.objects.create(name="Opponent")
    opponent.debaters.add(isolated, temporary, synthetic)
    events = [
        Tournament.objects.create(host=school, season=season, date=date(year, 1, 1))
        for season, year in [("2024", 2024), ("2025", 2025)]
    ]
    for event in events:
        TeamResult.objects.create(team=team, tournament=event)
        Round.objects.create(gov=team, opp=opponent, tournament=event)
    TeamResult.objects.create(team=team, tournament=events[0], type_of_place=TeamResult.NOVICE)
    for person in [first, second]:
        QUAL.objects.create(debater=person, season="2025")
    data = build_partnership_network({})
    # A team with just one public member does not make a partnership.
    assert [node["id"] for node in data["nodes"]] == [first.pk, second.pk]
    assert data["edges"] == [{"from": first.pk, "to": second.pk, "tournaments": 2}]
    assert data["nodes"][0]["schools"] == ["Other School", "School"]
    assert data["nodes"][0]["tournaments"] == 2

    filters = {"schools": [school], "from_season": "2025", "to_season": "2025", "qualified": True}
    assert build_partnership_network(filters)["edges"][0]["tournaments"] == 1
    assert build_partnership_network({**filters, "min_tournaments": 2}) == {"nodes": [], "edges": []}
    assert build_partnership_network({**filters, "to_season": "2024"})["nodes"] == []
    assert build_partnership_network({"schools": [other_school]})["nodes"] == []

    speaker_event = Tournament.objects.create(host=school, season="2023", date=date(2023, 1, 1))
    for place, person in enumerate([first, second], start=1):
        SpeakerResult.objects.create(debater=person, tournament=speaker_event, place=place)
    assert len(build_partnership_network({"min_tournaments": 3})["nodes"]) == 2
    assert build_partnership_network({})["edges"][0]["tournaments"] == 2

    # A redacted partnership cannot reappear in the network.
    result = team.team_results.first()
    result.hidden_from_debaters.add(second)
    assert build_partnership_network({}) == {"nodes": [], "edges": []}


def test_network_keeps_unrecorded_partners_but_respects_season_and_synthetic_teams():
    first = Debater.objects.create(first_name="First")
    second = Debater.objects.create(first_name="Second")
    team = Team.objects.create(name="Partners")
    team.debaters.add(first, second)
    assert build_partnership_network({})["edges"] == [
        {"from": first.pk, "to": second.pk, "tournaments": 0}
    ]
    assert build_partnership_network({"from_season": "2025"})["edges"] == []
    team.synthetic = True
    team.save()
    assert build_partnership_network({})["edges"] == []


def test_network_is_public_and_validates_filters(client):
    url = reverse("core:partnership_network")
    response = client.get(url)
    assert response.status_code == 200
    assert b"Apply filters" in response.content
    assert b"partnership-network" in response.content
    data_url = reverse("core:partnership_network_data")
    assert client.get(data_url).json() == {"nodes": [], "edges": []}
    assert client.get(data_url, {"min_tournaments": -1}).status_code == 400
    invalid = client.get(url, {"min_tournaments": "invalid"})
    assert invalid.context["form"].errors
    assert "network_url" not in invalid.context

    host = School.objects.create(name="Host")
    for year in [2024, 2025]:
        Tournament.objects.create(host=host, season=str(year), date=date(year, 1, 1))
    assert client.get(data_url, {"from_season": "2025", "to_season": "2024"}).status_code == 400


def test_network_paths_follow_filters_and_aliases(client):
    group = DebaterAliasGroup.objects.create()
    school = School.objects.create(name="School")
    first = Debater.objects.create(first_name="First", school=school, alias_group=group)
    alias = Debater.objects.create(first_name="Alias", school=school, alias_group=group)
    middle = Debater.objects.create(first_name="Middle", school=school)
    last = Debater.objects.create(first_name="Last", school=school)
    event = Tournament.objects.create(host=school, season="2025", date=date(2025, 1, 1))
    for members in [(alias, middle), (middle, last)]:
        team = Team.objects.create(name="Partners")
        team.debaters.add(*members)
        TeamResult.objects.create(team=team, tournament=event)
    url = reverse("core:partnership_network_paths")
    query = {"first_debater": alias.pk, "second_debater": last.pk}
    response = client.get(url, query)
    assert response.status_code == 200
    assert [[node["id"] for node in path] for path in response.json()["paths"]] == [
        [first.pk, middle.pk, last.pk]
    ]
    assert client.get(url, {**query, "min_tournaments": 2}).json() == {"paths": []}
    assert client.get(url, {**query, "first_debater": "invalid"}).status_code == 400
    temporary = Debater.all_objects.create(first_name="Temporary", temporary=True)
    assert client.get(url, {**query, "first_debater": temporary.pk}).status_code == 400
    same = client.get(url, {**query, "second_debater": first.pk})
    assert [node["id"] for node in same.json()["paths"][0]] == [first.pk]


def test_axis_values_aggregate_aliases_and_respect_seasons(client):
    from core.models import QualPoints, TOTY

    school = School.objects.create(name="Axis School")
    group = DebaterAliasGroup.objects.create()
    first = Debater.objects.create(
        first_name="First", school=school, alias_group=group,
        first_season="2000-01", latest_season="2002",
    )
    alias = Debater.objects.create(
        first_name="Alias", school=school, alias_group=group,
        first_season="2010", latest_season="2012-13",
    )
    partner = Debater.objects.create(first_name="Partner", school=school)
    Debater.objects.filter(pk=partner.pk).update(first_season="", latest_season=None)
    for year, points in [(2024, 12.5), (2025, 5)]:
        event = Tournament.objects.create(host=school, season=str(year), date=date(year, 1, 1))
        team = Team.objects.create(name="Axis Partners")
        team.debaters.add(first, alias, partner)
        TeamResult.objects.create(team=team, tournament=event)
        TOTY.objects.create(team=team, season=str(year), points=points)
        QUAL.objects.create(debater=alias, season=str(year))
    QUAL.objects.create(debater=first, season="2024", qual_type=QUAL.BRANDEIS)
    QualPoints.objects.create(debater=first, season="2024", points=2.5)
    QualPoints.objects.create(debater=alias, season="2024", points=3)
    QualPoints.objects.create(debater=alias, season="2025", points=7)

    all_nodes = {node["id"]: node for node in build_partnership_network({})["nodes"]}
    person = all_nodes[first.pk]
    assert person["toty_points"] == 17.5
    assert person["qual_points"] == 12.5
    assert person["qualification_count"] == 2
    assert person["first_year"] == 2000
    assert person["final_year"] == 2012
    assert person["partner_count"] == 1
    assert all_nodes[partner.pk]["first_year"] is None
    assert all_nodes[partner.pk]["final_year"] is None
    limited = build_partnership_network({"from_season": "2024", "to_season": "2024"})
    person = {node["id"]: node for node in limited["nodes"]}[first.pk]
    assert person["toty_points"] == 12.5
    assert person["qual_points"] == 5.5
    assert person["qualification_count"] == 1
    assert person["first_year"] == 2000
    assert person["final_year"] == 2012

    url = reverse("core:partnership_network")
    response = client.get(url)
    assert response.context["form"].cleaned_data["x_axis"] == ""
    assert response.context["form"].cleaned_data["y_axis"] == ""
    assert client.get(reverse("core:partnership_network_data"), {"x_axis": "invalid"}).status_code == 400
