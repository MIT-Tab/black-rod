"""Validates ELO dashboard form bounds/defaults/step rules."""


import pytest

from core.elo_forms import LocalEloDashboardForm
from core.views import elo_views


def _valid_elo_form_data(**overrides):
    data = {
        "season_start": 2017,
        "season_end": 2025,
        "active_season_start": 2018,
        "active_season_end": 2025,
        "k_max": 40,
        "k_min": 10,
        "k_decay_scale": 250,
        "initial_rating": 1500,
        "higher_elo_win_share": 50,
        "higher_elo_loss_share": 50,
        "min_rounds": 0,
        "min_outrounds": 0,
    }
    data.update(overrides)
    return data


def test_elo_dashboard_form_clamps_slider_min_to_2017():
    form = LocalEloDashboardForm(season_min=2003, season_max=2025)

    assert form.season_min == 2017
    for field_name in (
        "season_start",
        "season_end",
        "active_season_start",
        "active_season_end",
    ):
        field = form.fields[field_name]
        assert field.min_value == 2017
        assert field.widget.attrs["min"] == "2017"


def test_elo_dashboard_form_defaults_active_start_to_2018_when_available():
    form = LocalEloDashboardForm(season_min=2017, season_max=2025)

    assert form.fields["active_season_start"].initial == 2018
    assert form.fields["active_season_end"].initial == 2025


def test_elo_dashboard_form_defaults_active_start_to_2017_if_thats_only_option():
    form = LocalEloDashboardForm(season_min=2003, season_max=2010)

    assert form.season_min == 2017
    assert form.season_max == 2017
    assert form.fields["active_season_start"].initial == 2017


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("higher_elo_win_share", 53),
        ("higher_elo_loss_share", 52),
        ("k_max", 42),
        ("k_min", 11),
        ("initial_rating", 1550),
    ),
)
def test_elo_dashboard_form_rejects_invalid_increments(field_name, invalid_value):
    data = _valid_elo_form_data(**{field_name: invalid_value})
    form = LocalEloDashboardForm(data=data)

    assert not form.is_valid()
    assert field_name in form.errors


def test_elo_dashboard_form_accepts_requested_increment_ranges():
    form = LocalEloDashboardForm(data=_valid_elo_form_data())

    assert form.is_valid()


def test_elo_dashboard_form_sets_input_steps_and_limits():
    form = LocalEloDashboardForm()

    assert form.fields["higher_elo_win_share"].widget.attrs["step"] == "5"
    assert form.fields["higher_elo_loss_share"].widget.attrs["step"] == "5"
    assert form.fields["k_max"].widget.attrs["step"] == "5"
    assert form.fields["k_min"].widget.attrs["step"] == "5"
    assert str(form.fields["k_max"].widget.attrs["min"]) == "5"
    assert str(form.fields["k_max"].widget.attrs["max"]) == "100"
    assert str(form.fields["k_min"].widget.attrs["min"]) == "5"
    assert str(form.fields["k_min"].widget.attrs["max"]) == "100"
    assert form.fields["initial_rating"].widget.attrs["step"] == "100"
    assert str(form.fields["initial_rating"].widget.attrs["min"]) == "500"
    assert str(form.fields["initial_rating"].widget.attrs["max"]) == "2000"


@pytest.mark.django_db
def test_resolve_elo_season_bounds_clamps_minimum(monkeypatch):
    monkeypatch.setattr(
        elo_views.Tournament.objects,
        "values_list",
        lambda *args, **kwargs: ["2003", "2018", "2025"],
    )

    assert elo_views._resolve_elo_season_bounds() == (2017, 2025)
