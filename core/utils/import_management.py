import json
import math


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
                if debater['num_rounds'] > math.ceil(num_rounds / 2)])
