import json
import math

from django.conf import settings

from core.utils.team import get_or_create_team_for_debaters

from core.models.school import School, SchoolLookup
from core.models.debater import Debater
from core.models.team import Team
from core.models.round import Round, RoundStats

from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult

from core.models.standings.toty import TOTY
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.soty import SOTY
from core.models.standings.online_qual import OnlineQUAL

from core.utils.rankings import (
    redo_rankings,
    update_toty,
    update_qual_points,
    update_soty,
    update_noty,
    update_online_quals
)


CREATE = 0
LINK = 1


def get_debaters(teams):
    to_return = []
    for team in teams:
        for debater in team['debaters']:
            debater['num_rounds'] = team['num_rounds']
            debater['school_id'] = team['school_id']
            debater['hybrid_school_id'] = team['hybrid_school_id']
            to_return += [debater]
    return to_return


def get_dict(_json):
    return json.loads(_json)


def get_num_teams(teams_list, num_rounds=5):
    return len([team \
                for team in teams_list \
                if team['num_rounds'] > math.ceil(num_rounds / 2)])


def get_num_novice_debaters(teams_list, num_rounds=5):
    debaters = get_debaters(teams_list)

    return len([debater \
                for debater in debaters \
                if debater['num_rounds'] > math.ceil(num_rounds / 2) and debater['status'] == 1])


def clean_keys(d):
    new_dict = {}

    for key in d:
        new_dict[int(key)] = d[key]

    return new_dict


def lookup_school(name):
    school = School.objects.filter(name=name)

    if school.exists():
        return school.first()

    lookup = SchoolLookup.objects.filter(server_name=name)

    if lookup.exists():
        return lookup.first().school

    return None


def create_schools(school_actions):
    completed_actions = {}

    for key, action in school_actions.items():
        if not 'id' in action:
            continue

        if action['action'] == LINK:
            completed_actions[key] = action['school']

            school = School.objects.get(id=action['school'])

            if school.name == action['name']:
                continue

            lookup = SchoolLookup.objects.filter(server_name=action['name']).first()

            if not lookup:
                lookup = SchoolLookup(server_name=action['name'])

            lookup.school = school
            lookup.save()

        if action['action'] == CREATE:
            school = School.objects.create(name=action['name'])

            completed_actions[key] = school.id

    return completed_actions


def create_teams(debater_completed_actions, teams):
    completed_actions = {}

    for team in teams:
        debaters = []

        for debater in team['debaters']:
            found_debater = Debater.objects.get(id=debater_completed_actions[debater['id']])

            debaters += [found_debater]

        found_team = get_or_create_team_for_debaters(debaters[0], debaters[1])
        completed_actions[team['id']] = found_team.id

    return completed_actions

def create_debaters(school_completed_actions, debater_actions):
    completed_actions = {}

    for key, action in debater_actions.items():
        if not 'id' in action:
            continue

        if action['action'] == LINK:
            completed_actions[key] = action['debater']

        if action['action'] == CREATE:
            name = action['name'].split(' ')

            first_name = action['name']
            last_name = ''

            if len(name) > 1:
                first_name = name[0]
                last_name = name[1]

            if action['school'] != -1:
                debater = Debater.objects.create(first_name=first_name,
                                                 last_name=last_name,
                                                 status=action['status'],
                                                 school=School.objects.get(id=action['school']))

                completed_actions[key] = debater.id

            else:
                debater = Debater.objects.create(first_name=first_name,
                                                 last_name=last_name,
                                                 status=action['status'],
                                                 school=School.objects.get(id=school_completed_actions[action['school_id']]))

                completed_actions[key] = debater.id

    return completed_actions


def create_rounds(team_completed_actions, tournament, rounds):
    completed_actions = {}

    Round.objects.filter(tournament=tournament).delete()

    for round in rounds:
        new_round = Round.objects.create(round_number=int(round['round_number']),
                                         gov=Team.objects.get(id=team_completed_actions[round['gov']]),
                                         opp=Team.objects.get(id=team_completed_actions[round['opp']]),
                                         victor=round['victor'],
                                         tournament=tournament)

        completed_actions[round['id']] = new_round.id

    return completed_actions


def create_round_stats(debater_completed_actions, round_completed_actions, tournament, round_stats):
    RoundStats.objects.filter(round__tournament=tournament).all().delete()

    for round_stat in round_stats:
        round_stat = RoundStats.objects.create(round=Round.objects.get(id=round_completed_actions[round_stat['round']]),
                                               debater=Debater.objects.get(id=debater_completed_actions[round_stat['debater']]),
                                               speaks=round_stat['speaks'],
                                               ranks=round_stat['ranks'],
                                               debater_role=round_stat['role'])


def create_speaker_awards(debater_completed_actions,
                          speaker_awards,
                          type_of_result,
                          tournament):
    debaters_changed = []

    to_delete = SpeakerResult.objects.filter(tournament=tournament,
                                             type_of_place=type_of_result)

    for speaker in to_delete:
        debaters_changed += [speaker.debater]
        speaker.delete()

    for award in speaker_awards[:10]:
        debater = Debater.objects.get(id=debater_completed_actions[award['debater']])
        SpeakerResult.objects.create(tournament=tournament,
                                     debater=debater,
                                     type_of_place=type_of_result,
                                     place=award['place'])

        debaters_changed += [debater]

    for debater in debaters_changed:
        update_soty(debater)
        update_noty(debater)

    redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='soty')
    redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='noty')


def create_team_awards(team_completed_actions,
                       team_awards,
                       type_of_result,
                       tournament):
    teams_changed = []
    
    to_delete = TeamResult.objects.filter(tournament=tournament,
                                          type_of_place=type_of_result)
    
    for team in to_delete:
        teams_changed += [team.team]
        team.delete()

    for award in team_awards[:16]:
        team = Team.objects.get(id=team_completed_actions[award['team']])
        TeamResult.objects.create(tournament=tournament,
                                  team=team,
                                  type_of_place=type_of_result,
                                  place=award['place'])

        teams_changed += [team]

    for team in teams_changed:
        update_toty(team)
        update_qual_points(team)

        if tournament.season in settings.ONLINE_SEASONS:
            update_online_quals(team)

    redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='toty')
    redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='coty')
    redo_rankings(OnlineQUAL.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='online_quals')
    
