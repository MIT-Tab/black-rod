"""Build playable clues and their answer sets from public APDA records."""

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations, pairwise
from math import log2

from core.models import (
    Debater, ImmaculateGridHiddenDebater, QualPoints, Round, School, SOTY, SpeakerResult,
    Team, TeamResult, TOTY, Tournament,
)
from core.utils.elo_runtime_engine.constants import normalize_school_name, season_to_int
from core.utils.rounds import visible_canonical_rounds

DEBATER_ACTIVITY_YEARS = 4
HOST_ACTIVITY_YEARS = 3
# Keep current and very recent careers close together, then let age start
# offsetting career achievements and attendance from the third season away.
AGE_POINTS = ((0, 0), (2, 1), (3, 12), (4, 29), (6, 49), (10, 74), (13, 87), (17, 94), (18, 101), (24, 117))
# Qualification and speaker points have a wider combined scale than SOTY/TOTY.
POINTS_FOR_HALF_DISCOUNT = 120

ELIMINATION_PLACES = {
    1: "Won",
    2: "Finals or better",
    4: "Semis or better",
    8: "Quarters or better",
}
EVENT_TYPES = {
    Tournament.PROAMS: "Pro-Am",
    Tournament.ONLINE_PROAMS_POINTS: "Pro-Am",
    Tournament.NOVICE: "Novice",
    Tournament.BIPOC: "BIPOC",
    Tournament.GENDER_MINORITY: "Gender Minority",
    Tournament.ONLINE_GM_POINTS: "Gender Minority",
    Tournament.NATIONALS: "Nationals",
}


def person_key(debater_id, alias_group_id):
    return f"a{alias_group_id}" if alias_group_id else f"d{debater_id}"


def hidden_people():
    rows = list(ImmaculateGridHiddenDebater.objects.values_list("debater_id", "debater__alias_group_id"))
    groups = {group for _, group in rows if group}
    linked = Debater.all_objects.filter(alias_group_id__in=groups).values_list("pk", flat=True)
    return {f"d{pk}" for pk, _ in rows} | {f"a{group}" for group in groups} | {f"d{pk}" for pk in linked}


def _age_points(seasons_since_active):
    age = max(0, seasons_since_active)
    for (first, low), (last, high) in pairwise(AGE_POINTS):
        if age <= last:
            return low + (high - low) * (age - first) / (last - first)
    return AGE_POINTS[-1][1]


def rarity_score(seasons_since_active, points, prestige, career_span=0, attended_tournaments=0):
    """Estimate rarity in display points before clue-specific adjustments."""
    standings_discount = 70 * points / (points + POINTS_FOR_HALF_DISCOUNT)
    career_discount = min(12, max(0, career_span) * 1.5)
    # Frequent appearances make recent names especially familiar. Fade the
    # extra discount between four and eight seasons after their last season.
    recent_weight = max(0, min(1, (8 - seasons_since_active) / 4))
    activity = log2(1 + attended_tournaments / 5)
    activity_discount = 3 * activity + recent_weight * min(10, 5.5 * activity)
    prestige_discount = 0.45 * prestige
    return (
        64 + _age_points(seasons_since_active)
        - standings_discount - career_discount - activity_discount - prestige_discount
    )


@dataclass
class Clue:
    key: str
    label: str
    topic: str
    people: set = field(default_factory=set)
    subject: str | None = None
    latest_host_result: date | None = None
    occurrences: dict = field(default_factory=dict)

    def frequency(self, person):
        return max(1, len(self.occurrences.get(person, ())))

    @property
    def kind(self):
        category, _, variant = self.key.partition(":")
        if category in ("host", "event"):
            return ("placement", variant.rsplit(":", 1)[-1])
        return (category, "")

    def display(self):
        result = {"key": self.key, "label": self.label}
        if self.subject is not None:
            result["subject"] = self.subject
        return result


