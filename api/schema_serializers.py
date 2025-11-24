from rest_framework import serializers


class LinkSerializer(serializers.Serializer):
    self = serializers.CharField()
    html = serializers.CharField(required=False)
    replay_api = serializers.CharField(required=False)
    replay_html = serializers.CharField(required=False)
    full_standings = serializers.CharField(required=False)
    plain_text = serializers.CharField(required=False)
    llm_proxy = serializers.CharField(required=False)


class SeasonOptionSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class LiteSchoolSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    included_in_oty = serializers.BooleanField(required=False)
    url = serializers.CharField(required=False)
    api_url = serializers.CharField(required=False)


class LiteDebaterSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    status = serializers.CharField()
    status_code = serializers.CharField()
    school_id = serializers.IntegerField(required=False, allow_null=True)
    school_name = serializers.CharField(required=False, allow_null=True)
    school = LiteSchoolSerializer(required=False, allow_null=True)
    url = serializers.CharField(required=False)
    api_url = serializers.CharField(required=False)


class LiteTournamentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    season = serializers.CharField()
    season_display = serializers.CharField()
    date = serializers.CharField(allow_null=True, required=False)
    display = serializers.CharField(required=False)
    num_teams = serializers.IntegerField()
    num_novice_debaters = serializers.IntegerField()
    host = LiteSchoolSerializer(required=False, allow_null=True)
    url = serializers.CharField(required=False)
    api_url = serializers.CharField(required=False)


class LiteTeamSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    hybrid = serializers.BooleanField()
    debaters = LiteDebaterSerializer(many=True, required=False)
    url = serializers.CharField(required=False)
    api_url = serializers.CharField(required=False)


class StandingMarkerSerializer(serializers.Serializer):
    slot = serializers.IntegerField(required=False)
    points = serializers.FloatField()
    tournament = LiteTournamentSerializer(required=False, allow_null=True)
    earned_on = serializers.CharField(required=False, allow_null=True)
    result_id = serializers.IntegerField(required=False)


class StandingEntrySerializer(serializers.Serializer):
    season = serializers.CharField()
    season_display = serializers.CharField()
    place_display = serializers.CharField(required=False, allow_blank=True)
    points = serializers.FloatField()
    markers = StandingMarkerSerializer(many=True, required=False)
    id = serializers.IntegerField(required=False)
    place = serializers.IntegerField(required=False, allow_null=True)
    tied = serializers.BooleanField(required=False)
    team = LiteTeamSerializer(required=False, allow_null=True)
    debater = LiteDebaterSerializer(required=False, allow_null=True)
    school = LiteSchoolSerializer(required=False, allow_null=True)


class CotyBreakdownEntrySerializer(serializers.Serializer):
    debater = LiteDebaterSerializer()
    points = serializers.FloatField()
    qualled = serializers.BooleanField()
    contribution = serializers.FloatField()
    auto_quals = serializers.CharField(allow_blank=True)


class CotyStandingEntrySerializer(StandingEntrySerializer):
    breakdown = CotyBreakdownEntrySerializer(many=True)


class StandingsSerializer(serializers.Serializer):
    toty = StandingEntrySerializer(many=True, required=False)
    soty = StandingEntrySerializer(many=True, required=False)
    coty = CotyStandingEntrySerializer(many=True, required=False)
    noty = StandingEntrySerializer(many=True, required=False)
    online_quals = StandingEntrySerializer(many=True, required=False)


class SeasonStandingsResponseSerializer(serializers.Serializer):
    season = serializers.CharField()
    season_display = serializers.CharField()
    available_seasons = SeasonOptionSerializer(many=True)
    render_noty = serializers.BooleanField()
    using_online_quals = serializers.BooleanField()
    online_qual_bar = serializers.FloatField()
    standings = StandingsSerializer()
    links = LinkSerializer()


class ReplayStandingEntrySerializer(StandingEntrySerializer):
    all_markers = StandingMarkerSerializer(many=True)


class ReplayStandingsSerializer(serializers.Serializer):
    toty = ReplayStandingEntrySerializer(many=True)
    soty = ReplayStandingEntrySerializer(many=True)


class SeasonStandingsReplayResponseSerializer(serializers.Serializer):
    season = serializers.CharField()
    season_display = serializers.CharField()
    available_seasons = SeasonOptionSerializer(many=True)
    standings = ReplayStandingsSerializer()
    timeline_dates = serializers.ListField(child=serializers.CharField())
    marker_limits = serializers.DictField(child=serializers.IntegerField())
    links = LinkSerializer()


class StandingsThroughDateResponseSerializer(serializers.Serializer):
    season = serializers.CharField()
    season_display = serializers.CharField()
    through = serializers.CharField()
    marker_limits = serializers.DictField(child=serializers.IntegerField())
    standings = StandingsSerializer()
    links = LinkSerializer()


class SchoolListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    schools = LiteSchoolSerializer(many=True)


class SchoolDebatersResponseSerializer(serializers.Serializer):
    school = LiteSchoolSerializer()
    count = serializers.IntegerField()
    debaters = LiteDebaterSerializer(many=True)


class OTYGuideResponseSerializer(serializers.Serializer):
    title = serializers.CharField()
    season = serializers.CharField()
    season_display = serializers.CharField()
    format = serializers.CharField()
    body = serializers.CharField()
    last_modified = serializers.CharField(allow_null=True)
    links = LinkSerializer()


