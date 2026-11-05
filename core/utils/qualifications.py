from core.models.standings.qual import QUAL
from core.models.tournament import Tournament


BP_COTY_RULE_START_SEASON = 2026


def qual_awards_coty_bonus(qual_type, season):
    """BP autoquals qualify individuals but stop awarding a bonus in 2026–27."""
    return (
        int(str(season).split("-", maxsplit=1)[0]) < BP_COTY_RULE_START_SEASON
        or qual_type not in Tournament.BP_AUTOQUAL_TYPES
    )


def coty_bonus_quals(quals, season):
    if int(str(season).split("-", maxsplit=1)[0]) >= BP_COTY_RULE_START_SEASON:
        return quals.exclude(qual_type__in=Tournament.BP_AUTOQUAL_TYPES)
    return quals


def sync_manual_autoquals(debater, season):
    """Rebuild derived qualifications from the persistent individual recipients."""
    if not debater.school_id or not debater.school.included_in_oty:
        return
    for recipient in debater.tournament_autoquals.filter(
        tournament__season=season
    ).select_related("tournament"):
        tournament = recipient.tournament
        if tournament.qual_type not in dict(QUAL.QUAL_TYPES) or tournament.qual_type == QUAL.POINTS:
            continue
        QUAL.objects.get_or_create(
            season=season,
            debater=debater,
            qual_type=tournament.qual_type,
            defaults={"tournament": tournament},
        )