@dataclass
class Catalog:
    aliases: dict
    clues: dict = field(default_factory=dict)
    rarity: dict = field(default_factory=dict)
    recent_people: set = field(default_factory=set)
    progress: dict = field(default_factory=dict)

    def measure(self, topic, person, value):
        if topic in self.progress:
            measurements = self.progress[topic]
            measurements[person] = max(value, measurements.get(person, value))

    def add(self, key, label, person, topic=None, subject=None, host_result_date=None, occurrences=()):
        if key not in self.clues:
            self.clues[key] = Clue(key, label, topic or key, subject=subject)
        clue = self.clues[key]
        clue.people.add(person)
        clue.occurrences.setdefault(person, set()).update(occurrences)
        if host_result_date is not None:
            clue.latest_host_result = max(host_result_date, clue.latest_host_result or host_result_date)


@dataclass
class Career:
    name: str
    schools: set = field(default_factory=set)
    school_tournaments: dict = field(default_factory=dict)
    attended_tournaments: set = field(default_factory=set)
    first_season: int | None = None
    latest_season: int | None = None
    latest_affiliated: int | None = None
    latest_tournament: date | None = None
    points: dict = field(default_factory=dict)
    prestige: int = 0
    top_ten: bool = False
    winning_speaker_divisions: set = field(default_factory=set)
    winning_speaker_tournaments: set = field(default_factory=set)
    varsity_seasons: set = field(default_factory=set)
    speaker_seasons: set = field(default_factory=set)
    finalist_seasons: set = field(default_factory=set)
    semifinal_hosts_by_season: dict = field(default_factory=dict)
    placed_partners: set = field(default_factory=set)
    partner_schools: set = field(default_factory=set)

    def record_season(self, first, latest, affiliated):
        if first is not None:
            self.first_season = min(first, self.first_season or first)
        if latest is not None:
            self.latest_season = max(latest, self.latest_season or latest)
            if affiliated:
                self.latest_affiliated = max(latest, self.latest_affiliated or latest)

    def record_tournament(self, season, day, school_id, tournament_id):
        year = season_to_int(season)
        self.record_season(year, year, school_id is not None)
        self.latest_tournament = max(day, self.latest_tournament or day)
        if school_id is not None:
            self.school_tournaments.setdefault(school_id, set()).add(tournament_id)
        return year

    def record_points(self, source, season, points):
        key = (source, season)
        self.points[key] = max(self.points.get(key, 0), points)

    def record_standing(self, place):
        self.prestige = max(self.prestige, 11 - place)
        self.top_ten = self.top_ten or place <= 10


def _season_label(season):
    return f"{season}–{(season + 1) % 100:02d}"


def _schools():
    return {
        pk: name
        for pk, name in School.objects.filter(synthetic=False).values_list("pk", "name")
        if normalize_school_name(name) not in ("", "unaffiliated")
    }


def _profiles(year, schools):
    aliases, careers, affiliations = {}, {}, {}
    rows = Debater.objects.order_by("pk").values_list(
        "pk", "alias_group_id", "first_name", "last_name", "school_id", "first_season", "latest_season"
    )
    for pk, group, first, last, school, started, latest in rows.iterator():
        person = person_key(pk, group)
        aliases[pk] = person
        if person not in careers:
            careers[person] = Career(f"{first} {last}".strip())
        career = careers[person]
        if school in schools:
            career.schools.add(school)
            affiliations[pk] = school
        first_year, last_year = season_to_int(started), season_to_int(latest)
        career.record_season(
            first_year if first_year and first_year <= year else None,
            last_year if last_year and last_year <= year else None,
            pk in affiliations,
        )
    return aliases, careers, affiliations


def _team_members():
    teams = defaultdict(list)
    memberships = Team.debaters.through.objects.filter(
        team__synthetic=False, debater__in=Debater.objects.all()
    ).values_list("team_id", "debater_id")
    for team, debater in memberships.iterator():
        teams[team].append(debater)
    return teams


