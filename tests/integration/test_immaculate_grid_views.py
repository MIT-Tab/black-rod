from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Debater, DebaterAliasGroup, ImmaculateGridBoard, ImmaculateGridHiddenDebater,
    ImmaculateGridPlay, School, SOTY, SpeakerResult, Team, TeamResult, Tournament, User,
)
from core.utils.immaculate_grid.catalog import load_catalog, person_key, rarity_score
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
        self.answers_url = reverse("core:immaculate_grid_answers")
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

    def _finish_grid(self):
        self.client.force_login(self.player)
        people = [self.answer, self.miss, *Debater.objects.bulk_create([
            Debater(first_name="Review", last_name=str(index)) for index in range(7)
        ])]
        scores = (60, 100, 90, 89, 75, 74, 50, 49, 1)
        answers = {}
        for person, score in zip(people, scores):
            key = person_key(person.pk, person.alias_group_id)
            self.board.aliases[str(person.pk)] = key
            answers[key] = score
        self.board.answers = [answers.copy() for _ in range(9)]
        self.board.save(update_fields=["answers", "aliases"])
        for cell, person in enumerate(people):
            self._guess(person, cell, cell)
        return people

    def test_grid_search_and_guesses_require_login(self):
        assert self.client.get(self.grid_url).status_code == 302
        assert self.client.get(self.search_url, {"q": "Avery"}).status_code == 302
        assert self.client.get(self.answers_url, {"day": self.day, "cell": 0}).status_code == 302
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
        assert "🟪 60 | ⬜ — | ⬜ —\n⬜ — | ⬜ — | ⬜ —\n⬜ — | ⬜ — | ⬜ —" in share
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
        original = (self.board.rows, self.board.columns, self.board.answers, self.board.aliases)
        with patch("core.utils.immaculate_grid.boards.load_catalog", side_effect=AssertionError), \
                patch("core.utils.immaculate_grid.boards.build_board", side_effect=AssertionError):
            with self.assertNumQueries(2):
                first = daily_board(self.day)
                second = daily_board(self.day)
            assert first.pk == second.pk == self.board.pk
            assert first.get_deferred_fields() == {"answers", "aliases"}
            self.client.force_login(self.player)
            assert self.client.get(self.grid_url).status_code == 200
        self.board.refresh_from_db()
        assert (self.board.rows, self.board.columns, self.board.answers, self.board.aliases) == original

    def test_answer_review_requires_the_current_account_to_finish(self):
        self.client.force_login(self.player)
        request = {"day": self.day, "cell": 0}
        assert self.client.get(self.answers_url, request).status_code == 403
        self._guess(self.answer, 0, 0)
        response = self.client.get(self.answers_url, request)
        assert response.status_code == 403 and "answers" not in response.json()
        assert self.client.get(self.answers_url, {"day": self.day, "cell": 9}).status_code == 400
        self._finish_grid()
        self.client.force_login(self.other)
        assert self.client.get(self.answers_url, request).status_code == 403

    def test_completed_answer_review_uses_frozen_scores_and_deduplicates_linked_profiles(self):
        people = self._finish_grid()
        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        with patch("core.utils.immaculate_grid.boards.load_catalog", side_effect=AssertionError):
            response = self.client.get(self.answers_url, {"day": self.day, "cell": 0})
        assert response.status_code == 200
        options = response.json()["answers"]
        assert len(options) == 9
        assert options[0]["score"] == 100 and options[0]["tier"]["emoji"] == "🌌"
        assert next(option for option in options if option["name"] == people[0].name)["score"] == 60
        self.clock.return_value = self.day + timedelta(days=1)
        assert self.client.get(self.answers_url, {"day": self.day, "cell": 0}).status_code == 200

    def test_copied_results_include_each_answers_rarity_color(self):
        self._finish_grid()
        response = self.client.get(self.grid_url)
        text = response.context["share_results"]
        for fragment in ("🟪 60", "🌌 100", "🟧 89", "🟦 49", "🟩 1"):
            assert fragment in text
        self.assertContains(response, "View valid answers")
        self.assertContains(response, "immaculate-rarity-galaxy")

    def test_hidden_debaters_remain_searchable_and_scorable(self):
        self.client.force_login(self.player)
        ImmaculateGridHiddenDebater.objects.create(debater=self.alias)
        names = {item["name"] for item in self.client.get(self.search_url, {"q": "Avery"}).json()["results"]}
        assert names == {"Avery Answer"}
        alias_result = self.client.get(self.search_url, {"q": "Avery Other"}).json()["results"]
        assert len(alias_result) == 1 and alias_result[0]["person"] == self.identity
        self._guess(self.answer, 0, 0)
        assert self._progress()[0]["score"] == 60

    def test_linked_search_profiles_share_one_person_and_do_not_use_up_result_slots(self):
        Debater.objects.bulk_create([
            Debater(first_name="Avery", last_name=f"Alias {index}", alias_group=self.alias_group)
            for index in range(15)
        ] + [
            Debater(first_name="Avery", last_name=f"Separate {index}") for index in range(14)
        ])
        self.client.force_login(self.player)
        results = self.client.get(self.search_url, {"q": "Avery"}).json()["results"]
        assert len(results) == 12
        assert len({result["person"] for result in results}) == 12
        first = self.client.get(self.search_url, {"q": "Avery Answer"}).json()["results"][0]
        alternate = self.client.get(self.search_url, {"q": "Avery Other"}).json()["results"][0]
        assert first["person"] == alternate["person"] == self.identity

    def test_profiles_linked_after_publication_cannot_solve_two_squares(self):
        first_key, alternate_key = f"d{self.answer.pk}", f"d{self.alias.pk}"
        self.board.aliases = {str(self.answer.pk): first_key, str(self.alias.pk): alternate_key}
        self.board.answers = [{first_key: 60, alternate_key: 60} for _ in range(9)]
        self.board.save(update_fields=["answers", "aliases"])
        original = (self.board.answers, self.board.aliases)
        self.client.force_login(self.player)
        self._guess(self.answer, 0, 0)
        self._guess(self.alias, 1, 1)
        assert len(self._progress()) == 1 and self._progress()[0]["score"] == 60
        self.board.refresh_from_db()
        assert (self.board.answers, self.board.aliases) == original

    def test_same_answer_cannot_fill_two_squares_or_add_a_penalty(self):
        self.client.force_login(self.player)
        self._guess(self.answer, 0, 0)
        response = self._guess(self.answer, 1, 1)
        assert len(self._progress()) == 1
        self.assertContains(self.client.get(response.url), "You’ve already used that debater.")

    def test_scoring_combines_linked_career_spans_and_season_points(self):
        first_school = School.objects.create(name="First school")
        second_school = School.objects.create(name="Second school")
        self.answer.first_season, self.answer.latest_season, self.answer.school = "2021", "2023", first_school
        self.alias.first_season, self.alias.latest_season, self.alias.school = "2023", "2025", second_school
        self.answer.save()
        self.alias.save()
        SOTY.objects.create(debater=self.answer, season="2023", place=2, points=100)
        SOTY.objects.create(debater=self.alias, season="2023", place=2, points=100)
        catalog = load_catalog(self.day)
        assert catalog.rarity[self.identity] == rarity_score(1, 100, 9, career_span=4)
        assert catalog.clues["SOTY:10"].frequency(self.identity) == 1
        assert catalog.clues[f"school:{first_school.pk}"].people == {self.identity}
        assert catalog.clues[f"school:{second_school.pk}"].people == {self.identity}

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
        assert catalog.clues[f"host:{school.pk}:8"].label == "Quarters or better at a tournament hosted by Fixture University"
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

        clue = load_catalog(self.day).clues["both_divisions"]
        assert clue.people == {self.identity}
        assert clue.label == "At least one novice top speak and at least one varsity top speak"
        assert clue.frequency(self.identity) == 2

    def test_partner_frequency_counts_distinct_tournaments_across_profiles_and_divisions(self):
        host = School.objects.create(name="Partner Host")
        self.answer.school = host
        self.answer.save(update_fields=["school"])
        first = Tournament.objects.create(host=host, date=date(2025, 10, 1), season="2025")
        second = Tournament.objects.create(host=host, date=date(2025, 10, 8), season="2025")
        team = Team.objects.create(name="First partnership")
        team.debaters.add(self.answer, self.miss)
        alias_team = Team.objects.create(name="Linked partnership")
        alias_team.debaters.add(self.alias, self.miss)
        TeamResult.objects.create(team=team, tournament=first, place=1)
        TeamResult.objects.create(team=team, tournament=first, place=1, type_of_place=TeamResult.NOVICE)
        TeamResult.objects.create(team=alias_team, tournament=first, place=2)
        TeamResult.objects.create(team=team, tournament=second, place=1)
        SOTY.objects.create(debater=self.answer, place=5, season="2025", points=20)
        catalog = load_catalog(self.day)
        assert catalog.clues[f"partner:{self.identity}"].frequency(f"d{self.miss.pk}") == 2
        assert catalog.clues[f"school:{host.pk}"].frequency(self.identity) == 2

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
