from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth.mixins import PermissionRequiredMixin

from core.models.school import School
from core.utils import generics, perms
from core.models.video import Video


class DummyContextBase:
    def get_context_data(self, **kwargs):  # pylint: disable=unused-argument
        return {"base": "value"}


class DummyPublicView(generics.CustomMixin):
    public_view = True
    model = School


class DummyProtectedView(generics.CustomMixin):
    public_view = False
    permission_required = "core.view_school"
    model = School


class DummyViewWithButtons(generics.CustomMixin, DummyContextBase):
    buttons = ["edit"]
    model = School
    permission_required = "core.view_school"


def test_custom_mixin_allows_public_view_without_permission_check():
    view = DummyPublicView()
    assert view.has_permission() is True


def test_custom_mixin_delegates_permission_check(monkeypatch):
    view = DummyProtectedView()

    def fake_has_permission(self, *args, **kwargs):  # pylint: disable=unused-argument
        fake_has_permission.called = True
        return True

    fake_has_permission.called = False
    monkeypatch.setattr(PermissionRequiredMixin, "has_permission", fake_has_permission)

    assert view.has_permission() is True
    assert fake_has_permission.called is True


def test_custom_mixin_builds_permission_from_model():
    class PermissionlessView(generics.CustomMixin):
        permission_required = None
        permission_type = "view"
        model = School

    view = PermissionlessView()

    perms_required = view.get_permission_required()

    assert perms_required == ("core.view_school",)


def test_custom_mixin_merges_buttons_into_context():
    view = DummyViewWithButtons()

    context = view.get_context_data()

    assert context["buttons"] == ["edit"]
    assert context["base"] == "value"


def test_marker_column_renders_when_marker_present():
    record = SimpleNamespace(marker_one=Decimal("12.5"), tournament_one="Invitational")
    column = generics.MarkerColumn("one")

    rendered = column.render(record)

    assert "Invitational" in rendered
    assert "12.5" in rendered


def test_marker_column_suppresses_empty_markers():
    record = SimpleNamespace(marker_one=0, tournament_one=None)
    column = generics.MarkerColumn("one")

    rendered = column.render(record)

    assert rendered == ""


def test_place_column_formats_ties():
    record = SimpleNamespace(tied=True, place=2)
    assert generics.PlaceColumn().render(record) == "T-2"

    record.tied = False
    assert generics.PlaceColumn().render(record) == "2"


def test_points_column_returns_decimal_string():
    record = SimpleNamespace(points=Decimal("7.25"))
    value = generics.PointsColumn().render(record)
    assert value == "7.25"

    record.points = Decimal("20.0")
    assert generics.PointsColumn().render(record) == "20"


def test_season_column_prefers_display_methods():
    class RecordWithDisplay:
        @staticmethod
        def get_season_display():
            return "2024-25"

    assert generics.SeasonColumn().render(RecordWithDisplay()) == "2024-25"

    class Tournament:
        @staticmethod
        def get_season_display():
            return "2023-24"

    class RecordWithTournament:
        tournament = Tournament()

    assert generics.SeasonColumn().render(RecordWithTournament()) == "2023-24"

    class RecordWithSeason:
        season = "Unknown"

    assert generics.SeasonColumn().render(RecordWithSeason()) == "Unknown"


@pytest.mark.parametrize(
    "user_kwargs,video_permissions,expected",
    [
        ({"is_superuser": True}, Video.ALL, True),
        ({"is_superuser": False, "is_authenticated": False}, Video.ACCOUNTS_ONLY, False),
        (
            {
                "is_superuser": False,
                "is_authenticated": True,
                "can_view_private_videos": True,
                "has_perm": lambda perm: False,
            },
            Video.ACCOUNTS_ONLY,
            True,
        ),
        (
            {
                "is_superuser": False,
                "is_authenticated": True,
                "can_view_private_videos": False,
                "has_perm": lambda perm: False,
            },
            Video.DEBATERS_IN_ROUND,
            False,
        ),
    ],
)
def test_permission_utility(user_kwargs, video_permissions, expected):
    defaults = {
        "is_superuser": False,
        "is_authenticated": True,
        "can_view_private_videos": False,
        "has_perm": lambda perm: False,
    }
    defaults.update(user_kwargs)
    user = SimpleNamespace(**defaults)
    video = SimpleNamespace(permissions=video_permissions)

    assert perms.has_perm(user, video) is expected
