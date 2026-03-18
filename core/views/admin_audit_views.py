from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core.admin_audit_forms import ROUND_BALLOT_SLOTS, TournamentRoundBallotForm
from core.forms import TournamentImportMoveForm
from core.models import (
    DebaterAlias,
    ImportedRoundMetadata,
    Round,
    RoundStats,
    TeamResult,
    Tournament,
    TournamentImport,
)
from core.models.round import sanitize_round_stat_values
from core.utils.elo_runtime_engine.cache import clear_runtime_caches
from core.utils.round_amendment_recorder import (
    build_round_delete_action,
    build_round_upsert_action,
    build_tournament_import_delete_action,
    build_tournament_import_move_action,
    ensure_development_round_import_key,
    record_round_amendment_action,
    round_amendment_recording_context,
)
from core.utils.round_amendments import apply_round_amendments
from core.utils.team import get_or_create_team_for_debaters


class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser


class TournamentAuditDataMixin:
    min_season_start = 2017
    included_qual_types = (
        Tournament.POINTS,
        Tournament.PROAMS,
        Tournament.NATIONALS,
        Tournament.NOVICE,
        Tournament.GENDER_MINORITY,
        Tournament.BIPOC,
        Tournament.ONLINE_POINTS,
        Tournament.ONLINE_PROAMS_POINTS,
        Tournament.ONLINE_GM_POINTS,
    )
    sortable_fields = {
        "season": "season_sort",
        "date": "date",
        "tournament": "tournament_name",
        "type": "qual_type_label",
        "teams": "recorded_team_count",
        "prelims": "recorded_num_rounds",
        "recorded_in": "recorded_prelim",
        "expected_in": "expected_prelim",
        "in_pct": "prelim_pct",
        "recorded_out": "recorded_outround",
        "expected_out": "expected_outround",
        "out_pct": "outround_pct",
        "total_pct": "total_pct",
        "imports": "linked_import_count",
    }

    def build_rows(self, tournament_ids=None):
        tournaments = Tournament.objects.filter(
            qual_type__in=self.included_qual_types
        ).select_related("host")
        if tournament_ids:
            tournaments = tournaments.filter(id__in=tournament_ids)
        tournaments = list(tournaments.order_by("date", "id"))

        filtered_tournaments = []
        for tournament in tournaments:
            season_start = self._season_start(tournament.season)
            if season_start is None or season_start < self.min_season_start:
                continue
            filtered_tournaments.append(tournament)

        tournament_ids = [int(tournament.id) for tournament in filtered_tournaments]
        num_rounds_map = {
            int(tournament.id): int(tournament.num_rounds or 0)
            for tournament in filtered_tournaments
        }

        round_counts = defaultdict(
            lambda: {
                "prelim": 0,
                "outround": 0,
                "imported": 0,
                "manual": 0,
            }
        )
        round_queryset = (
            Round.objects.filter(tournament_id__in=tournament_ids)
            .select_related("imported_metadata")
        )
        for round_obj in round_queryset:
            tournament_id = int(round_obj.tournament_id)
            num_rounds = int(num_rounds_map.get(tournament_id) or 0)
            is_outround = self._is_outround(round_obj, num_rounds)
            bucket = "outround" if is_outround else "prelim"
            round_counts[tournament_id][bucket] += 1
            if self._is_imported_round(round_obj):
                round_counts[tournament_id]["imported"] += 1
            else:
                round_counts[tournament_id]["manual"] += 1

        result_counts = defaultdict(int)
        for row in (
            TeamResult.objects.filter(tournament_id__in=tournament_ids, place__gt=0)
            .values("tournament_id", "type_of_place")
            .annotate(total=Count("id"))
        ):
            result_counts[(int(row["tournament_id"]), int(row["type_of_place"]))] = int(
                row["total"] or 0
            )

        linked_import_counts = defaultdict(int)
        for row in (
            Tournament.objects.filter(id__in=tournament_ids)
            .annotate(linked_import_count=Count("tournament_imports", distinct=True))
            .values("id", "linked_import_count")
        ):
            linked_import_counts[int(row["id"])] = int(row["linked_import_count"] or 0)

        rows = []
        qual_type_labels = dict(Tournament.QUAL_TYPES)
        for tournament in filtered_tournaments:
            team_count = self._recorded_team_count(tournament)
            if team_count < 1:
                continue

            tournament_id = int(tournament.id)
            expected_prelim_rounds = self._expected_prelim_rounds(tournament)
            expected_prelim = self._expected_prelim_count(team_count, expected_prelim_rounds)
            expected_outround = self._expected_outround_count(
                result_counts.get((tournament_id, self._relevant_team_result_type(tournament)), 0)
            )
            recorded_prelim = int(round_counts[tournament_id]["prelim"])
            recorded_outround = int(round_counts[tournament_id]["outround"])
            recorded_total = recorded_prelim + recorded_outround
            expected_total = int(expected_prelim + expected_outround)

            rows.append(
                {
                    "tournament_id": tournament_id,
                    "tournament_name": str(tournament.name or ""),
                    "public_tournament_url": tournament.get_absolute_url(),
                    "audit_detail_url": reverse(
                        "core:tournament_audit_detail",
                        kwargs={"pk": tournament_id},
                    ),
                    "season": tournament.season,
                    "season_display": tournament.get_season_display(),
                    "season_sort": self._season_start(tournament.season) or 0,
                    "date": tournament.date,
                    "qual_type_label": qual_type_labels.get(
                        int(tournament.qual_type),
                        str(tournament.qual_type),
                    ),
                    "recorded_team_count": int(team_count),
                    "recorded_num_rounds": int(tournament.num_rounds or 0),
                    "recorded_prelim": recorded_prelim,
                    "expected_prelim": int(expected_prelim),
                    "prelim_pct": self._pct(recorded_prelim, expected_prelim),
                    "recorded_outround": recorded_outround,
                    "expected_outround": int(expected_outround),
                    "outround_pct": self._pct(recorded_outround, expected_outround),
                    "recorded_total": recorded_total,
                    "expected_total": expected_total,
                    "total_pct": self._pct(recorded_total, expected_total),
                    "imported_round_count": int(round_counts[tournament_id]["imported"]),
                    "manual_round_count": int(round_counts[tournament_id]["manual"]),
                    "linked_import_count": int(linked_import_counts.get(tournament_id, 0)),
                }
            )
        return rows

    @staticmethod
    def _season_start(season_value):
        token = str(season_value or "").strip()
        if not token:
            return None
        if "-" in token:
            token = token.split("-", 1)[0].strip()
        if token.isdigit():
            return int(token)
        return None

    @staticmethod
    def _recorded_team_count(tournament):
        if int(tournament.qual_type) == int(Tournament.NOVICE) and int(
            tournament.num_novice_teams or 0
        ) > 0:
            return int(tournament.num_novice_teams or 0)
        if int(tournament.num_teams or 0) > 0:
            return int(tournament.num_teams or 0)
        return max(int(tournament.num_novice_teams or 0), 0)

    @staticmethod
    def _relevant_team_result_type(tournament):
        if int(tournament.qual_type) == int(Tournament.NOVICE):
            return TeamResult.NOVICE
        return TeamResult.VARSITY

    @staticmethod
    def _expected_prelim_rounds(tournament):
        if int(tournament.qual_type) == int(Tournament.NATIONALS):
            return 7
        return int(tournament.num_rounds or 0)

    @staticmethod
    def _expected_prelim_count(team_count, num_rounds):
        teams = int(team_count or 0)
        rounds = int(num_rounds or 0)
        if teams <= 0 or rounds <= 0:
            return 0
        return (teams * rounds) // 2

    @staticmethod
    def _expected_outround_count(placed_team_count):
        teams = int(placed_team_count or 0)
        if teams <= 1:
            return 0
        return teams - 1

    @staticmethod
    def _pct(recorded_count, expected_count):
        recorded = float(recorded_count or 0)
        expected = float(expected_count or 0)
        if expected <= 0:
            return 100.0
        return round((100.0 * recorded) / expected, 1)

    @staticmethod
    def _is_outround(round_obj, tournament_num_rounds):
        stage = str(round_obj.stage or "").strip().lower()
        if stage == Round.Stage.OUTROUND:
            return True
        return int(round_obj.round_number or 0) > int(tournament_num_rounds or 0)

    @staticmethod
    def _is_imported_round(round_obj):
        origin = str(round_obj.import_origin or "").strip().lower()
        return bool(getattr(round_obj, "imported_metadata_id", None) or (origin and origin != "manual"))

    def sort_rows(self, rows, sort_key, sort_dir):
        field = self.sortable_fields.get(sort_key)
        if not field:
            return rows

        reverse = sort_dir == "desc"

        def _value(row):
            value = row.get(field)
            if isinstance(value, str):
                return value.lower()
            return value

        return sorted(
            rows,
            key=lambda row: (_value(row), row.get("date"), row.get("tournament_id")),
            reverse=reverse,
        )

    def sort_headers(self, show_incomplete_only, active_sort_key, active_sort_dir):
        headers = {}
        for key in self.sortable_fields:
            is_active = key == active_sort_key
            if is_active and active_sort_dir == "asc":
                next_dir = "desc"
                indicator = " [asc]"
            elif is_active and active_sort_dir == "desc":
                next_dir = "asc"
                indicator = " [desc]"
            else:
                next_dir = "asc"
                indicator = ""

            query_parts = []
            if show_incomplete_only:
                query_parts.append("incomplete_only=1")
            query_parts.append("sort=%s" % key)
            query_parts.append("dir=%s" % next_dir)
            headers[key] = {"url": "?%s" % "&".join(query_parts), "indicator": indicator}
        return headers


