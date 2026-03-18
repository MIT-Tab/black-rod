"""Top-level runtime ELO orchestrator that parses arguments, reuses caches, applies rating updates, and builds filtered dashboard ranking rows."""


from datetime import date

from core.utils.elo_runtime_engine.cache import (
    data_fingerprint,
    get_ingestion_cached,
    get_pipeline_cached,
    ingestion_cache_key,
    pipeline_cache_key,
    set_ingestion_cached,
    set_pipeline_cached,
)
from core.utils.elo_runtime_engine.constants import (
    DEFAULT_HIGHER_ELO_LOSS_SHARE,
    DEFAULT_HIGHER_ELO_WIN_SHARE,
    DEFAULT_K_DECAY_SCALE,
    DEFAULT_K_MAX,
    DEFAULT_K_MIN,
    DEFAULT_RATING,
    INDIVIDUAL_MODE,
    PARTNER_MODE,
    parse_iso_date,
)
from core.utils.elo_runtime_engine.ingestion import build_ingested_snapshots_and_debates
from core.utils.elo_runtime_engine.models import EloComputeResult, EloRunResult
from core.utils.elo_runtime_engine.profiles import build_dashboard_payload
from core.utils.elo_runtime_engine.rating import apply_elo


def _arg_date(args, key, default=None):
    value = getattr(args, key, default)
    if value is None:
        return None
    return parse_iso_date(value) if isinstance(value, str) else value


def _clamp_share(value, default_value):
    return max(0.0, min(100.0, float(value if value is not None else default_value))) / 100.0


def _weighted_stage_count(debates, stage):
    return sum(1 for row in debates if row.stage == stage)


def _arg_bool(args, key, default=False):
    return bool(getattr(args, key, default))


def _arg_int(args, key, default=0):
    return int(getattr(args, key, default))


def _arg_float(args, key, default=0.0):
    return float(getattr(args, key, default))


def _pipeline_cache_payload(
    *,
    allowed_seasons,
    include_novice,
    include_proam,
    completed_only,
    max_date,
    initial_rating,
    k_max,
    k_min,
    k_decay_scale,
    higher_elo_win_share,
    higher_elo_loss_share,
    ignore_partners,
    exclude_proam_partnerships,
):
    return {
        "allowed_seasons": sorted(allowed_seasons),
        "include_novice": bool(include_novice),
        "include_proam": bool(include_proam),
        "completed_only": bool(completed_only),
        "max_date": max_date.isoformat() if max_date else "",
        "initial_rating": float(initial_rating),
        "k_max": float(k_max),
        "k_min": float(k_min),
        "k_decay_scale": float(k_decay_scale),
        "higher_elo_win_share": float(higher_elo_win_share),
        "higher_elo_loss_share": float(higher_elo_loss_share),
        "ignore_partners": bool(ignore_partners),
        "exclude_proam_partnerships": bool(exclude_proam_partnerships),
    }


