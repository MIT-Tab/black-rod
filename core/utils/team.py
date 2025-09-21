from core.models.team import Team


def get_or_create_team_for_debaters(debater_one, debater_two):

    team = (
        Team.objects.filter(debaters=debater_one).filter(debaters=debater_two).first()
    )

    if team:
        return team

    team = Team.objects.create()
    team.debaters.add(debater_one)
    team.debaters.add(debater_two)

    team.save()

    team.update_name()
    team.save()

    return team
