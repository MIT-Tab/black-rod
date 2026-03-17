import csv
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from django.db.models import Q

from core.access import can_download_debater_tab_cards
from core.models.debater import Debater
from core.models.round import Round, RoundStats
from core.models.team import Team
from core.utils.debater_aliases import load_linked_debater_ids
from core.utils.rounds import visible_canonical_rounds

_PARTNER_PIE_COLORS = (
    "#2563eb",
    "#f97316",
    "#16a34a",
    "#9333ea",
    "#db2777",
    "#0891b2",
    "#ca8a04",
    "#4f46e5",
    "#dc2626",
    "#0d9488",
    "#7c3aed",
    "#ea580c",
)


def _team_debaters_all(team):
    if not team:
        return []
    return list(
        Debater.all_objects.filter(teams__id=team.id)
        .select_related("school")
        .order_by("id")
    )


def _team_debater_names(team):
    return [
        str(debater.name or "").strip()
        for debater in _team_debaters_all(team)
        if str(debater.name or "").strip()
    ]


def _team_debater_apda_ids(team):
    return [
        str(debater.id) if not debater.synthetic else ""
        for debater in _team_debaters_all(team)
    ]


def _team_partner_for_debater(team, subject_debater_ids):
    return next(
        (
            debater
            for debater in _team_debaters_all(team)
            if int(debater.id) not in subject_debater_ids
        ),
        None,
    )


def _format_score_value(value):
    if value is None:
        return "NA"
    return round(float(value), 4)


def _round_label_value(round_obj):
    return str(round_obj.round_label or "").strip() or str(round_obj.round_number or "")


def _round_stage_label(round_obj):
    if getattr(round_obj, "stage", "") == Round.Stage.OUTROUND:
        if getattr(round_obj, "elim_size", None):
            return f"Outround ({round_obj.elim_size})"
        return "Outround"
    return "Prelim"


def _round_result_code(round_obj, team):
    if round_obj.victor == Round.BYE:
        return "BYE"

    gov_wins = {Round.GOV, Round.GOV_VIA_FORFEIT, Round.ALL_WIN}
    opp_wins = {Round.OPP, Round.OPP_VIA_FORFEIT, Round.ALL_WIN}

    if round_obj.gov == team and round_obj.victor in gov_wins:
        if round_obj.victor == Round.GOV_VIA_FORFEIT:
            return "WF"
        if round_obj.victor == Round.ALL_WIN:
            return "AW"
        return "W"

    if round_obj.opp == team and round_obj.victor in opp_wins:
        if round_obj.victor == Round.OPP_VIA_FORFEIT:
            return "WF"
        if round_obj.victor == Round.ALL_WIN:
            return "AW"
        return "W"

    if round_obj.victor == Round.ALL_DROP:
        return "AL"
    if round_obj.victor in {Round.GOV_VIA_FORFEIT, Round.OPP_VIA_FORFEIT, Round.BYE}:
        return "LF"
    return "L"


def _source_team_names(round_obj, side):
    team = round_obj.gov if side == "GOV" else round_obj.opp
    return _team_debater_names(team)


def _team_school_display_from_debaters(debaters):
    school_names = []
    for debater in debaters:
        school_name = getattr(getattr(debater, "school", None), "name", "")
        if school_name and school_name not in school_names:
            school_names.append(school_name)
    return " / ".join(school_names)


def _build_round_stat_map(debater_ids, round_ids):
    grouped = defaultdict(list)
    for row in (
        RoundStats.objects.filter(round_id__in=round_ids, debater_id__in=debater_ids)
        .order_by("round_id", "score_index", "id")
    ):
        grouped[row.round_id].append(row)
    return grouped


def _resolve_round_stat_values(stat_rows):
    if not stat_rows:
        return None, None, ""

    speaks_values = []
    ranks_values = []
    role = ""
    for stat in stat_rows:
        if stat.speaks is not None:
            speaks_values.append(float(stat.speaks))
        if stat.ranks is not None:
            ranks_values.append(float(stat.ranks))
        if not role and stat.debater_role:
            role = stat.debater_role

    speaks_value = (sum(speaks_values) / len(speaks_values)) if speaks_values else None
    ranks_value = (sum(ranks_values) / len(ranks_values)) if ranks_values else None
    return speaks_value, ranks_value, role


def _source_name_from_stat_rows(stat_rows):
    names = []
    for stat in stat_rows or []:
        metadata = stat.metadata if isinstance(stat.metadata, dict) else {}
        speaker_name = str(metadata.get("speaker_name") or "").strip()
        if speaker_name:
            names.append(speaker_name)
    if not names:
        return ""
    counts = defaultdict(int)
    for name in names:
        counts[name] += 1
    return sorted(counts.items(), key=lambda row: (-row[1], row[0].lower()))[0][0]


