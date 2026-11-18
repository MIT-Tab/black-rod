"""Build playable clues and their answer sets from public APDA records."""

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from core.models import (
    Debater, ImmaculateGridHiddenDebater, School, SOTY, SpeakerResult,
    Team, TeamResult, TOTY, Tournament,
)
from core.utils.elo_runtime_engine.constants import normalize_school_name, season_to_int

DEBATER_ACTIVITY_YEARS = 4
HOST_ACTIVITY_YEARS = 3

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


def rarity_score(seasons_since_active, points, prestige):
    age = max(0, seasons_since_active)
    return 45 * age / (age + 4) + 30 * 35 / (35 + points) + 25 * 10 / (10 + prestige)


@dataclass
class Clue:
    key: str
    label: str
    topic: str
    people: set = field(default_factory=set)
    subject: str | None = None
    latest_host_result: date | None = None

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

    def add(self, key, label, person, topic=None, subject=None, host_result_date=None):
        if key not in self.clues:
            self.clues[key] = Clue(key, label, topic or key, subject=subject)
        clue = self.clues[key]
        clue.people.add(person)
        if host_result_date is not None:
            clue.latest_host_result = max(host_result_date, clue.latest_host_result or host_result_date)


@dataclass
class Career:
    name: str
    schools: set = field(default_factory=set)
    first_season: int | None = None
    latest_season: int | None = None
    latest_affiliated: int | None = None
    latest_tournament: date | None = None
    points: dict = field(default_factory=dict)
    prestige: int = 0
    top_ten: bool = False
    winning_speaker_divisions: set = field(default_factory=set)
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

    def record_tournament(self, season, day, affiliated):
        year = season_to_int(season)
        self.record_season(year, year, affiliated)
        self.latest_tournament = max(day, self.latest_tournament or day)
        return year

    def record_standing(self, award, season, place, points):
        key = (award, season)
        self.points[key] = max(self.points.get(key, 0), points)
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


def _placing_clues(catalog, person, place, host, kind, schools, event_day):
    for cutoff, label in ELIMINATION_PLACES.items():
        if place <= cutoff:
            if host in schools:
                catalog.add(
                    f"host:{host}:{cutoff}",
                    f"Tournament winner at {schools[host]}" if cutoff == 1 else f"{label} at {schools[host]}",
                    person, topic=f"host:{host}", host_result_date=event_day,
                )
            if kind in EVENT_TYPES:
                event = EVENT_TYPES[kind]
                event_label = "Nationals" if kind == Tournament.NATIONALS else f"a {event} tournament"
                catalog.add(
                    f"event:{event}:{cutoff}",
                    f"{label} {event_label}" if cutoff == 1 else f"{label} at {event_label}",
                    person, topic=f"event:{event}",
                )


def _record_partnership(catalog, careers, members, affiliations, placing, partners):
    for left, right in combinations(members, 2):
        first, second = catalog.aliases[left], catalog.aliases[right]
        if first == second:
            continue
        partners[first].add(second)
        partners[second].add(first)
        if not placing:
            continue
        for person, partner, partner_profile in ((first, second, right), (second, first, left)):
            careers[person].placed_partners.add(partner)
            if partner_profile in affiliations:
                careers[person].partner_schools.add(affiliations[partner_profile])


def _record_team_results(day, catalog, careers, schools, affiliations, teams, redacted):
    partners = defaultdict(set)
    rows = TeamResult.objects.filter(
        tournament__date__lte=day, team__synthetic=False, hidden_from_debaters__isnull=True
    ).values_list(
        "team_id", "place", "type_of_place", "tournament__season", "tournament__date",
        "tournament__host_id", "tournament__qual_type",
    )
    for team, place, division, season, event_day, host, kind in rows.iterator():
        members = teams[team]
        placing = place > 0
        if team not in redacted:
            _record_partnership(catalog, careers, members, affiliations, placing, partners)
        hybrid = len({affiliations[pk] for pk in members if pk in affiliations}) > 1
        for pk in members:
            person = catalog.aliases[pk]
            career = careers[person]
            year = career.record_tournament(season, event_day, pk in affiliations)
            if not placing:
                continue
            _placing_clues(catalog, person, place, host, kind, schools, event_day)
            if hybrid and team not in redacted:
                catalog.add("hybrid", "Placed on a hybrid team", person)
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


