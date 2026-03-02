import json
from pathlib import Path
import re

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch, Q
from django.utils import timezone

from core.models import (
    Debater,
    DebaterAliasGroup,
    School,
    SchoolLookup,
    SpeakerResult,
    Team,
    TeamResult,
    Tournament,
)


class Command(BaseCommand):
    help = (
        "Export canonical tournament, school, debater, team, and result data "
        "for local tournament bundle matching."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--starting-season",
            default="2021",
            help="Only export tournaments and related canonical data from this season onward.",
        )
        parser.add_argument(
            "--output",
            default="canonical_import_export.json",
            help="Path to the JSON file to write.",
        )

    def handle(self, *args, **options):
        starting_season = options["starting_season"]
        self._validate_starting_season(starting_season)

        payload = self._build_payload(starting_season)
        output_path = Path(options["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        counts = payload["counts"]
        self.stdout.write(
            self.style.SUCCESS(
                "Exported canonical import data to "
                f"{output_path} "
                f"({counts['tournaments']} tournaments, "
                f"{counts['debaters']} debaters, "
                f"{counts['teams']} teams)"
            )
        )

    def _validate_starting_season(self, starting_season):
        try:
            int(starting_season)
        except (TypeError, ValueError) as exc:
            raise CommandError(
                f"Invalid --starting-season value '{starting_season}'. Expected a four-digit season key like 2021."
            ) from exc

    def _build_payload(self, starting_season):
        tournament_qs = self._relevant_tournaments(starting_season)
        tournaments = list(tournament_qs)

        seed_debaters = list(
            Debater.objects.select_related("school", "alias_group")
            .filter(
                Q(latest_season__gte=starting_season)
                | Q(speaker_results__tournament__season__gte=starting_season)
                | Q(teams__team_results__tournament__season__gte=starting_season)
            )
            .distinct()
            .order_by("id")
        )
        self._prepare_debater_identity_maps()
        debaters = self._expand_same_name_debaters(seed_debaters)
        debater_ids = {debater.id for debater in debaters}

        team_qs = (
            Team.objects.prefetch_related(
                Prefetch(
                    "debaters",
                    queryset=Debater.objects.select_related("school", "alias_group").order_by(
                        "id"
                    ),
                )
            )
            .filter(
                Q(team_results__tournament__season__gte=starting_season)
                | Q(debaters__in=debaters)
            )
            .distinct()
            .order_by("id")
        )
        teams = list(team_qs)

        school_ids = {
            tournament.host_id for tournament in tournaments if tournament.host_id is not None
        }
        school_ids.update(
            debater.school_id for debater in debaters if debater.school_id is not None
        )

        schools = list(School.objects.filter(id__in=school_ids).order_by("id"))
        school_lookups = list(
            SchoolLookup.objects.filter(school_id__in=school_ids)
            .select_related("school")
            .order_by("server_name", "id")
        )
        alias_group_ids = {
            debater.alias_group_id
            for debater in debaters
            if debater.alias_group_id is not None
        }
        alias_groups = list(
            DebaterAliasGroup.objects.filter(id__in=alias_group_ids).order_by("id")
        )

        return {
            "schema_version": 1,
            "exported_at": timezone.now().isoformat(),
            "starting_season": starting_season,
            "counts": {
                "tournaments": len(tournaments),
                "schools": len(schools),
                "school_lookups": len(school_lookups),
                "debater_alias_groups": len(alias_groups),
                "seed_debaters": len(seed_debaters),
                "debaters": len(debaters),
                "teams": len(teams),
            },
            "schools": [self._school_payload(school) for school in schools],
            "school_lookups": [
                {
                    "id": lookup.id,
                    "server_name": lookup.server_name,
                    "school_id": lookup.school_id,
                    "school_name": lookup.school.name,
                }
                for lookup in school_lookups
            ],
            "debater_alias_groups": [
                {
                    "id": alias_group.id,
                    "label": alias_group.label,
                    "name": alias_group.name,
                }
                for alias_group in alias_groups
            ],
            "debaters": [
                self._debater_payload(debater, include_school=True) for debater in debaters
            ],
            "teams": [self._team_payload(team) for team in teams],
            "tournaments": [
                self._tournament_payload(tournament, debater_ids) for tournament in tournaments
            ],
        }

    def _relevant_tournaments(self, starting_season):
        speaker_results = Prefetch(
            "speaker_results",
            queryset=SpeakerResult.objects.select_related("debater__school").order_by(
                "type_of_place", "place", "id"
            ),
        )
        team_results = Prefetch(
            "team_results",
            queryset=TeamResult.objects.select_related("team")
            .prefetch_related(
                Prefetch(
                    "team__debaters",
                    queryset=Debater.objects.select_related("school", "alias_group").order_by(
                        "id"
                    ),
                )
            )
            .order_by("type_of_place", "place", "id"),
        )
        return (
            Tournament.objects.select_related("host")
            .filter(season__gte=starting_season)
            .prefetch_related(speaker_results, team_results)
            .order_by("season", "date", "id")
        )

    def _school_payload(self, school):
        return {
            "id": school.id,
            "name": school.name,
            "short_name": school.short_name,
            "included_in_oty": school.included_in_oty,
            "profile_path": school.get_absolute_url(),
        }

    def _debater_payload(self, debater, include_school=False):
        payload = {
            "id": debater.id,
            "first_name": debater.first_name,
            "last_name": debater.last_name,
            "name": debater.name,
            "first_season": debater.first_season,
            "latest_season": debater.latest_season,
            "year_start": debater.first_season,
            "year_end": debater.latest_season,
            "status": debater.status,
            "status_display": debater.get_status_display(),
            "school_id": debater.school_id,
            "alias_group_id": debater.alias_group_id,
            "profile_path": debater.get_absolute_url(),
            "same_name_debater_ids": self._same_name_debater_ids(debater),
            "linked_debater_ids": self._linked_debater_ids(debater),
        }
        if include_school:
            payload["school_name"] = debater.school.name if debater.school else None
            payload["school_profile_path"] = (
                debater.school.get_absolute_url() if debater.school else None
            )
        return payload

    def _team_payload(self, team):
        debaters = list(team.debaters.all())
        return {
            "id": team.id,
            "name": team.name,
            "short_name": team.short_name,
            "profile_path": team.get_absolute_url(),
            "debaters": [
                self._debater_payload(debater, include_school=True) for debater in debaters
            ],
        }

    def _tournament_payload(self, tournament, debater_ids):
        return {
            "id": tournament.id,
            "name": tournament.name,
            "short_name": tournament.short_name,
            "manual_name": tournament.manual_name,
            "date": tournament.date.isoformat(),
            "season": tournament.season,
            "host_id": tournament.host_id,
            "host_name": tournament.host.name if tournament.host else None,
            "host_profile_path": (
                tournament.host.get_absolute_url() if tournament.host else None
            ),
            "num_rounds": tournament.num_rounds,
            "num_teams": tournament.num_teams,
            "num_novice_teams": tournament.num_novice_teams,
            "num_debaters": tournament.num_debaters,
            "num_novice_debaters": tournament.num_novice_debaters,
            "qual": tournament.qual,
            "noty": tournament.noty,
            "soty": tournament.soty,
            "toty": tournament.toty,
            "online_qual_points": tournament.online_qual_points,
            "autoqual_bar": tournament.autoqual_bar,
            "profile_path": tournament.get_absolute_url(),
            "speaker_results": [
                self._speaker_result_payload(result, debater_ids)
                for result in tournament.speaker_results.all()
            ],
            "team_results": [
                self._team_result_payload(result) for result in tournament.team_results.all()
            ],
        }

    def _speaker_result_payload(self, result, debater_ids):
        debater = result.debater
        return {
            "id": result.id,
            "debater_id": result.debater_id,
            "debater_name": debater.name,
            "debater_school_id": debater.school_id,
            "debater_school_name": debater.school.name if debater.school else None,
            "debater_profile_path": debater.get_absolute_url(),
            "debater_school_profile_path": (
                debater.school.get_absolute_url() if debater.school else None
            ),
            "debater_first_season": debater.first_season,
            "debater_latest_season": debater.latest_season,
            "debater_in_export": result.debater_id in debater_ids,
            "type_of_place": result.type_of_place,
            "type_of_place_display": result.get_type_of_place_display(),
            "place": result.place,
            "tie": result.tie,
            "counts_for_points": result.counts_for_points,
        }

    def _team_result_payload(self, result):
        team = result.team
        return {
            "id": result.id,
            "team_id": result.team_id,
            "team_name": team.name,
            "team_short_name": team.short_name,
            "team_profile_path": team.get_absolute_url(),
            "type_of_place": result.type_of_place,
            "type_of_place_display": result.get_type_of_place_display(),
            "place": result.place,
            "ghost_points": result.ghost_points,
            "counts_for_points": result.counts_for_points,
            "debaters": [
                self._debater_payload(debater, include_school=True)
                for debater in team.debaters.all()
            ],
        }

    def _expand_same_name_debaters(self, seed_debaters):
        seed_name_keys = {self._debater_name_key(debater) for debater in seed_debaters}
        expanded_ids = []
        for name_key in seed_name_keys:
            expanded_ids.extend(self._same_name_map.get(name_key, []))
        alias_group_ids = {
            debater.alias_group_id for debater in seed_debaters if debater.alias_group_id
        }
        for alias_group_id in alias_group_ids:
            expanded_ids.extend(self._alias_group_map.get(alias_group_id, []))
        expanded_ids = sorted(set(expanded_ids))
        return list(
            Debater.objects.select_related("school", "alias_group")
            .filter(id__in=expanded_ids)
            .order_by("id")
        )

    def _same_name_debater_ids(self, debater):
        return self._same_name_map.get(self._debater_name_key(debater), [])

    def _linked_debater_ids(self, debater):
        return self._linked_debater_map.get(debater.id, [])

    def _debater_name_key(self, debater):
        return self._normalize_name(debater.first_name, debater.last_name)

    def _normalize_name(self, first_name, last_name):
        name = f"{first_name} {last_name}".strip().lower()
        return re.sub(r"\s+", " ", name)

    def _prepare_debater_identity_maps(self):
        self._same_name_map = {}
        self._linked_debater_map = {}
        self._alias_group_map = {}

        all_debaters = list(
            Debater.objects.select_related("alias_group").order_by("id").only(
                "id",
                "first_name",
                "last_name",
                "alias_group_id",
            )
        )
        alias_groups = {}

        for debater in all_debaters:
            name_key = self._debater_name_key(debater)
            self._same_name_map.setdefault(name_key, []).append(debater.id)
            if debater.alias_group_id:
                alias_groups.setdefault(debater.alias_group_id, []).append(debater.id)

        for alias_group_id, linked_ids in alias_groups.items():
            linked_ids.sort()
            self._alias_group_map[alias_group_id] = linked_ids
            for debater_id in linked_ids:
                self._linked_debater_map[debater_id] = linked_ids
