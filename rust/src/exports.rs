use std::collections::{HashMap, HashSet};

use axum::{
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::Value;
use sqlx::{AnyConnection, AnyPool, Row};
use unicode_normalization::UnicodeNormalization;

use crate::{
    ApiError, AppState, is_django_integer, login, session, team_profile::read_transaction,
};

const LINKED: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, CAST(d.user_id AS BIGINT) AS user_id
    FROM core_debater d JOIN core_debater subject ON subject.id = $1
    WHERE d.id = subject.id OR (subject.alias_group_id IS NOT NULL AND d.alias_group_id = subject.alias_group_id)
";
const ROUND_FIELDS: &str = "
    CAST(r.id AS BIGINT) AS id, CAST(r.gov_id AS BIGINT) AS gov, CAST(r.opp_id AS BIGINT) AS opp,
    r.stage, CAST(r.elim_size AS BIGINT) AS elim_size, r.round_label, CAST(r.round_number AS BIGINT) AS number,
    CAST(r.victor AS BIGINT) AS victor, t.name AS tournament, CAST(t.date AS TEXT) AS date,
    CAST(a.debater_id AS BIGINT) AS a_id, a.source_name AS a_name, CAST(b.debater_id AS BIGINT) AS b_id, b.source_name AS b_name,
    CAST(c.debater_id AS BIGINT) AS c_id, c.source_name AS c_name, CAST(d.debater_id AS BIGINT) AS d_id, d.source_name AS d_name
";
const ROUNDS: &str = "
    WITH linked AS ({linked})
    SELECT {fields}
    FROM core_round r JOIN core_tournament t ON t.id = r.tournament_id
    LEFT JOIN core_importedroundmetadata m ON m.round_id = r.id
    LEFT JOIN core_debateralias a ON a.id = m.gov_1_alias_id
    LEFT JOIN core_debateralias b ON b.id = m.gov_2_alias_id
    LEFT JOIN core_debateralias c ON c.id = m.opp_1_alias_id
    LEFT JOIN core_debateralias d ON d.id = m.opp_2_alias_id
    WHERE EXISTS (SELECT 1 FROM linked l WHERE l.id IN (a.debater_id,b.debater_id,c.debater_id,d.debater_id))
       OR EXISTS (SELECT 1 FROM core_roundstats s JOIN linked l ON l.id = s.debater_id WHERE s.round_id = r.id)
       OR EXISTS (SELECT 1 FROM core_team_debaters m JOIN linked l ON l.id = m.debater_id
            JOIN core_team x ON x.id = m.team_id WHERE NOT x.synthetic AND m.team_id IN (r.gov_id,r.opp_id))
    ORDER BY t.date, r.tournament_id, r.round_number, r.id
";
const STATS: &str = "
    WITH linked AS ({linked})
    SELECT CAST(s.round_id AS BIGINT) AS round_id, s.debater_role,
        CAST(s.speaks AS DOUBLE PRECISION) AS speaks, CAST(s.ranks AS DOUBLE PRECISION) AS ranks,
        CAST(s.metadata AS TEXT) AS metadata
    FROM core_roundstats s JOIN linked l ON l.id = s.debater_id
    ORDER BY s.round_id, s.score_index, s.id
";
const MEMBERS: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name,
        CAST(CASE WHEN d.synthetic THEN 1 ELSE 0 END AS BIGINT) AS synthetic, s.name AS school
    FROM core_team_debaters t JOIN core_debater d ON d.id = t.debater_id
    LEFT JOIN core_school s ON s.id = d.school_id WHERE t.team_id = $1 ORDER BY d.id
";
const HEADERS: &[&str] = &[
    "debater_name",
    "tournament_name",
    "tournament_date",
    "round_label",
    "stage",
    "partner_name",
    "partner_apda_id",
    "side",
    "result",
    "opponent_1_name",
    "opponent_1_apda_id",
    "opponent_2_name",
    "opponent_2_apda_id",
    "opponent_school",
    "speaks",
    "ranks",
];

pub(crate) async fn can_download(
    pool: &AnyPool,
    user: &session::User,
    subject: i64,
) -> Result<bool, sqlx::Error> {
    let Some(viewer) = user.id else {
        return Ok(false);
    };
    if session::has_permission(pool, user, "can_view_debug_tab_cards").await? {
        return Ok(true);
    }
    Ok(sqlx::query(LINKED)
        .bind(subject)
        .fetch_all(pool)
        .await?
        .iter()
        .any(|row| row.try_get::<Option<i64>, _>("user_id").ok().flatten() == Some(viewer)))
}

fn csv_row(output: &mut String, values: impl IntoIterator<Item = impl AsRef<str>>) {
    let mut first = true;
    for value in values {
        if !first {
            output.push(',');
        }
        first = false;
        let value = value.as_ref();
        if value.contains([',', '\r', '\n', '"']) {
            output.push('"');
            for ch in value.chars() {
                if ch == '"' {
                    output.push('"');
                }
                output.push(ch);
            }
            output.push('"');
        } else {
            output.push_str(value);
        }
    }
    output.push_str("\r\n");
}

fn filename(name: &str, id: i64) -> String {
    let ascii = name
        .nfkd()
        .filter(char::is_ascii)
        .collect::<String>()
        .to_ascii_lowercase();
    let filtered: String = ascii
        .chars()
        .filter(|ch| {
            ch.is_ascii_alphanumeric() || ch.is_ascii_whitespace() || matches!(ch, '_' | '-')
        })
        .collect();
    let slug = filtered
        .split(|ch: char| ch.is_ascii_whitespace() || ch == '-')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    let slug = slug.trim_matches(['-', '_']);
    let base = if slug.is_empty() {
        format!("debater-{id}")
    } else {
        slug.to_owned()
    };
    format!("{base}-career-tab-cards.csv")
}

#[derive(Default)]
struct Stats {
    scores: Vec<f64>,
    ranks: Vec<f64>,
    role: String,
    inferred_side: Option<Side>,
    source_names: Vec<String>,
}

fn score(values: &[f64]) -> String {
    if values.is_empty() {
        return "NA".to_owned();
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    let printed = format!("{mean:.4}");
    let trimmed = printed.trim_end_matches('0');
    if trimmed.ends_with('.') {
        format!("{trimmed}0")
    } else {
        trimmed.to_owned()
    }
}

impl Stats {
    fn from_rows(rows: &[sqlx::any::AnyRow]) -> Result<HashMap<i64, Self>, ApiError> {
        let mut stats = HashMap::<_, Self>::new();
        for row in rows {
            let entry = stats.entry(row.try_get("round_id")?).or_default();
            entry
                .scores
                .extend(row.try_get::<Option<f64>, _>("speaks")?);
            entry.ranks.extend(row.try_get::<Option<f64>, _>("ranks")?);
            if entry.role.is_empty() || entry.inferred_side.is_none() {
                let role = row
                    .try_get::<Option<&str>, _>("debater_role")?
                    .unwrap_or_default();
                if entry.inferred_side.is_none() {
                    entry.inferred_side = match role.trim().to_ascii_uppercase().as_str() {
                        "PM" | "MG" => Some(Side::Gov),
                        "LO" | "MO" => Some(Side::Opp),
                        _ => None,
                    };
                }
                if entry.role.is_empty() {
                    entry.role = role.to_owned();
                }
            }
            let metadata: Value = serde_json::from_str(row.try_get("metadata")?)?;
            if let Some(name) = metadata
                .get("speaker_name")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|name| !name.is_empty())
            {
                entry.source_names.push(name.to_owned());
            }
        }
        Ok(stats)
    }

    fn source_name(&self) -> &str {
        let mut candidates = self
            .source_names
            .iter()
            .map(|name| {
                (
                    name,
                    self.source_names
                        .iter()
                        .filter(|other| other == &name)
                        .count(),
                )
            })
            .collect::<Vec<_>>();
        candidates.sort_by(|(a_name, a_count), (b_name, b_count)| {
            b_count
                .cmp(a_count)
                .then_with(|| a_name.to_lowercase().cmp(&b_name.to_lowercase()))
        });
        candidates
            .first()
            .map(|(name, _)| name.as_str())
            .unwrap_or_default()
    }
}

struct Person {
    id: i64,
    name: String,
    school: Option<String>,
    synthetic: bool,
}

impl Person {
    fn public_id(&self) -> String {
        if self.synthetic {
            String::new()
        } else {
            self.id.to_string()
        }
    }
}

#[derive(Clone, Copy)]
enum Side {
    Gov,
    Opp,
}

impl Side {
    fn name(self) -> &'static str {
        match self {
            Self::Gov => "GOV",
            Self::Opp => "OPP",
        }
    }

    fn result(self, victor: i64) -> &'static str {
        match (self, victor) {
            (_, 7) => "BYE",
            (_, 6) => "AW",
            (_, 5) => "AL",
            (Self::Gov, 1) | (Self::Opp, 2) => "W",
            (Self::Gov, 3) | (Self::Opp, 4) => "WF",
            (_, 3 | 4) => "LF",
            _ => "L",
        }
    }
}

