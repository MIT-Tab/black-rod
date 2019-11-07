from decimal import *

from django import template

from core.utils.rankings import get_relevant_debaters

register = template.Library()


@register.filter
def wl(round, team):
    gov_wins = [1, 3, 6]
    opp_wins = [2, 4, 6]

    if round.gov == team and round.victor in gov_wins:
        if round.victor == 3:
            return 'WF'
        if round.victor == 6:
            return 'AW'
        return 'W'

    if round.opp == team and round.victor in opp_wins:
        if round.victor == 4:
            return 'WF'
        if round.victor == 6:
            return 'AW'
        return 'W'

    if round.victor == 5:
        return 'AL'
    if round.victor > 2:
        return 'LF'
    return 'L'


@register.filter
def opponent(round, team):
    if round.gov == team:
        return round.opp
    return round.gov


@register.filter
def opponent_url(round, team):
    return opponent(round, team).get_absolute_url()


@register.filter
def opponent_side(round, team):
    if round.gov == team:
        return 'OPP'
    return 'GOV'


@register.filter
def number(num):
    num = Decimal(num).normalize()
    return num


@register.filter
def range_filter(start, end):
    return range(start, end)


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
    return get_relevant_debaters(school, season)

@register.filter
def partner_display(team, debater):
    partner = team.debaters.exclude(id=debater.id).first()

    if not partner:
        return 'NO PARTNER'
    return '<a href="%s">%s</a> (<a href="%s">%s</a>)' % (partner.get_absolute_url(),
                                                          partner.name,
                                                          partner.school.get_absolute_url(),
                                                          partner.school.name)

@register.filter
def partner_name(team, debater):
    partner = team.debaters.exclude(id=debater.id).first()

    if not partner:
        return 'NO PARTNER'
    return '<a href="%s">%s</a>' % (partner.get_absolute_url(),
                                    partner.name)

@register.filter
def school(team):
    return '<a href="%s">%s</a>' % (team.debaters.first().school.get_absolute_url(),
                                    team.debaters.first().school.name)
