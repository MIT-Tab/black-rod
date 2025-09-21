from decimal import Decimal

from django import template

from core.utils.rankings import get_qualled_debaters, place_as_round

register = template.Library()


@register.filter
def wl(round, team):
    gov_wins = [1, 3, 6]
    opp_wins = [2, 4, 6]

    if round.gov == team and round.victor in gov_wins:
        if round.victor == 3:
            return "WF"
        if round.victor == 6:
            return "AW"
        return "W"

    if round.opp == team and round.victor in opp_wins:
        if round.victor == 4:
            return "WF"
        if round.victor == 6:
            return "AW"
        return "W"

    if round.victor == 5:
        return "AL"
    if round.victor > 2:
        return "LF"
    return "L"


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
        return "OPP"
    return "GOV"


@register.filter
def number(num):
    num = Decimal(num).normalize()
    return num


@register.filter
def range_filter(start, end):
    return range(start, end)


@register.filter
def qual_display(debater, season):
    return ", ".join(
        [
            qual.get_qual_type_display()
            for qual in debater.quals.filter(season=season).all()
            if qual.qual_type > 0
        ]
    )


@register.filter
def qual_contribution(debater, season):
    points = debater.points

    if debater.qualled:
        points += 6

    return min(66, points)


@register.filter
def relevant_debaters(school, season):
    return get_qualled_debaters(school, season)


@register.filter
def partner_display(team, debater):
    partner = team.debaters.exclude(id=debater.id).first()

    if not partner:
        return "NO PARTNER"
    return f'<a href="{partner.get_absolute_url()}">{partner.name}</a> (<a href="{partner.school.get_absolute_url()}">{partner.school.name}</a>)'


@register.filter
def partner_name(team, debater):
    partner = team.debaters.exclude(id=debater.id).first()

    if not partner:
        return "NO PARTNER"
    return f'<a href="{partner.get_absolute_url()}">{partner.name}</a>'


@register.filter
def school(team):
    return f'<a href="{team.debaters.first().school.get_absolute_url()}">{team.debaters.first().school.name}</a>'


@register.filter
def years_on_team(current_season, first_season):
    """Calculate years on team: current_season - first_season + 1"""
    try:
        return int(current_season) - int(first_season) + 1
    except (ValueError, TypeError):
        return 0


@register.filter
def place_as_round_filter(place):
    """Convert numeric place to round name (e.g., 1 -> '1st', 4 -> 'Semi-Finalist')"""
    return place_as_round(place)


@register.filter
def form_field(form, field_name):
    """Access a form field by name so templates can look up fields dynamically."""
    if not form or not field_name:
        return ""
    try:
        return form[field_name]
    except Exception:
        return ""
