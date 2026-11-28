use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;
use sqlx::{AnyConnection, Row};

use crate::ApiError;

#[derive(Clone, Copy, Serialize)]
pub(super) struct Tab {
    pub(super) key: &'static str,
    pub(super) kind: &'static str,
    pub(super) division: i64,
    pub(super) item: &'static str,
    pub(super) label: &'static str,
    pub(super) ghost: bool,
}

pub(super) const SCHOOL: Tab = Tab {
    key: "schools",
    kind: "school",
    division: 0,
    item: "School",
    label: "Schools",
    ghost: false,
};
pub(super) const DEBATER: Tab = Tab {
    key: "debaters",
    kind: "debater",
    division: 0,
    item: "Debater",
    label: "Debaters",
    ghost: false,
};

pub(super) const TABS: [Tab; 5] = [
    Tab {
        key: "varsity_teams",
        kind: "team",
        division: 1,
        item: "Team",
        label: "Varsity Teams",
        ghost: true,
    },
    Tab {
        key: "varsity_speakers",
        kind: "speaker",
        division: 1,
        item: "Speaker",
        label: "Varsity Speakers",
        ghost: false,
    },
    Tab {
        key: "novice_teams",
        kind: "team",
        division: 0,
        item: "Team",
        label: "Novice Teams",
        ghost: false,
    },
    Tab {
        key: "novice_speakers",
        kind: "speaker",
        division: 0,
        item: "Speaker",
        label: "Novice Speakers",
        ghost: false,
    },
    Tab {
        key: "unplaced_teams",
        kind: "team",
        division: 1,
        item: "Unplaced Team",
        label: "Non-placing Teams",
        ghost: false,
    },
];

#[derive(Default, Serialize)]
pub(super) struct ResultRow {
    pub(super) first: Option<i64>,
    pub(super) record: Option<i64>,
    pub(super) name: String,
    pub(super) last_name: String,
    pub(super) short_name: String,
    pub(super) school: Option<i64>,
    pub(super) school_name: String,
    pub(super) included: bool,
    pub(super) second: Option<i64>,
    pub(super) first_source: String,
    pub(super) second_source: String,
    pub(super) order: i64,
    pub(super) scoring: bool,
    pub(super) ghost: bool,
    pub(super) tie: bool,
    pub(super) deleted: bool,
    pub(super) errors: Vec<String>,
    pub(super) field_errors: BTreeMap<&'static str, &'static str>,
}

#[derive(Serialize)]
pub(super) struct Panel {
    #[serde(flatten)]
    pub(super) tab: Tab,
    pub(super) rows: Vec<ResultRow>,
    pub(super) initial_count: usize,
    pub(super) errors: Vec<String>,
}

impl Panel {
    pub(super) fn new(tab: Tab) -> Self {
        Self {
            tab,
            rows: Vec::new(),
            initial_count: 0,
            errors: Vec::new(),
        }
    }
    pub(super) fn valid(&self) -> bool {
        self.errors.is_empty()
            && self
                .rows
                .iter()
                .all(|row| row.deleted || (row.errors.is_empty() && row.field_errors.is_empty()))
    }
}

pub(super) type FormValues = BTreeMap<String, String>;

fn extract_person(value: Option<&String>, errors: &mut Vec<String>) -> Option<i64> {
    let value = value.filter(|value| !value.trim().is_empty())?;
    let id = value.parse::<i64>().ok().filter(|id| *id > 0);
    if id.is_none() {
        errors.push(
            "Select a valid choice. That choice is not one of the available choices.".to_owned(),
        );
    }
    id
}

