import urllib.request

from django.conf import settings

from django.shortcuts import reverse

from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key

from core.models.debater import Debater, QualPoints

from core.models.results.team import TeamResult

from core.models.standings.toty import TOTY
from core.models.standings.soty import SOTY
from core.models.standings.noty import NOTY
from core.models.standings.coty import COTY
from core.models.standings.qual import QUAL
from core.models.standings.online_qual import OnlineQUAL


def get_relevant_debaters(school, season):
    qualled_debaters = [q.debater for q in QUAL.objects.filter(debater__school=school,
                                                               season=season)]

    qualled_debaters = list(set(qualled_debaters))

    qual_points = QualPoints.objects.filter(
        debater__school=school
    ).filter(
        season=season
    ).order_by(
        '-points'
    )

    to_return = []
    handled_debaters = []

    for qual_point in qual_points:
        if (qual_point.points > 0 or qual_point.debater in qualled_debaters):
            to_return += [qual_point]
            handled_debaters.append(qual_point.debater)

    for debater in qualled_debaters:
        if debater in handled_debaters:
            continue

        qual_point = QualPoints.objects.create(debater=debater,
                                               points=0,
                                               season=season)

        to_return += [qual_point]

    return to_return
    

def update_toty(team):
    if team.team_results.count() == 0 and team.govs.count() == 0 and team.opps.count():
        team.delete()
        return
    
    if team.hybrid:
        return

    if team.debaters.count() == 0:
        return

    if team.debaters.first() and \
       not team.debaters.first().school.included_in_oty:
        TOTY.objects.filter(
            season=settings.CURRENT_SEASON,
            team__debaters__school=team.debaters.first().school
        ).delete()
        return

    results = team.team_results.filter(
        tournament__season=settings.CURRENT_SEASON
    ).filter(
        tournament__toty=True
    ).filter(
        type_of_place=Debater.VARSITY
    )

    markers = [(result.tournament.get_toty_points(result.place), result) \
               for result in results]
    
    markers.sort(key=lambda marker: marker[0], reverse=True)
    
    toty = TOTY.objects.filter(
        season=settings.CURRENT_SEASON
    ).filter(
        team=team
    ).first()
    
    if len(markers) == 0:
        if toty:
            toty.delete()
        return
        
    if not toty:
        toty = TOTY.objects.create(
            season=settings.CURRENT_SEASON,
            team=team)

    labels = ['one', 'two', 'three', 'four', 'five']

    points = 0
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue

        if i > 4:
            continue
        
        setattr(toty,
                'marker_%s' % (labels[i],),
                markers[i][0])
        
        setattr(toty,
                'tournament_%s' % (labels[i],),
                markers[i][1].tournament)

        points += markers[i][0]

    toty.points = points
    toty.save()

    return toty


def update_soty(debater):
    if not any([team.team_results.count() > 0 or team.govs.count() > 0 or team.opps.count() > 0 for team in debater.teams.all()]) and \
       debater.speaker_results.count() == 0:
        debater.delete()
        return
    
    if not debater.school.included_in_oty:
        SOTY.objects.filter(
            season=settings.CURRENT_SEASON,
            debater__school=debater.school
        ).delete()
        return
    
    results = debater.speaker_results.filter(
            tournament__season=settings.CURRENT_SEASON
        ).filter(
            tournament__soty=True
        ).filter(
            type_of_place=Debater.VARSITY
        )

    markers = [(result.tournament.get_soty_points(result.place), result) \
               for result in results]
    
    markers.sort(key=lambda marker: marker[0], reverse=True)
    
    soty = SOTY.objects.filter(
        season=settings.CURRENT_SEASON
    ).filter(
        debater=debater
    ).first()
    
    if len(markers) == 0:
        if soty:
            soty.delete()
        return
        
    if not soty:
        soty = SOTY.objects.create(
            season=settings.CURRENT_SEASON,
            debater=debater)

    labels = ['one', 'two', 'three', 'four', 'five', 'six']

    points = 0
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue

        if i > 5:
            continue
        
        setattr(soty,
                'marker_%s' % (labels[i],),
                markers[i][0])
        
        setattr(soty,
                'tournament_%s' % (labels[i],),
                markers[i][1].tournament)

        points += markers[i][0]

    soty.points = points
    soty.save()

    return soty


