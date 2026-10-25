"""Shared-memory snapshots for ephemeral mit-tab sites (never stored in SQL)."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import time
import uuid
import zlib

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

LOGGER = logging.getLogger(__name__)
INDEX_KEY = "mittab-import:index:v1"
RETENTION = 7 * 24 * 60 * 60
CHUNK_SIZE = 250_000
RESULT_LISTS = ("varsity_team_placements", "novice_team_placements", "non_placing_teams",
                "varsity_speaker_awards", "novice_speaker_awards", "new_schools", "new_debater_data")


def source_key(url):
    return hashlib.sha256(url.rstrip("/").encode()).hexdigest()


def source_index():
    now = timezone.now().timestamp()
    return {key: row for key, row in (cache.get(INDEX_KEY) or {}).items()
            if not row.get("missing_since") or now - row["missing_since"] < RETENTION}


def cached_source(key):
    row = source_index().get(key)
    if not row:
        return None
    chunks = cache.get_many(row["chunks"])
    if len(chunks) != len(row["chunks"]):
        return None
    try:
        return json.loads(zlib.decompress(b"".join(chunks[name] for name in row["chunks"])))
    except (ValueError, zlib.error):
        return None


def store_source(url, payload):
    """Chunk compressed payloads below memcached's item limit; publish last."""
    key = source_key(url)
    compressed = zlib.compress(json.dumps(payload).encode())
    generation = uuid.uuid4().hex
    chunks = {f"mittab-import:{key}:{generation}:{offset}": compressed[offset:offset + CHUNK_SIZE]
              for offset in range(0, len(compressed), CHUNK_SIZE)}
    failed = cache.set_many(chunks, timeout=None)
    if failed:
        raise RuntimeError("Could not cache the complete tournament snapshot")
    # Serialize index updates across web and scheduled workers.
    for _attempt in range(100):
        if cache.add(f"{INDEX_KEY}:lock", 1, timeout=30):
            break
        time.sleep(0.01)
    else:
        cache.delete_many(chunks)
        raise RuntimeError("Another worker is updating the tournament cache; try again.")
    try:
        index = source_index()
        previous_chunks = index.get(key, {}).get("chunks", [])
        index[key] = {"url": url, "name": payload["tournament"]["name"],
                      "updated_at": timezone.now().isoformat(), "missing_since": None,
                      "chunks": list(chunks)}
        cache.set(INDEX_KEY, index, timeout=None)
        published = cache.get(INDEX_KEY) or {}
        if published.get(key, {}).get("chunks") != list(chunks):
            raise RuntimeError("Could not publish the tournament snapshot")
    except Exception:
        cache.delete_many(chunks)
        raise
    finally:
        cache.delete(f"{INDEX_KEY}:lock")
    cache.delete_many(previous_chunks)
    return payload


def fetch_import_source(url, *, save=True):
    response = requests.get(f"{url.rstrip('/')}/api/import-results", timeout=(5, 30))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported tournament import snapshot")
    metadata = payload.get("tournament")
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise ValueError("Missing tournament metadata in import snapshot")
    if (not isinstance(payload.get("import_ready"), bool)
            or not all(isinstance(payload.get(name), list) for name in RESULT_LISTS)
            or not isinstance(payload.get("debater_counts"), dict)
            or (payload["import_ready"] and not isinstance(payload.get("bundle"), dict))):
        raise ValueError("Incomplete tournament import snapshot")
    payload["source_url"] = url.rstrip("/")
    return store_source(url, payload) if save else payload


def fresh_or_cached_source(key):
    row = source_index().get(key)
    if not row:
        raise ValueError("Tournament snapshot expired; refresh the source directory.")
    try:
        return fetch_import_source(row["url"])
    except (requests.RequestException, ValueError):
        payload = cached_source(key)
        if payload is None:
            raise ValueError("The tournament is unavailable and its snapshot has expired.") from None
        return payload


def _update_presence(row, present, confirmed_missing):
    if present:
        if row.get("missing_since"):
            for chunk in row["chunks"]:
                cache.touch(chunk, timeout=None)
        row["missing_since"] = None
    elif confirmed_missing and not row.get("missing_since"):
        row["missing_since"] = timezone.now().timestamp()
        for chunk in row["chunks"]:
            cache.touch(chunk, timeout=RETENTION)


def refresh_import_sources():
    """Refresh all current sites; retain disappeared sites for seven days.

    A directory outage or a transient endpoint error must not start eviction.
    """
    configured = [url.strip().rstrip("/") for url in
                  getattr(settings, "LIVE_MITTAB_SOURCES", "").split(",") if url.strip()]
    directory_ok = False
    try:
        response = requests.get(settings.LIVE_MITTAB_DIRECTORY_URL, timeout=5)
        response.raise_for_status()
        configured += [row["url"].rstrip("/") for row in response.json()["tournaments"]]
        directory_ok = True
    except (requests.RequestException, ValueError, KeyError, TypeError):
        LOGGER.warning("Could not refresh the import source directory", exc_info=True)
    urls = set(configured)
    index = source_index()

    def refresh(url):
        try:
            return url, fetch_import_source(url, save=False)
        except (requests.RequestException, ValueError):
            LOGGER.warning("Could not refresh import snapshot for %s", url)
            return url, None

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = dict(executor.map(refresh, urls | {row["url"] for row in index.values()}))
    for url, payload in outcomes.items():
        if payload:
            store_source(url, payload)
    if cache.add(f"{INDEX_KEY}:lock", 1, timeout=30):
        try:
            index = source_index()
            for row in index.values():
                _update_presence(row, row["url"] in urls,
                                 directory_ok and not outcomes.get(row["url"]))
            cache.set(INDEX_KEY, index, timeout=None)
        finally:
            cache.delete(f"{INDEX_KEY}:lock")
    return source_index()


def save_review(payload, tournament_id, user_id):
    """Keep the reviewed payload immutable while the source continues changing."""
    token = uuid.uuid4().hex
    data = zlib.compress(json.dumps(payload).encode())
    chunks = {f"mittab-review:{token}:{offset}": data[offset:offset + CHUNK_SIZE]
              for offset in range(0, len(data), CHUNK_SIZE)}
    if cache.set_many(chunks, timeout=3600):
        raise ValueError("The review snapshot could not be saved; try again.")
    cache.set(f"mittab-review:{token}", (list(chunks), tournament_id, user_id), 3600)
    return token


def load_review(token, tournament_id, user_id):
    row = cache.get(f"mittab-review:{token}")
    if not row or row[1:] != (tournament_id, user_id):
        raise ValueError("This import review expired. Open the tournament import again.")
    chunks = cache.get_many(row[0])
    if len(chunks) != len(row[0]):
        raise ValueError("This import review expired. Open the tournament import again.")
    return json.loads(zlib.decompress(b"".join(chunks[name] for name in row[0])))