class ConsolidatedTournamentAuditView(
    SuperuserRequiredMixin,
    TournamentAuditDataMixin,
    TemplateView,
):
    template_name = "admin/tournament_pipeline_audit.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        show_incomplete_only = str(self.request.GET.get("incomplete_only") or "").strip() == "1"
        sort_key = str(self.request.GET.get("sort") or "").strip().lower()
        sort_dir = str(self.request.GET.get("dir") or "").strip().lower()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"

        rows = self.build_rows()
        if show_incomplete_only:
            rows = [row for row in rows if float(row["total_pct"]) < 100.0]
        rows = self.sort_rows(rows, sort_key, sort_dir)

        context["rows"] = rows
        context["show_incomplete_only"] = show_incomplete_only
        context["sort_headers"] = self.sort_headers(show_incomplete_only, sort_key, sort_dir)
        context["included_type_labels"] = [
            dict(Tournament.QUAL_TYPES).get(value, str(value))
            for value in self.included_qual_types
        ]
        context["summary"] = {
            "total_tournaments": len(rows),
            "incomplete_tournaments": sum(1 for row in rows if float(row["total_pct"]) < 100.0),
            "complete_tournaments": sum(1 for row in rows if float(row["total_pct"]) >= 100.0),
            "linked_imports": sum(int(row["linked_import_count"]) for row in rows),
        }
        return context