def _record_speaker_results(day, catalog, careers, schools, affiliations):
    rows = SpeakerResult.objects.filter(
        tournament__date__lte=day, place__gt=0, hidden_from_profile=False, debater__in=Debater.objects.all()
    ).values_list(
        "debater_id", "place", "tie", "type_of_place", "tournament__season",
        "tournament__date", "tournament__host_id", "tournament__qual_type",
    )
    for pk, place, tie, division, season, event_day, host, kind in rows.iterator():
        person = catalog.aliases[pk]
        career = careers[person]
        year = career.record_tournament(season, event_day, pk in affiliations)
        rank = max(1, place - int(tie))
        if rank == 1:
            career.winning_speaker_divisions.add(division)
        if division != SpeakerResult.VARSITY:
            continue
        if year is not None:
            career.varsity_seasons.add(year)
            career.speaker_seasons.add(year)
        if rank == 1:
            catalog.add("top_speaker", "Won top varsity speaker", person)
        if host in schools:
            for cutoff in (5, 10):
                if rank <= cutoff:
                    catalog.add(
                        f"speaker:{host}:{cutoff}", f"Top-{cutoff} varsity speaker at {schools[host]}",
                        person, topic=f"host:{host}", host_result_date=event_day,
                    )
        prominence = (11 - rank) * (2 if kind == Tournament.NATIONALS else 1)
        career.prestige = max(career.prestige, prominence)


def _record_standings(year, catalog, careers, teams, redacted):
    for model, award, member_field in ((SOTY, "SOTY", "debater_id"), (TOTY, "TOTY", "team_id")):
        rows = model.objects.filter(season__lte=str(year), place__gt=0).values_list(
            member_field, "season", "place", "points"
        )
        for pk, season, place, points in rows.iterator():
            members = (pk,) if award == "SOTY" else (() if pk in redacted else teams[pk])
            for member in members:
                person = catalog.aliases.get(member)
                if person is None:
                    continue
                careers[person].record_standing(award, season, place, points)
                for cutoff in (10, 20):
                    if place <= cutoff:
                        catalog.add(f"{award}:{cutoff}", f"Top-{cutoff} {award}", person, topic=award)


def _career_clues(catalog, person, career, schools):
    for school in career.schools:
        catalog.add(f"school:{school}", f"Debated for {schools[school]}", person)
    for key, season, label in (
        ("first", career.first_season, "Debuted in"),
        ("latest", career.latest_season, "Last debated in"),
        ("affiliated", career.latest_affiliated, "Last debated with a school in"),
    ):
        if season is not None:
            catalog.add(f"{key}:{season}", f"{label} {_season_label(season)}", person)
    if {SpeakerResult.NOVICE, SpeakerResult.VARSITY} <= career.winning_speaker_divisions:
        catalog.add("both_divisions", "Top speaker in novice and varsity", person)
    most_hosts = max(map(len, career.semifinal_hosts_by_season.values()), default=0)
    for cutoff in range(3, 7):
        if most_hosts >= cutoff:
            catalog.add(
                f"placed_hosts:{cutoff}", f"Varsity semis at {cutoff}+ hosts in one season",
                person, topic="placed_hosts",
            )
    if len(career.finalist_seasons) >= 2:
        catalog.add("multiple_finals", "Varsity team finalist in 2+ seasons", person)
    for key, records, cutoffs, label in (
        ("speaks_seasons", career.speaker_seasons, (2, 3, 4), "Varsity speaker awards in {}+ seasons"),
        ("varsity_seasons", career.varsity_seasons, (2, 3, 4), "Varsity placements in {}+ seasons"),
        ("placed_partners", career.placed_partners, (3, 4, 5, 6), "Placed with {}+ different partners"),
        ("partner_schools", career.partner_schools, (2, 3, 4), "Placed with partners from {}+ schools"),
    ):
        for cutoff in cutoffs:
            if len(records) >= cutoff:
                catalog.add(f"{key}:{cutoff}", label.format(cutoff), person, topic=key)


def _years_before(day, years):
    year = day.year - years
    return day.replace(year=year, day=min(day.day, monthrange(year, day.month)[1]))


def load_catalog(day):
    year = day.year - (day.month < 8)
    schools = _schools()
    aliases, careers, affiliations = _profiles(year, schools)
    catalog = Catalog(aliases)
    teams = _team_members()
    redacted = set(
        TeamResult.objects.filter(hidden_from_debaters__isnull=False).values_list("team_id", flat=True)
    )
    partners = _record_team_results(day, catalog, careers, schools, affiliations, teams, redacted)
    _record_speaker_results(day, catalog, careers, schools, affiliations)
    _record_standings(year, catalog, careers, teams, redacted)
    hidden = hidden_people()
    cutoff = _years_before(day, DEBATER_ACTIVITY_YEARS)
    for person, career in careers.items():
        _career_clues(catalog, person, career, schools)
        catalog.rarity[person] = rarity_score(
            year - (career.latest_season or year), sum(career.points.values()), career.prestige
        )
        if career.latest_tournament is not None and career.latest_tournament >= cutoff:
            catalog.recent_people.add(person)
            if career.top_ten and person not in hidden:
                for partner in partners[person]:
                    catalog.add(f"partner:{person}", f"Partnered with {career.name}", partner, subject=person)

    host_cutoff = _years_before(day, HOST_ACTIVITY_YEARS)
    catalog.clues = {
        key: clue for key, clue in catalog.clues.items()
        if clue.latest_host_result is None or clue.latest_host_result >= host_cutoff
    }
    return catalog