async fn roster(
    cx: &mut AnyConnection,
    teams: &mut HashMap<i64, Vec<Person>>,
    id: i64,
) -> Result<(), sqlx::Error> {
    if let std::collections::hash_map::Entry::Vacant(entry) = teams.entry(id) {
        let rows = sqlx::query(MEMBERS).bind(id).fetch_all(cx).await?;
        let result = rows
            .iter()
            .map(|row| {
                Ok(Person {
                    id: row.try_get("id")?,
                    name: row.try_get("name")?,
                    school: row.try_get("school")?,
                    synthetic: row.try_get::<i64, _>("synthetic")? != 0,
                })
            })
            .collect::<Result<Vec<_>, sqlx::Error>>()?;
        entry.insert(result);
    }
    Ok(())
}

fn school_names(people: &[Person]) -> String {
    let mut schools = Vec::new();
    for name in people
        .iter()
        .filter_map(|person| person.school.as_deref())
        .filter(|name| !name.is_empty())
    {
        if !schools.contains(&name) {
            schools.push(name);
        }
    }
    schools.join(" / ")
}

fn side_for_round(
    gov: &[Person],
    opp: &[Person],
    subject: &HashSet<i64>,
    stats: &Stats,
    alias: Option<(Side, &str, &str)>,
) -> Option<Side> {
    if gov.iter().any(|person| subject.contains(&person.id)) {
        return Some(Side::Gov);
    }
    if opp.iter().any(|person| subject.contains(&person.id)) {
        return Some(Side::Opp);
    }
    stats
        .inferred_side
        .or_else(|| alias.map(|(side, _, _)| side))
}

