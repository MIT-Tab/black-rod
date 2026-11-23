from django.db.models import Count

from core.models.team import Team


def get_or_create_team_for_debaters(debater_one, debater_two=None):
    if debater_two is None:
        team = (
            Team.objects.alias(roster_size=Count("debaters"))
            .filter(debaters=debater_one, roster_size=1)
            .first()
        )
    else:
        team = Team.objects.filter(debaters=debater_one).filter(debaters=debater_two).first()

    if team:
        return team

    team = Team.objects.create()
    members = [debater_one] if debater_two is None else [debater_one, debater_two]
    team.debaters.add(*members)
    team.update_name()
    team.save(update_fields=["name"])

    return team
