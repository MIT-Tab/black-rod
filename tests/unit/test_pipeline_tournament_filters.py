"""Covers runtime tournament exclusion rules for novice/proam handling."""


from types import SimpleNamespace

from core.utils.elo_runtime_engine.constants import should_exclude_tournament


def _runtime_tournament(*, qual_type, name="Weekend Open"):
    return SimpleNamespace(
        qual_type=qual_type,
        name=name,
        short_name="",
        manual_name="",
    )


def test_elo_excludes_novice_tournament_by_qual_type():
    tournament = _runtime_tournament(qual_type=9, name="Spring Warmup")

    assert should_exclude_tournament(tournament, include_novice=False, include_proam=False)


def test_elo_excludes_proam_tournament_by_qual_type():
    tournament = _runtime_tournament(qual_type=7, name="Campus Classic")

    assert should_exclude_tournament(tournament, include_novice=False, include_proam=False)


def test_elo_still_honors_include_novice_for_qual_type_filters():
    novice = _runtime_tournament(qual_type=9, name="Campus Classic")

    assert not should_exclude_tournament(novice, include_novice=True, include_proam=False)


def test_elo_always_excludes_proam_qual_type_even_if_flag_is_true():
    proam = _runtime_tournament(qual_type=7, name="Campus Classic")

    assert should_exclude_tournament(proam, include_novice=False, include_proam=True)


def test_elo_still_excludes_by_name_when_qual_type_is_points():
    tournament = _runtime_tournament(qual_type=0, name="Winter ProAm")

    assert should_exclude_tournament(tournament, include_novice=False, include_proam=False)
