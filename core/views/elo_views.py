"""Handles ELO dashboard requests end to end: resolve season bounds, validate form input, build pipeline args, run/cache results, and render output."""


import argparse
from datetime import date

from django.contrib.auth.decorators import login_required
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.shortcuts import render

from core.access import EXCLUSIVE_PRE_ACCESS_PERMISSION
from core.elo_forms import LocalEloDashboardForm
from core.models import Tournament
from core.utils.elo_pipeline import (
    run_elo_pipeline,
)
from core.utils.elo_runtime_engine.constants import season_to_int
from core.views.elo_cache import (
    get_cached_elo_state,
    set_cached_elo_state,
    settings_signature,
)

ELO_RANKINGS_PER_PAGE = 50


def _format_season_label(season):
    try:
        start_year = int(season)
    except (TypeError, ValueError):
        return str(season)
    return f"{start_year}-{start_year + 1}"


def _format_season_range_label(start_season, end_season):
    start_label = _format_season_label(start_season)
    end_label = _format_season_label(end_season)
    if start_label == end_label:
        return start_label
    return f"{start_label} through {end_label}"


def _resolve_form_season_value(form, field_name, fallback):
    value = form[field_name].value()
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _resolve_elo_season_bounds():
    min_season = LocalEloDashboardForm.DEFAULT_SEASON_MIN
    try:
        seasons = []
        for season in Tournament.objects.values_list("season", flat=True):
            season_value = season_to_int(season)
            if season_value is not None:
                seasons.append(season_value)
        if seasons:
            resolved_min = max(min_season, min(seasons))
            resolved_max = max(resolved_min, max(seasons))
            return resolved_min, resolved_max
    except Exception:
        pass
    return min_season, max(min_season, date.today().year)


def _build_elo_run_args(form):
    return argparse.Namespace(
        k_max=form.cleaned_data["k_max"],
        k_min=form.cleaned_data["k_min"],
        k_decay_scale=form.cleaned_data["k_decay_scale"],
        initial_rating=form.cleaned_data["initial_rating"],
        higher_elo_win_share=form.cleaned_data["higher_elo_win_share"],
        higher_elo_loss_share=form.cleaned_data["higher_elo_loss_share"],
        min_rounds=form.cleaned_data["min_rounds"],
        min_outrounds=form.cleaned_data["min_outrounds"],
        top=0,
        seasons=form.cleaned_data["seasons"],
        active_seasons=form.cleaned_data["active_seasons"],
        include_novice=False,
        exclude_proam_partnerships=form.cleaned_data["exclude_proam_partnerships"],
        exclude_dino_rounds=form.cleaned_data["exclude_dino_rounds"],
        max_date=None,
        ignore_partners=False,
    )


def _compute_cache_payload(cleaned_data):
    return {
        "seasons": [str(row).strip() for row in cleaned_data.get("seasons", []) if str(row).strip()],
        "higher_elo_win_share": int(cleaned_data["higher_elo_win_share"]),
        "higher_elo_loss_share": int(cleaned_data["higher_elo_loss_share"]),
        "k_max": int(cleaned_data["k_max"]),
        "k_min": int(cleaned_data["k_min"]),
        "k_decay_scale": float(cleaned_data["k_decay_scale"]),
        "initial_rating": int(cleaned_data["initial_rating"]),
        "exclude_proam_partnerships": bool(cleaned_data["exclude_proam_partnerships"]),
    }


def _display_cache_payload(cleaned_data):
    return {
        "active_seasons": [str(row).strip() for row in cleaned_data.get("active_seasons", []) if str(row).strip()],
        "min_rounds": int(cleaned_data["min_rounds"]),
        "min_outrounds": int(cleaned_data["min_outrounds"]),
        "exclude_dino_rounds": bool(cleaned_data["exclude_dino_rounds"]),
    }


def local_elo_dashboard(request):
    errors = []
    result = get_cached_elo_state(request)
    season_min, season_max = _resolve_elo_season_bounds()
    form = LocalEloDashboardForm(
        request.POST or None,
        prefix="elo",
        season_min=season_min,
        season_max=season_max,
    )

    if request.method == "POST" and form.is_valid():
        try:
            compute_signature = settings_signature(_compute_cache_payload(form.cleaned_data))
            display_signature = settings_signature(_display_cache_payload(form.cleaned_data))
            result = get_cached_elo_state(
                request=request,
                compute_signature=compute_signature,
                display_signature=display_signature,
            )
            if result is None:
                result = run_elo_pipeline(_build_elo_run_args(form))
                set_cached_elo_state(
                    request=request,
                    result=result,
                    compute_signature=compute_signature,
                    display_signature=display_signature,
                )
        except FileNotFoundError as exc:
            errors.append(str(exc))
        except SystemExit as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))

    table_rows = list(result.ranking_rows) if result else []
    page_obj = None
    if table_rows:
        page_obj = Paginator(table_rows, ELO_RANKINGS_PER_PAGE).get_page(request.GET.get("page"))
        table_rows = list(page_obj.object_list)

    compute_season_start = _resolve_form_season_value(form, "season_start", season_min)
    compute_season_end = _resolve_form_season_value(form, "season_end", season_max)
    active_season_start = _resolve_form_season_value(
        form,
        "active_season_start",
        season_min,
    )
    active_season_end = _resolve_form_season_value(
        form,
        "active_season_end",
        season_max,
    )

    return render(
        request,
        "elo/dashboard.html",
        {
            "form": form,
            "result": result,
            "table_rows": table_rows,
            "season_min": season_min,
            "season_max": season_max,
            "errors": errors,
            "page_obj": page_obj,
            "rankings_per_page": ELO_RANKINGS_PER_PAGE,
            "season_min_label": _format_season_label(season_min),
            "season_max_label": _format_season_label(season_max),
            "compute_season_start_label": _format_season_label(compute_season_start),
            "compute_season_end_label": _format_season_label(compute_season_end),
            "compute_season_range_label": _format_season_range_label(
                compute_season_start,
                compute_season_end,
            ),
            "active_season_start_label": _format_season_label(active_season_start),
            "active_season_end_label": _format_season_label(active_season_end),
            "active_season_range_label": _format_season_range_label(
                active_season_start,
                active_season_end,
            ),
        },
    )


def elo_dashboard(request):
    return local_elo_dashboard(request)


elo_dashboard = permission_required(
    EXCLUSIVE_PRE_ACCESS_PERMISSION,
    raise_exception=True,
)(elo_dashboard)

elo_dashboard = login_required(elo_dashboard)