def update_noty(debater):
    if not any([team.team_results.count() > 0 or team.govs.count() > 0 or team.opps.count() > 0 for team in debater.teams.all()]) and \
       debater.speaker_results.count() == 0:
        debater.delete()
        return
    
    if not debater.school.included_in_oty:
        NOTY.objects.filter(
            season=settings.CURRENT_SEASON,
            debater__school=debater.school
        ).delete()

        return
    
    results = debater.speaker_results.filter(
            tournament__season=settings.CURRENT_SEASON
        ).filter(
            tournament__noty=True
        ).filter(
            type_of_place=Debater.NOVICE
        )

    markers = [(result.tournament.get_noty_points(result.place), result) \
               for result in results]
    
    markers.sort(key=lambda marker: marker[0], reverse=True)
    
    noty = NOTY.objects.filter(
        season=settings.CURRENT_SEASON
    ).filter(
        debater=debater
    ).first()
    
    if len(markers) == 0:
        if noty:
            noty.delete()
        return
        
    if not noty:
        noty = NOTY.objects.create(
            season=settings.CURRENT_SEASON,
            debater=debater)

    labels = ['one', 'two', 'three', 'four', 'five']

    points = 0
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue

        if i > 4:
            continue
        
        setattr(noty,
                'marker_%s' % (labels[i],),
                markers[i][0])
        
        setattr(noty,
                'tournament_%s' % (labels[i],),
                markers[i][1].tournament)

        points += markers[i][0]

    noty.points = points
    noty.save()

    return noty


def update_qual_points(team):
    if team.team_results.count() == 0:
        team.delete()
        return
    
    for debater in team.debaters.all():
        results = TeamResult.objects.filter(
            tournament__season=settings.CURRENT_SEASON
        ).filter(
            type_of_place=Debater.VARSITY
        ).filter(
            team__debaters=debater
        )

        if not debater.school.included_in_oty:
            QUAL.objects.filter(
                season=settings.CURRENT_SEASON,
                debater__school=debater.school
            ).delete()

            QualPoints.objects.filter(
                season=settings.CURRENT_SEASON,
                debater__school=debater.school
            ).delete()

            COTY.objects.filter(
                school=debater.school
            ).delete()
            continue
        
        QUAL.objects.filter(
            season=settings.CURRENT_SEASON,
            debater=debater
        ).delete()

        qual = None

        for result in results:
            if result.place <= result.tournament.autoqual_bar:
                qual = QUAL.objects.create(season=settings.CURRENT_SEASON,
                                           tournament=result.tournament,
                                           qual_type=result.tournament.qual_type,
                                           debater=debater)

        results = results.filter(
            tournament__qual=True
        )

        markers = [(result.tournament.get_qual_points(result.place), result) \
                   for result in results]
        
        markers.sort(key=lambda marker: marker[0], reverse=True)
        
        points = sum([marker[0] for marker in markers])

        qual_points = QualPoints.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater=debater
        ).first()

        if settings.CURRENT_SEASON in settings.ONLINE_SEASONS:
            continue

        if points <= 0:
            if qual_points and not qual:
                qual_points.delete()

            continue
        
        if not qual_points:
            qual_points = QualPoints.objects.create(
                season=settings.CURRENT_SEASON,
                debater=debater)

        qual_points.points = points
        qual_points.save()
        
        if points >= settings.QUAL_BAR:
            qual = QUAL.objects.filter(
                debater=debater
            ).filter(
                qual_type=QUAL.POINTS
            ).filter(
                season=settings.CURRENT_SEASON
            ).first()
            
            if not qual:
                QUAL.objects.create(debater=debater,
                                    season=settings.CURRENT_SEASON,
                                    qual_type=QUAL.POINTS)

    for debater in team.debaters.all():
        if not debater.school.included_in_oty:
            continue

        coty = COTY.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            school=debater.school
        ).first()

        if not coty:
            coty = COTY.objects.create(season=settings.CURRENT_SEASON,
                                       school=debater.school)

        relevant_qual_points = QualPoints.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater__school=debater.school
        ).all()

        relevant_qual_points = sum([min(60, q.points) for q in relevant_qual_points])

        qualled_debaters = [q.debater for q in QUAL.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater__school=debater.school
        ).all()]
        qualled_debaters = len(list(set(qualled_debaters)))

        relevant_qual_points += qualled_debaters * 6

        coty.points = relevant_qual_points
        coty.save()


