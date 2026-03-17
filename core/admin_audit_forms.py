from decimal import Decimal

from dal import autocomplete
from django import forms

from core.models import Debater, Round


ROUND_BALLOT_SLOTS = (
    {
        "role": "PM",
        "side": "gov",
        "member_index": 0,
        "debater_field": "pm_debater",
        "source_name_field": "pm_source_name",
        "speaks_field": "pm_speaks",
        "ranks_field": "pm_ranks",
    },
    {
        "role": "MG",
        "side": "gov",
        "member_index": 1,
        "debater_field": "mg_debater",
        "source_name_field": "mg_source_name",
        "speaks_field": "mg_speaks",
        "ranks_field": "mg_ranks",
    },
    {
        "role": "LO",
        "side": "opp",
        "member_index": 0,
        "debater_field": "lo_debater",
        "source_name_field": "lo_source_name",
        "speaks_field": "lo_speaks",
        "ranks_field": "lo_ranks",
    },
    {
        "role": "MO",
        "side": "opp",
        "member_index": 1,
        "debater_field": "mo_debater",
        "source_name_field": "mo_source_name",
        "speaks_field": "mo_speaks",
        "ranks_field": "mo_ranks",
    },
)


class TournamentRoundBallotForm(forms.Form):
    canonical_round_name = forms.CharField(max_length=32)
    source_round_name = forms.CharField(max_length=128, required=False)
    stage = forms.ChoiceField(choices=Round.Stage.choices)
    round_number = forms.IntegerField(min_value=1)
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

    pm_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="PM",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    pm_source_name = forms.CharField(max_length=128, required=False, label="PM Source Name")
    pm_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="PM Speaks")
    pm_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="PM Ranks")

    mg_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="MG",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    mg_source_name = forms.CharField(max_length=128, required=False, label="MG Source Name")
    mg_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="MG Speaks")
    mg_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="MG Ranks")

    lo_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="LO",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    lo_source_name = forms.CharField(max_length=128, required=False, label="LO Source Name")
    lo_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="LO Speaks")
    lo_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="LO Ranks")

    mo_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.none(),
        label="MO",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    mo_source_name = forms.CharField(max_length=128, required=False, label="MO Source Name")
    mo_speaks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="MO Speaks")
    mo_ranks = forms.DecimalField(required=False, max_digits=6, decimal_places=4, label="MO Ranks")

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

    def clean(self):
        cleaned_data = super().clean()
        selected_ids = []

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

        weight = cleaned_data.get("weight")
        if weight in (None, ""):
            cleaned_data["weight"] = Decimal("1.0")

        return cleaned_data

    @classmethod
    def initial_from_round(cls, round_obj):
        metadata = round_obj.metadata if isinstance(round_obj.metadata, dict) else {}
        team_a_names = cls._metadata_name_list(metadata.get("team_a_names"))
        team_b_names = cls._metadata_name_list(metadata.get("team_b_names"))
        stat_map = cls._build_role_stat_map(round_obj)
        imported_metadata = getattr(round_obj, "imported_metadata", None)
        alias_name_by_role = cls._alias_name_by_role(imported_metadata)

        gov_members = list(
            round_obj.gov.debaters.all().select_related("school").order_by("id")
        )
        opp_members = list(
            round_obj.opp.debaters.all().select_related("school").order_by("id")
        )

        initial = {
            "canonical_round_name": str(round_obj.round_label or "").strip(),
            "source_round_name": str(metadata.get("source_round_name") or "").strip(),
            "stage": round_obj.stage,
            "round_number": round_obj.round_number,
            "victor": round_obj.victor,
            "is_rated": bool(round_obj.is_rated),
            "weight": round_obj.weight,
        }

        for slot in ROUND_BALLOT_SLOTS:
            role_data = stat_map.get(slot["role"], {})
            members = gov_members if slot["side"] == "gov" else opp_members
            source_names = team_a_names if slot["side"] == "gov" else team_b_names
            fallback_debater = (
                members[slot["member_index"]]
                if len(members) > slot["member_index"]
                else None
            )
            debater = role_data.get("debater") or fallback_debater
            initial[slot["debater_field"]] = debater

            fallback_source_name = (
                source_names[slot["member_index"]]
                if len(source_names) > slot["member_index"]
                else ""
            )
            initial[slot["source_name_field"]] = (
                role_data.get("speaker_name")
                or alias_name_by_role.get(slot["role"])
                or fallback_source_name
                or (debater.name if debater else "")
            )
            initial[slot["speaks_field"]] = role_data.get("speaks")
            initial[slot["ranks_field"]] = role_data.get("ranks")

        return initial

    @staticmethod
    def _alias_name_by_role(imported_metadata):
        if imported_metadata is None:
            return {}
        role_map = {}
        for alias_field, role_field in (
            ("gov_1_alias", "gov_1_role"),
            ("gov_2_alias", "gov_2_role"),
            ("opp_1_alias", "opp_1_role"),
            ("opp_2_alias", "opp_2_role"),
        ):
            alias = getattr(imported_metadata, alias_field, None)
            role = str(getattr(imported_metadata, role_field, "") or "").strip()
            if alias is not None and role:
                role_map[role] = alias.source_name
        return role_map

    @staticmethod
    def _metadata_name_list(raw_value):
        if not isinstance(raw_value, list):
            return []
        return [str(value or "").strip() for value in raw_value if str(value or "").strip()]

    @staticmethod
    def _build_role_stat_map(round_obj):
        grouped = {}
        aggregates = {}
        stat_rows = round_obj.stats.select_related("debater").order_by("score_index", "id")
        for stat in stat_rows:
            role = str(stat.debater_role or "").strip().upper()
            if role not in {"PM", "MG", "LO", "MO"}:
                continue
            metadata = stat.metadata if isinstance(stat.metadata, dict) else {}
            if role not in grouped:
                grouped[role] = {
                    "debater": stat.debater,
                    "speaker_name": str(metadata.get("speaker_name") or "").strip(),
                }
                aggregates[role] = {
                    "speaks_total": Decimal("0"),
                    "speaks_count": 0,
                    "ranks_total": Decimal("0"),
                    "ranks_count": 0,
                }
            if stat.speaks is not None:
                aggregates[role]["speaks_total"] += stat.speaks
                aggregates[role]["speaks_count"] += 1
            if stat.ranks is not None:
                aggregates[role]["ranks_total"] += stat.ranks
                aggregates[role]["ranks_count"] += 1

        for role, summary in grouped.items():
            counts = aggregates[role]
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
