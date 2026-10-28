"""Visibility rules for public debater and team profiles only.

Keep the default result managers unchanged so scoring and administrative tools
continue to see the complete competition record.
"""

from contextlib import contextmanager

from django.db.models import Q

from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.round import Round
from core.utils.rounds import visible_canonical_rounds


class DebaterProfileResults:
    """Share profile filtering and partner redaction between HTML and API views."""

    def __init__(self, debater):
        self.debater = debater
        results = TeamResult.objects.filter(team__debaters=debater, team__synthetic=False)
        self.team_results = results.exclude(hidden_from_debaters=debater)
        self.speaker_results = debater.speaker_results.filter(hidden_from_profile=False)
        self.redacted_pairs = set(
            results.filter(hidden_from_debaters__isnull=False)
            .values_list("team_id", "tournament_id")
        )
        self.redacted_team_ids = {team_id for team_id, _ in self.redacted_pairs}

    def can_show_team(self, team, tournament):
        return team is not None and (team.pk, tournament.pk) not in self.redacted_pairs

    def rounds(self):
        rounds = visible_canonical_rounds(
            Round.objects.filter(
                Q(gov__debaters=self.debater, gov__synthetic=False)
                | Q(opp__debaters=self.debater, opp__synthetic=False)
            )
        )
        # A hidden award must not reappear as a participation row or tab card.
        for team_id, tournament_id in self.redacted_pairs:
            rounds = rounds.exclude(
                Q(tournament_id=tournament_id) & (Q(gov_id=team_id) | Q(opp_id=team_id))
            )
        return rounds

    def teams(self):
        # The partner sidebar must not reveal a pairing redacted from a result.
        return self.debater.teams.filter(synthetic=False).exclude(pk__in=self.redacted_team_ids)


def team_profile_results(team):
    """Return awards and round participation safe to show on a team profile."""
    results = team.team_results.filter(hidden_from_debaters__isnull=True)
    hidden_tournaments = team.team_results.filter(
        hidden_from_debaters__isnull=False
    ).values("tournament_id")
    rounds = visible_canonical_rounds(
        Round.objects.filter(Q(gov=team) | Q(opp=team))
    ).exclude(tournament_id__in=hidden_tournaments)
    return results, rounds


@contextmanager
def preserve_profile_result_visibility(tournament):
    """Keep admin visibility choices when data entry replaces tournament results."""
    hidden_members = {}
    for result in tournament.team_results.filter(
        hidden_from_debaters__isnull=False
    ).distinct().prefetch_related("hidden_from_debaters"):
        hidden_members[(result.team_id, result.type_of_place)] = set(
            member.pk for member in result.hidden_from_debaters.all()
        )
    hidden_speakers = set(
        tournament.speaker_results.filter(hidden_from_profile=True)
        .values_list("debater_id", "type_of_place")
    )

    yield

    for result in tournament.team_results.all():
        members = hidden_members.get((result.team_id, result.type_of_place))
        if members:
            result.hidden_from_debaters.set(members)
    for debater_id, type_of_place in hidden_speakers:
        SpeakerResult.objects.filter(
            tournament=tournament, debater_id=debater_id, type_of_place=type_of_place
        ).update(hidden_from_profile=True)
