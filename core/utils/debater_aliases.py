"""Shared alias-group helpers for collapsing linked debater profiles."""

from collections import defaultdict

from core.models import Debater


def build_representative_debater_maps(id_alias_rows):
    alias_group_to_ids = defaultdict(set)
    representative_by_id = {}
    linked_ids_by_representative = {}

    for debater_id, alias_group_id in id_alias_rows:
        if debater_id is None:
            continue
        debater_id = int(debater_id)
        if alias_group_id:
            alias_group_to_ids[int(alias_group_id)].add(debater_id)
            continue
        representative_by_id[debater_id] = debater_id
        linked_ids_by_representative[debater_id] = {debater_id}

    for member_ids in alias_group_to_ids.values():
        cluster = {int(member_id) for member_id in member_ids if member_id is not None}
        if not cluster:
            continue
        representative_id = min(cluster)
        linked_ids_by_representative[representative_id] = cluster
        for member_id in cluster:
            representative_by_id[member_id] = representative_id

    return representative_by_id, linked_ids_by_representative


def load_representative_debater_maps():
    return build_representative_debater_maps(
        Debater.all_objects.values_list("id", "alias_group_id")
    )


def load_linked_debater_ids(debater_ids):
    normalized_ids = {
        int(debater_id)
        for debater_id in (debater_ids or [])
        if debater_id is not None
    }
    if not normalized_ids:
        return set()

    base_rows = list(
        Debater.all_objects.filter(id__in=normalized_ids).values_list("id", "alias_group_id")
    )
    alias_group_ids = {
        int(alias_group_id)
        for _debater_id, alias_group_id in base_rows
        if alias_group_id
    }
    cluster_rows = []
    if alias_group_ids:
        cluster_rows = list(
            Debater.all_objects.filter(alias_group_id__in=alias_group_ids).values_list(
                "id",
                "alias_group_id",
            )
        )

    representative_by_id, linked_ids_by_representative = build_representative_debater_maps(
        [*base_rows, *cluster_rows]
    )
    resolved_ids = set()
    for debater_id in normalized_ids:
        representative_id = representative_by_id.get(debater_id, debater_id)
        resolved_ids.update(
            linked_ids_by_representative.get(representative_id, {debater_id})
        )
    return resolved_ids
