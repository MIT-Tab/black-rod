pub(crate) fn team_points_for_size(teams: i64, place: i64, ghost: bool) -> f64 {
    if place < 0 || teams < 8 {
        return 0.0;
    }
    if teams < 16 {
        return match place {
            1 => 8.0,
            2 => 4.0,
            _ => 0.0,
        };
    }
    let semi = place < 5 || (place == 5 && ghost);
    if teams < 72 {
        let step = ((teams - 16) / 8) as f64;
        return match place {
            1 => 12.0 + step,
            2 => 8.0 + step,
            _ if semi => 3.0 + 0.75 * step,
            _ if place < 9 || ghost => 0.5 * step,
            _ => 0.0,
        };
    }
    let quarter = place < 9 || (place == 9 && ghost);
    let octo = place < 17 || ghost;
    let points = if teams < 80 {
        [19.0, 15.0, 8.25, 3.5, 0.75]
    } else {
        [20.0, 16.0, 9.0, 4.0, 1.5]
    };
    match place {
        1 => points[0],
        2 => points[1],
        _ if semi => points[2],
        _ if quarter => points[3],
        _ if octo => points[4],
        _ => 0.0,
    }
}

pub(crate) fn speaker_points_for_size(teams: i64, place: i64) -> f64 {
    let first = match teams {
        ..=7 => 0.0,
        8..=15 => 8.0,
        16..=79 => (12 + (teams - 16) / 8) as f64,
        _ => 20.0,
    };
    (first - 2.5 * (place - 1) as f64).max(0.0)
}

pub(crate) fn novice_points_for_size(novices: i64, place: i64) -> f64 {
    let first = (10 + novices.div_euclid(8)).min(20) as f64;
    (first - 2.5 * (place - 1) as f64).max(0.0)
}

pub(crate) fn online_points(place: i64) -> f64 {
    match place {
        1 => 12.5,
        2 => 10.0,
        ..=4 => 7.5,
        5..=8 => 5.0,
        9..=16 => 2.5,
        17..=32 => 1.25,
        _ => 0.0,
    }
}

pub(crate) fn historical_qualification_bar(season: &str) -> Option<f64> {
    Some(match season {
        "2025" => 11.5,
        "2024" | "2022" => 10.5,
        "2023" => 9.0,
        "2019" => 16.75,
        "2018" | "2016" => 18.5,
        "2017" => 19.5,
        "2015" => 14.25,
        "2012" => 15.0,
        "2007" | "2008" | "2009" | "2010" | "2011" | "2013" | "2014" => 14.0,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn team_awards_boundaries_and_ghost_places() {
        for (teams, place, ghost, expected) in [
            (7, 1, false, 0.0),
            (8, 1, false, 8.0),
            (15, 2, false, 4.0),
            (16, 1, false, 12.0),
            (71, 1, false, 18.0),
            (72, 1, false, 19.0),
            (79, 2, false, 15.0),
            (80, 1, false, 20.0),
            (80, 2, false, 16.0),
            (80, 5, false, 4.0),
            (80, 5, true, 9.0),
            (72, 9, false, 0.75),
            (72, 9, true, 3.5),
            (72, 17, false, 0.0),
            (72, 33, true, 0.75),
            (71, 5, true, 7.5),
            (71, 5, false, 3.0),
            (80, -1, true, 0.0),
        ] {
            assert_eq!(
                team_points_for_size(teams, place, ghost),
                expected,
                "{teams} {place} {ghost}"
            );
        }
    }

    #[test]
    fn speaker_awards_preserve_place_arithmetic() {
        for (teams, place, expected) in [
            (7, 1, 0.0),
            (8, 1, 8.0),
            (16, 2, 9.5),
            (79, 1, 19.0),
            (80, 3, 15.0),
            (80, 10, 0.0),
            (8, 0, 10.5),
        ] {
            assert_eq!(speaker_points_for_size(teams, place), expected);
        }
    }
}
