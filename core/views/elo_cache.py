"""Caches ELO dashboard results per user/session, with deterministic compute/display signatures so runs can be reused across equivalent settings."""


import hashlib
import json

from django.core.cache import cache


ELO_CACHE_TIMEOUT_SECONDS = 60 * 60 * 6
ELO_CACHE_KEY_PREFIX = "elo_dashboard_state"
ELO_CACHE_NAMESPACE_VERSION_KEY = f"{ELO_CACHE_KEY_PREFIX}:namespace_version"
ELO_CACHE_NAMESPACE_VERSION_DEFAULT = 1


def _namespace_version():
    raw_value = cache.get(ELO_CACHE_NAMESPACE_VERSION_KEY)
    try:
        version = int(raw_value)
    except (TypeError, ValueError):
        version = ELO_CACHE_NAMESPACE_VERSION_DEFAULT
        cache.set(ELO_CACHE_NAMESPACE_VERSION_KEY, version, None)
    return version


def _cache_key(request):
    key_prefix = f"{ELO_CACHE_KEY_PREFIX}:v{_namespace_version()}"
    if request is None:
        return f"{key_prefix}:global"

    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"{key_prefix}:user:{user.pk}"

    session = getattr(request, "session", None)
    if session is None:
        return f"{key_prefix}:anon:global"
    if not session.session_key:
        session.save()
    return f"{key_prefix}:anon:{session.session_key}"


def _settings_cache_key(request, compute_signature, display_signature):
    return (
        f"{_cache_key(request)}:compute:{str(compute_signature)}:display:{str(display_signature)}"
    )


def settings_signature(payload):
    normalized = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def get_cached_elo_state(request, compute_signature=None, display_signature=None):
    if compute_signature and display_signature:
        payload = cache.get(_settings_cache_key(request, compute_signature, display_signature))
        if not isinstance(payload, dict):
            return None
        return payload.get("result")

    payload = cache.get(_cache_key(request))
    if not isinstance(payload, dict):
        return None
    return payload.get("result")


def set_cached_elo_state(request, result, compute_signature=None, display_signature=None):
    payload = {
        "result": result,
    }

    if compute_signature and display_signature:
        cache.set(
            _settings_cache_key(request, compute_signature, display_signature),
            {
                "result": result,
            },
            ELO_CACHE_TIMEOUT_SECONDS,
        )
        payload.update(
            {
                "compute_signature": str(compute_signature),
                "display_signature": str(display_signature),
            }
        )

    cache.set(
        _cache_key(request),
        payload,
        ELO_CACHE_TIMEOUT_SECONDS,
    )


def invalidate_cached_elo_state():
    next_version = _namespace_version() + 1
    cache.set(ELO_CACHE_NAMESPACE_VERSION_KEY, next_version, None)
    return next_version
