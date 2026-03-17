from decimal import Decimal

from dal import autocomplete
from django import forms
from django.core.exceptions import ObjectDoesNotExist

from core.models import Debater, ImportedRoundMetadata, Round


ROUND_BALLOT_SLOTS = (
    {
        "label": "Gov 1",
        "side": "gov",
        "member_index": 0,
        "alias_field": "gov_1_alias",
        "import_role_field": "gov_1_role",
        "debater_field": "gov_1_debater",
        "source_name_field": "gov_1_source_name",
        "role_field": "gov_1_role",
        "speaks_field": "gov_1_speaks",
        "ranks_field": "gov_1_ranks",
    },
    {
        "label": "Gov 2",
        "side": "gov",
        "member_index": 1,
        "alias_field": "gov_2_alias",
        "import_role_field": "gov_2_role",
        "debater_field": "gov_2_debater",
        "source_name_field": "gov_2_source_name",
        "role_field": "gov_2_role",
        "speaks_field": "gov_2_speaks",
        "ranks_field": "gov_2_ranks",
    },
    {
        "label": "Opp 1",
        "side": "opp",
        "member_index": 0,
        "alias_field": "opp_1_alias",
        "import_role_field": "opp_1_role",
        "debater_field": "opp_1_debater",
        "source_name_field": "opp_1_source_name",
        "role_field": "opp_1_role",
        "speaks_field": "opp_1_speaks",
        "ranks_field": "opp_1_ranks",
    },
    {
        "label": "Opp 2",
        "side": "opp",
        "member_index": 1,
        "alias_field": "opp_2_alias",
        "import_role_field": "opp_2_role",
        "debater_field": "opp_2_debater",
        "source_name_field": "opp_2_source_name",
        "role_field": "opp_2_role",
        "speaks_field": "opp_2_speaks",
        "ranks_field": "opp_2_ranks",
    },
)


