use std::collections::HashMap;

use sqlx::{AnyConnection, Row, any::AnyRow};

use super::{Field, FilterValue, Filters, Input, RowCells};
use crate::{ApiError, team_profile};

#[derive(Clone, Copy)]
pub(super) enum Standing {
    Team,
    Speaker,
    Novice,
    Club,
}

const TEAM: &str = "FROM core_toty x JOIN core_team t ON t.id=x.team_id WHERE 1=1";
const SPEAKER: &str = "FROM core_soty x JOIN core_debater d ON d.id=x.debater_id
    LEFT JOIN core_school s ON s.id=d.school_id WHERE 1=1";
const NOVICE: &str = "FROM core_noty x JOIN core_debater d ON d.id=x.debater_id
    LEFT JOIN core_school s ON s.id=d.school_id WHERE 1=1";
const CLUB: &str = "FROM core_coty x JOIN core_school s ON s.id=x.school_id WHERE 1=1";

const PLACE: Field = Field {
    name: "place",
    label: "Place",
    order: "x.place",
};
const POINTS: Field = Field {
    name: "points",
    label: "Points",
    order: "x.points",
};
const SCHOOL: Field = Field {
    name: "school",
    label: "School",
    order: "s.name",
};
const DEBATER: Field = Field {
    name: "debater",
    label: "Debater",
    order: "d.last_name, d.first_name",
};
const MARKERS: [Field; 6] = [
    Field {
        name: "marker_one",
        label: "1",
        order: "x.marker_one",
    },
    Field {
        name: "marker_two",
        label: "2",
        order: "x.marker_two",
    },
    Field {
        name: "marker_three",
        label: "3",
        order: "x.marker_three",
    },
    Field {
        name: "marker_four",
        label: "4",
        order: "x.marker_four",
    },
    Field {
        name: "marker_five",
        label: "5",
        order: "x.marker_five",
    },
    Field {
        name: "marker_six",
        label: "6",
        order: "x.marker_six",
    },
];
const WORDS: [&str; 6] = ["one", "two", "three", "four", "five", "six"];

