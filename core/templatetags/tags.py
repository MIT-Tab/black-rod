from django import template

from core.models import QualPoints

register = template.Library()


@register.filter
def qual_display(debater, season):
    return ', '.join([qual.get_qual_type_display() for qual in debater.quals.filter(season=season).all() if qual.qual_type > 0])


@register.filter
def qual_contribution(debater, season):
    points = debater.points

    if debater.qualled:
        points += 6

    return min(66,
               points)


@register.filter
def relevant_debaters(school, season):
    return QualPoints.objects.filter(
        debater__school=school
    ).filter(
        season=season
    ).filter(
        points__gt=0
    ).order_by(
        '-points'
    )


@register.filter
def partner_display(team, debater):
    partner = team.debaters.exclude(id=debater.id).first()
    return '<a href="%s">%s</a> (<a href="%s">%s</a>)' % (partner.get_absolute_url(),
                                                          partner.name,
                                                          partner.school.get_absolute_url,
                                                          partner.school.name)

@register.filter
def school(team):
    return '<a href="%s">%s</a>' % (team.debaters.first().school.get_absolute_url(),
                                    team.debaters.first().school.name)
