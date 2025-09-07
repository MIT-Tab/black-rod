import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.shortcuts import reverse

from core.models.debater import Debater, QualPoints, Reaff
from core.models.results.team import TeamResult
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY, TOTYReaff
from django.test import Client


def get_qualled_debaters(school, season):
    qualled_debaters = [
        q.debater for q in QUAL.objects.filter(debater__school=school, season=season)
    ]

    qualled_debaters = list(set(qualled_debaters))

    qual_points = (
        QualPoints.objects.filter(debater__school=school)
        .filter(season=season)
        .order_by("-points")
    )

    to_return = []
    handled_debaters = []

    for qual_point in qual_points:
        if qual_point.points > 0 or qual_point.debater in qualled_debaters:
            to_return += [qual_point]
            handled_debaters.append(qual_point.debater)

    for debater in qualled_debaters:
        if debater in handled_debaters:
            continue

        qual_point = QualPoints.objects.create(debater=debater, points=0, season=season)

        to_return += [qual_point]

    return to_return


def update_toty(team, season=settings.CURRENT_SEASON):
    if team.team_results.count() == 0:
        team.delete()
        return

    if team.hybrid:
        return

    if team.debaters.count() == 0:
        return

    if TOTYReaff.objects.filter(old_team=team).filter(season=season).count() > 0:
        TOTY.objects.filter(season=season).filter(team=team).delete()
        return

    if team.debaters.first() and not team.debaters.first().school.included_in_oty:
        TOTY.objects.filter(
            season=season, team__debaters__school=team.debaters.first().school
        ).delete()
        return

    results = (
        team.team_results.filter(tournament__season=season)
        .filter(tournament__toty=True)
        .filter(type_of_place=Debater.VARSITY)
    )

    reaff = TOTYReaff.objects.filter(new_team=team).filter(season=season).all()
    if len(reaff) > 0:
        results = results | reaff[0].old_team.team_results.filter(
            tournament__season=season
        ).filter(tournament__toty=True).filter(type_of_place=Debater.VARSITY)

    markers = [
        (
            result.tournament.get_toty_points(
                result.place, ghost_points=result.ghost_points
            ),
            result,
        )
        for result in results
    ]

    markers.sort(key=lambda marker: marker[0], reverse=True)

    toty = TOTY.objects.filter(season=season).filter(team=team).first()

    if len(markers) == 0:
        if toty:
            toty.delete()
        return

    if not toty:
        toty = TOTY.objects.create(season=season, team=team)

    labels = ["one", "two", "three", "four", "five"]

    points = 0
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue

        if i > 4:
            continue

        setattr(toty, f"marker_{labels[i]}", markers[i][0])

        setattr(toty, f"tournament_{labels[i]}", markers[i][1].tournament)

        points += markers[i][0]

    toty.points = points
    toty.save()

    return toty


def update_soty(debater, season=settings.CURRENT_SEASON):
    if isinstance(season, str):
        season = int(season.split("-")[0])
    if (
        not any(
            [
                team.team_results.count() > 0
                or team.govs.count() > 0
                or team.opps.count() > 0
                for team in debater.teams.all()
            ]
        )
        and debater.speaker_results.count() == 0
    ):
        debater.delete()
        return

    if Reaff.objects.filter(old_debater=debater).filter(season=season).count() > 0:
        SOTY.objects.filter(season=season).filter(debater=debater).delete()
        return

    if not debater.school.included_in_oty:
        SOTY.objects.filter(season=season, debater__school=debater.school).delete()
        return

    results = (
        debater.speaker_results.filter(tournament__season=season)
        .filter(tournament__soty=True)
        .filter(type_of_place=Debater.VARSITY)
    )

    reaff = Reaff.objects.filter(new_debater=debater).filter(season=season).all()
    if len(reaff) > 0:
        results = results | reaff[0].old_debater.speaker_results.filter(
            tournament__season=season
        ).filter(tournament__soty=True).filter(type_of_place=Debater.VARSITY)

    markers = [
        (
            result.tournament.get_soty_points(result.place - (1 if result.tie else 0)),
            result,
        )
        for result in results
    ]

    markers.sort(key=lambda marker: marker[0], reverse=True)

    soty = SOTY.objects.filter(season=season).filter(debater=debater).first()

    if len(markers) == 0:
        if soty:
            soty.delete()
        return

    if not soty:
        soty = SOTY.objects.create(season=season, debater=debater)

    labels = ["one", "two", "three", "four", "five", "six"]

    points = 0
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue

        if i > 5:
            continue

        setattr(soty, f"marker_{labels[i]}", markers[i][0])

        setattr(soty, f"tournament_{labels[i]}", markers[i][1].tournament)

        points += markers[i][0]

    soty.points = points
    soty.save()

    return soty