def run_elo_pipeline(args):
    active_seasons = getattr(args, "active_seasons", [])
    min_rounds = _arg_int(args, "min_rounds")
    min_outrounds = _arg_int(args, "min_outrounds")
    top = _arg_int(args, "top")
    exclude_dino_rounds = _arg_bool(args, "exclude_dino_rounds")

    higher_elo_win_share = _clamp_share(
        getattr(args, "higher_elo_win_share", None),
        DEFAULT_HIGHER_ELO_WIN_SHARE,
    )
    higher_elo_loss_share = _clamp_share(
        getattr(args, "higher_elo_loss_share", None),
        DEFAULT_HIGHER_ELO_LOSS_SHARE,
    )
    allowed_seasons = {str(season).strip() for season in getattr(args, "seasons", []) if str(season).strip()}
    max_date = _arg_date(args, "max_date", date.today())
    initial_rating = _arg_float(args, "initial_rating", DEFAULT_RATING)
    k_max = _arg_float(args, "k_max", DEFAULT_K_MAX)
    k_min = _arg_float(args, "k_min", DEFAULT_K_MIN)
    k_decay_scale = _arg_float(args, "k_decay_scale", DEFAULT_K_DECAY_SCALE)
    ignore_partners = _arg_bool(args, "ignore_partners")
    include_novice = _arg_bool(args, "include_novice")
    include_proam = False
    completed_only = _arg_bool(args, "completed_only")
    exclude_proam_partnerships = _arg_bool(args, "exclude_proam_partnerships")

    fingerprint = data_fingerprint()
    cache_payload = _pipeline_cache_payload(
        allowed_seasons=allowed_seasons,
        include_novice=include_novice,
        include_proam=include_proam,
        completed_only=completed_only,
        max_date=max_date,
        initial_rating=initial_rating,
        k_max=k_max,
        k_min=k_min,
        k_decay_scale=k_decay_scale,
        higher_elo_win_share=higher_elo_win_share,
        higher_elo_loss_share=higher_elo_loss_share,
        ignore_partners=ignore_partners,
        exclude_proam_partnerships=exclude_proam_partnerships,
    )
    run_cache_key = pipeline_cache_key(
        args_payload=cache_payload,
        fingerprint=fingerprint,
    )
    compute_cached = get_pipeline_cached(run_cache_key)

    if compute_cached is None:
        ingest_cache_key = ingestion_cache_key(
            allowed_seasons=allowed_seasons,
            include_novice=include_novice,
            include_proam=include_proam,
            completed_only=completed_only,
            max_date=max_date,
            fingerprint=fingerprint,
        )
        ingest_cached = get_ingestion_cached(ingest_cache_key)
        if ingest_cached is None:
            snapshots, debates = build_ingested_snapshots_and_debates(
                allowed_seasons=allowed_seasons,
                include_novice=include_novice,
                include_proam=include_proam,
                completed_only=completed_only,
                max_date=max_date,
            )
            set_ingestion_cached(ingest_cache_key, (snapshots, debates))
        else:
            snapshots, debates = ingest_cached

        if not snapshots:
            raise SystemExit("No imported rounds found for the selected filters.")

        excluded_proam_debates = 0
        if exclude_proam_partnerships:
            filtered = [debate for debate in debates if not debate.is_proam_partnership]
            excluded_proam_debates = len(debates) - len(filtered)
            debates = filtered

        if not debates:
            raise SystemExit("No rated debates found in imported rounds for the selected filters.")

        stats, debates_processed = apply_elo(
            debates=debates,
            initial_rating=initial_rating,
            k_max=k_max,
            k_min=k_min,
            k_decay_scale=k_decay_scale,
            mode=INDIVIDUAL_MODE if ignore_partners else PARTNER_MODE,
            higher_elo_win_share=higher_elo_win_share,
            higher_elo_loss_share=higher_elo_loss_share,
            debates_sorted=True,
        )

        compute_cached = EloComputeResult(
            matched_tournaments=len(snapshots),
            debates_processed=debates_processed,
            prelims_processed=_weighted_stage_count(debates, "prelim"),
            outrounds_processed=_weighted_stage_count(debates, "outround"),
            excluded_proam_debates=excluded_proam_debates,
            stats=stats,
        )
        set_pipeline_cached(run_cache_key, compute_cached)

    ranking_rows, excluded_default_opt_out_debaters, qual_data_available = build_dashboard_payload(
        stats=compute_cached.stats,
        min_rounds=min_rounds,
        min_outrounds=min_outrounds,
        output_limit=top,
        active_seasons=active_seasons,
        exclude_dino_rounds=exclude_dino_rounds,
    )

    return EloRunResult(
        matched_tournaments=compute_cached.matched_tournaments,
        debates_processed=compute_cached.debates_processed,
        prelims_processed=compute_cached.prelims_processed,
        outrounds_processed=compute_cached.outrounds_processed,
        excluded_proam_debates=compute_cached.excluded_proam_debates,
        qual_data_available=qual_data_available,
        excluded_default_opt_out_debaters=excluded_default_opt_out_debaters,
        ranking_rows=ranking_rows,
    )
