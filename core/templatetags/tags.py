from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html

from core.utils.rankings import get_qualled_debaters, place_as_round

register = template.Library()


def _as_bool(value):
    """Coerce template values into booleans."""
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "y", "t"}
    return bool(value)


@register.filter
def get_item(dictionary, key):
    """Get an item from a dictionary in a template."""
    if not isinstance(dictionary, dict):
        return None
    return dictionary.get(key)


@register.filter
def attr(obj, attribute):
    """Get an attribute from an object in a template."""
    return getattr(obj, attribute, None)


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
def number(num, exponent=None):
    if num in {None, ""}:
        return ""

    try:
        decimal_value = Decimal(num)
    except (TypeError, ValueError, InvalidOperation):
        return ""

    if exponent is not None:
        try:
            quantizer = Decimal("1").scaleb(int(exponent))
            decimal_value = decimal_value.quantize(quantizer)
        except (ValueError, ArithmeticError, InvalidOperation):
            # Fall back to default normalization when the exponent is invalid.
            pass

    return decimal_value.normalize()


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
    if not team or getattr(team, "synthetic", False):
        return "NO PARTNER"
    partner = team.debaters.exclude(id=debater.id).exclude(synthetic=True).first()

    if not partner or getattr(partner, "synthetic", False):
        return "NO PARTNER"
    if not partner.school:
        return format_html('<a href="{}">{}</a>', partner.get_absolute_url(), partner.name)
    partner_link = format_html('<a href="{}">{}</a>', partner.get_absolute_url(), partner.name)
    school_link = format_html(
        '<a href="{}">{}</a>',
        partner.school.get_absolute_url(),
        partner.school.name,
    )
    return format_html('{} ({})', partner_link, school_link)


@register.filter
def partner_name(team, debater):
    if not team or getattr(team, "synthetic", False):
        return "NO PARTNER"
    partner = team.debaters.exclude(id=debater.id).exclude(synthetic=True).first()

    if not partner or getattr(partner, "synthetic", False):
        return "NO PARTNER"
    return format_html('<a href="{}">{}</a>', partner.get_absolute_url(), partner.name)


@register.filter
def school(team):
    current_school = team.debaters.first().school
    return format_html('<a href="{}">{}</a>', current_school.get_absolute_url(), current_school.name)


@register.filter
def years_on_team(current_season, first_season):
    """Calculate years on team: current_season - first_season + 1"""
    try:
        return int(current_season) - int(first_season) + 1
    except (ValueError, TypeError):
        return 0


@register.filter
def place_as_round_filter(place, ghost_points=False):
    """
    Convert numeric place to round name (e.g., 1 -> '1st', 4 -> 'Semi-Finalist').
    When ghost points are present, return the Alternate label instead.
    """
    if _as_bool(ghost_points):
        return "Alternate"

    try:
        place_value = int(place)
    except (TypeError, ValueError):
        return ""

    if place_value <= 0:
        return ""

    return place_as_round(place_value)


@register.filter
def form_field(form, field_name):
    """Access a form field by name so templates can look up fields dynamically."""
    if not form or not field_name:
        return ""
    try:
        return form[field_name]
    except Exception:
        return ""


@register.filter
def dict_item(dictionary, key):
    if not dictionary:
        return None
    return dictionary.get(key)