def update_noty(debater, season=settings.CURRENT_SEASON):
    if isinstance(season, str):
        season = int(season.split("-")[0])
    if season > settings.LAST_NOTY_SEASON:
        return None
    if (
        not any(
            [
                team.team_results.count() > 0
                or team.govs.count() > 0
                or team.opps.count() > 0
                for team in debater.teams.all()
            ]
        )
        and debater.speaker_results.count() == 0
    ):
        debater.delete()
        return

    if not debater.school.included_in_oty:
        NOTY.objects.filter(season=season, debater__school=debater.school).delete()

        return

    results = (
        debater.speaker_results.filter(tournament__season=season)
        .filter(tournament__noty=True)
        .filter(type_of_place=Debater.NOVICE)
    )

    markers = [
        (result.tournament.get_noty_points(result.place), result) for result in results
    ]

    markers.sort(key=lambda marker: marker[0], reverse=True)

    noty = NOTY.objects.filter(season=season).filter(debater=debater).first()

    if len(markers) == 0:
        if noty:
            noty.delete()
        return

    if not noty:
        noty = NOTY.objects.create(season=season, debater=debater)

    labels = ["one", "two", "three", "four", "five"]

    points = 0
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue

        if i > 4:
            continue

        setattr(noty, f"marker_{labels[i]}", markers[i][0])

        setattr(noty, f"tournament_{labels[i]}", markers[i][1].tournament)

        points += markers[i][0]

    noty.points = points
    noty.save()

    return noty


def update_qual_points(team, season=settings.CURRENT_SEASON):
    if team.team_results.count() == 0:
        if season == settings.CURRENT_SEASON:
            team.delete()
        return

    for debater in team.debaters.all():
        results = (
            TeamResult.objects.filter(tournament__season=season)
            .filter(type_of_place=Debater.VARSITY)
            .filter(team__debaters=debater)
        )

        if not debater.school.included_in_oty:
            if season == settings.CURRENT_SEASON:
                QUAL.objects.filter(season=season, debater__school=debater.school).delete()
                QualPoints.objects.filter(
                    season=season, debater__school=debater.school
                ).delete()
                COTY.objects.filter(school=debater.school).delete()
            continue

        if season == settings.CURRENT_SEASON:
            QUAL.objects.filter(season=season, debater=debater).delete()

        qual = None
        if results.exists():
            latest_season = debater.latest_season
            current_season = int(settings.CURRENT_SEASON)
            if latest_season is None or int(latest_season) < current_season:
                debater.latest_season = settings.CURRENT_SEASON
                debater.save()
                
        for result in results:
            if result.place != -1 and result.place <= result.tournament.autoqual_bar:
                try:
                    qual = QUAL.objects.create(
                        season=season,
                        tournament=result.tournament,
                        qual_type=result.tournament.qual_type,
                        debater=debater,
                    )
                except:
                    pass

        results = results.filter(tournament__qual=True)

        markers = [
            (
                result.tournament.get_qual_points(
                    result.place, ghost_points=result.ghost_points
                ),
                result,
            )
            for result in results
        ]

        markers.sort(key=lambda marker: marker[0], reverse=True)

        points = sum([marker[0] for marker in markers])

        qual_points = (
            QualPoints.objects.filter(season=season).filter(debater=debater).first()
        )

        if points <= 0:
            if qual_points and not qual and season == settings.CURRENT_SEASON:
                qual_points.delete()
            continue

        if not qual_points:
            qual_points = QualPoints.objects.create(season=season, debater=debater)

        qual_points.points = points
        qual_points.save()

        if season in settings.ONLINE_SEASONS:
            continue

        if points >= settings.QUAL_BAR:
            qual = (
                QUAL.objects.filter(debater=debater)
                .filter(qual_type=QUAL.POINTS)
                .filter(season=season)
                .first()
            )

            if not qual:
                QUAL.objects.create(
                    debater=debater, season=season, qual_type=QUAL.POINTS
                )

    for debater in team.debaters.all():
        if not debater.school.included_in_oty:
            continue

        coty = COTY.objects.filter(season=season).filter(school=debater.school).first()

        if not coty:
            coty = COTY.objects.create(season=season, school=debater.school)

        relevant_qual_points = (
            QualPoints.objects.filter(season=season)
            .filter(debater__school=debater.school)
            .all()
        )

        relevant_qual_points = sum([min(60, q.points) for q in relevant_qual_points])

        qualled_debaters = [
            q.debater
            for q in QUAL.objects.filter(season=season)
            .filter(debater__school=debater.school)
            .all()
        ]
        qualled_debaters = len(list(set(qualled_debaters)))

        relevant_qual_points += qualled_debaters * 6

        coty.points = relevant_qual_points
        coty.save()


