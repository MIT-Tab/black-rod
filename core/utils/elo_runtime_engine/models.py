"""Defines lightweight runtime data objects for ingested debates, rolling player stats, profiles, and final ELO result payloads."""


from collections import Counter, defaultdict

from core.utils.elo_runtime_engine.constants import DEFAULT_RATING


class TournamentSnapshot:
    __slots__ = ("timestamp", "tournament_id", "tournament_name", "season")

    def __init__(self, timestamp, tournament_id, tournament_name, season):
        self.timestamp = timestamp
        self.tournament_id = tournament_id
        self.tournament_name = tournament_name
        self.season = season


class PlayerStats:
    __slots__ = (
        "rating",
        "rounds",
        "prelim_rounds",
        "outround_rounds",
        "yearly_results",
        "season_snapshots",
        "name_hints",
        "school_hints",
        "school_hints_by_season",
    )

    def __init__(self):
        self.rating = DEFAULT_RATING
        self.rounds = 0.0
        self.prelim_rounds = 0.0
        self.outround_rounds = 0.0
        self.yearly_results = defaultdict(lambda: [0.0, 0.0])
        self.season_snapshots = {}
        self.name_hints = Counter()
        self.school_hints = Counter()
        self.school_hints_by_season = defaultdict(Counter)


class Debate:
    __slots__ = (
        "timestamp",
        "tournament_key",
        "tournament_name",
        "season",
        "stage",
        "sort_key",
        "round_label",
        "team_a",
        "team_b",
        "winner",
        "source_kind",
        "source_label",
        "participant_names",
        "team_a_school",
        "team_b_school",
        "is_proam_partnership",
    )

    def __init__(
        self,
        timestamp,
        tournament_key,
        tournament_name,
        season,
        stage,
        sort_key,
        round_label,
        team_a,
        team_b,
        winner,
        source_kind,
        source_label,
        participant_names=None,
        team_a_school="",
        team_b_school="",
        is_proam_partnership=False,
    ):
        self.timestamp = timestamp
        self.tournament_key = tournament_key
        self.tournament_name = tournament_name
        self.season = season
        self.stage = stage
        self.sort_key = sort_key
        self.round_label = round_label
        self.team_a = team_a
        self.team_b = team_b
        self.winner = winner
        self.source_kind = source_kind
        self.source_label = source_label
        self.participant_names = participant_names or {}
        self.team_a_school = team_a_school
        self.team_b_school = team_b_school
        self.is_proam_partnership = bool(is_proam_partnership)


class DebaterRankingRow:
    __slots__ = (
        "rank",
        "name",
        "school_name",
        "schools",
        "debater_id",
        "elo",
        "rounds",
        "prelim_rounds",
        "outround_rounds",
    )

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class EloRunResult:
    __slots__ = (
        "matched_tournaments",
        "debates_processed",
        "prelims_processed",
        "outrounds_processed",
        "excluded_proam_debates",
        "qual_data_available",
        "excluded_default_opt_out_debaters",
        "ranking_rows",
    )

    def __init__(
        self,
        matched_tournaments,
        debates_processed,
        prelims_processed,
        outrounds_processed,
        excluded_proam_debates,
        qual_data_available,
        excluded_default_opt_out_debaters,
        ranking_rows,
    ):
        self.matched_tournaments = matched_tournaments
        self.debates_processed = debates_processed
        self.prelims_processed = prelims_processed
        self.outrounds_processed = outrounds_processed
        self.excluded_proam_debates = excluded_proam_debates
        self.qual_data_available = qual_data_available
        self.excluded_default_opt_out_debaters = excluded_default_opt_out_debaters
        self.ranking_rows = ranking_rows


class EloComputeResult:
    __slots__ = (
        "matched_tournaments",
        "debates_processed",
        "prelims_processed",
        "outrounds_processed",
        "excluded_proam_debates",
        "stats",
    )

    def __init__(
        self,
        matched_tournaments,
        debates_processed,
        prelims_processed,
        outrounds_processed,
        excluded_proam_debates,
        stats,
    ):
        self.matched_tournaments = matched_tournaments
        self.debates_processed = debates_processed
        self.prelims_processed = prelims_processed
        self.outrounds_processed = outrounds_processed
        self.excluded_proam_debates = excluded_proam_debates
        self.stats = stats


class DebaterProfile:
    __slots__ = (
        "player_id",
        "display_name",
        "primary_debater_id",
        "school_name",
        "schools",
        "first_season",
        "latest_season",
        "first_year_tournament_count",
        "has_nat_qual",
        "affiliated_active_seasons",
        "latest_affiliated_season",
    )

    def __init__(
        self,
        player_id,
        display_name,
        primary_debater_id,
        school_name,
        schools,
        first_season,
        latest_season,
        first_year_tournament_count,
        has_nat_qual,
        affiliated_active_seasons,
        latest_affiliated_season,
    ):
        self.player_id = player_id
        self.display_name = display_name
        self.primary_debater_id = primary_debater_id
        self.school_name = school_name
        self.schools = schools
        self.first_season = first_season
        self.latest_season = latest_season
        self.first_year_tournament_count = first_year_tournament_count
        self.has_nat_qual = has_nat_qual
        self.affiliated_active_seasons = affiliated_active_seasons
        self.latest_affiliated_season = latest_affiliated_season