def redo_rankings(rankings, season=settings.CURRENT_SEASON, cache_type='toty'):
    handled_through_tie = []

    rankings = rankings.order_by(
        '-points'
    )

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
    print ('CLEARING: %s (%s)' % (key,season))
    cache.delete(key)

    contents = urllib.request.urlopen(settings.BASE_URL + reverse('core:index') + '?season=%s' % (season,)).read()

    
def update_online_quals(team):
    if team.team_results.count() == 0 and team.govs.count() == 0 and team.opps.count():
        team.delete()
        return
    
    if team.debaters.count() == 0:
        return

    for debater in team.debaters.all():
        results = TeamResult.objects.filter(
            tournament__season=settings.CURRENT_SEASON
        ).filter(
            type_of_place=Debater.VARSITY
        ).filter(
            team__debaters=debater
        )

        markers = [(result.tournament.get_online_qual_points(result.place), result) \
                   for result in results]
        
        markers.sort(key=lambda marker: marker[0], reverse=True)

        online_qual = OnlineQUAL.objects.filter(
            season=settings.CURRENT_SEASON,
            debater=debater
        ).first()
        
        if len(markers) == 0 or not debater.school.included_in_oty:
            if online_qual:
                online_qual.delete()
            continue
        
        if not online_qual:
            online_qual = OnlineQUAL.objects.create(
                season=settings.CURRENT_SEASON,
                debater=debater
            )

        labels = ['one', 'two', 'three', 'four', 'five', 'six']

        points = 0
        for i in range(len(markers)):
            if not markers[i][1].tournament:
                continue

            if i > 5:
                continue
        
            setattr(online_qual,
                    'marker_%s' % (labels[i],),
                    markers[i][0])
            
            setattr(online_qual,
                    'tournament_%s' % (labels[i],),
                    markers[i][1].tournament)

            points += markers[i][0]

        online_qual.points = points
        online_qual.save()
            
        if settings.CURRENT_SEASON not in settings.ONLINE_SEASONS:
            continue

        if points >= settings.ONLINE_QUAL_BAR:
            qual = QUAL.objects.filter(
                debater=debater
            ).filter(
                qual_type=QUAL.POINTS
            ).filter(
                season=settings.CURRENT_SEASON
            ).first()
            
            if not qual:
                QUAL.objects.create(debater=debater,
                                    season=settings.CURRENT_SEASON,
                                    qual_type=QUAL.POINTS)
            

    for debater in team.debaters.all():
        if not debater.school.included_in_oty:
            continue

        coty = COTY.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            school=debater.school
        ).first()

        if not coty:
            coty = COTY.objects.create(season=settings.CURRENT_SEASON,
                                       school=debater.school)

        relevant_qual_points = QualPoints.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater__school=debater.school
        ).all()

        relevant_qual_points = sum([min(60, q.points) for q in relevant_qual_points])

        qualled_debaters = [q.debater for q in QUAL.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater__school=debater.school
        ).all()]
        qualled_debaters = len(list(set(qualled_debaters)))

        relevant_qual_points += qualled_debaters * 6

        coty.points = relevant_qual_points
        coty.save()

        
        
    return True    