def redo_rankings(rankings, season=settings.CURRENT_SEASON, cache_type="toty"):
    handled_through_tie = []

    rankings = rankings.order_by("-points")

    place = 1

    for ranking in rankings:
        if ranking in handled_through_tie:
            continue

        if ranking.points == 0:
            ranking.delete()
            continue

        if rankings.filter(points=ranking.points).count() > 1:
            next_place = place
            for tied_ranking in rankings.filter(points=ranking.points):
                tied_ranking.place = place
                tied_ranking.tied = True
                tied_ranking.save()
                handled_through_tie += [tied_ranking]
                next_place += 1
            place = next_place - 1
        else:
            ranking.tied = False
            ranking.place = place
            ranking.save()

        place += 1

    key = make_template_fragment_key(cache_type, [season])
    print(f"CLEARING: {key} ({season})")
    cache.delete(key)
    client = Client()
    client.get(reverse("core:index") + f"?season={season}")


def update_online_quals(team, season=settings.CURRENT_SEASON):
    if team.team_results.count() == 0 and team.govs.count() == 0 and team.opps.count():
        team.delete()
        return

    if team.debaters.count() == 0:
        return

    for debater in team.debaters.all():
        results = (
            TeamResult.objects.filter(tournament__season=season)
            .filter(type_of_place=Debater.VARSITY)
            .filter(team__debaters=debater)
        )

        markers = [
            (result.tournament.get_online_qual_points(result.place), result)
            for result in results
        ]

        markers.sort(key=lambda marker: marker[0], reverse=True)

        online_qual = OnlineQUAL.objects.filter(season=season, debater=debater).first()

        if len(markers) == 0 or not debater.school.included_in_oty:
            if online_qual:
                online_qual.delete()
            continue

        if not online_qual:
            online_qual = OnlineQUAL.objects.create(season=season, debater=debater)

        labels = ["one", "two", "three", "four", "five", "six"]

        points = 0
        for i in range(len(markers)):
            if not markers[i][1].tournament:
                continue

            if i > 5:
                continue

            setattr(online_qual, f"marker_{labels[i]}", markers[i][0])

            setattr(online_qual, f"tournament_{labels[i]}", markers[i][1].tournament)

            points += markers[i][0]

        online_qual.points = points
        online_qual.save()

        if season not in settings.ONLINE_SEASONS:
            continue

        if points >= settings.ONLINE_QUAL_BAR:
            qual = (
                QUAL.objects.filter(debater=debater)
                .filter(qual_type=QUAL.POINTS)
                .filter(season=season)
                .first()
            )

            if not qual:
                QUAL.objects.create(
                    debater=debater, season=season, qual_type=QUAL.POINTS
                )

    for debater in team.debaters.all():
        if not debater.school.included_in_oty:
            continue

        coty = COTY.objects.filter(season=season).filter(school=debater.school).first()

        if not coty:
            coty = COTY.objects.create(season=season, school=debater.school)

        relevant_qual_points = (
            QualPoints.objects.filter(season=season)
            .filter(debater__school=debater.school)
            .all()
        )

        relevant_qual_points = sum([min(60, q.points) for q in relevant_qual_points])

        qualled_debaters = [
            q.debater
            for q in QUAL.objects.filter(season=season)
            .filter(debater__school=debater.school)
            .all()
        ]
        qualled_debaters = len(list(set(qualled_debaters)))

        relevant_qual_points += qualled_debaters * 6

        coty.points = relevant_qual_points
        coty.save()

    return True

def place_as_round(place):
        rounds = [
            (1, "1st"),
            (2, "2nd"),
            (4, "Semi-Finalist"),
            (8, "Quarter-Finalist"),
            (16, "Octo-Finalist"),
            (32, "Double Octo-Finalist"),
            (64, "Quadruple Octo-Finalist"),
        ]
        for cutoff, name in rounds:
            if place <= cutoff:
                return name
        return f"Top {place}"