def _placing_clues(catalog, person, place, host, kind, schools, event_day, tournament_id):
    if host in schools:
        catalog.measure(f"host:{host}", person, -place)
    if kind in EVENT_TYPES:
        catalog.measure(f"event:{EVENT_TYPES[kind]}", person, -place)
    for cutoff, label in ELIMINATION_PLACES.items():
        if place <= cutoff:
            if host in schools:
                catalog.add(
                    f"host:{host}:{cutoff}",
                    f"Won a tournament hosted by {schools[host]}" if cutoff == 1
                    else f"{label} at a tournament hosted by {schools[host]}",
                    person, topic=f"host:{host}", host_result_date=event_day, occurrences=(tournament_id,),
                )
            if kind in EVENT_TYPES:
                event = EVENT_TYPES[kind]
                event_label = "Nationals" if kind == Tournament.NATIONALS else f"a {event} tournament"
                catalog.add(
                    f"event:{event}:{cutoff}",
                    f"{label} {event_label}" if cutoff == 1 else f"{label} at {event_label}",
                    person, topic=f"event:{event}", occurrences=(tournament_id,),
                )


def _record_partnership(catalog, careers, members, affiliations, placing, partners, tournament_id):
    for left, right in combinations(members, 2):
        first, second = catalog.aliases[left], catalog.aliases[right]
        if first == second:
            continue
        partners[first][second].add(tournament_id)
        partners[second][first].add(tournament_id)
        if not placing:
            continue
        for person, partner, partner_profile in ((first, second, right), (second, first, left)):
            careers[person].placed_partners.add(partner)
            if partner_profile in affiliations:
                careers[person].partner_schools.add(affiliations[partner_profile])


def _record_team_results(day, catalog, careers, schools, affiliations, teams, redacted, profile_ids=None):
    partners = defaultdict(lambda: defaultdict(set))
    rows = TeamResult.objects.filter(
        tournament__date__lte=day, team__synthetic=False, hidden_from_debaters__isnull=True
    ).values_list(
        "team_id", "place", "type_of_place", "tournament__season", "tournament__date",
        "tournament__host_id", "tournament__qual_type", "tournament_id",
    )
    if profile_ids is not None:
        rows = rows.filter(team__debaters__in=profile_ids).distinct()
    for team, place, division, season, event_day, host, kind, tournament_id in rows.iterator():
        members = teams[team]
        placing = place > 0
        if team not in redacted:
            _record_partnership(catalog, careers, members, affiliations, placing, partners, tournament_id)
        hybrid = len({affiliations[pk] for pk in members if pk in affiliations}) > 1
        for pk in members:
            person = catalog.aliases[pk]
            career = careers[person]
            year = career.record_tournament(season, event_day, affiliations.get(pk), tournament_id)
            career.attended_tournaments.add(tournament_id)
            if not placing:
                continue
            _placing_clues(catalog, person, place, host, kind, schools, event_day, tournament_id)
            if hybrid and team not in redacted:
                catalog.add("hybrid", "Placed on a hybrid team", person, occurrences=(tournament_id,))
            if division == TeamResult.VARSITY and year is not None:
                career.varsity_seasons.add(year)
                if place <= 4 and host in schools:
                    career.semifinal_hosts_by_season.setdefault(year, set()).add(host)
                if place <= 2:
                    career.finalist_seasons.add(year)
                # Later outrounds and Nationals runs raise a debater's prominence.
                prominence = (9 - place) * (2 if kind == Tournament.NATIONALS else 1)
                career.prestige = max(career.prestige, prominence)
    return partners


