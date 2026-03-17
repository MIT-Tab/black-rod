from django.db import transaction

from core.models import (
    COTY,
    TOTY,
    TOTYReaff,
    Debater,
    Round,
    School,
    SchoolLookup,
    SyntheticResolutionLog,
    Team,
    TeamResult,
    Tournament,
)
from core.utils.merge import merge_debaters


def _snapshot_debater(row):
    return {
        "id": int(row.id),
        "name": row.name,
        "school_id": row.school_id,
        "school_name": row.school.name if row.school_id else "",
        "temporary": bool(row.temporary),
        "synthetic": bool(getattr(row, "synthetic", False)),
    }


def _snapshot_school(row):
    return {
        "id": int(row.id),
        "name": row.name,
        "short_name": row.short_name,
        "temporary": bool(row.temporary),
        "synthetic": bool(getattr(row, "synthetic", False)),
    }


def _snapshot_team(row):
    return {
        "id": int(row.id),
        "name": row.name,
        "short_name": row.short_name,
        "member_ids": sorted(list(row.debaters.values_list("id", flat=True))),
        "temporary_members": sorted(
            list(row.debaters.filter(temporary=True).values_list("id", flat=True))
        ),
        "synthetic": bool(getattr(row, "synthetic", False)),
    }


def _create_log(
    entity_type,
    synthetic_row,
    target_row,
    actor=None,
    reason="",
    source_context=None,
    snapshot=None,
):
    snapshot = snapshot or {}
    synthetic_id = getattr(synthetic_row, "id", None)
    synthetic_name = (
        getattr(synthetic_row, "name", "")
        or getattr(synthetic_row, "display_name", "")
        or snapshot.get("name", "")
    )
    if synthetic_id is None:
        synthetic_id = snapshot.get("id")

    return SyntheticResolutionLog.objects.create(
        entity_type=entity_type,
        synthetic_id=int(synthetic_id),
        synthetic_name=str(synthetic_name)[:255],
        resolved_to_id=int(target_row.id),
        resolved_to_name=str(
            getattr(target_row, "name", "")
            or getattr(target_row, "display_name", "")
        )[:255],
        actor=actor,
        reason=str(reason or ""),
        source_context=source_context or {},
        synthetic_snapshot=snapshot,
    )


def resolve_synthetic_debater(
    synthetic_debater,
    target_debater,
    actor=None,
    reason="",
    source_context=None,
):
    if not getattr(synthetic_debater, "synthetic", False):
        raise ValueError("Synthetic source debater must have synthetic=True")
    if getattr(target_debater, "synthetic", False):
        raise ValueError("Target debater must have synthetic=False")
    if synthetic_debater.id == target_debater.id:
        raise ValueError("Cannot resolve synthetic debater into itself")

    snapshot = _snapshot_debater(synthetic_debater)
    with transaction.atomic():
        log = _create_log(
            SyntheticResolutionLog.EntityType.DEBATER,
            synthetic_row=synthetic_debater,
            target_row=target_debater,
            actor=actor,
            reason=reason,
            source_context=source_context,
            snapshot=snapshot,
        )
        result = merge_debaters(primary=target_debater, secondary=synthetic_debater)
    result["log_id"] = int(log.id)
    return result


def resolve_synthetic_school(
    synthetic_school,
    target_school,
    actor=None,
    reason="",
    source_context=None,
):
    if not getattr(synthetic_school, "synthetic", False):
        raise ValueError("Synthetic source school must have synthetic=True")
    if getattr(target_school, "synthetic", False):
        raise ValueError("Target school must have synthetic=False")
    if synthetic_school.id == target_school.id:
        raise ValueError("Cannot resolve synthetic school into itself")

    snapshot = _snapshot_school(synthetic_school)

    with transaction.atomic():
        Debater.all_objects.filter(school=synthetic_school).update(school=target_school)
        SchoolLookup.objects.filter(school=synthetic_school).update(school=target_school)
        Tournament.objects.filter(host=synthetic_school).update(host=target_school)

        for row in COTY.objects.filter(school=synthetic_school).order_by("id"):
            existing = COTY.objects.filter(season=row.season, school=target_school).first()
            if existing:
                existing.points = max(float(existing.points or 0), float(row.points or 0))
                existing.save(update_fields=["points"])
                row.delete()
            else:
                row.school = target_school
                row.save(update_fields=["school"])

        _create_log(
            SyntheticResolutionLog.EntityType.SCHOOL,
            synthetic_row=synthetic_school,
            target_row=target_school,
            actor=actor,
            reason=reason,
            source_context=source_context,
            snapshot=snapshot,
        )
        synthetic_school.delete()


def resolve_synthetic_team(
    synthetic_team,
    target_team,
    actor=None,
    reason="",
    source_context=None,
):
    if not getattr(synthetic_team, "synthetic", False):
        raise ValueError("Synthetic source team must have synthetic=True")
    if getattr(target_team, "synthetic", False):
        raise ValueError("Target team must have synthetic=False")
    if synthetic_team.id == target_team.id:
        raise ValueError("Cannot resolve synthetic team into itself")

    snapshot = _snapshot_team(synthetic_team)

    with transaction.atomic():
        Round.objects.filter(gov=synthetic_team).update(gov=target_team)
        Round.objects.filter(opp=synthetic_team).update(opp=target_team)
        TeamResult.objects.filter(team=synthetic_team).update(team=target_team)

        for row in TOTY.objects.filter(team=synthetic_team).order_by("id"):
            existing = TOTY.objects.filter(season=row.season, team=target_team).first()
            if existing:
                existing.points = max(float(existing.points or 0), float(row.points or 0))
                existing.save(update_fields=["points"])
                row.delete()
            else:
                row.team = target_team
                row.save(update_fields=["team"])

        TOTYReaff.objects.filter(old_team=synthetic_team).update(old_team=target_team)
        TOTYReaff.objects.filter(new_team=synthetic_team).update(new_team=target_team)

        _create_log(
            SyntheticResolutionLog.EntityType.TEAM,
            synthetic_row=synthetic_team,
            target_row=target_team,
            actor=actor,
            reason=reason,
            source_context=source_context,
            snapshot=snapshot,
        )
        synthetic_team.delete()


def resolve_synthetic_entity(
    entity_type,
    synthetic_id,
    target_id,
    actor=None,
    reason="",
    source_context=None,
):
    entity_key = str(entity_type or "").strip().lower()

    if entity_key == SyntheticResolutionLog.EntityType.DEBATER:
        synthetic = Debater.all_objects.get(id=synthetic_id)
        target = Debater.all_objects.get(id=target_id)
        return resolve_synthetic_debater(
            synthetic, target, actor, reason, source_context
        )

    if entity_key == SyntheticResolutionLog.EntityType.SCHOOL:
        synthetic = School.all_objects.get(id=synthetic_id)
        target = School.all_objects.get(id=target_id)
        resolve_synthetic_school(synthetic, target, actor, reason, source_context)
        return {"entity_type": entity_key}

    if entity_key == SyntheticResolutionLog.EntityType.TEAM:
        synthetic = Team.objects.get(id=synthetic_id)
        target = Team.objects.get(id=target_id)
        resolve_synthetic_team(synthetic, target, actor, reason, source_context)
        return {"entity_type": entity_key}

    raise ValueError("Unsupported entity_type=%r" % entity_type)
