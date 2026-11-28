use std::collections::{HashMap, HashSet};

use sqlx::{AnyPool, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError,
    directory::{Filters, RowCells},
    schedule::season_display,
    schools::{region_label, region_list},
};

const PERSON: &str = "
    CAST(x.id AS BIGINT) AS id, CAST(x.alias_group_id AS BIGINT) AS alias_group,
    x.first_name, x.last_name, x.latest_season, CAST(x.status AS BIGINT) AS status,
    CAST(x.region AS TEXT) AS region, CAST(s.id AS BIGINT) AS school_id, s.name AS school,
    CAST(CASE WHEN x.dino_to_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS outreach,
    CAST(CASE WHEN x.dino_judge_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS judge
";

pub(crate) const REGIONS: &[&str] = &[
    "north", "south", "central", "midwest", "west", "online", "other",
];

struct Profile {
    id: i64,
    alias: Option<i64>,
    first: String,
    last: String,
    season: String,
    school: Option<(i64, String)>,
    status: i64,
    regions: Vec<String>,
    outreach: bool,
    judge: bool,
}

impl Profile {
    fn row(row: &AnyRow) -> Result<Self, sqlx::Error> {
        let outreach = row.try_get::<i64, _>("outreach")? != 0;
        let judge = row.try_get::<i64, _>("judge")? != 0;
        let school_id = row.try_get::<Option<i64>, _>("school_id")?;
        let school_name = row.try_get::<Option<String>, _>("school")?;
        Ok(Self {
            id: row.try_get("id")?,
            alias: row.try_get("alias_group")?,
            first: row.try_get("first_name")?,
            last: row.try_get("last_name")?,
            season: row
                .try_get::<Option<String>, _>("latest_season")?
                .unwrap_or_default(),
            school: school_id.zip(school_name),
            status: row.try_get("status")?,
            regions: if outreach || judge {
                region_list(row.try_get("region")?)
            } else {
                Vec::new()
            },
            outreach,
            judge,
        })
    }

    fn opted_in(&self, judge: bool) -> bool {
        if judge {
            self.judge && self.status == 2
        } else {
            self.outreach
        }
    }

    fn unaffiliated(&self) -> bool {
        self.school
            .as_ref()
            .is_some_and(|(_, name)| name.eq_ignore_ascii_case("unaffiliated"))
    }
}

struct Entry<'a> {
    person: &'a Profile,
    schools: Vec<(i64, String)>,
    season: &'a str,
}

fn year(season: &str) -> Option<i32> {
    season.split('-').next()?.parse().ok()
}

fn group<'a>(profiles: &'a [Profile], primary: &'a Profile, judge: bool) -> Entry<'a> {
    let mut schools = HashMap::new();
    let mut unaffiliated = HashMap::new();
    let mut season = None;
    for profile in profiles
        .iter()
        .filter(|profile| !judge || profile.status == 2)
    {
        if let Some((id, name)) = &profile.school {
            if profile.unaffiliated() {
                unaffiliated.insert(*id, name.clone());
            } else {
                schools.insert(*id, name.clone());
                if let Some(year) = year(&profile.season)
                    && season.is_none_or(|(before, _)| year > before)
                {
                    season = Some((year, profile.season.as_str()));
                }
            }
        }
    }
    let person = profiles
        .iter()
        .find(|p| p.opted_in(judge) && p.unaffiliated())
        .or_else(|| profiles.iter().find(|p| p.opted_in(judge)))
        .or_else(|| profiles.iter().find(|p| !judge || p.status == 2))
        .unwrap_or(primary);
    if schools.is_empty() {
        schools = unaffiliated;
    }
    let mut schools: Vec<_> = schools.into_iter().collect();
    schools.sort_by(|a, b| a.1.cmp(&b.1));
    Entry {
        person,
        schools,
        season: season.map_or(&person.season, |(_, season)| season),
    }
}

pub(crate) async fn rows(
    pool: &AnyPool,
    filters: &Filters,
    judge: bool,
    region: &str,
    ordering: &[&str],
) -> Result<Vec<RowCells>, ApiError> {
    let opt = if judge {
        "x.status=2 AND x.dino_judge_contact_opt_in"
    } else {
        "x.dino_to_contact_opt_in"
    };
    let query = format!(
        "SELECT {PERSON} {} AND ({opt}) ORDER BY x.last_name,x.first_name,x.id",
        filters.query
    );
    let eligible = filters
        .bind(sqlx::query(AssertSqlSafe(query)))
        .fetch_all(pool)
        .await?;
    let selected = eligible
        .iter()
        .map(Profile::row)
        .collect::<Result<Vec<_>, _>>()?;
    let mut seen = HashSet::new();
    let mut relevant = Vec::new();
    for profile in selected
        .iter()
        .filter(|profile| region.is_empty() || profile.regions.iter().any(|name| name == region))
    {
        if profile.alias.is_none_or(|key| seen.insert(key)) {
            relevant.push(profile);
        }
    }
    let mut groups = HashMap::new();
    if !seen.is_empty() {
        let query = format!(
            "SELECT {PERSON} FROM core_debater x LEFT JOIN core_school s ON s.id=x.school_id
            WHERE x.alias_group_id=$1 AND NOT x.synthetic AND NOT x.temporary ORDER BY x.id"
        );
        for id in seen {
            let rows = sqlx::query(AssertSqlSafe(query.as_str()))
                .bind(id)
                .fetch_all(pool)
                .await?;
            let profiles = rows
                .iter()
                .map(Profile::row)
                .collect::<Result<Vec<_>, _>>()?;
            groups.insert(id, profiles);
        }
    }
    let mut entries: Vec<_> = relevant
        .into_iter()
        .map(|profile| {
            if let Some(group_profiles) = profile.alias.and_then(|alias| groups.get(&alias)) {
                group(group_profiles, profile, judge)
            } else {
                Entry {
                    person: profile,
                    schools: profile.school.iter().cloned().collect(),
                    season: &profile.season,
                }
            }
        })
        .collect();
    entries.sort_by(|a, b| {
        year(a.season)
            .unwrap_or(9999)
            .cmp(&year(b.season).unwrap_or(9999))
            .then_with(|| a.person.last.cmp(&b.person.last))
            .then_with(|| a.person.first.cmp(&b.person.first))
    });
    for order in ordering.iter().rev() {
        let key = order.strip_prefix('-').unwrap_or(order);
        if !matches!(key, "id" | "first_name" | "last_name" | "latest_year") {
            continue;
        }
        entries.sort_by(|a, b| {
            let cmp = match key {
                "id" => a.person.id.cmp(&b.person.id),
                "first_name" => a.person.first.cmp(&b.person.first),
                "last_name" => a.person.last.cmp(&b.person.last),
                _ => a.season.cmp(b.season),
            };
            if order.starts_with('-') {
                cmp.reverse()
            } else {
                cmp
            }
        });
    }
    Ok(entries
        .into_iter()
        .map(|entry| {
            let person = entry.person;
            let url = format!("/core/debaters/{}", person.id);
            let regions = person
                .regions
                .iter()
                .map(|region| region_label(region))
                .collect::<Vec<_>>()
                .join(", ");
            let season = year(entry.season).map_or_else(|| entry.season.to_owned(), season_display);
            let last = if person.last.is_empty() {
                vec![("—".to_owned(), String::new())]
            } else {
                vec![(person.last.clone(), url.clone())]
            };
            vec![
                vec![(person.id.to_string(), url.clone())],
                vec![(person.first.clone(), url)],
                last,
                entry
                    .schools
                    .into_iter()
                    .map(|(id, name)| (name, format!("/core/schools/{id}")))
                    .collect(),
                vec![(
                    if regions.is_empty() {
                        "—".to_owned()
                    } else {
                        regions
                    },
                    String::new(),
                )],
                vec![(
                    if season.is_empty() {
                        "—".to_owned()
                    } else {
                        season
                    },
                    String::new(),
                )],
            ]
        })
        .collect())
}