def _record_speaker_results(day, catalog, careers, schools, affiliations, profile_ids=None):
    rows = SpeakerResult.objects.filter(
        tournament__date__lte=day, place__gt=0, hidden_from_profile=False, debater__in=Debater.objects.all()
    ).values_list(
        "debater_id", "place", "tie", "type_of_place", "tournament__season",
        "tournament__date", "tournament__host_id", "tournament__qual_type", "tournament_id",
    )
    if profile_ids is not None:
        rows = rows.filter(debater_id__in=profile_ids)
    for pk, place, tie, division, season, event_day, host, kind, tournament_id in rows.iterator():
        person = catalog.aliases[pk]
        career = careers[person]
        year = career.record_tournament(season, event_day, affiliations.get(pk), tournament_id)
        career.attended_tournaments.add(tournament_id)
        rank = max(1, place - int(tie))
        if rank == 1:
            career.winning_speaker_divisions.add(division)
            career.winning_speaker_tournaments.add((tournament_id, division))
        if division != SpeakerResult.VARSITY:
            continue
        if year is not None:
            career.varsity_seasons.add(year)
            career.speaker_seasons.add(year)
        catalog.measure("top_speaker", person, -rank)
        if rank == 1:
            catalog.add("top_speaker", "Won at least one varsity top speak", person, occurrences=(tournament_id,))
        if host in schools:
            catalog.measure(f"speaker:{host}", person, -rank)
            for cutoff in (5, 10):
                if rank <= cutoff:
                    catalog.add(
                        f"speaker:{host}:{cutoff}", f"Top-{cutoff} varsity speaker at a tournament hosted by {schools[host]}",
                        person, topic=f"host:{host}", host_result_date=event_day, occurrences=(tournament_id,),
                    )
        prominence = (11 - rank) * (2 if kind == Tournament.NATIONALS else 1)
        career.prestige = max(career.prestige, prominence)


def _record_qual_points(year, catalog, careers):
    rows = QualPoints.objects.filter(season__lte=str(year), points__gt=0).values_list(
        "debater_id", "season", "points"
    )
    for pk, season, points in rows.iterator():
        person = catalog.aliases.get(pk)
        if person is not None:
            careers[person].record_points("QUAL", season, points)


def _record_standings(year, catalog, careers, teams, redacted, profile_ids=None):
    if profile_ids is None:
        _record_qual_points(year, catalog, careers)

    for model, award, member_field in ((SOTY, "SOTY", "debater_id"), (TOTY, "TOTY", "team_id")):
        rows = model.objects.filter(season__lte=str(year), place__gt=0).values_list(
            member_field, "season", "place", "points"
        )
        if profile_ids is not None:
            lookup = "debater_id__in" if award == "SOTY" else "team__debaters__in"
            rows = rows.filter(**{lookup: profile_ids}).distinct()
        for pk, season, place, points in rows.iterator():
            members = (pk,) if award == "SOTY" else (() if pk in redacted else teams[pk])
            for member in members:
                person = catalog.aliases.get(member)
                if person is None:
                    continue
                career = careers[person]
                if award == "SOTY":
                    career.record_points(award, season, points)
                career.record_standing(place)
                catalog.measure(award, person, -place)
                for cutoff in (10, 20):
                    if place <= cutoff:
                        catalog.add(
                            f"{award}:{cutoff}", f"Top-{cutoff} {award} in at least one season",
                            person, topic=award, occurrences=(season,),
                        )


def _career_clues(catalog, person, career, schools):
    for school in career.schools:
        catalog.add(
            f"school:{school}", f"Debated for {schools[school]}", person,
            occurrences=career.school_tournaments.get(school, ()),
        )
    for key, season, label in (
        ("first", career.first_season, "First season was"),
        ("latest", career.latest_season, "Most recent season was"),
        ("affiliated", career.latest_affiliated, "Most recent season representing a school was"),
    ):
        if season is not None:
            catalog.add(f"{key}:{season}", f"{label} {_season_label(season)}", person)
    if {SpeakerResult.NOVICE, SpeakerResult.VARSITY} <= career.winning_speaker_divisions:
        catalog.add(
            "both_divisions", "At least one novice top speak and at least one varsity top speak", person,
            occurrences=career.winning_speaker_tournaments,
        )
    most_hosts = max(map(len, career.semifinal_hosts_by_season.values()), default=0)
    catalog.measure("placed_hosts", person, most_hosts)
    for cutoff in range(3, 7):
        if most_hosts >= cutoff:
            qualifying_seasons = {
                season for season, hosts in career.semifinal_hosts_by_season.items() if len(hosts) >= cutoff
            }
            catalog.add(
                f"placed_hosts:{cutoff}", f"Varsity semis or better at {cutoff}+ different hosts in one season",
                person, topic="placed_hosts",
                occurrences=qualifying_seasons,
            )
    catalog.measure("multiple_finals", person, len(career.finalist_seasons))
    if len(career.finalist_seasons) >= 2:
        catalog.add(
            "multiple_finals", "Varsity team finalist or better in at least 2 seasons", person,
            occurrences=career.finalist_seasons,
        )
    for key, records, cutoffs, label in (
        ("speaks_seasons", career.speaker_seasons, (2, 3, 4), "Varsity speaker awards in {}+ seasons"),
        ("varsity_seasons", career.varsity_seasons, (2, 3, 4), "Varsity placements in {}+ seasons"),
        ("placed_partners", career.placed_partners, (3, 4, 5, 6), "Placed with {}+ different partners"),
        ("partner_schools", career.partner_schools, (2, 3, 4), "Placed with partners from {}+ schools"),
    ):
        catalog.measure(key, person, len(records))
        for cutoff in cutoffs:
            if len(records) >= cutoff:
                catalog.add(f"{key}:{cutoff}", label.format(cutoff), person, topic=key, occurrences=records)


