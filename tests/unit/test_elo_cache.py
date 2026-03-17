"""Unit coverage for ELO cache round-trips and signature-scoped retrieval so mismatched display/computation settings do not reuse stale state."""


from types import SimpleNamespace

from core.views.elo_cache import (
    get_cached_elo_state,
    invalidate_cached_elo_state,
    set_cached_elo_state,
    settings_signature,
)


def test_elo_cache_state_round_trip():
    marker = {"ok": True}
    request = SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, pk=77),
        session=None,
    )
    set_cached_elo_state(request=request, result=marker)

    result = get_cached_elo_state(request)

    assert result == marker


def test_elo_cache_respects_compute_and_display_signatures():
    request = SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, pk=88),
        session=None,
    )
    marker = {"ok": True}
    compute_signature = settings_signature({"seasons": ["2024", "2025"], "k_max": 40})
    display_signature = settings_signature({"min_rounds": 0, "active_seasons": ["2025"]})
    other_display_signature = settings_signature({"min_rounds": 5, "active_seasons": ["2025"]})

    set_cached_elo_state(
        request=request,
        result=marker,
        compute_signature=compute_signature,
        display_signature=display_signature,
    )

    assert get_cached_elo_state(
        request,
        compute_signature=compute_signature,
        display_signature=display_signature,
    ) == marker
    assert get_cached_elo_state(
        request,
        compute_signature=compute_signature,
        display_signature=other_display_signature,
    ) is None


def test_elo_cache_invalidation_bumps_namespace_and_hides_old_entries():
    request = SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, pk=99),
        session=None,
    )
    marker = {"ok": True}

    set_cached_elo_state(request=request, result=marker)

    assert get_cached_elo_state(request) == marker

    invalidate_cached_elo_state()

    assert get_cached_elo_state(request) is None
