from django.db.models import Q

from core.models.round import Round


def get_record(tournament, team):
    gov_wins = [1, 3, 6]
    opp_wins = [2, 4, 6]

    num_wins = 0

    rounds = tournament.rounds.filter(Q(gov=team) | Q(opp=team))

    if not rounds.exists():
        return ""

    for round in rounds:
        if round.gov == team and round.victor in gov_wins:
            num_wins += 1
        if round.opp == team and round.victor in opp_wins:
            num_wins += 1

    return f"{num_wins} - {tournament.num_rounds - num_wins}"


def get_tab_card_data(team, tournament):
    if not team:
        return None

    speaker_one = team.debaters.first()
    speaker_two = team.debaters.last()

    rounds = Round.objects.filter(Q(gov=team) | Q(opp=team))

    if not rounds.exists():
        return None

    to_return = []

    print(rounds)

    for round in rounds.order_by("round_number").all():
        to_add = {
            "round": round,
            "stats": [
                round.stats.filter(debater=speaker_one).first(),
                round.stats.filter(debater=speaker_two).first(),
            ],
        }

        to_return += [to_add]

    return to_return