async fn export(pool: &AnyPool, subject: i64, display: &str) -> Result<String, ApiError> {
    let mut tx = read_transaction(pool).await?;
    let ids = sqlx::query(LINKED)
        .bind(subject)
        .fetch_all(&mut *tx)
        .await?
        .iter()
        .map(|row| row.try_get("id"))
        .collect::<Result<HashSet<i64>, sqlx::Error>>()?;
    let stat_query = STATS.replace("{linked}", LINKED);
    let rows = sqlx::query(sqlx::AssertSqlSafe(stat_query))
        .bind(subject)
        .fetch_all(&mut *tx)
        .await?;
    let stats = Stats::from_rows(&rows)?;
    let rounds_query = ROUNDS
        .replace("{linked}", LINKED)
        .replace("{fields}", ROUND_FIELDS);
    let rounds = sqlx::query(sqlx::AssertSqlSafe(rounds_query))
        .bind(subject)
        .fetch_all(&mut *tx)
        .await?;
    let mut teams = HashMap::new();
    let mut output = String::new();
    csv_row(&mut output, HEADERS);
    for row in rounds {
        let id: i64 = row.try_get("id")?;
        let gov_id = row.try_get("gov")?;
        let opp_id = row.try_get("opp")?;
        roster(&mut tx, &mut teams, gov_id).await?;
        roster(&mut tx, &mut teams, opp_id).await?;
        let (gov, opp) = (&teams[&gov_id], &teams[&opp_id]);
        let mut alias = None;
        for (col, side, role) in [
            ("a", Side::Gov, "PM"),
            ("b", Side::Gov, "MG"),
            ("c", Side::Opp, "LO"),
            ("d", Side::Opp, "MO"),
        ] {
            let owner: Option<i64> = row.try_get(format!("{col}_id").as_str())?;
            if owner.is_some_and(|id| ids.contains(&id)) {
                let name = row
                    .try_get::<Option<&str>, _>(format!("{col}_name").as_str())?
                    .unwrap_or_default()
                    .trim();
                alias = Some((side, role, name));
                break;
            }
        }
        let empty = Stats::default();
        let stat = stats.get(&id).unwrap_or(&empty);
        let Some(side) = side_for_round(gov, opp, &ids, stat, alias) else {
            continue;
        };
        let (team, opponent) = match side {
            Side::Gov => (gov, opp),
            Side::Opp => (opp, gov),
        };
        let partner = team.iter().find(|member| !ids.contains(&member.id));
        let stage: &str = row.try_get("stage")?;
        let role = if stage == "outround" {
            ""
        } else if stat.role.is_empty() {
            alias.map(|(_, role, _)| role).unwrap_or_default()
        } else {
            stat.role.as_str()
        };
        let preferred = stat.source_name();
        let name = if preferred.is_empty() {
            alias.map(|(_, _, name)| name).unwrap_or_default()
        } else {
            preferred
        };
        let source_names = team
            .iter()
            .map(|p| p.name.trim())
            .filter(|name| !name.is_empty())
            .collect::<Vec<_>>();
        let source_name = if !name.is_empty() {
            name
        } else {
            let first = source_names.first().copied().unwrap_or(display);
            match role.trim().to_ascii_uppercase().as_str() {
                "MG" | "MO" => source_names.get(1).copied().unwrap_or(first),
                _ => first,
            }
        };
        let number: i64 = row.try_get("number")?;
        let label: &str = row.try_get("round_label")?;
        let round = if label.trim().is_empty() {
            if number == 0 {
                String::new()
            } else {
                number.to_string()
            }
        } else {
            label.trim().to_owned()
        };
        let stage = if stage == "outround" {
            row.try_get::<Option<i64>, _>("elim_size")?
                .filter(|n| *n != 0)
                .map_or_else(
                    || "Outround".to_owned(),
                    |size| format!("Outround ({size})"),
                )
        } else {
            "Prelim".to_owned()
        };
        let names = opponent
            .iter()
            .map(|p| p.name.trim())
            .filter(|name| !name.is_empty())
            .collect::<Vec<_>>();
        csv_row(
            &mut output,
            [
                source_name.to_owned(),
                row.try_get("tournament")?,
                row.try_get("date")?,
                round,
                stage,
                partner.map(|p| p.name.clone()).unwrap_or_default(),
                partner.map(Person::public_id).unwrap_or_default(),
                side.name().to_owned(),
                side.result(row.try_get("victor")?).to_owned(),
                names.first().copied().unwrap_or_default().to_owned(),
                opponent.first().map(Person::public_id).unwrap_or_default(),
                names.get(1).copied().unwrap_or_default().to_owned(),
                opponent.get(1).map(Person::public_id).unwrap_or_default(),
                school_names(opponent),
                score(&stat.scores),
                score(&stat.ranks),
            ],
        );
    }
    tx.commit().await?;
    Ok(output)
}