def _imported_alias_rows(round_obj):
    metadata = getattr(round_obj, "imported_metadata", None)
    if not metadata:
        return []
    return [
        ("GOV", "PM", getattr(metadata, "gov_1_alias", None)),
        ("GOV", "MG", getattr(metadata, "gov_2_alias", None)),
        ("OPP", "LO", getattr(metadata, "opp_1_alias", None)),
        ("OPP", "MO", getattr(metadata, "opp_2_alias", None)),
    ]


def _imported_alias_match(round_obj, linked_debater_ids):
    linked_ids = {int(debater_id) for debater_id in linked_debater_ids}
    for side, role, alias in _imported_alias_rows(round_obj):
        if alias and alias.debater_id in linked_ids:
            return {
                "side": side,
                "role": role,
                "source_name": str(alias.source_name or "").strip(),
            }
    return None


def build_debater_partner_breakdown(debater):
    linked_debater_ids = load_linked_debater_ids([debater.id])
    team_ids = list(
        Team.objects.filter(debaters__id__in=linked_debater_ids, synthetic=False)
        .distinct()
        .values_list("id", flat=True)
    )
    if not team_ids:
        return None

    rounds = (
        visible_canonical_rounds(
            Round.objects.filter(Q(gov_id__in=team_ids) | Q(opp_id__in=team_ids))
        )
        .select_related("gov", "opp")
        .prefetch_related("gov__debaters", "opp__debaters")
        .order_by("tournament__date", "round_number", "id")
    )
    if not rounds.exists():
        return None

    counts = defaultdict(int)
    total_partnered_rounds = 0
    for round_obj in rounds:
        team = None
        gov_member_ids = {member.id for member in _team_debaters_all(round_obj.gov)}
        opp_member_ids = {member.id for member in _team_debaters_all(round_obj.opp)}
        if gov_member_ids & linked_debater_ids:
            team = round_obj.gov
        elif opp_member_ids & linked_debater_ids:
            team = round_obj.opp
        if not team:
            continue
        partner = next(
            (
                member
                for member in _team_debaters_all(team)
                if int(member.id) not in linked_debater_ids and not member.synthetic
            ),
            None,
        )
        if not partner:
            continue
        counts[partner.name] += 1
        total_partnered_rounds += 1

    if not total_partnered_rounds:
        return None

    rows = sorted(
        [{"name": name, "rounds": rounds_count} for name, rounds_count in counts.items()],
        key=lambda row: (-row["rounds"], row["name"].lower()),
    )

    pie_segments = []
    current_pct = 0.0
    for index, row in enumerate(rows):
        row["share_pct"] = (100.0 * row["rounds"] / total_partnered_rounds) if total_partnered_rounds else 0.0
        row["color"] = _PARTNER_PIE_COLORS[index % len(_PARTNER_PIE_COLORS)]
        next_pct = current_pct + row["share_pct"]
        pie_segments.append(f"{row['color']} {current_pct:.3f}% {next_pct:.3f}%")
        current_pct = next_pct

    return {
        "total_partner_rounds": total_partnered_rounds,
        "rows": rows,
        "pie_style": f"conic-gradient({', '.join(pie_segments)})" if pie_segments else "",
    }