class TournamentAuditDetailView(
    SuperuserRequiredMixin,
    TournamentAuditDataMixin,
    TemplateView,
):
    template_name = "admin/tournament_audit_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tournament = get_object_or_404(Tournament.objects.select_related("host"), pk=kwargs.get("pk"))
        rows = self.build_rows(tournament_ids=[tournament.id])
        audit_row = rows[0] if rows else None
        rounds = (
            Round.objects.filter(tournament=tournament)
            .select_related("gov", "opp", "imported_metadata")
            .prefetch_related("stats__debater", "gov__debaters__school", "opp__debaters__school")
            .order_by("stage", "round_number", "id")
        )

        round_rows = []
        imported_round_count = 0
        manual_round_count = 0
        for round_obj in rounds:
            metadata = round_obj.metadata if isinstance(round_obj.metadata, dict) else {}
            source_round_name = str(metadata.get("source_round_name") or "").strip()
            if self._is_imported_round(round_obj):
                imported_round_count += 1
            else:
                manual_round_count += 1
            round_rows.append(
                {
                    "id": round_obj.id,
                    "stage_label": round_obj.get_stage_display(),
                    "round_number": int(round_obj.round_number or 0),
                    "canonical_round_name": str(round_obj.round_label or "").strip(),
                    "source_round_name": source_round_name,
                    "gov_name": str(round_obj.gov.long_name or round_obj.gov.name or ""),
                    "opp_name": str(round_obj.opp.long_name or round_obj.opp.name or ""),
                    "victor_label": round_obj.get_victor_display(),
                    "is_rated": bool(round_obj.is_rated),
                    "origin_label": self._origin_label(round_obj),
                    "modify_url": reverse(
                        "core:tournament_audit_round_edit",
                        kwargs={"tournament_id": tournament.id, "round_id": round_obj.id},
                    ),
                    "delete_url": reverse(
                        "core:tournament_audit_round_delete",
                        kwargs={"tournament_id": tournament.id, "round_id": round_obj.id},
                    ),
                }
            )

        import_rows = []
        linked_imports = TournamentImport.objects.filter(tournament=tournament).order_by(
            "-imported_at",
            "-id",
        )
        for import_row in linked_imports:
            import_rows.append(
                {
                    "id": int(import_row.id),
                    "import_type_label": import_row.get_import_type_display(),
                    "original_file_name": str(import_row.original_file_name or ""),
                    "source_hash": str(import_row.source_hash or ""),
                    "imported_at": import_row.imported_at,
                    "linked_round_count": int(
                        Round.objects.filter(imported_metadata__sources=import_row)
                        .distinct()
                        .count()
                    ),
                    "delete_url": reverse(
                        "core:tournament_import_delete",
                        kwargs={"tournament_id": tournament.id, "import_id": import_row.id},
                    ),
                    "move_url": reverse(
                        "core:tournament_import_move",
                        kwargs={"tournament_id": tournament.id, "import_id": import_row.id},
                    ),
                    "move_form": TournamentImportMoveForm(
                        prefix=f"import-{import_row.id}",
                        initial={"tournament_import_id": int(import_row.id)},
                        current_tournament=tournament,
                    ),
                }
            )

        context["tournament"] = tournament
        context["audit_row"] = audit_row
        context["round_rows"] = round_rows
        context["import_rows"] = import_rows
        context["add_round_url"] = reverse(
            "core:tournament_audit_round_create",
            kwargs={"tournament_id": tournament.id},
        )
        context["imported_round_count"] = imported_round_count
        context["manual_round_count"] = manual_round_count
        context["import_move_form_media"] = TournamentImportMoveForm(current_tournament=tournament).media
        context["round_amendment_recording"] = round_amendment_recording_context()
        return context

    @staticmethod
    def _origin_label(round_obj):
        origin = str(round_obj.import_origin or "").strip()
        if getattr(round_obj, "imported_metadata_id", None):
            if origin:
                return origin.replace("_", " ").title()
            return "Imported"
        if not origin or origin.lower() == "manual":
            return "Manual"
        return origin.replace("_", " ").title()