pub(super) fn submitted(values: &FormValues, reviewing: bool) -> Vec<Panel> {
    const FORM_TAMPERED: &str = "ManagementForm data is missing or has been tampered with.";
    [SCHOOL, DEBATER]
        .into_iter()
        .filter(|_| reviewing)
        .chain(TABS)
        .map(|tab| {
            let mut panel = Panel::new(tab);
            let input = |field: &str| values.get(&format!("{}-{field}", tab.key));
            let count = |field| {
                input(field)
                    .and_then(|value| value.parse::<i64>().ok())
                    .map(|value| value.max(0) as usize)
            };
            let Some(total) = count("TOTAL_FORMS") else {
                panel.errors.push(format!(
                    "{FORM_TAMPERED} Missing fields: {}-TOTAL_FORMS.",
                    tab.key
                ));
                return panel;
            };
            let Some(initial) = count("INITIAL_FORMS") else {
                panel.errors.push(format!(
                    "{FORM_TAMPERED} Missing fields: {}-INITIAL_FORMS.",
                    tab.key
                ));
                return panel;
            };
            panel.initial_count = initial;
            let maximum = if matches!(tab.kind, "school" | "debater") {
                500
            } else {
                150
            };
            let absolute_maximum = maximum + 1000;
            if total > absolute_maximum {
                panel
                    .errors
                    .push(format!("Please submit at most {maximum} forms."));
            }
            for index in 0..total.min(absolute_maximum) {
                let field = |name: &str| input(&format!("{index}-{name}"));
                let check = |name| {
                    field(name).is_some_and(|value| {
                        !value.is_empty() && !value.eq_ignore_ascii_case("false")
                    })
                };
                let mut row = ResultRow {
                    order: index as i64 + 1,
                    scoring: check("counts_for_points"),
                    ghost: tab.ghost && check("ghost_points"),
                    tie: check("tie"),
                    deleted: check("DELETE"),
                    ..Default::default()
                };
                if matches!(tab.kind, "team" | "speaker")
                    && let Some(value) = field("ORDER")
                        .map(|value| value.trim())
                        .filter(|value| !value.is_empty())
                {
                    // Django's IntegerField also accepts a decimal representation ending in zeroes.
                    let integer = value
                        .split_once('.')
                        .filter(|(_, fraction)| fraction.bytes().all(|byte| byte == b'0'))
                        .map_or(value, |(integer, _)| integer);
                    match integer.parse::<i32>() {
                        Ok(order) => row.order = i64::from(order),
                        Err(_) => row.errors.push("Enter a whole number.".to_owned()),
                    }
                }
                if matches!(tab.kind, "school" | "debater") {
                    row.record = extract_person(field("id"), &mut row.errors);
                    row.name = field(if tab.kind == "school" {
                        "name"
                    } else {
                        "first_name"
                    })
                    .cloned()
                    .unwrap_or_default();
                    row.last_name = field("last_name").cloned().unwrap_or_default();
                    row.short_name = field("short_name")
                        .map(|name| name.trim().to_owned())
                        .unwrap_or_default();
                    row.school = extract_person(field("school"), &mut row.errors);
                    let field_name = if tab.kind == "school" {
                        "existing_school"
                    } else {
                        "existing_debater"
                    };
                    row.first = extract_person(field(field_name), &mut row.errors);
                    row.school_name = field("school_name").cloned().unwrap_or_default();
                    row.first_source = field(if tab.kind == "school" {
                        "server_name"
                    } else {
                        "tournament_id"
                    })
                    .cloned()
                    .unwrap_or_default();
                    row.included = check("included_in_oty");
                    if tab.kind == "debater" && !row.deleted {
                        let nonempty =
                            |name| field(name).is_some_and(|value| !value.trim().is_empty());
                        let edited = index < initial
                            || [
                                "id",
                                "first_name",
                                "last_name",
                                "school",
                                "existing_debater",
                                "school_name",
                                "tournament_id",
                            ]
                            .into_iter()
                            .any(nonempty);
                        if edited {
                            for name in ["first_name", "school"] {
                                if !nonempty(name) {
                                    row.field_errors.insert(name, "This field is required.");
                                }
                            }
                        }
                    }
                } else if tab.kind == "team" {
                    row.first = extract_person(field("debater_one"), &mut row.errors);
                    row.second = extract_person(field("debater_two"), &mut row.errors);
                    row.first_source = field("debater_one_tournament_id")
                        .cloned()
                        .unwrap_or_default();
                    row.second_source = field("debater_two_tournament_id")
                        .cloned()
                        .unwrap_or_default();
                } else {
                    row.first = extract_person(field("speaker"), &mut row.errors);
                    row.first_source = field("tournament_id").cloned().unwrap_or_default();
                }
                panel.rows.push(row);
            }
            panel
        })
        .collect()
}

