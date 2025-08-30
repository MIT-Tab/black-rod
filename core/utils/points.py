import math


def online_points(place):
    if place == 1:
        return 12.5
    if place == 2:
        return 10
    if place < 5:
        return 7.5
    if place < 9:
        return 5
    if place < 17:
        return 2.5
    if place < 33:
        return 1.25
    return 0


def team_points_for_size(num_teams, place, ghost_points=False):
    toty = 0
    if place < 0:
        return 0

    if num_teams < 8:
        return 0

    if num_teams < 16:
        if place == 1:
            return 8
        if place == 2:
            return 4
        return 0

    if num_teams < 72:
        if place == 1:
            return 12 + math.floor((num_teams - 16) / 8)
        if place == 2:
            return 8 + math.floor((num_teams - 16) / 8)
        if place < 5:
            return 3 + 0.75 * math.floor((num_teams - 16) / 8)
        if place < 9 or ghost_points:
            return 0.5 * math.floor((num_teams - 16) / 8)
        return 0

    if num_teams < 80:
        if place == 1:
            return 19
        if place == 2:
            return 15
        if place < 5:
            return 8.25
        if place < 9:
            return 3.5
        if place < 17 or ghost_points:
            return 0.75
        return 0

    if place == 1:
        return 20
    if place == 2:
        return 16
    if place < 5:
        return 9
    if place < 9:
        return 4
    if place < 17 or ghost_points:
        return 1.5

    return 0


def speaker_points_for_size(num_teams, place):
    soty = 0

    if num_teams < 8:
        soty = 0
    elif num_teams < 16:
        soty = 8
    elif num_teams < 80:
        soty = 12 + math.floor((num_teams - 16) / 8)
    else:
        soty = 20

    return max(0, soty - 2.5 * (place - 1))


def novice_points_for_size(num_novices, place):
    nsize = min(20, 10 + math.floor(num_novices / 8))

    noty = max(0, nsize - 2.5 * (place - 1))

    return noty