pub(crate) async fn career_tab_cards(
    State(state): State<AppState>,
    Path(raw): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(id) = raw.parse::<i64>().ok().filter(|_| is_django_integer(&raw)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_authenticated {
        return login::redirect_to_login(&uri);
    }
    let name = match sqlx::query_scalar::<_,String>(
        "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1 AND NOT temporary AND NOT synthetic"
    ).bind(id).fetch_optional(&state.pool).await {
        Ok(Some(name)) => name,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    if !can_download(&state.pool, &user, id).await.unwrap_or(false) {
        return StatusCode::NOT_FOUND.into_response();
    }
    match export(&state.pool, id, &name).await {
        Ok(body) => (
            [
                (header::CONTENT_TYPE, "text/csv".to_owned()),
                (
                    header::CONTENT_DISPOSITION,
                    format!(r#"attachment; filename="{}""#, filename(&name, id)),
                ),
                (header::VARY, "Cookie".to_owned()),
            ],
            body,
        )
            .into_response(),
        Err(error) => {
            eprintln!("Career tab card export failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn partner_breakdown(
    pool: &AnyPool,
    subject: i64,
) -> Result<Option<Value>, ApiError> {
    const ROUNDS: &str = "
        WITH linked AS ({linked}), teams AS (
            SELECT DISTINCT x.id
            FROM core_team x JOIN core_team_debaters m ON m.team_id = x.id
            JOIN linked l ON l.id = m.debater_id WHERE NOT x.synthetic
        )
        SELECT CAST(r.gov_id AS BIGINT) AS gov, CAST(r.opp_id AS BIGINT) AS opp,
            CAST(r.tournament_id AS BIGINT) AS tournament,
            CAST(CASE WHEN EXISTS (
                SELECT 1 FROM core_teamresult h JOIN core_teamresult_hidden_from_debaters v ON v.teamresult_id=h.id
                WHERE h.team_id = r.gov_id AND h.tournament_id = r.tournament_id
            ) THEN 1 ELSE 0 END AS BIGINT) AS gov_hidden,
            CAST(CASE WHEN EXISTS (
                SELECT 1 FROM core_teamresult h JOIN core_teamresult_hidden_from_debaters v ON v.teamresult_id=h.id
                WHERE h.team_id = r.opp_id AND h.tournament_id = r.tournament_id
            ) THEN 1 ELSE 0 END AS BIGINT) AS opp_hidden
        FROM core_round r JOIN core_tournament t ON t.id = r.tournament_id
        WHERE EXISTS (SELECT 1 FROM teams WHERE teams.id IN (r.gov_id,r.opp_id))
        ORDER BY t.date, r.round_number, r.id
    ";
    const COLORS: [&str; 12] = [
        "#2563eb", "#f97316", "#16a34a", "#9333ea", "#db2777", "#0891b2", "#ca8a04", "#4f46e5",
        "#dc2626", "#0d9488", "#7c3aed", "#ea580c",
    ];
    let mut tx = read_transaction(pool).await?;
    let ids = sqlx::query(LINKED)
        .bind(subject)
        .fetch_all(&mut *tx)
        .await?
        .iter()
        .map(|row| row.try_get("id"))
        .collect::<Result<HashSet<i64>, sqlx::Error>>()?;
    let rows = sqlx::query(sqlx::AssertSqlSafe(ROUNDS.replace("{linked}", LINKED)))
        .bind(subject)
        .fetch_all(&mut *tx)
        .await?;
    let mut teams = HashMap::new();
    let mut counts = HashMap::<String, i64>::new();
    let mut total = 0;
    for row in rows {
        let gov_id = row.try_get("gov")?;
        let opp_id = row.try_get("opp")?;
        roster(&mut tx, &mut teams, gov_id).await?;
        roster(&mut tx, &mut teams, opp_id).await?;
        let gov = &teams[&gov_id];
        let opp = &teams[&opp_id];
        let (side, hidden) = if gov.iter().any(|person| ids.contains(&person.id)) {
            (gov, row.try_get::<i64, _>("gov_hidden")? != 0)
        } else if opp.iter().any(|person| ids.contains(&person.id)) {
            (opp, row.try_get::<i64, _>("opp_hidden")? != 0)
        } else {
            continue;
        };
        if !hidden
            && let Some(partner) = side
                .iter()
                .find(|person| !person.synthetic && !ids.contains(&person.id))
        {
            *counts.entry(partner.name.clone()).or_default() += 1;
            total += 1;
        }
    }
    tx.commit().await?;
    if total == 0 {
        return Ok(None);
    }
    let mut counts: Vec<_> = counts.into_iter().collect();
    counts.sort_by(|(name_a, count_a), (name_b, count_b)| {
        count_b
            .cmp(count_a)
            .then_with(|| name_a.to_lowercase().cmp(&name_b.to_lowercase()))
    });
    let mut segments = Vec::new();
    let mut previous = 0.0;
    let mut partners = Vec::new();
    for (index, (name, rounds)) in counts.into_iter().enumerate() {
        let share = 100.0 * rounds as f64 / total as f64;
        let color = COLORS[index % COLORS.len()];
        let next = previous + share;
        segments.push(format!("{color} {previous:.3}% {next:.3}%"));
        previous = next;
        partners
            .push(serde_json::json!({"name":name,"rounds":rounds,"share_pct":share,"color":color}));
    }
    Ok(Some(
        serde_json::json!({"total_partner_rounds":total,"rows":partners,
        "pie_style":format!("conic-gradient({})",segments.join(", "))}),
    ))
}
