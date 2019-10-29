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


@register.filter
def partner_display(team, debater):
    partner = team.debaters.exclude(id=debater.id).first()
    return '<a href="%s">%s</a> (<a href="%s">%s</a>)' % (partner.get_absolute_url(),
                                                          partner.name,
                                                          partner.school.get_absolute_url,
                                                          partner.school.name)