class ScheduleTournamentSerializer(serializers.Serializer):
    tournament = LiteTournamentSerializer()
    otys = serializers.DictField(child=serializers.BooleanField())


class ScheduleWeekSerializer(serializers.Serializer):
    date = serializers.IntegerField()
    one_more = serializers.IntegerField()
    tournaments = ScheduleTournamentSerializer(many=True)


class ScheduleMonthSerializer(serializers.Serializer):
    month = serializers.IntegerField()
    display = serializers.CharField()
    year = serializers.IntegerField()
    weeks = ScheduleWeekSerializer(many=True)


class ScheduleResponseSerializer(serializers.Serializer):
    season = serializers.CharField()
    season_display = serializers.CharField()
    notes = serializers.ListField(child=serializers.CharField())
    months = ScheduleMonthSerializer(many=True)
    links = LinkSerializer()


class VideoParticipantsSerializer(serializers.Serializer):
    pm = LiteDebaterSerializer(allow_null=True)
    mg = LiteDebaterSerializer(allow_null=True)
    lo = LiteDebaterSerializer(allow_null=True)
    mo = LiteDebaterSerializer(allow_null=True)


class TournamentVideoSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    round = serializers.CharField()
    round_code = serializers.CharField()
    link = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    case = serializers.CharField(allow_blank=True)
    permissions = serializers.CharField()
    tournament = LiteTournamentSerializer()
    participants = VideoParticipantsSerializer()
    tags = serializers.ListField(child=serializers.CharField())
    url = serializers.CharField(required=False)


class TeamResultSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    place = serializers.IntegerField(allow_null=True)
    place_display = serializers.CharField()
    type = serializers.CharField()
    type_code = serializers.IntegerField()
    ghost_points = serializers.BooleanField()
    team = LiteTeamSerializer()
    tournament = LiteTournamentSerializer()


class SpeakerResultSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    place = serializers.IntegerField()
    type = serializers.CharField()
    type_code = serializers.IntegerField()
    tie = serializers.BooleanField()
    debater = LiteDebaterSerializer()
    tournament = LiteTournamentSerializer()


class TeamTournamentEntrySerializer(serializers.Serializer):
    kind = serializers.CharField()
    team_result = TeamResultSerializer(required=False)
    record = serializers.CharField(allow_blank=True, required=False)
    tab_card = serializers.ListField(child=serializers.DictField(), required=False)
    tournament = LiteTournamentSerializer()


class TeamDetailResponseSerializer(serializers.Serializer):
    team = LiteTeamSerializer()
    toty_history = ReplayStandingEntrySerializer(many=True)
    tournaments = TeamTournamentEntrySerializer(many=True)
    links = LinkSerializer()


class TabCardEntrySerializer(serializers.Serializer):
    team = LiteTeamSerializer()
    tab_card = serializers.ListField(child=serializers.DictField())


class TeamAwardsSerializer(serializers.Serializer):
    varsity = TeamResultSerializer(many=True)
    novice = TeamResultSerializer(many=True)


class SpeakerAwardsSerializer(serializers.Serializer):
    varsity = SpeakerResultSerializer(many=True)
    novice = SpeakerResultSerializer(many=True)


class TournamentDetailResponseSerializer(serializers.Serializer):
    tournament = LiteTournamentSerializer()
    team_awards = TeamAwardsSerializer()
    speaker_awards = SpeakerAwardsSerializer()
    tab_cards_available = serializers.BooleanField()
    team_tab_cards = TabCardEntrySerializer(many=True)
    videos = TournamentVideoSerializer(many=True)
    links = LinkSerializer()


class SchoolSeasonSummarySerializer(serializers.Serializer):
    season = serializers.CharField()
    season_display = serializers.CharField()
    members = LiteDebaterSerializer(many=True)
    coty_breakdown = CotyBreakdownEntrySerializer(many=True)


class SchoolDetailResponseSerializer(serializers.Serializer):
    school = LiteSchoolSerializer()
    season = serializers.CharField()
    season_display = serializers.CharField()
    available_seasons = SeasonOptionSerializer(many=True)
    coty_history = CotyStandingEntrySerializer(many=True)
    hosted_tournaments = LiteTournamentSerializer(many=True)
    season_summary = SchoolSeasonSummarySerializer()
    links = LinkSerializer()


class DebaterStandingHistorySerializer(serializers.Serializer):
    toty = StandingEntrySerializer(many=True)
    soty = StandingEntrySerializer(many=True)
    noty = StandingEntrySerializer(many=True)


class DebaterTeamSummarySerializer(serializers.Serializer):
    team = LiteTeamSerializer()
    tournament_count = serializers.IntegerField()
    toty_points = serializers.FloatField()


class DebaterDetailResponseSerializer(serializers.Serializer):
    debater = LiteDebaterSerializer()
    first_season = serializers.CharField(allow_null=True)
    latest_season = serializers.CharField(allow_null=True)
    is_dino = serializers.BooleanField()
    contact_preferences = serializers.DictField(child=serializers.CharField(allow_null=True))
    also_debated_under = LiteDebaterSerializer(many=True)
    teams = DebaterTeamSummarySerializer(many=True)
    standings = DebaterStandingHistorySerializer()
    season_summaries = serializers.ListField(child=serializers.DictField())
    videos = TournamentVideoSerializer(many=True)
    links = LinkSerializer()


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
