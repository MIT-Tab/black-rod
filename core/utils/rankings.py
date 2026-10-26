from django.conf import settings
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db.models import Exists, OuterRef, Q

from core.live_results import invalidate_live_standings
from core.models.debater import Debater, QualPoints, Reaff
from core.models.results.team import TeamResult
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY, TOTYReaff
from core.models.team import Team
from core.models.tournament import Tournament


def _school_included_in_oty(debater):
    """Return True if the debater has a school and it is eligible for OTY."""
    school = getattr(debater, "school", None)
    return bool(school and getattr(school, "included_in_oty", False))


def get_qualled_debaters(school, season):
    season = str(season)
    qual_points = list(
        QualPoints.objects.filter(debater__school=school, season=season).order_by(
            "-points"
        )
    )

    qualled_debater_ids = set(
        QUAL.objects.filter(debater__school=school, season=season).values_list(
            "debater_id", flat=True
        )
    )

    to_return = []
    handled_debaters = set()

    for qual_point in qual_points:
        qualled = qual_point.debater_id in qualled_debater_ids
        if qual_point.points > 0 or qualled:
            to_return += [qual_point]
            handled_debaters.add(qual_point.debater_id)

    for debater_id in qualled_debater_ids:
        if debater_id in handled_debaters:
            continue

        # Display-only placeholder; do not persist from read paths.
        qual_point = QualPoints(debater_id=debater_id, points=0, season=season)

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

    first_debater = team.debaters.first()
    if first_debater and not _school_included_in_oty(first_debater):
        TOTY.objects.filter(
            season=season, team__debaters__school=first_debater.school
        ).delete()
        return

    results = (
        team.team_results.select_related("tournament").filter(tournament__season=season)
        .filter(tournament__toty=True)
        .filter(type_of_place=Debater.VARSITY)
        .filter(counts_for_points=True)
    )

    reaff = TOTYReaff.objects.filter(new_team=team).filter(season=season).all()
    if len(reaff) > 0:
        results = results | reaff[0].old_team.team_results.filter(
            tournament__season=season
        ).filter(tournament__toty=True).filter(type_of_place=Debater.VARSITY).filter(
            counts_for_points=True
        )

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

    if not _school_included_in_oty(debater):
        SOTY.objects.filter(season=season, debater=debater).delete()
        return

    results = (
        debater.speaker_results.select_related("tournament").filter(tournament__season=season)
        .filter(tournament__soty=True)
        .filter(type_of_place=Debater.VARSITY)
        .filter(counts_for_points=True)
    )

    reaff = Reaff.objects.filter(new_debater=debater).filter(season=season).all()
    if len(reaff) > 0:
        results = results | reaff[0].old_debater.speaker_results.select_related("tournament").filter(
            tournament__season=season
        ).filter(tournament__soty=True).filter(type_of_place=Debater.VARSITY).filter(
            counts_for_points=True
        )

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

    if not _school_included_in_oty(debater):
        NOTY.objects.filter(season=season, debater=debater).delete()

        return

    results = (
        debater.speaker_results.select_related("tournament").filter(tournament__season=season)
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


def update_qual_points(team, season=settings.CURRENT_SEASON, delete_empty_team=True):
    season_token = str(season).split("-", maxsplit=1)[0]
    is_online_season = season_token in {str(value).split("-", maxsplit=1)[0] for value in settings.ONLINE_SEASONS}
    if team.team_results.count() == 0:
        if delete_empty_team and season == settings.CURRENT_SEASON:
            team.delete()
        return

    for debater in team.debaters.all():
        all_results = (
            TeamResult.objects.select_related("tournament").filter(tournament__season=season)
            .filter(type_of_place=Debater.VARSITY)
            .filter(team__debaters=debater)
        )
        scoring_results = all_results.filter(counts_for_points=True)

        if not _school_included_in_oty(debater):
            if season == settings.CURRENT_SEASON:
                QUAL.objects.filter(season=season, debater__school=debater.school).delete()
                QualPoints.objects.filter(
                    season=season, debater__school=debater.school
                ).delete()
                COTY.objects.filter(season=season, school=debater.school).delete()
            continue

        if season == settings.CURRENT_SEASON:
            QUAL.objects.filter(season=season, debater=debater).delete()

        qual = None
        if all_results.exists():
            latest_season = debater.latest_season
            target_season = int(season_token)
            latest_season_token = (
                str(latest_season).split("-", maxsplit=1)[0] if latest_season is not None else None
            )
            if latest_season_token is None or int(latest_season_token) < target_season:
                debater.latest_season = season_token
                debater.save()

        for result in scoring_results:
            if (
                result.tournament.qual_type != Tournament.NATIONALS
                and result.place != -1
                and result.place <= result.tournament.autoqual_bar
            ):
                qual, created = QUAL.objects.get_or_create(
                    season=season,
                    debater=debater,
                    qual_type=result.tournament.qual_type,
                    defaults={"tournament": result.tournament},
                )
                if not created and result.tournament and not qual.tournament:
                    qual.tournament = result.tournament
                    qual.save(update_fields=["tournament"])

        results = scoring_results.filter(tournament__qual=True)

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

        if is_online_season:
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

    if is_online_season:
        for debater in team.debaters.all():
            school = getattr(debater, "school", None)
            if school is not None:
                COTY.objects.filter(season=season, school=school).delete()
        return

    for debater in team.debaters.all():
        if not _school_included_in_oty(debater):
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
    rows = list(rankings.exclude(points=0).order_by("-points", "pk"))
    rankings.filter(points=0).delete()
    previous_points = None
    previous_place = 0
    for index, row in enumerate(rows):
        if row.points != previous_points:
            previous_place = index + 1
        row.place = previous_place
        row.tied = ((index > 0 and rows[index - 1].points == row.points)
                    or (index + 1 < len(rows) and rows[index + 1].points == row.points))
        previous_points = row.points
    if rows:
        rankings.model.objects.bulk_update(rows, ["place", "tied"], batch_size=500)
    cache.delete(make_template_fragment_key(cache_type, [season]))
    invalidate_live_standings(season)


def rebuild_coty_related_rankings(season=settings.CURRENT_SEASON):
    """
    Recompute season-wide COTY/qual state.

    COTY is not safely incremental because update_qual_points deletes and rebuilds
    season qual state per debater. After any tournament import, we need a full
    rebuild to avoid leaving COTY rows or qual point totals in a partial state.
    """
    season = str(season)
    teams = Team.objects.annotate(
        has_results=Exists(
            TeamResult.objects.filter(team=OuterRef("pk"), tournament__season=season)
        ),
        has_qual_points=Exists(
            QualPoints.objects.filter(
                debater__teams=OuterRef("pk"), season=season
            )
        ),
        has_quals=Exists(
            QUAL.objects.filter(debater__teams=OuterRef("pk"), season=season)
        ),
        has_online_quals=Exists(
            OnlineQUAL.objects.filter(debater__teams=OuterRef("pk"), season=season)
        ),
    ).filter(
        Q(has_results=True)
        | Q(has_qual_points=True)
        | Q(has_quals=True)
        | Q(has_online_quals=True)
    )
    teams = teams.prefetch_related("debaters")
    for team in teams:
        update_qual_points(team, season=season, delete_empty_team=False)
        update_online_quals(team, season=season, delete_empty_team=False)

    redo_rankings(
        COTY.objects.filter(season=season),
        season=season,
        cache_type="coty",
    )
    redo_rankings(
        OnlineQUAL.objects.filter(season=season),
        season=season,
        cache_type="online_quals",
    )


def update_online_quals(team, season=settings.CURRENT_SEASON, delete_empty_team=True):
    season_token = str(season).split("-", maxsplit=1)[0]
    is_online_season = season_token in {str(value).split("-", maxsplit=1)[0] for value in settings.ONLINE_SEASONS}
    if (
        team.team_results.count() == 0
        and team.govs.count() == 0
        and team.opps.count() == 0
    ):
        if delete_empty_team:
            team.delete()
        return

    if team.debaters.count() == 0:
        return

    for debater in team.debaters.all():
        results = (
            TeamResult.objects.select_related("tournament").filter(tournament__season=season)
            .filter(type_of_place=Debater.VARSITY)
            .filter(team__debaters=debater)
            .filter(counts_for_points=True)
        )

        markers = [
            (result.tournament.get_online_qual_points(result.place), result)
            for result in results
        ]

        markers.sort(key=lambda marker: marker[0], reverse=True)

        online_qual = OnlineQUAL.objects.filter(season=season, debater=debater).first()

        if len(markers) == 0 or not _school_included_in_oty(debater):
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

        if not is_online_season:
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

    if is_online_season:
        for debater in team.debaters.all():
            school = getattr(debater, "school", None)
            if school is not None:
                COTY.objects.filter(season=season, school=school).delete()

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


def rebuild_import_qualifications(season=settings.CURRENT_SEASON):
    """Rebuild season qualification state with bulk reads and writes.

    Each debater is computed once, even when they have several partnerships.
    This also removes stale qualifications after corrections or deleted results.
    """
    from collections import defaultdict  # pylint: disable=import-outside-toplevel
    from django.db import transaction  # pylint: disable=import-outside-toplevel
    from django.db.models import Prefetch  # pylint: disable=import-outside-toplevel

    season = str(season)
    online = season in {str(value).split("-", 1)[0] for value in settings.ONLINE_SEASONS}
    results_by_debater = defaultdict(list)
    people = {}
    results = TeamResult.objects.filter(
        tournament__season=season, type_of_place=Debater.VARSITY,
    ).select_related("tournament", "team").prefetch_related(Prefetch(
        "team__debaters", queryset=Debater.objects.select_related("school"),
    ))
    for result in results:
        for debater in result.team.debaters.all():
            people[debater.pk] = debater
            if result.counts_for_points:
                results_by_debater[debater.pk].append(result)
    # Standalone qualifications entered without scoring records are not derived
    # season state. Keep them while replacing computed qualifications.
    standalone = list(QUAL.objects.filter(season=season).exclude(
        debater_id__in=people,
    ).exclude(debater_id__in=QualPoints.objects.filter(season=season).values("debater_id"))
      .exclude(debater_id__in=OnlineQUAL.objects.filter(season=season).values("debater_id"))
      .select_related("debater__school"))
    quals, points_rows, online_rows, latest_updates = [], [], [], []
    school_totals = defaultdict(float)
    labels = ("one", "two", "three", "four", "five", "six")
    for debater_id, debater in people.items():
        if not debater.latest_season or int(debater.latest_season.split("-", 1)[0]) < int(season):
            debater.latest_season = season
            latest_updates.append(debater)
        if not _school_included_in_oty(debater):
            continue
        scoring = results_by_debater[debater_id]
        autoquals = {}
        for result in scoring:
            tournament = result.tournament
            if (tournament.qual_type != Tournament.NATIONALS
                    and 0 < result.place <= tournament.autoqual_bar):
                autoquals.setdefault(tournament.qual_type, tournament.pk)
        points = sum(result.tournament.get_qual_points(result.place, ghost_points=result.ghost_points)
                     for result in scoring if result.tournament.qual)
        if points > 0:
            points_rows.append(QualPoints(season=season, debater_id=debater_id, points=points))
        markers = sorted(((result.tournament.get_online_qual_points(result.place), result)
                          for result in scoring), key=lambda marker: marker[0], reverse=True)[:6]
        online_points = sum(marker[0] for marker in markers)
        if markers:
            row = OnlineQUAL(season=season, debater_id=debater_id, points=online_points)
            for label, (value, result) in zip(labels, markers):
                setattr(row, f"marker_{label}", value)
                setattr(row, f"tournament_{label}_id", result.tournament_id)
            online_rows.append(row)
        if (online_points >= settings.ONLINE_QUAL_BAR if online else points >= settings.QUAL_BAR):
            autoquals.setdefault(QUAL.POINTS, None)
        quals.extend(QUAL(season=season, debater_id=debater_id, qual_type=kind,
                          tournament_id=tournament_id) for kind, tournament_id in autoquals.items())
        if not online:
            school_totals[debater.school_id] += min(60, points) + (6 if autoquals else 0)
    standalone_people = set()
    for qual in standalone:
        qual.pk = None
        quals.append(qual)
        if not online and qual.debater_id not in standalone_people and _school_included_in_oty(qual.debater):
            school_totals[qual.debater.school_id] += 6
            standalone_people.add(qual.debater_id)
    with transaction.atomic():
        for model in (QUAL, QualPoints, OnlineQUAL, COTY):
            model.objects.filter(season=season).delete()
        QUAL.objects.bulk_create(quals, batch_size=500)
        QualPoints.objects.bulk_create(points_rows, batch_size=500)
        OnlineQUAL.objects.bulk_create(online_rows, batch_size=500)
        COTY.objects.bulk_create([COTY(season=season, school_id=school_id, points=points)
                                  for school_id, points in school_totals.items() if points > 0])
        if latest_updates:
            Debater.all_objects.bulk_update(latest_updates, ["latest_season"], batch_size=500)
    redo_rankings(COTY.objects.filter(season=season), season=season, cache_type="coty")
    redo_rankings(OnlineQUAL.objects.filter(season=season), season=season, cache_type="online_quals")
