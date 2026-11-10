"""Compute hypothetical DOTY standings without persisting standings or markers."""

from collections import defaultdict
from dataclasses import dataclass, field

from core.models import Debater, Reaff, SpeakerResult, TeamResult


@dataclass
class DOTYMarker:
    tournament: object
    soty_points: float = 0
    qual_points: float = 0

    @property
    def points(self):
        return self.soty_points + self.qual_points


@dataclass
class DOTYStanding:
    debater: Debater
    markers: list = field(default_factory=list)
    place: int = 0
    tied: bool = False

    @property
    def points(self):
        return sum(marker.points for marker in self.markers[:6])

    @property
    def counting_markers(self):
        return self.markers[:6] + [None] * max(0, 6 - len(self.markers))


def get_doty_standings(season):
    """Rank every eligible debater by their best six combined tournament markers.

    Each component follows its existing tournament flag and result eligibility.
    As in SOTY, a reaffirmed profile receives its old profile's season results.
    Only SELECT queries are used; no ranking update helpers are called.
    """
    reaffirmations = list(Reaff.objects.filter(season=season).order_by("pk"))
    old_ids = {reaff.old_debater_id for reaff in reaffirmations}
    transfers = {}
    handled_new_ids = set()
    for reaff in reaffirmations:
        # SOTY takes the first reaffirmation for each new profile.
        if reaff.new_debater_id not in handled_new_ids:
            transfers[reaff.old_debater_id] = reaff.new_debater_id
            handled_new_ids.add(reaff.new_debater_id)

    debaters = {
        debater.pk: debater
        for debater in Debater.all_objects.select_related("school").filter(
            school__included_in_oty=True
        ).exclude(pk__in=old_ids)
    }
    markers = defaultdict(dict)

    def get_marker(debater_id, tournament):
        recipient_id = transfers.get(debater_id, debater_id)
        if recipient_id not in debaters:
            return None
        return markers[recipient_id].setdefault(
            tournament.pk, DOTYMarker(tournament=tournament)
        )

    speakers = SpeakerResult.objects.filter(
        tournament__season=season, tournament__soty=True,
        type_of_place=Debater.VARSITY, counts_for_points=True,
    ).select_related("tournament")
    for result in speakers:
        marker = get_marker(result.debater_id, result.tournament)
        if marker is not None:
            marker.soty_points += result.tournament.get_soty_points(
                result.place - int(result.tie)
            )

    teams = TeamResult.objects.filter(
        tournament__season=season, tournament__qual=True,
        type_of_place=Debater.VARSITY, counts_for_points=True,
    ).select_related("tournament", "team").prefetch_related("team__debaters")
    for result in teams:
        points = result.tournament.get_qual_points(
            result.place, ghost_points=result.ghost_points
        )
        for debater in result.team.debaters.all():
            marker = get_marker(debater.pk, result.tournament)
            if marker is not None:
                marker.qual_points += points

    standings = []
    for debater_id, tournament_markers in markers.items():
        positive_markers = sorted(
            (marker for marker in tournament_markers.values() if marker.points > 0),
            key=lambda marker: (-marker.points, marker.tournament.date, marker.tournament.pk),
        )
        if positive_markers:
            standings.append(DOTYStanding(debaters[debater_id], positive_markers))
    standings.sort(key=lambda row: (-row.points, row.debater.name, row.debater.pk))
    totals = defaultdict(int)
    for row in standings:
        totals[row.points] += 1
    place = 0
    previous_points = None
    for index, row in enumerate(standings, start=1):
        if row.points != previous_points:
            place = index
        row.place = place
        row.tied = totals[row.points] > 1
        previous_points = row.points
    return standings
