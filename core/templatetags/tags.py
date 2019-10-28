from django import template

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
