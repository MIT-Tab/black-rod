from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Debater, DebaterAliasGroup, ImmaculateGridBoard, ImmaculateGridHiddenDebater,
    ImmaculateGridPlay, School, SOTY, Team, TeamResult, Tournament, User,
)
from core.utils.immaculate_grid.catalog import load_catalog, person_key
from core.utils.merge import merge_debaters


class ImmaculateGridTests(TestCase):
    day = date(2026, 11, 18)

    @classmethod
    def setUpTestData(cls):
        cls.player = User.objects.create_user("solver")
        cls.other = User.objects.create_user("another-solver")
        cls.alias_group = DebaterAliasGroup.objects.create()
        cls.answer = Debater.objects.create(first_name="Avery", last_name="Answer", alias_group=cls.alias_group)
        cls.alias = Debater.objects.create(first_name="Avery", last_name="Other", alias_group=cls.alias_group)
        cls.miss = Debater.objects.create(first_name="Wrong", last_name="Guess")
        cls.identity = person_key(cls.answer.pk, cls.answer.alias_group_id)
        cls.board = ImmaculateGridBoard.objects.create(
            date=cls.day,
            rows=[{"key": f"row{i}", "label": f"Row {i}"} for i in range(3)],
            columns=[{"key": f"column{i}", "label": f"Column {i}"} for i in range(3)],
            answers=[{cls.identity: 60} for _ in range(9)],
            aliases={str(cls.answer.pk): cls.identity, str(cls.alias.pk): cls.identity},
        )

    def setUp(self):
        self.grid_url = reverse("core:immaculate_grid")
        self.guess_url = reverse("core:immaculate_grid_guess")
        self.search_url = reverse("core:immaculate_grid_search")
        clock = patch("core.views.immaculate_grid_views.game_date", return_value=self.day)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def _guess(self, debater, cell, attempts, day=None):
        return self.client.post(self.guess_url, {
            "day": day or self.day,
            "cell": cell,
            "attempts": attempts,
            "debater": debater.pk,
        })

    def _progress(self):
        return ImmaculateGridPlay.objects.get(user=self.player, board=self.board).guesses

    def test_grid_search_and_guesses_require_login(self):
        assert self.client.get(self.grid_url).status_code == 302
        assert self.client.get(self.search_url, {"q": "Avery"}).status_code == 302
        assert self._guess(self.answer, 0, 0).status_code == 302
        assert not ImmaculateGridPlay.objects.exists()

    def test_incorrect_guesses_cost_attempts_and_correct_guesses_lock_the_name_and_cell(self):
        self.client.force_login(self.player)
        self._guess(self.miss, 0, 0)
        self._guess(self.answer, 0, 1)
        self._guess(self.alias, 1, 2)
        self._guess(self.miss, 0, 2)
        self._guess(self.miss, 1, 1)  # Submission from an older tab.
        assert [(item["cell"], item["score"]) for item in self._progress()] == [(0, None), (0, 60)]
        response = self.client.get(self.grid_url)
        self.assertContains(response, "Avery Answer")
        self.assertContains(response, "60 pts")
        assert response.context["remaining"] == 7
        assert response.context["solved"] == 1

    def test_daily_limit_is_per_account_and_old_board_forms_expire(self):
        self.client.force_login(self.player)
        for cell in range(9):
            self._guess(self.miss, cell, cell)
        self._guess(self.answer, 0, 8)
        assert len(self._progress()) == 9
        self.assertContains(self.client.get(self.grid_url), "You solved 0 of 9 squares.")

        self.clock.return_value = self.day + timedelta(days=1)
        self._guess(self.answer, 0, 0)
        assert ImmaculateGridPlay.objects.count() == 1

        self.clock.return_value = self.day
        self.client.force_login(self.other)
        self._guess(self.answer, 0, 0)
        assert ImmaculateGridPlay.objects.count() == 2

    def test_hidden_debaters_remain_searchable_and_scorable(self):
        self.client.force_login(self.player)
        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        names = {item["name"] for item in self.client.get(self.search_url, {"q": "Avery"}).json()["results"]}
        assert names == {"Avery Answer", "Avery Other"}
        self._guess(self.answer, 0, 0)
        assert self._progress()[0]["score"] == 60

    def test_hidden_partner_is_removed_from_an_already_published_board(self):
        self.board.rows[0].update(label="Partnered with Avery Answer", subject=self.identity)
        self.board.save(update_fields=["rows"])
        self.client.force_login(self.player)
        self.assertContains(self.client.get(self.grid_url), "Partnered with Avery Answer")
        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        response = self.client.get(self.grid_url)
        self.assertNotContains(response, "Avery Answer", status_code=503)
        self._guess(self.answer, 0, 0)
        assert not ImmaculateGridPlay.objects.exists()

    def test_catalog_uses_dated_results_and_linked_profiles_without_naming_hidden_people(self):
        school = School.objects.create(name="Fixture University")
        self.answer.school = school
        self.answer.save(update_fields=["school"])
        tournament = Tournament.objects.create(
            name="Fixture Nationals", host=school, date=date(2025, 4, 25), season="2024",
            qual_type=Tournament.NATIONALS,
        )
        team = Team.objects.create(name="Fixture Team")
        team.debaters.add(self.answer, self.miss)
        TeamResult.objects.create(tournament=tournament, team=team, place=1)
        SOTY.objects.create(debater=self.alias, season="2024", place=8, points=12)

        catalog = load_catalog(self.day)
        assert catalog.clues[f"school:{school.pk}"].people == {self.identity}
        assert catalog.clues[f"host:{school.pk}:1"].people == {self.identity, f"d{self.miss.pk}"}
        assert catalog.clues["event:Nationals:1"].people == {self.identity, f"d{self.miss.pk}"}
        assert catalog.clues[f"partner:{self.identity}"].people == {f"d{self.miss.pk}"}
        assert f"partner:{self.identity}" not in load_catalog(date(2029, 4, 26)).clues

        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        catalog = load_catalog(self.day)
        assert f"partner:{self.identity}" not in catalog.clues
        assert self.identity in catalog.clues["SOTY:10"].people

    def test_hidden_debater_flag_survives_profile_merge(self):
        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        merge_debaters(self.answer, self.alias)
        assert ImmaculateGridHiddenDebater.objects.filter(debater=self.answer).exists()
