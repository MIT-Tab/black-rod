"""Tests runtime pipeline cache layering to ensure ingestion and compute reuse behave correctly across argument changes."""


from argparse import Namespace
from datetime import datetime, timezone

from core.utils.elo_runtime_engine import pipeline
from core.utils.elo_runtime_engine.cache import clear_runtime_caches
from core.utils.elo_runtime_engine.models import Debate, TournamentSnapshot


def _args(**overrides):
    defaults = {
        "k_max": 40.0,
        "k_min": 10.0,
        "k_decay_scale": 75.0,
        "initial_rating": 1500.0,
        "higher_elo_win_share": 50.0,
        "higher_elo_loss_share": 50.0,
        "min_rounds": 0,
        "min_outrounds": 0,
        "top": 0,
        "seasons": ["2025"],
        "active_seasons": ["2025"],
        "include_novice": False,
        "include_proam": False,
        "exclude_proam_partnerships": False,
        "exclude_dino_rounds": False,
        "completed_only": False,
        "max_date": None,
        "ignore_partners": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def _sample_data():
    timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    snapshots = [
        TournamentSnapshot(
            timestamp=timestamp,
            tournament_id=1,
            tournament_name="Sample",
            season="2025",
        )
    ]
    debates = [
        Debate(
            timestamp=timestamp,
            tournament_key="1:2025:Sample",
            tournament_name="Sample",
            season="2025",
            stage="prelim",
            sort_key=(0, 1, 1),
            round_label="P1",
            team_a=(1, 2),
            team_b=(3, 4),
            winner="a",
            source_kind="bundle",
            source_label="sample",
            participant_names={1: "A", 2: "B", 3: "C", 4: "D"},
        )
    ]
    return snapshots, debates


def test_pipeline_result_cache_reuses_full_run(monkeypatch):
    clear_runtime_caches()
    counters = {"ingest": 0, "apply": 0, "payload": 0}
    snapshots, debates = _sample_data()

    def fake_fingerprint():
        return "fingerprint"

    def fake_ingest(**kwargs):
        counters["ingest"] += 1
        return snapshots, debates

    def fake_apply(**kwargs):
        counters["apply"] += 1
        return {}, 1

    def fake_payload(**kwargs):
        counters["payload"] += 1
        return [], 0, True

    monkeypatch.setattr(pipeline, "data_fingerprint", fake_fingerprint)
    monkeypatch.setattr(pipeline, "build_ingested_snapshots_and_debates", fake_ingest)
    monkeypatch.setattr(pipeline, "apply_elo", fake_apply)
    monkeypatch.setattr(pipeline, "build_dashboard_payload", fake_payload)

    args = _args()
    first = pipeline.run_elo_pipeline(args)
    second = pipeline.run_elo_pipeline(args)

    assert counters["ingest"] == 1
    assert counters["apply"] == 1
    assert counters["payload"] == 2
    assert first is not second


def test_pipeline_cache_reuses_ingestion_when_rating_params_change(monkeypatch):
    clear_runtime_caches()
    counters = {"ingest": 0, "apply": 0}
    snapshots, debates = _sample_data()

    def fake_fingerprint():
        return "fingerprint"

    def fake_ingest(**kwargs):
        counters["ingest"] += 1
        return snapshots, debates

    def fake_apply(**kwargs):
        counters["apply"] += 1
        return {}, 1

    def fake_payload(**kwargs):
        return [], 0, True

    monkeypatch.setattr(pipeline, "data_fingerprint", fake_fingerprint)
    monkeypatch.setattr(pipeline, "build_ingested_snapshots_and_debates", fake_ingest)
    monkeypatch.setattr(pipeline, "apply_elo", fake_apply)
    monkeypatch.setattr(pipeline, "build_dashboard_payload", fake_payload)

    pipeline.run_elo_pipeline(_args(k_max=40.0))
    pipeline.run_elo_pipeline(_args(k_max=41.0))

    assert counters["ingest"] == 1
    assert counters["apply"] == 2


def test_pipeline_cache_reuses_compute_when_only_display_filters_change(monkeypatch):
    clear_runtime_caches()
    counters = {"ingest": 0, "apply": 0, "payload": 0}
    snapshots, debates = _sample_data()

    def fake_fingerprint():
        return "fingerprint"

    def fake_ingest(**kwargs):
        counters["ingest"] += 1
        return snapshots, debates

    def fake_apply(**kwargs):
        counters["apply"] += 1
        return {}, 1

    def fake_payload(**kwargs):
        counters["payload"] += 1
        return [], 0, True

    monkeypatch.setattr(pipeline, "data_fingerprint", fake_fingerprint)
    monkeypatch.setattr(pipeline, "build_ingested_snapshots_and_debates", fake_ingest)
    monkeypatch.setattr(pipeline, "apply_elo", fake_apply)
    monkeypatch.setattr(pipeline, "build_dashboard_payload", fake_payload)

    pipeline.run_elo_pipeline(_args(min_rounds=0, min_outrounds=0, active_seasons=["2025"]))
    pipeline.run_elo_pipeline(_args(min_rounds=5, min_outrounds=2, active_seasons=["2024", "2025"]))

    assert counters["ingest"] == 1
    assert counters["apply"] == 1
    assert counters["payload"] == 2


def test_pipeline_normalizes_include_proam_flag_away(monkeypatch):
    clear_runtime_caches()
    counters = {"ingest": 0, "apply": 0}
    snapshots, debates = _sample_data()

    def fake_fingerprint():
        return "fingerprint"

    def fake_ingest(**kwargs):
        counters["ingest"] += 1
        assert kwargs["include_proam"] is False
        return snapshots, debates

    def fake_apply(**kwargs):
        counters["apply"] += 1
        return {}, 1

    def fake_payload(**kwargs):
        return [], 0, True

    monkeypatch.setattr(pipeline, "data_fingerprint", fake_fingerprint)
    monkeypatch.setattr(pipeline, "build_ingested_snapshots_and_debates", fake_ingest)
    monkeypatch.setattr(pipeline, "apply_elo", fake_apply)
    monkeypatch.setattr(pipeline, "build_dashboard_payload", fake_payload)

    pipeline.run_elo_pipeline(_args(include_proam=False))
    pipeline.run_elo_pipeline(_args(include_proam=True))

    assert counters["ingest"] == 1
    assert counters["apply"] == 1
