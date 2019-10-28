from django.conf import settings

from core.models.debater import Debater, QualPoints

from core.models.standings.toty import TOTY
from core.models.standings.soty import SOTY
from core.models.standings.noty import NOTY
from core.models.standings.coty import COTY
from core.models.standings.qual import QUAL

def update_toty(team):
    num_schools = len(list(set([d.school for d in team.debaters.all()])))

    if not team.debaters.first().school.included_in_oty:
        return

    if num_schools > 1:
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

    points = sum([marker[0] for marker in markers])
    toty.points = points
    
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue
        
        setattr(toty,
                'marker_%s' % (labels[i],),
                markers[i][0])
        
        setattr(toty,
                'tournament_%s' % (labels[i],),
                markers[i][1].tournament)
        
    toty.save()

    return toty


def update_soty(debater):
    if not debater.school.included_in_oty:
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

    points = sum([marker[0] for marker in markers])
    soty.points = points
    
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue
        
        setattr(soty,
                'marker_%s' % (labels[i],),
                markers[i][0])
        
        setattr(soty,
                'tournament_%s' % (labels[i],),
                markers[i][1].tournament)
        
    soty.save()

    return soty


def update_noty(debater):
    if not debater.school.included_in_oty:
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

    labels = ['one', 'two', 'three', 'four', 'five', 'six']

    points = sum([marker[0] for marker in markers])
    noty.points = points
    
    for i in range(len(markers)):
        if not markers[i][1].tournament:
            continue
        
        setattr(noty,
                'marker_%s' % (labels[i],),
                markers[i][0])
        
        setattr(noty,
                'tournament_%s' % (labels[i],),
                markers[i][1].tournament)
        
    noty.save()

    return noty


def update_qual_points(team):
    results = team.team_results.filter(
        tournament__season=settings.CURRENT_SEASON
    ).filter(
        tournament__qual=True
    ).filter(
        type_of_place=Debater.VARSITY
    )

    markers = [(result.tournament.get_qual_points(result.place), result) \
               for result in results]
    
    markers.sort(key=lambda marker: marker[0], reverse=True)

    for debater in team.debaters.all():
        if not debater.school.included_in_oty:
            continue

        coty = QualPoints.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater=debater
        ).first()
    
        if not coty:
            coty = QualPoints.objects.create(
                season=settings.CURRENT_SEASON,
                debater=debater)

        points = sum([marker[0] for marker in markers])
        coty.points = points

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
    
        coty.save()

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

        relevant_qual_points = sum([q.points for q in relevant_qual_points])

        qualled_debaters = [q.debater for q in QUAL.objects.filter(
            season=settings.CURRENT_SEASON
        ).filter(
            debater__school=debater.school
        ).all()]
        qualled_debaters = len(list(set(qualled_debaters)))

        relevant_qual_points += qualled_debaters * 6

        coty.points = relevant_qual_points
        coty.save()


def redo_rankings(rankings):
    rankings = rankings.order_by(
        '-points'
    )

    place = 1

    for ranking in rankings:
        ranking.place = place
        ranking.save()

        place += 1