pub(super) async fn current(
    conn: &mut AnyConnection,
    tournament: i64,
) -> Result<Vec<Panel>, ApiError> {
    let mut panels = TABS.map(Panel::new);
    let teams=sqlx::query("SELECT CAST(r.id AS BIGINT) AS id,CAST(r.team_id AS BIGINT) AS team_id,
        CAST(r.type_of_place AS BIGINT) AS division,CAST(r.place AS BIGINT) AS place,
        CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
        CAST(CASE WHEN r.counts_for_points THEN 1 ELSE 0 END AS BIGINT) AS scoring,
        CAST((SELECT d.id FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id
            WHERE m.team_id=r.team_id AND NOT d.temporary AND NOT d.synthetic ORDER BY d.id LIMIT 1) AS BIGINT) AS first,
        CAST((SELECT d.id FROM core_team_debaters m JOIN core_debater d ON d.id=m.debater_id
            WHERE m.team_id=r.team_id AND NOT d.temporary AND NOT d.synthetic ORDER BY d.id LIMIT 1 OFFSET 1) AS BIGINT) AS second
        FROM core_teamresult r WHERE r.tournament_id=$1 AND (r.place>0 OR (r.place=-1 AND r.type_of_place=1))
        ORDER BY r.place,r.id").bind(tournament).fetch_all(&mut *conn).await?;
    for row in teams {
        let panel = &mut panels[match (
            row.try_get::<i64, _>("division")?,
            row.try_get::<i64, _>("place")?,
        ) {
            (1, -1) => 4,
            (1, _) => 0,
            _ => 2,
        }];
        panel.rows.push(ResultRow {
            first: row.try_get("first")?,
            second: row.try_get("second")?,
            order: panel.rows.len() as i64 + 1,
            ghost: panel.tab.ghost && row.try_get::<i64, _>("ghost")? == 1,
            scoring: row.try_get::<i64, _>("scoring")? == 1,
            ..Default::default()
        });
    }
    let speakers=sqlx::query("SELECT CAST(debater_id AS BIGINT) AS person,CAST(type_of_place AS BIGINT) AS division,
        CAST(CASE WHEN tie THEN 1 ELSE 0 END AS BIGINT) AS tie,
        CAST(CASE WHEN counts_for_points THEN 1 ELSE 0 END AS BIGINT) AS scoring FROM core_speakerresult
        WHERE tournament_id=$1 AND place>0 ORDER BY place,id").bind(tournament).fetch_all(&mut *conn).await?;
    for row in speakers {
        let panel = &mut panels[if row.try_get::<i64, _>("division")? == 1 {
            1
        } else {
            3
        }];
        panel.rows.push(ResultRow {
            first: row.try_get("person")?,
            order: panel.rows.len() as i64 + 1,
            scoring: row.try_get::<i64, _>("scoring")? == 1,
            tie: row.try_get::<i64, _>("tie")? == 1,
            ..Default::default()
        });
    }
    for panel in &mut panels {
        panel.initial_count = panel.rows.len();
    }
    Ok(panels.into())
}

#[derive(Default)]
pub(super) struct Choices {
    pub(super) labels: BTreeMap<String, String>,
    pub(super) active: BTreeSet<i64>,
}

pub(super) async fn labels(
    conn: &mut AnyConnection,
    panels: &[Panel],
    autoquals: &[i64],
    temporary: bool,
) -> Result<Choices, ApiError> {
    let mut ids = autoquals.iter().copied().collect::<BTreeSet<_>>();
    ids.extend(
        panels
            .iter()
            .filter(|panel| panel.tab.kind != "school")
            .flat_map(|panel| panel.rows.iter())
            .flat_map(|row| [row.first, row.second])
            .flatten(),
    );
    let mut people = Choices::default();
    for id in ids {
        let entry = sqlx::query_as::<_, (String, i64)>(
            "SELECT TRIM(first_name || ' ' || last_name),
                CAST(CASE WHEN NOT (synthetic OR temporary) THEN 1 ELSE 0 END AS BIGINT)
             FROM core_debater WHERE id=$1",
        )
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?;
        if let Some((label, active)) = entry {
            if active == 1 {
                people.active.insert(id);
            }
            if active == 1 || temporary {
                people.labels.insert(id.to_string(), label);
            }
        }
    }
    Ok(people)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn review_panel_caps_follow_the_matching_django_formsets() {
        let mut values = FormValues::new();
        for (tab, total) in [(SCHOOL, 1300), (DEBATER, 1501), (TABS[0], 1151)] {
            values.insert(format!("{}-TOTAL_FORMS", tab.key), total.to_string());
            values.insert(format!("{}-INITIAL_FORMS", tab.key), "0".to_owned());
        }
        let panels = submitted(&values, true);
        assert!(panels[0].valid());
        assert_eq!(panels[0].rows.len(), 1300);
        assert_eq!(panels[1].rows.len(), 1500);
        assert_eq!(panels[1].errors, ["Please submit at most 500 forms."]);
        assert_eq!(panels[2].rows.len(), 1150);
        assert_eq!(panels[2].errors, ["Please submit at most 150 forms."]);
    }
}