@login_required
def debater_tab_cards_csv(request, pk):
    debater = get_object_or_404(Debater, pk=pk)
    if not can_download_debater_tab_cards(request.user, debater):
        raise Http404("Career tab cards are only available for your linked debater profile.")
    linked_debater_ids = load_linked_debater_ids([debater.id])
    team_ids = list(
        Team.objects.filter(debaters__id__in=linked_debater_ids, synthetic=False)
        .distinct()
        .values_list("id", flat=True)
    )
    rounds = (
        visible_canonical_rounds(
            Round.objects.filter(
                Q(stats__debater_id__in=linked_debater_ids)
                | Q(gov_id__in=team_ids)
                | Q(opp_id__in=team_ids)
                | Q(imported_metadata__gov_1_alias__debater_id__in=linked_debater_ids)
                | Q(imported_metadata__gov_2_alias__debater_id__in=linked_debater_ids)
                | Q(imported_metadata__opp_1_alias__debater_id__in=linked_debater_ids)
                | Q(imported_metadata__opp_2_alias__debater_id__in=linked_debater_ids)
            )
        )
        .select_related(
            "tournament",
            "gov",
            "opp",
            "imported_metadata__gov_1_alias",
            "imported_metadata__gov_2_alias",
            "imported_metadata__opp_1_alias",
            "imported_metadata__opp_2_alias",
        )
        .prefetch_related("gov__debaters__school", "opp__debaters__school")
        .distinct()
        .order_by("tournament__date", "tournament_id", "round_number", "id")
    )

    filename = f"{slugify(debater.name) or f'debater-{debater.id}'}-career-tab-cards.csv"
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "debater_name",
            "tournament_name",
            "tournament_date",
            "round_label",
            "stage",
            "partner_name",
            "partner_apda_id",
            "side",
            "result",
            "opponent_1_name",
            "opponent_1_apda_id",
            "opponent_2_name",
            "opponent_2_apda_id",
            "opponent_school",
            "debater_role",
            "speaks",
            "ranks",
        ]
    )

    round_ids = [row.id for row in rounds]
    stats_by_round_id = _build_round_stat_map(linked_debater_ids, round_ids)

    for round_obj in rounds:
        stat_rows = stats_by_round_id.get(round_obj.id)
        imported_alias_match = _imported_alias_match(round_obj, linked_debater_ids)
        gov_member_ids = {member.id for member in _team_debaters_all(round_obj.gov)}
        opp_member_ids = {member.id for member in _team_debaters_all(round_obj.opp)}

        if gov_member_ids & linked_debater_ids:
            team = round_obj.gov
            side = "GOV"
            opponent = round_obj.opp
        elif opp_member_ids & linked_debater_ids:
            team = round_obj.opp
            side = "OPP"
            opponent = round_obj.gov
        else:
            inferred_side = ""
            for stat in stat_rows or []:
                role = str(stat.debater_role or "").strip().upper()
                if role in {"PM", "MG"}:
                    inferred_side = "GOV"
                    break
                if role in {"LO", "MO"}:
                    inferred_side = "OPP"
                    break
            if inferred_side == "GOV":
                team = round_obj.gov
                side = "GOV"
                opponent = round_obj.opp
            elif inferred_side == "OPP":
                team = round_obj.opp
                side = "OPP"
                opponent = round_obj.gov
            elif imported_alias_match and imported_alias_match["side"] == "GOV":
                team = round_obj.gov
                side = "GOV"
                opponent = round_obj.opp
            elif imported_alias_match and imported_alias_match["side"] == "OPP":
                team = round_obj.opp
                side = "OPP"
                opponent = round_obj.gov
            else:
                continue

        partner = _team_partner_for_debater(team, linked_debater_ids)
        speaks_value, ranks_value, debater_role = _resolve_round_stat_values(stat_rows)
        if not debater_role and imported_alias_match:
            debater_role = imported_alias_match["role"]
        source_debater_name = _source_name_from_stat_rows(stat_rows)
        if not source_debater_name and imported_alias_match:
            source_debater_name = imported_alias_match["source_name"]
        if not source_debater_name:
            cleaned_source_names = _source_team_names(round_obj, side)
            role = str(debater_role or "").strip().upper()
            if role in {"PM", "LO"} and len(cleaned_source_names) >= 1:
                source_debater_name = cleaned_source_names[0]
            elif role in {"MG", "MO"} and len(cleaned_source_names) >= 2:
                source_debater_name = cleaned_source_names[1]
            elif cleaned_source_names:
                source_debater_name = cleaned_source_names[0]
        if not source_debater_name:
            source_debater_name = debater.name

        opponent_side = "OPP" if side == "GOV" else "GOV"
        opponent_names = _source_team_names(round_obj, opponent_side) or _team_debater_names(opponent)
        opponent_ids = _team_debater_apda_ids(opponent)

        writer.writerow(
            [
                source_debater_name,
                round_obj.tournament.name,
                round_obj.tournament.date.isoformat() if round_obj.tournament.date else "",
                _round_label_value(round_obj),
                _round_stage_label(round_obj),
                partner.name if partner else "",
                str(partner.id) if partner and not partner.synthetic else "",
                side,
                _round_result_code(round_obj, team),
                opponent_names[0] if len(opponent_names) > 0 else "",
                opponent_ids[0] if len(opponent_ids) > 0 else "",
                opponent_names[1] if len(opponent_names) > 1 else "",
                opponent_ids[1] if len(opponent_ids) > 1 else "",
                _team_school_display_from_debaters(_team_debaters_all(opponent)) if opponent else "",
                debater_role,
                _format_score_value(speaks_value),
                _format_score_value(ranks_value),
            ]
        )

    return response