type SearchField = (&'static str, &'static str, &'static str);
const TEAM_SEARCH: &[SearchField] = &[
    ("team__id", "Team ID", "t.id"),
    ("team__name__icontains", "Team name contains", "t.name"),
];
const DEBATER_SEARCH: &[SearchField] = &[
    ("debater__id", "Debater ID", "d.id"),
    (
        "debater__first_name__icontains",
        "Debater first name contains",
        "d.first_name",
    ),
    (
        "debater__last_name__icontains",
        "Debater last name contains",
        "d.last_name",
    ),
    (
        "debater__school__name__icontains",
        "Debater school name contains",
        "s.name",
    ),
];
const SCHOOL_SEARCH: &[SearchField] = &[
    ("school__id", "School ID", "s.id"),
    ("school__name__icontains", "School name contains", "s.name"),
];

impl Standing {
    pub(super) fn title(self) -> &'static str {
        match self {
            Self::Team => "Team of the Year",
            Self::Speaker => "Speaker of the Year",
            Self::Novice => "Novice of the Year",
            Self::Club => "Club of the Year",
        }
    }

    pub(super) fn source(self) -> &'static str {
        match self {
            Self::Team => TEAM,
            Self::Speaker => SPEAKER,
            Self::Novice => NOVICE,
            Self::Club => CLUB,
        }
    }

    fn marker_count(self) -> usize {
        match self {
            Self::Club => 0,
            Self::Speaker => 6,
            Self::Team | Self::Novice => 5,
        }
    }

    pub(super) fn columns(self) -> &'static [Field] {
        const TEAM_COL: Field = Field {
            name: "team",
            label: "Team",
            order: "t.name",
        };
        const TEAM_DEBATERS: Field = Field {
            name: "team__debaters",
            label: "Debaters",
            order: "",
        };
        const TEAM: &[Field] = &[
            PLACE,
            TEAM_COL,
            TEAM_DEBATERS,
            POINTS,
            MARKERS[0],
            MARKERS[1],
            MARKERS[2],
            MARKERS[3],
            MARKERS[4],
        ];
        const SPEAKER: &[Field] = &[
            PLACE, DEBATER, SCHOOL, POINTS, MARKERS[0], MARKERS[1], MARKERS[2], MARKERS[3],
            MARKERS[4], MARKERS[5],
        ];
        const NOVICE: &[Field] = &[
            PLACE, DEBATER, SCHOOL, POINTS, MARKERS[0], MARKERS[1], MARKERS[2], MARKERS[3],
            MARKERS[4],
        ];
        const CLUB: &[Field] = &[PLACE, SCHOOL, POINTS];
        match self {
            Self::Team => TEAM,
            Self::Speaker => SPEAKER,
            Self::Novice => NOVICE,
            Self::Club => CLUB,
        }
    }

    pub(super) fn projection(self) -> String {
        let mut select = String::from(
            "CAST(x.id AS BIGINT) AS id,
            CAST(x.place AS BIGINT) AS place, CAST(x.points AS DOUBLE PRECISION) AS points,
            CAST(CASE WHEN x.tied THEN 1 ELSE 0 END AS BIGINT) AS tied",
        );
        select.push_str(match self {
            Self::Team => ", CAST(t.id AS BIGINT) AS entity_id,t.name AS entity_name",
            Self::Speaker | Self::Novice => ", CAST(d.id AS BIGINT) AS entity_id,
                TRIM(d.first_name || ' ' || d.last_name) AS entity_name, CAST(s.id AS BIGINT) AS school_id,s.name AS school_name",
            Self::Club => ", CAST(s.id AS BIGINT) AS entity_id,s.name AS entity_name",
        });
        for word in WORDS.iter().take(self.marker_count()) {
            select.push_str(&format!(", CAST(x.marker_{word} AS DOUBLE PRECISION) AS marker_{word},
                (SELECT m.name FROM core_tournament m WHERE m.id=x.tournament_{word}_id) AS tournament_{word}"));
        }
        select
    }

    fn search(self) -> &'static [SearchField] {
        match self {
            Self::Team => TEAM_SEARCH,
            Self::Speaker | Self::Novice => DEBATER_SEARCH,
            Self::Club => SCHOOL_SEARCH,
        }
    }

    pub(super) fn filters(self, filters: &mut Filters, values: &HashMap<&str, &str>) {
        filters.add(
            "x.season = {}",
            FilterValue::Text(values["season"].trim().to_owned()),
        );
        for (name, _, column) in self.search() {
            let value = values.get(name).copied().unwrap_or_default().trim();
            if name.ends_with("__id") {
                if let Ok(id) = value.parse() {
                    filters.add(&format!("{column} = {{}}"), FilterValue::Number(id));
                }
            } else if !value.is_empty() {
                let literal = value
                    .replace('!', "!!")
                    .replace('%', "!%")
                    .replace('_', "!_");
                filters.add(
                    &format!("LOWER({column}) LIKE LOWER({{}}) ESCAPE '!'"),
                    FilterValue::Text(format!("%{literal}%")),
                );
            }
        }
    }

    pub(super) fn inputs<'a>(self, values: &HashMap<&str, &'a str>) -> Vec<Input<'a>> {
        self.search()
            .iter()
            .copied()
            .chain([("season", "Season", "")])
            .map(|(name, label, _)| Input {
                name,
                label,
                value: values.get(name).copied().unwrap_or_default(),
                numeric: name.ends_with("__id"),
                options: Vec::new(),
                autocomplete: None,
                date: false,
            })
            .collect()
    }

    pub(super) async fn cells(
        self,
        row: &AnyRow,
        connection: &mut AnyConnection,
    ) -> Result<RowCells, ApiError> {
        let id: i64 = row.try_get("entity_id")?;
        let place: i64 = row.try_get("place")?;
        let tied = row.try_get::<i64, _>("tied")? != 0;
        let plain = |value| vec![(value, String::new())];
        let mut cells = vec![plain(if tied {
            format!("T-{place}")
        } else {
            place.to_string()
        })];
        let directory = match self {
            Self::Team => "teams",
            Self::Speaker | Self::Novice => "debaters",
            Self::Club => "schools",
        };
        cells.push(vec![(
            row.try_get("entity_name")?,
            format!("/core/{directory}/{id}"),
        )]);
        match self {
            Self::Team => {
                let people = team_profile::members(connection, id, "").await?;
                cells.push(plain(
                    people
                        .iter()
                        .filter_map(|person| person["name"].as_str())
                        .collect::<Vec<_>>()
                        .join(", "),
                ));
            }
            Self::Speaker | Self::Novice => {
                let school_id: Option<i64> = row.try_get("school_id")?;
                let school_name: Option<String> = row.try_get("school_name")?;
                cells.push(school_id.zip(school_name).map_or_else(
                    || plain("—".to_owned()),
                    |(id, name)| vec![(name, format!("/core/schools/{id}"))],
                ));
            }
            Self::Club => (),
        }
        cells.push(plain(row.try_get::<f64, _>("points")?.to_string()));
        for word in WORDS.iter().take(self.marker_count()) {
            let points: f64 = row.try_get(format!("marker_{word}").as_str())?;
            let name: Option<String> = row.try_get(format!("tournament_{word}").as_str())?;
            let marker = match name {
                Some(name) if points != 0.0 => {
                    let rounded = (points * 100.0).round_ties_even() / 100.0;
                    let mut display = rounded.to_string();
                    if !display.contains('.') && rounded.abs() >= 10.0 {
                        let trailing = display.len() - display.trim_end_matches('0').len();
                        if trailing > 0 {
                            display.truncate(display.len() - trailing);
                            display.push_str(&format!("E+{trailing}"));
                        }
                    }
                    format!("{display} ({name})")
                }
                _ => String::new(),
            };
            cells.push(plain(marker));
        }
        Ok(cells)
    }
}
