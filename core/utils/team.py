from django.db.models import Count

from core.models.team import Team


def get_or_create_team_for_debaters(debater_one, debater_two):

    teams = Team.objects.filter(debaters=debater_one)
    if debater_one.pk == debater_two.pk:
        teams = Team.objects.annotate(member_count=Count("debaters")).filter(
            member_count=1, debaters=debater_one,
        )
    else:
        teams = teams.filter(debaters=debater_two)
    team = teams.first()

    if team:
        return team

    team = Team.objects.create()
    team.debaters.add(debater_one, debater_two)
    team.update_name()
    team.save(update_fields=["name"])

    return team