class TournamentAuditRoundEditView(SuperuserRequiredMixin, TemplateView):
    template_name = "admin/tournament_round_ballot.html"
    form_class = TournamentRoundBallotForm

    def dispatch(self, request, *args, **kwargs):
        self.tournament = get_object_or_404(Tournament, pk=kwargs.get("tournament_id"))
        self.round_obj = None
        round_id = kwargs.get("round_id")
        if round_id is not None:
            self.round_obj = get_object_or_404(
                Round.objects.filter(tournament=self.tournament)
                .select_related("imported_metadata")
                .prefetch_related("stats__debater", "gov__debaters__school", "opp__debaters__school"),
                pk=round_id,
            )
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = self.form_class(tournament=self.tournament, round_obj=self.round_obj)
        return self.render_to_response(self.get_context_data(form=form))

    def post(self, request, *args, **kwargs):
        form = self.form_class(
            request.POST,
            tournament=self.tournament,
            round_obj=self.round_obj,
        )
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        round_obj, action_type, recorded_path, recording_error = self.save_round(form)
        messages.success(
            request,
            "Saved round %s for %s."
            % (
                str(round_obj.round_label or ""),
                str(self.tournament.name or ""),
            ),
        )
        if recorded_path:
            messages.info(request, f"Recorded {action_type} amendment to {recorded_path}.")
        elif recording_error:
            messages.warning(
                request,
                f"Round was saved but amendment recording failed: {recording_error}",
            )
        return redirect("core:tournament_audit_detail", pk=self.tournament.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs["form"]
        context["tournament"] = self.tournament
        context["round_obj"] = self.round_obj
        context["audit_detail_url"] = reverse(
            "core:tournament_audit_detail",
            kwargs={"pk": self.tournament.id},
        )
        context["round_amendment_recording"] = round_amendment_recording_context()
        return context

    def save_round(self, form):
        is_new_round = self.round_obj is None
        cleaned_data = form.cleaned_data
        existing_stat_meta = self._existing_stat_meta_by_role(self.round_obj)
        gov_team = get_or_create_team_for_debaters(
            cleaned_data["gov_1_debater"],
            cleaned_data["gov_2_debater"],
        )
        opp_team = get_or_create_team_for_debaters(
            cleaned_data["opp_1_debater"],
            cleaned_data["opp_2_debater"],
        )

        round_obj = self.round_obj or Round(tournament=self.tournament)
        ensure_development_round_import_key(round_obj)
        metadata = dict(round_obj.metadata or {}) if isinstance(round_obj.metadata, dict) else {}
        canonical_round_name = str(cleaned_data.get("canonical_round_name") or "").strip()
        outround_stage = cleaned_data.get("outround_stage")
        source_round_name = str(cleaned_data.get("source_round_name") or "").strip()
        if source_round_name:
            metadata["source_round_name"] = source_round_name
        else:
            metadata.pop("source_round_name", None)

        team_a_names = []
        team_b_names = []
        for slot in ROUND_BALLOT_SLOTS:
            debater = cleaned_data[slot["debater_field"]]
            source_name = self._resolved_slot_source_name(cleaned_data, slot)
            if slot["side"] == "gov":
                team_a_names.append(source_name)
            else:
                team_b_names.append(source_name)

        metadata["round_label"] = canonical_round_name
        metadata["stage"] = str(cleaned_data["stage"] or "")
        metadata["is_rated"] = bool(cleaned_data.get("is_rated"))
        metadata["weight"] = float(cleaned_data.get("weight") or Decimal("1.0"))
        metadata["team_a_ids"] = [
            int(cleaned_data["gov_1_debater"].id),
            int(cleaned_data["gov_2_debater"].id),
        ]
        metadata["team_b_ids"] = [
            int(cleaned_data["opp_1_debater"].id),
            int(cleaned_data["opp_2_debater"].id),
        ]
        metadata["team_a_names"] = team_a_names
        metadata["team_b_names"] = team_b_names

        round_obj.gov = gov_team
        round_obj.opp = opp_team
        round_obj.round_label = canonical_round_name
        round_obj.stage = str(cleaned_data["stage"] or "")
        round_obj.round_number = int(cleaned_data["round_number"] or 0)
        round_obj.victor = int(cleaned_data["victor"])
        round_obj.is_rated = bool(cleaned_data.get("is_rated"))
        round_obj.weight = float(cleaned_data.get("weight") or Decimal("1.0"))
        round_obj.metadata = metadata
        round_obj.elim_size = (
            int(outround_stage)
            if round_obj.stage == Round.Stage.OUTROUND and outround_stage
            else None
        )
        if not round_obj.import_origin:
            round_obj.import_origin = "manual"

        with transaction.atomic():
            round_obj.save()
            self._replace_round_stats(round_obj, cleaned_data, existing_stat_meta)
            self._sync_imported_metadata(round_obj, cleaned_data)

        clear_runtime_caches()
        action_type = "create_round" if is_new_round else "update_round"
        recorded_path = None
        recording_error = None
        try:
            recorded_path = record_round_amendment_action(
                build_round_upsert_action(round_obj, action_type=action_type)
            )
        except Exception as exc:
            recording_error = exc
        return round_obj, action_type, recorded_path, recording_error

    @staticmethod
    def _existing_stat_meta_by_role(round_obj):
        if round_obj is None:
            return {}
        stat_meta = {}
        for stat in round_obj.stats.all():
            role = str(stat.debater_role or "").strip().upper()
            if role and role not in stat_meta:
                stat_meta[role] = {
                    "metadata": dict(stat.metadata or {})
                    if isinstance(stat.metadata, dict)
                    else {},
                    "source_status": str(stat.source_status or ""),
                }
        return stat_meta

    @staticmethod
    def _replace_round_stats(round_obj, cleaned_data, existing_stat_meta):
        round_obj.stats.all().delete()
        stat_rows = []
        for slot in ROUND_BALLOT_SLOTS:
            debater = cleaned_data[slot["debater_field"]]
            role = str(cleaned_data.get(slot["role_field"]) or "").strip().upper()
            role_data = existing_stat_meta.get(role, {})
            stat_metadata = dict(role_data.get("metadata") or {})
            stat_metadata["speaker_name"] = TournamentAuditRoundEditView._resolved_slot_source_name(
                cleaned_data,
                slot,
            )
            stat_values = sanitize_round_stat_values(
                round_obj,
                speaks=cleaned_data.get(slot["speaks_field"]),
                ranks=cleaned_data.get(slot["ranks_field"]),
                debater_role=role or None,
            )
            stat_rows.append(
                RoundStats(
                    round=round_obj,
                    debater=debater,
                    stage=stat_values["stage"],
                    speaks=stat_values["speaks"],
                    ranks=stat_values["ranks"],
                    debater_role=stat_values["debater_role"],
                    score_index=1,
                    source_status=str(role_data.get("source_status") or ""),
                    metadata=stat_metadata,
                )
            )
        RoundStats.objects.bulk_create(stat_rows)

    def _sync_imported_metadata(self, round_obj, cleaned_data):
        origin = str(round_obj.import_origin or "").strip().lower()
        should_sync = bool(getattr(round_obj, "imported_metadata_id", None) or (origin and origin != "manual"))
        if not should_sync:
            return

        imported_metadata, _ = ImportedRoundMetadata.objects.get_or_create(round=round_obj)
        imported_metadata.gov_1_alias = self._alias_for(
            cleaned_data["gov_1_debater"],
            self._resolved_slot_source_name(cleaned_data, ROUND_BALLOT_SLOTS[0]),
        )
        imported_metadata.gov_2_alias = self._alias_for(
            cleaned_data["gov_2_debater"],
            self._resolved_slot_source_name(cleaned_data, ROUND_BALLOT_SLOTS[1]),
        )
        imported_metadata.opp_1_alias = self._alias_for(
            cleaned_data["opp_1_debater"],
            self._resolved_slot_source_name(cleaned_data, ROUND_BALLOT_SLOTS[2]),
        )
        imported_metadata.opp_2_alias = self._alias_for(
            cleaned_data["opp_2_debater"],
            self._resolved_slot_source_name(cleaned_data, ROUND_BALLOT_SLOTS[3]),
        )
        imported_metadata.save()

    @staticmethod
    def _alias_for(debater, source_name):
        cleaned = str(source_name or debater.name or "").strip()
        if not cleaned:
            return None
        normalized = cleaned.casefold()
        alias, created = DebaterAlias.objects.get_or_create(
            debater=debater,
            source_name=cleaned,
            defaults={"normalized_name": normalized},
        )
        if not created and alias.normalized_name != normalized:
            alias.normalized_name = normalized
            alias.save(update_fields=["normalized_name", "updated_at"])
        return alias

    @staticmethod
    def _resolved_slot_source_name(cleaned_data, slot):
        debater = cleaned_data[slot["debater_field"]]
        source_name = str(cleaned_data.get(slot["source_name_field"]) or "").strip()
        if source_name:
            return source_name
        return str(debater.name or "").strip()


class TournamentAuditRoundDeleteView(SuperuserRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        tournament = get_object_or_404(Tournament, pk=kwargs.get("tournament_id"))
        round_obj = get_object_or_404(Round.objects.filter(tournament=tournament), pk=kwargs.get("round_id"))
        action = build_round_delete_action(round_obj)

        apply_round_amendments({"actions": [action]}, actor=request.user, source_context={"source": "tournament_audit_round_delete"})
        messages.success(request, f"Deleted round {round_obj.round_label or round_obj.id} from {tournament.name}.")

        try:
            recorded_path = record_round_amendment_action(action)
        except Exception as exc:
            messages.warning(request, f"Round was deleted but amendment recording failed: {exc}")
        else:
            if recorded_path:
                messages.info(request, f"Recorded delete_round amendment to {recorded_path}.")

        return redirect("core:tournament_audit_detail", pk=tournament.id)


class TournamentImportDeleteView(SuperuserRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        tournament = get_object_or_404(Tournament, pk=kwargs.get("tournament_id"))
        import_row = get_object_or_404(TournamentImport.objects.filter(tournament=tournament), pk=kwargs.get("import_id"))
        action = build_tournament_import_delete_action(import_row)

        apply_round_amendments({"actions": [action]}, actor=request.user, source_context={"source": "tournament_import_delete"})
        messages.success(request, f"Deleted tournament import {import_row.id} from {tournament.name}.")

        try:
            recorded_path = record_round_amendment_action(action)
        except Exception as exc:
            messages.warning(request, f"Tournament import was deleted but amendment recording failed: {exc}")
        else:
            if recorded_path:
                messages.info(request, f"Recorded delete_tournament_import amendment to {recorded_path}.")

        return redirect("core:tournament_audit_detail", pk=tournament.id)


class TournamentImportMoveView(SuperuserRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        tournament = get_object_or_404(Tournament, pk=kwargs.get("tournament_id"))
        import_row = get_object_or_404(TournamentImport.objects.filter(tournament=tournament), pk=kwargs.get("import_id"))
        form = TournamentImportMoveForm(
            request.POST,
            prefix=f"import-{import_row.id}",
            current_tournament=tournament,
        )
        if not form.is_valid():
            messages.error(request, "Please choose a valid target tournament.")
            return redirect("core:tournament_audit_detail", pk=tournament.id)

        target_tournament = form.cleaned_data["target_tournament"]
        action = build_tournament_import_move_action(import_row, target_tournament.id)
        apply_round_amendments({"actions": [action]}, actor=request.user, source_context={"source": "tournament_import_move"})
        messages.success(
            request,
            f"Moved tournament import {import_row.id} from {tournament.name} to {target_tournament.name}.",
        )

        try:
            recorded_path = record_round_amendment_action(action)
        except Exception as exc:
            messages.warning(request, f"Tournament import was moved but amendment recording failed: {exc}")
        else:
            if recorded_path:
                messages.info(request, f"Recorded move_tournament_import amendment to {recorded_path}.")

        return redirect("core:tournament_audit_detail", pk=tournament.id)
