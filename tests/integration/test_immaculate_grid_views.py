from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Debater, DebaterAliasGroup, ImmaculateGridBoard, ImmaculateGridHiddenDebater,
    ImmaculateGridPlay, School, SOTY, SpeakerResult, Team, TeamResult, Tournament, User,
)
from core.utils.immaculate_grid.catalog import load_catalog, person_key
from core.utils.immaculate_grid.boards import daily_board
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
        self.assertContains(response, "Immaculate Grid (Beta)")
        self.assertContains(response, '<span class="immaculate-beta">Beta</span>', html=True)
        assert response.context["attempts"] == 2
        assert response.context["score"] == 60
        assert response.context["penalty"] == 0
        assert response.context["solved"] == 1

    def test_daily_progress_is_per_account_and_old_board_forms_expire(self):
        self.client.force_login(self.player)
        for cell in range(9):
            self._guess(self.miss, cell, cell)
        self._guess(self.answer, 0, 8)
        assert len(self._progress()) == 9
        response = self.client.get(self.grid_url)
        assert not response.context["finished"]
        self.assertContains(response, 'data-cell="0"')
        self._guess(self.answer, 0, 9)
        assert len(self._progress()) == 10
        response = self.client.get(self.grid_url)
        assert response.context["rarity"] == 60
        assert response.context["penalty"] == 5
        assert response.context["score"] == 55
        self._guess(self.alias, 1, 10)
        assert len(self._progress()) == 10

        self.clock.return_value = self.day + timedelta(days=1)
        self._guess(self.answer, 0, 0)
        assert ImmaculateGridPlay.objects.count() == 1

        self.clock.return_value = self.day
        self.client.force_login(self.other)
        self._guess(self.answer, 0, 0)
        assert ImmaculateGridPlay.objects.count() == 2

    def test_old_nine_guess_games_resume_and_extra_misses_also_cost_five_points(self):
        previous = [
            {"cell": 0, "person": f"d{index}", "name": "Previous guess", "score": None}
            for index in range(1000, 1009)
        ]
        ImmaculateGridPlay.objects.create(board=self.board, user=self.player, guesses=previous)
        self.client.force_login(self.player)
        self._guess(self.miss, 1, 9)
        self._guess(self.answer, 0, 10)
        response = self.client.get(self.grid_url)
        assert len(self._progress()) == 11
        assert response.context["score"] == 50
        assert response.context["penalty"] == 10

    def test_share_results_gives_square_scores_and_hides_guessed_names(self):
        self.client.force_login(self.player)
        self._guess(self.answer, 0, 0)
        self._guess(self.miss, 1, 1)
        response = self.client.get(self.grid_url)
        share = response.context["share_results"]
        assert "2026-11-18" in share
        assert "🟩 60 | ⬜ — | ⬜ —\n⬜ — | ⬜ — | ⬜ —\n⬜ — | ⬜ — | ⬜ —" in share
        assert "1/9 solved · 60 points" in share
        assert "2 guesses · 0 penalty points" in share
        assert self.answer.name not in share and self.miss.name not in share
        self.assertContains(response, f'data-play-url="{self.grid_url}"')

    def test_finished_grid_remains_shareable(self):
        guesses = [
            {"cell": cell, "person": f"d{1000 + cell}", "name": "Private answer", "score": cell + 1}
            for cell in range(9)
        ]
        ImmaculateGridPlay.objects.create(board=self.board, user=self.player, guesses=guesses)
        self.client.force_login(self.player)
        response = self.client.get(self.grid_url)
        assert response.context["finished"]
        self.assertContains(response, "You solved every square!")
        self.assertContains(response, "Copy results")
        self.assertNotContains(response, 'id="immaculate-guess"')
        assert "Private answer" not in response.context["share_results"]
        self._guess(self.answer, 0, 9)
        assert len(self._progress()) == 9

    def test_search_matches_ben_koch_when_extending_a_partial_name(self):
        ben = Debater.objects.create(first_name="Ben", last_name="Koch")
        for index in range(13):
            Debater.objects.create(first_name=f"Abel Ben {index}", last_name="Koch")
        self.client.force_login(self.player)
        for query in ("Ben Koc", "Ben Koch", "  Ben\u00a0Koch  "):
            results = self.client.get(self.search_url, {"q": query}).json()["results"]
            assert results[0]["id"] == ben.pk

    def test_saved_board_refresh_skips_generation_and_loading_answer_maps(self):
        with patch("core.utils.immaculate_grid.boards.load_catalog", side_effect=AssertionError), \
                patch("core.utils.immaculate_grid.boards.build_board", side_effect=AssertionError):
            with self.assertNumQueries(2):
                first = daily_board(self.day)
                second = daily_board(self.day)
            assert first.pk == second.pk == self.board.pk
            assert first.get_deferred_fields() == {"answers", "aliases"}
            self.client.force_login(self.player)
            assert self.client.get(self.grid_url).status_code == 200

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
        assert catalog.recent_people == {self.identity, f"d{self.miss.pk}"}
        assert catalog.clues[f"host:{school.pk}:1"].people == {self.identity, f"d{self.miss.pk}"}
        assert catalog.clues["event:Nationals:1"].people == {self.identity, f"d{self.miss.pk}"}
        assert catalog.clues["event:Nationals:8"].label == "Quarters or better at Nationals"
        assert catalog.clues[f"host:{school.pk}:8"].label == "Quarters or better at Fixture University"
        assert catalog.clues[f"partner:{self.identity}"].people == {f"d{self.miss.pk}"}
        assert f"partner:{self.identity}" not in load_catalog(date(2029, 4, 26)).clues

        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        catalog = load_catalog(self.day)
        assert f"partner:{self.identity}" not in catalog.clues
        assert self.identity in catalog.clues["SOTY:10"].people

    def test_host_clues_require_recent_results_and_accept_historical_answers(self):
        former = School.objects.create(name="Former Host")
        current = School.objects.create(name="Current Host")
        previous = Tournament.objects.create(host=former, date=date(2023, 3, 3), season="2022")
        historical = Tournament.objects.create(host=current, date=date(2020, 3, 1), season="2019")
        recent = Tournament.objects.create(host=current, date=date(2024, 10, 1), season="2024")
        Tournament.objects.create(host=former, date=date(2026, 11, 7), season="2026")
        SpeakerResult.objects.create(tournament=previous, debater=self.answer, place=1)
        SpeakerResult.objects.create(tournament=historical, debater=self.miss, place=1)
        SpeakerResult.objects.create(tournament=recent, debater=self.answer, place=1)

        catalog = load_catalog(self.day)
        assert catalog.recent_people == {self.identity}
        assert f"speaker:{former.pk}:5" not in catalog.clues
        assert catalog.clues[f"speaker:{current.pk}:5"].people == {self.identity, f"d{self.miss.pk}"}

    def test_novice_and_varsity_clue_requires_winning_speaker_in_both_divisions(self):
        host = School.objects.create(name="Dual Division Host")
        tournament = Tournament.objects.create(host=host, date=date(2025, 10, 1), season="2025")
        second_tournament = Tournament.objects.create(host=host, date=date(2025, 10, 8), season="2025")
        SpeakerResult.objects.create(
            tournament=tournament, debater=self.answer, place=1, type_of_place=SpeakerResult.NOVICE,
        )
        SpeakerResult.objects.create(
            tournament=tournament, debater=self.miss, place=2, type_of_place=SpeakerResult.NOVICE,
        )
        SpeakerResult.objects.create(tournament=second_tournament, debater=self.answer, place=1)
        SpeakerResult.objects.create(tournament=tournament, debater=self.miss, place=1)

        assert load_catalog(self.day).clues["both_divisions"].people == {self.identity}

    def test_multi_host_clue_counts_varsity_semifinals_at_distinct_hosts_in_one_season(self):
        hosts = [School.objects.create(name=f"Placement Host {i}") for i in range(3)]
        qualifying = Team.objects.create(name="Qualifying Team")
        qualifying.debaters.add(self.answer)
        other = Team.objects.create(name="Other Team")
        other.debaters.add(self.miss)
        for i, host in enumerate(hosts):
            tournament = Tournament.objects.create(host=host, date=date(2025, 10, i + 1), season="2025")
            TeamResult.objects.create(tournament=tournament, team=qualifying, place=3)
            TeamResult.objects.create(tournament=tournament, team=other, place=4 if i < 2 else 5)
        earlier = Tournament.objects.create(host=hosts[2], date=date(2024, 10, 1), season="2024")
        TeamResult.objects.create(tournament=earlier, team=other, place=3)

        assert load_catalog(self.day).clues["placed_hosts:3"].people == {self.identity}

    def test_hidden_debater_flag_survives_profile_merge(self):
        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        merge_debaters(self.answer, self.alias)
        assert ImmaculateGridHiddenDebater.objects.filter(debater=self.answer).exists()
