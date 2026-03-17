"""Implements small in-memory TTL caches and stable key/fingerprint helpers that let runtime ELO reuse ingestion and compute work safely."""


import hashlib
import json
import threading
import time
from datetime import date, datetime

from django.db.models import Count, Max, Q

from core.models import Debater, QUAL, Round, RoundStats


CACHE_TTL_SECONDS = 60 * 10
INGESTION_CACHE_MAX_ITEMS = 2
PIPELINE_CACHE_MAX_ITEMS = 2

_CACHE_LOCK = threading.Lock()
_INGESTION_CACHE = {}
_PIPELINE_CACHE = {}


def _iso(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _stable_hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _trim(cache_store, max_items):
    now = time.time()
    expired = [key for key, row in cache_store.items() if float(row.get("expires", 0)) <= now]
    for key in expired:
        cache_store.pop(key, None)

    while len(cache_store) > max_items:
        oldest_key = min(cache_store.keys(), key=lambda key: float(cache_store[key].get("last_access", 0)))
        cache_store.pop(oldest_key, None)


def _get(cache_store, key):
    now = time.time()
    row = cache_store.get(key)
    if not row:
        return None
    if float(row.get("expires", 0)) <= now:
        cache_store.pop(key, None)
        return None
    row["last_access"] = now
    return row.get("value")


def _set(cache_store, key, value, max_items):
    now = time.time()
    cache_store[key] = {
        "value": value,
        "expires": now + CACHE_TTL_SECONDS,
        "last_access": now,
    }
    _trim(cache_store, max_items)


def data_fingerprint():
    round_agg = Round.objects.aggregate(count=Count("id"), max_id=Max("id"))
    round_stats_agg = RoundStats.objects.aggregate(count=Count("id"), max_id=Max("id"))
    qual_agg = QUAL.objects.aggregate(count=Count("id"), max_id=Max("id"))
    debater_agg = Debater.all_objects.aggregate(
        count=Count("id"),
        max_id=Max("id"),
        opt_in_count=Count("id", filter=Q(elo_manual_opt=Debater.EloManualOpt.OPT_IN)),
        opt_out_count=Count("id", filter=Q(elo_manual_opt=Debater.EloManualOpt.OPT_OUT)),
    )

    debater_identity_rows = list(
        Debater.all_objects.order_by("id").values_list("id", "alias_group_id", "elo_manual_opt")
    )

    payload = {
        "round_count": int(round_agg.get("count") or 0),
        "round_max_id": int(round_agg.get("max_id") or 0),
        "round_stats_count": int(round_stats_agg.get("count") or 0),
        "round_stats_max_id": int(round_stats_agg.get("max_id") or 0),
        "qual_count": int(qual_agg.get("count") or 0),
        "qual_max_id": int(qual_agg.get("max_id") or 0),
        "debater_count": int(debater_agg.get("count") or 0),
        "debater_max_id": int(debater_agg.get("max_id") or 0),
        "debater_opt_in_count": int(debater_agg.get("opt_in_count") or 0),
        "debater_opt_out_count": int(debater_agg.get("opt_out_count") or 0),
        "debater_identity_rows": [
            [int(debater_id), int(alias_group_id) if alias_group_id else 0, str(elo_manual_opt or "")]
            for debater_id, alias_group_id, elo_manual_opt in debater_identity_rows
            if debater_id is not None
        ],
    }
    return _stable_hash(payload)


def ingestion_cache_key(*, allowed_seasons, include_novice, include_proam, completed_only, max_date, fingerprint):
    payload = {
        "allowed_seasons": sorted({str(season).strip() for season in allowed_seasons if str(season).strip()}),
        "include_novice": bool(include_novice),
        "include_proam": bool(include_proam),
        "completed_only": bool(completed_only),
        "max_date": _iso(max_date),
        "fingerprint": str(fingerprint or ""),
    }
    return "ingest:%s" % _stable_hash(payload)


def pipeline_cache_key(*, args_payload, fingerprint):
    payload = dict(args_payload)
    payload["fingerprint"] = str(fingerprint or "")
    return "pipeline:%s" % _stable_hash(payload)


def get_ingestion_cached(key):
    with _CACHE_LOCK:
        return _get(_INGESTION_CACHE, key)


def set_ingestion_cached(key, value):
    with _CACHE_LOCK:
        _set(_INGESTION_CACHE, key, value, INGESTION_CACHE_MAX_ITEMS)


def get_pipeline_cached(key):
    with _CACHE_LOCK:
        return _get(_PIPELINE_CACHE, key)


def set_pipeline_cached(key, value):
    with _CACHE_LOCK:
        _set(_PIPELINE_CACHE, key, value, PIPELINE_CACHE_MAX_ITEMS)


def clear_runtime_caches():
    with _CACHE_LOCK:
        _INGESTION_CACHE.clear()
        _PIPELINE_CACHE.clear()