class TournamentRoundBallotForm(forms.Form):
    OUTROUND_STAGE_CHOICES = (
        (2, "Final"),
        (4, "Semifinal"),
        (8, "Quarterfinal"),
        (16, "Octafinal"),
        (32, "Double Octafinal"),
        (64, "Triple Octafinal"),
        (128, "Quadruple Octafinal"),
    )
    OUTROUND_STAGE_LABELS = dict(OUTROUND_STAGE_CHOICES)
    GOV_ROLE_CHOICES = (
        ("", "---------"),
        (ImportedRoundMetadata.SpeakerRole.PM, "PM"),
        (ImportedRoundMetadata.SpeakerRole.MG, "MG"),
    )
    OPP_ROLE_CHOICES = (
        ("", "---------"),
        (ImportedRoundMetadata.SpeakerRole.LO, "LO"),
        (ImportedRoundMetadata.SpeakerRole.MO, "MO"),
    )

    canonical_round_name = forms.CharField(max_length=32, required=False)
    source_round_name = forms.CharField(max_length=128, required=False)
    stage = forms.ChoiceField(choices=Round.Stage.choices)
    outround_stage = forms.TypedChoiceField(
        choices=(("", "---------"),) + OUTROUND_STAGE_CHOICES,
        coerce=int,
        empty_value=None,
        required=False,
    )
    round_number = forms.IntegerField(min_value=1, required=False)
    victor = forms.TypedChoiceField(
        choices=Round.VICTOR_CHOICES,
        coerce=int,
        empty_value=Round.UNKNOWN,
    )
    is_rated = forms.BooleanField(required=False, initial=True)
    weight = forms.DecimalField(
        required=False,
        min_value=Decimal("0.0001"),
        max_digits=6,
        decimal_places=4,
        initial=Decimal("1.0"),
    )

    gov_1_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="Gov 1",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    gov_1_source_name = forms.CharField(max_length=128, required=False, label="Gov 1 Source Name")
    gov_1_role = forms.ChoiceField(required=False, choices=GOV_ROLE_CHOICES, label="Gov 1 Role")
    gov_1_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Gov 1 Speaks")
    gov_1_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Gov 1 Ranks")

    gov_2_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="Gov 2",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    gov_2_source_name = forms.CharField(max_length=128, required=False, label="Gov 2 Source Name")
    gov_2_role = forms.ChoiceField(required=False, choices=GOV_ROLE_CHOICES, label="Gov 2 Role")
    gov_2_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Gov 2 Speaks")
    gov_2_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Gov 2 Ranks")

    opp_1_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="Opp 1",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    opp_1_source_name = forms.CharField(max_length=128, required=False, label="Opp 1 Source Name")
    opp_1_role = forms.ChoiceField(required=False, choices=OPP_ROLE_CHOICES, label="Opp 1 Role")
    opp_1_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Opp 1 Speaks")
    opp_1_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Opp 1 Ranks")

    opp_2_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="Opp 2",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    opp_2_source_name = forms.CharField(max_length=128, required=False, label="Opp 2 Source Name")
    opp_2_role = forms.ChoiceField(required=False, choices=OPP_ROLE_CHOICES, label="Opp 2 Role")
    opp_2_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Opp 2 Speaks")
    opp_2_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="Opp 2 Ranks")

    def __init__(self, *args, tournament, round_obj=None, **kwargs):
        self.tournament = tournament
        self.round_obj = round_obj
        super().__init__(*args, **kwargs)

        debater_queryset = Debater.all_objects.select_related("school").order_by(
            "first_name",
            "last_name",
            "id",
        )
        for field in self.fields.values():
            if isinstance(field.widget, autocomplete.ModelSelect2):
                field.queryset = debater_queryset
                continue
            css_class = (
                "form-check-input"
                if isinstance(field.widget, forms.CheckboxInput)
                else "form-control"
            )
            existing = str(field.widget.attrs.get("class") or "").strip()
            field.widget.attrs["class"] = ("%s %s" % (existing, css_class)).strip()

        if not self.is_bound and round_obj is not None:
            self.initial.update(self.initial_from_round(round_obj))

        self.ballot_rows = [
            {
                "label": slot["label"],
                "side": slot["side"],
                "debater": self[slot["debater_field"]],
                "source_name": self[slot["source_name_field"]],
                "role": self[slot["role_field"]],
                "speaks": self[slot["speaks_field"]],
                "ranks": self[slot["ranks_field"]],
            }
            for slot in ROUND_BALLOT_SLOTS
        ]

    def clean(self):
        cleaned_data = super().clean()
        selected_ids = []
        role_selections = {"gov": [], "opp": []}

        for slot in ROUND_BALLOT_SLOTS:
            debater = cleaned_data.get(slot["debater_field"])
            if debater is None:
                continue
            if debater.id in selected_ids:
                self.add_error(
                    slot["debater_field"],
                    "Each ballot slot must use a different debater.",
                )
                continue
            selected_ids.append(debater.id)
            if debater.school_id is None:
                self.add_error(
                    slot["debater_field"],
                    "Selected debaters need a school so a canonical team can be built.",
                )

            role = str(cleaned_data.get(slot["role_field"]) or "").strip()
            if role:
                role_selections[slot["side"]].append((slot["role_field"], role))

        for side in ("gov", "opp"):
            seen_roles = set()
            for field_name, role in role_selections[side]:
                if role in seen_roles:
                    self.add_error(field_name, "Each side can only use a role once.")
                    continue
                seen_roles.add(role)

        if cleaned_data.get("stage") != Round.Stage.OUTROUND:
            cleaned_data["outround_stage"] = None

        if not str(cleaned_data.get("canonical_round_name") or "").strip():
            cleaned_data["canonical_round_name"] = self.default_round_name(
                cleaned_data.get("stage"),
                cleaned_data.get("round_number"),
                cleaned_data.get("outround_stage"),
            )

        weight = cleaned_data.get("weight")
        if weight in (None, ""):
            cleaned_data["weight"] = Decimal("1.0")

        return cleaned_data

    @classmethod
    def default_round_name(cls, stage, round_number, outround_stage):
        number = int(round_number or 0)
        if str(stage or "") == Round.Stage.OUTROUND:
            label = cls.OUTROUND_STAGE_LABELS.get(int(outround_stage or 0))
            if label:
                return label
            return "E%s" % (number or "?")
        return "P%s" % (number or "?")

    @classmethod
    def initial_from_round(cls, round_obj):
        metadata = round_obj.metadata if isinstance(round_obj.metadata, dict) else {}
        imported_metadata = cls._imported_metadata(round_obj)
        team_a_names = cls._metadata_name_list(metadata.get("team_a_names"))
        team_b_names = cls._metadata_name_list(metadata.get("team_b_names"))
        team_a_ids = cls._metadata_id_list(metadata.get("team_a_ids"))
        team_b_ids = cls._metadata_id_list(metadata.get("team_b_ids"))
        stat_map = cls._build_stat_map_by_debater(round_obj)

        gov_members = cls._ordered_team_members(round_obj.gov, team_a_ids)
        opp_members = cls._ordered_team_members(round_obj.opp, team_b_ids)

        initial = {
            "canonical_round_name": str(round_obj.round_label or "").strip(),
            "source_round_name": str(metadata.get("source_round_name") or "").strip(),
            "stage": round_obj.stage,
            "outround_stage": round_obj.elim_size,
            "round_number": round_obj.round_number,
            "victor": round_obj.victor,
            "is_rated": bool(round_obj.is_rated),
            "weight": round_obj.weight,
        }

        for slot in ROUND_BALLOT_SLOTS:
            members = gov_members if slot["side"] == "gov" else opp_members
            source_names = team_a_names if slot["side"] == "gov" else team_b_names
            alias = (
                getattr(imported_metadata, slot["alias_field"], None)
                if imported_metadata is not None
                else None
            )
            fallback_debater = (
                members[slot["member_index"]]
                if len(members) > slot["member_index"]
                else None
            )
            debater = getattr(alias, "debater", None) or fallback_debater
            debater_data = stat_map.get(getattr(debater, "id", None), {})
            fallback_source_name = (
                source_names[slot["member_index"]]
                if len(source_names) > slot["member_index"]
                else ""
            )

            initial[slot["debater_field"]] = debater
            initial[slot["source_name_field"]] = (
                getattr(alias, "source_name", "")
                or debater_data.get("speaker_name")
                or fallback_source_name
                or (debater.name if debater else "")
            )
            initial[slot["role_field"]] = str(
                getattr(imported_metadata, slot["import_role_field"], "") or debater_data.get("role") or ""
            ).strip()
            initial[slot["speaks_field"]] = debater_data.get("speaks")
            initial[slot["ranks_field"]] = debater_data.get("ranks")

        return initial

    @staticmethod
    def _imported_metadata(round_obj):
        try:
            return round_obj.imported_metadata
        except ObjectDoesNotExist:
            return None

    @staticmethod
    def _metadata_name_list(raw_value):
        if not isinstance(raw_value, list):
            return []
        return [str(value or "").strip() for value in raw_value if str(value or "").strip()]

    @staticmethod
    def _metadata_id_list(raw_value):
        if not isinstance(raw_value, list):
            return []
        values = []
        for value in raw_value:
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                continue
        return values

    @staticmethod
    def _ordered_team_members(team, ordered_ids):
        if team is None:
            return []
        by_id = {
            debater.id: debater
            for debater in team.debaters.all().select_related("school").order_by("id")
        }
        ordered_members = []
        for debater_id in ordered_ids:
            debater = by_id.pop(debater_id, None)
            if debater is not None:
                ordered_members.append(debater)
        ordered_members.extend(by_id.values())
        return ordered_members

    @staticmethod
    def _build_stat_map_by_debater(round_obj):
        grouped = {}
        aggregates = {}
        stat_rows = round_obj.stats.select_related("debater").order_by("score_index", "id")
        for stat in stat_rows:
            if stat.debater_id not in grouped:
                metadata = stat.metadata if isinstance(stat.metadata, dict) else {}
                grouped[stat.debater_id] = {
                    "role": str(stat.debater_role or "").strip().upper(),
                    "speaker_name": str(metadata.get("speaker_name") or "").strip(),
                }
                aggregates[stat.debater_id] = {
                    "speaks_total": Decimal("0"),
                    "speaks_count": 0,
                    "ranks_total": Decimal("0"),
                    "ranks_count": 0,
                }
            if stat.speaks is not None:
                aggregates[stat.debater_id]["speaks_total"] += stat.speaks
                aggregates[stat.debater_id]["speaks_count"] += 1
            if stat.ranks is not None:
                aggregates[stat.debater_id]["ranks_total"] += stat.ranks
                aggregates[stat.debater_id]["ranks_count"] += 1

        for debater_id, summary in grouped.items():
            counts = aggregates[debater_id]
            summary["speaks"] = (
                counts["speaks_total"] / counts["speaks_count"]
                if counts["speaks_count"]
                else None
            )
            summary["ranks"] = (
                counts["ranks_total"] / counts["ranks_count"]
                if counts["ranks_count"]
                else None
            )
        return grouped