def _years_before(day, years):
    year = day.year - years
    return day.replace(year=year, day=min(day.day, monthrange(year, day.month)[1]))


def _record_round_attendance(day, careers, aliases, teams, redacted_pairs):
    rounds = visible_canonical_rounds(
        Round.objects.filter(tournament__date__lte=day).exclude(victor__in=(Round.UNKNOWN, Round.BYE))
    )
    entries = rounds.values_list("tournament_id", "gov_id").union(
        rounds.values_list("tournament_id", "opp_id")
    )
    for tournament_id, team_id in entries.iterator():
        if (team_id, tournament_id) not in redacted_pairs:
            for pk in teams.get(team_id, ()):
                careers[aliases[pk]].attended_tournaments.add(tournament_id)


def load_catalog(day, progress_topics=(), review_guesses=None):
    """Load public records, optionally limited to the people in a completed square's guess history."""
    year = day.year - (day.month < 8)
    schools = _schools()
    aliases, careers, affiliations = _profiles(year, schools)
    catalog = Catalog(aliases, progress={topic: {} for topic in progress_topics})
    profile_ids = None
    if review_guesses is not None:
        review_people = {aliases.get(guess.get("debater_id"), guess["person"]) for guess in review_guesses}
        profile_ids = {pk for pk, person in aliases.items() if person in review_people}
    teams = _team_members()
    redacted_pairs = set(
        TeamResult.objects.filter(hidden_from_debaters__isnull=False).values_list("team_id", "tournament_id")
    )
    redacted = {team_id for team_id, _ in redacted_pairs}
    partners = _record_team_results(
        day, catalog, careers, schools, affiliations, teams, redacted, profile_ids=profile_ids,
    )
    _record_speaker_results(day, catalog, careers, schools, affiliations, profile_ids=profile_ids)
    _record_standings(year, catalog, careers, teams, redacted, profile_ids=profile_ids)
    if profile_ids is not None:
        for person in review_people & careers.keys():
            _career_clues(catalog, person, careers[person], schools)
        return catalog

    _record_round_attendance(day, careers, aliases, teams, redacted_pairs)
    hidden = hidden_people()
    cutoff = _years_before(day, DEBATER_ACTIVITY_YEARS)
    for person, career in careers.items():
        _career_clues(catalog, person, career, schools)
        catalog.rarity[person] = rarity_score(
            year - (career.latest_season or year), sum(career.points.values()), career.prestige,
            max(0, (career.latest_season or year) - (career.first_season or year)),
            len(career.attended_tournaments),
        )
        if career.latest_tournament is not None and career.latest_tournament >= cutoff:
            catalog.recent_people.add(person)
            if career.top_ten and person not in hidden:
                for partner, tournaments in partners[person].items():
                    catalog.add(
                        f"partner:{person}", f"Partnered with {career.name}", partner,
                        subject=person, occurrences=tournaments,
                    )

    host_cutoff = _years_before(day, HOST_ACTIVITY_YEARS)
    catalog.clues = {
        key: clue for key, clue in catalog.clues.items()
        if clue.latest_host_result is None or clue.latest_host_result >= host_cutoff
    }
    return catalog
