use std::collections::{BTreeMap, HashMap, HashSet};

use axum::{
    Extension, Json,
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState, api_response, is_django_integer, json_error, options,
    routing::limit,
    schedule::{TOURNAMENT_COLUMNS, Tournament, proxy_url, season_display},
    schools::{DEBATER_COLUMNS, Debater},
    session::{self, User},
    standings::{lite_school, place_display},
    team_profile::{
        Card, SCHOOL_COLUMNS, Team, cards, members, members_for, read_transaction, school,
    },
    tournament_profile::{tournament, videos},
};

const PERSON: &str = "
    SELECT {debater}, {school}, d.first_season, d.latest_season,
           CAST(d.alias_group_id AS BIGINT) AS alias_group_id, d.paradigm,
           CAST(CASE WHEN d.dino_to_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS outreach,
           CAST(CASE WHEN d.dino_judge_contact_opt_in THEN 1 ELSE 0 END AS BIGINT) AS judge
    FROM core_debater d LEFT JOIN core_school s ON s.id = d.school_id
    WHERE d.id = $1 AND NOT d.synthetic AND NOT d.temporary
";
const ALIASES: &str = "
    SELECT {debater}, {school} FROM core_debater d LEFT JOIN core_school s ON s.id = d.school_id
    WHERE d.alias_group_id = $1 AND d.id <> $2 AND NOT d.synthetic AND NOT d.temporary
    ORDER BY s.name, d.first_name, d.last_name
";
const TEAM_RESULTS: &str = "
    SELECT CAST(r.id AS BIGINT) AS id, CAST(r.tournament_id AS BIGINT) AS tournament_id,
           CAST(r.team_id AS BIGINT) AS team_id, CAST(r.place AS BIGINT) AS place,
           CAST(r.type_of_place AS BIGINT) AS division, t.name AS team_name,
           CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
           CAST(CASE WHEN EXISTS (SELECT 1 FROM core_teamresult_hidden_from_debaters h WHERE h.teamresult_id = r.id)
                     THEN 1 ELSE 0 END AS BIGINT) AS redacted,
           CAST(CASE WHEN EXISTS (SELECT 1 FROM core_teamresult_hidden_from_debaters h WHERE h.teamresult_id = r.id
                                  AND h.debater_id = $1) THEN 1 ELSE 0 END AS BIGINT) AS hidden
    FROM core_teamresult r JOIN core_team t ON t.id = r.team_id
    JOIN core_team_debaters td ON td.team_id = t.id
    WHERE td.debater_id = $1 AND NOT t.synthetic
";
const SPEAKER_RESULTS: &str = "
    SELECT CAST(r.id AS BIGINT) AS id, CAST(r.tournament_id AS BIGINT) AS tournament_id,
           CAST(r.place AS BIGINT) AS place, CAST(r.type_of_place AS BIGINT) AS division,
           CAST(CASE WHEN r.tie THEN 1 ELSE 0 END AS BIGINT) AS tie,
           CAST(CASE WHEN r.hidden_from_profile THEN 1 ELSE 0 END AS BIGINT) AS hidden
    FROM core_speakerresult r WHERE r.debater_id = $1
";
const ROUNDS: &str = "
    WITH eligible AS (
        SELECT t.id FROM core_team_debaters m JOIN core_team t ON t.id = m.team_id
        WHERE m.debater_id = $1 AND NOT t.synthetic
    ), round_ids AS (
        SELECT r.id FROM eligible e JOIN core_round r ON r.gov_id = e.id
        UNION
        SELECT r.id FROM eligible e JOIN core_round r ON r.opp_id = e.id
    )
    SELECT CAST(r.tournament_id AS BIGINT) AS tournament_id,
           CAST(r.gov_id AS BIGINT) AS gov_id, CAST(r.opp_id AS BIGINT) AS opp_id,
           CAST(CASE WHEN r.gov_id IN (SELECT id FROM eligible) THEN 1 ELSE 0 END AS BIGINT) AS is_gov,
           g.name AS gov_name, o.name AS opp_name
    FROM round_ids
    JOIN core_round r ON r.id = round_ids.id
    JOIN core_team g ON g.id = r.gov_id JOIN core_team o ON o.id = r.opp_id
    ORDER BY r.round_number, r.id
";
const TEAMS: &str = "
    SELECT CAST(t.id AS BIGINT) AS id, t.name,
           CAST((SELECT COUNT(DISTINCT r.tournament_id) FROM core_teamresult r WHERE r.team_id = t.id) AS BIGINT)
               AS tournament_count,
           CAST((SELECT SUM(b.points) FROM core_toty b WHERE b.team_id = t.id) AS DOUBLE PRECISION) AS points
    FROM core_team t JOIN core_team_debaters td ON td.team_id = t.id
    WHERE NOT t.synthetic AND td.debater_id = $1
    ORDER BY tournament_count DESC, COALESCE((SELECT SUM(b.points) FROM core_toty b WHERE b.team_id = t.id), 0) DESC, {tie_breaker}
";
const MARKER_NAMES: [&str; 6] = ["one", "two", "three", "four", "five", "six"];

fn person_payload(row: &AnyRow, origin: &str) -> Result<Value, ApiError> {
    let school = school(row, origin)?;
    Ok(serde_json::to_value(Debater::from_row(
        row,
        origin,
        school.as_ref(),
    )?)?)
}

#[derive(Default)]
struct SeasonEvent {
    team_candidates: Vec<TeamAward>,
    speaker_awards: Vec<SpeakerAward>,
    round_affiliation: Option<(i64, String)>,
}

struct TeamAward {
    id: i64,
    team_id: i64,
    team_name: String,
    place: i64,
    division: i64,
    ghost: bool,
}

struct SpeakerAward {
    id: i64,
    place: i64,
    division: i64,
    tied: bool,
}

fn division_label(code: i64) -> Value {
    match code {
        0 => json!("Novice"),
        1 => json!("Varsity"),
        _ => json!(code),
    }
}

struct ProfileEvents {
    events: BTreeMap<i64, SeasonEvent>,
    redacted_pairs: HashSet<(i64, i64)>,
    hidden_speaker_tournaments: HashMap<i64, HashSet<i64>>,
}

async fn events(conn: &mut AnyConnection, id: i64) -> Result<ProfileEvents, ApiError> {
    let team_awards = sqlx::query(TEAM_RESULTS)
        .bind(id)
        .fetch_all(&mut *conn)
        .await?;
    let speaker_awards = sqlx::query(SPEAKER_RESULTS)
        .bind(id)
        .fetch_all(&mut *conn)
        .await?;
    let rounds = sqlx::query(ROUNDS).bind(id).fetch_all(conn).await?;
    let mut buckets: BTreeMap<i64, SeasonEvent> = BTreeMap::new();
    let mut redacted_pairs = HashSet::new();
    let mut hidden_speaker_tournaments: HashMap<i64, HashSet<i64>> = HashMap::new();
    for result in team_awards {
        let tournament = result.try_get::<i64, _>("tournament_id")?;
        let team = result.try_get::<i64, _>("team_id")?;
        if result.try_get::<i64, _>("redacted")? != 0 {
            redacted_pairs.insert((team, tournament));
        }
        if result.try_get::<i64, _>("hidden")? != 0 {
            continue;
        }
        buckets
            .entry(tournament)
            .or_default()
            .team_candidates
            .push(TeamAward {
                id: result.try_get("id")?,
                team_id: team,
                team_name: result.try_get("team_name")?,
                place: result.try_get("place")?,
                division: result.try_get("division")?,
                ghost: result.try_get::<i64, _>("ghost")? != 0,
            });
    }
    for result in speaker_awards {
        let tournament = result.try_get::<i64, _>("tournament_id")?;
        let division = result.try_get::<i64, _>("division")?;
        if result.try_get::<i64, _>("hidden")? != 0 {
            hidden_speaker_tournaments
                .entry(division)
                .or_default()
                .insert(tournament);
            continue;
        }
        buckets
            .entry(tournament)
            .or_default()
            .speaker_awards
            .push(SpeakerAward {
                id: result.try_get("id")?,
                place: result.try_get("place")?,
                division,
                tied: result.try_get::<i64, _>("tie")? != 0,
            });
    }
    for row in rounds {
        let tournament = row.try_get::<i64, _>("tournament_id")?;
        let gov = row.try_get::<i64, _>("gov_id")?;
        let opp = row.try_get::<i64, _>("opp_id")?;
        if redacted_pairs.contains(&(gov, tournament))
            || redacted_pairs.contains(&(opp, tournament))
        {
            continue;
        }
        let is_gov = row.try_get::<i64, _>("is_gov")? != 0;
        let name = row.try_get(if is_gov { "gov_name" } else { "opp_name" })?;
        buckets
            .entry(tournament)
            .or_default()
            .round_affiliation
            .get_or_insert((if is_gov { gov } else { opp }, name));
    }
    Ok(ProfileEvents {
        events: buckets,
        redacted_pairs,
        hidden_speaker_tournaments,
    })
}

struct Teams {
    roster: HashMap<i64, Vec<Value>>,
}

impl Teams {
    async fn new(conn: &mut AnyConnection, ids: &[i64], origin: &str) -> Result<Self, ApiError> {
        Ok(Self {
            roster: members_for(conn, ids, origin).await?,
        })
    }

    async fn load(
        &mut self,
        conn: &mut AnyConnection,
        id: i64,
        origin: &str,
    ) -> Result<&[Value], ApiError> {
        let rows = match self.roster.entry(id) {
            std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
            std::collections::hash_map::Entry::Vacant(entry) => {
                entry.insert(members(conn, id, origin).await?)
            }
        };
        Ok(rows)
    }

    fn serialize(id: i64, name: &str, people: &[Value], origin: &str) -> Result<Value, ApiError> {
        let hybrid = people
            .iter()
            .map(|person| person["school_id"].as_i64())
            .collect::<HashSet<_>>()
            .len()
            == 2;
        Ok(serde_json::to_value(Team::new(
            id, name, hybrid, people, origin,
        ))?)
    }

    fn lite(id: i64, name: &str, people: &[Value], origin: &str) -> Value {
        let people = people
            .iter()
            .map(|person| {
                json!({
                    "id": person["id"], "name": person["name"], "school_id": person["school_id"],
                    "school_name": person["school_name"], "url": person["url"],
                })
            })
            .collect::<Vec<_>>();
        json!({"id": id, "name": name, "debaters": people, "url": format!("{origin}/core/teams/{id}")})
    }
}

fn format_season(value: &str) -> String {
    value
        .parse()
        .map_or_else(|_| value.to_owned(), season_display)
}

async fn tournaments_for(
    conn: &mut AnyConnection,
    ids: &[i64],
    origin: &str,
) -> Result<HashMap<i64, (Tournament, i64)>, ApiError> {
    let mut tournaments = HashMap::new();
    for chunk in ids.chunks(200) {
        let ids = (1..=chunk.len())
            .map(|index| format!("${index}"))
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT {TOURNAMENT_COLUMNS}, CAST(t.num_rounds AS BIGINT) AS num_rounds
            FROM core_tournament t LEFT JOIN core_school s ON s.id=t.host_id WHERE t.id IN ({ids})"
        );
        let mut statement = sqlx::query(AssertSqlSafe(sql));
        for id in chunk {
            statement = statement.bind(id);
        }
        for row in statement.fetch_all(&mut *conn).await? {
            let tournament = Tournament::from_row(&row, origin)?;
            tournaments.insert(tournament.id, (tournament, row.try_get("num_rounds")?));
        }
    }
    Ok(tournaments)
}

async fn summaries(
    conn: &mut AnyConnection,
    profile: &ProfileEvents,
    debater: &Value,
    origin: &str,
    teams: &mut Teams,
) -> Result<Vec<Value>, ApiError> {
    let mut seasons = BTreeMap::<String, Vec<(String, Value)>>::new();
    let tournament_ids = profile.events.keys().copied().collect::<Vec<_>>();
    let tournaments = tournaments_for(conn, &tournament_ids, origin).await?;
    let mut team_cards = HashMap::new();
    for (&id, event) in &profile.events {
        let (tournament, num_rounds) = tournaments
            .get(&id)
            .ok_or(ApiError::NotFound("Tournament not found"))?;
        let affiliation = event
            .team_candidates
            .first()
            .map(|result| (result.team_id, result.team_name.as_str()))
            .or_else(|| {
                event
                    .round_affiliation
                    .as_ref()
                    .map(|(id, name)| (*id, name.as_str()))
            })
            .filter(|(team, _)| !profile.redacted_pairs.contains(&(*team, id)));
        let (team, card) = if let Some((team, name)) = affiliation {
            let people = teams.load(conn, team, origin).await?;
            let team_json = Teams::serialize(team, name, people, origin)?;
            let cards = match team_cards.entry(team) {
                std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                std::collections::hash_map::Entry::Vacant(entry) => {
                    entry.insert(cards(&mut *conn, team, people, origin, None).await?)
                }
            };
            (team_json, cards.remove(&id))
        } else {
            (Value::Null, None)
        };
        let mut team_results: Vec<_> = event.team_candidates.iter().collect();
        team_results.sort_by_key(|result| (-result.division, result.place));
        let mut team_awards = Vec::new();
        for award in team_results {
            let team = if profile.redacted_pairs.contains(&(award.team_id, id)) {
                Value::Null
            } else {
                let people = teams.load(conn, award.team_id, origin).await?;
                Teams::serialize(award.team_id, &award.team_name, people, origin)?
            };
            team_awards.push(json!({
                "id": award.id, "place": award.place,
                "place_display": if award.place > 0 {place_display(award.place)} else {String::new()},
                "type": division_label(award.division), "type_code": award.division,
                "ghost_points": award.ghost, "team": team, "tournament": tournament,
            }));
        }
        let mut speaker_results: Vec<_> = event.speaker_awards.iter().collect();
        speaker_results.sort_by_key(|result| (-result.division, result.place));
        let speaker_awards = speaker_results
            .into_iter()
            .map(|award| {
                json!({
                    "id": award.id, "place": award.place, "tie": award.tied,
                    "type": division_label(award.division), "type_code": award.division,
                    "debater": debater, "tournament": tournament,
                })
            })
            .collect::<Vec<_>>();
        let (record, tab_card) = if let Some(Card { wins, rounds }) = card {
            (format!("{wins} - {}", num_rounds - wins), rounds)
        } else {
            (String::new(), Vec::new())
        };
        seasons.entry(tournament.season.clone()).or_default().push((
            tournament.date.clone(),
            json!({
                "tournament": tournament, "team": team, "team_results": team_awards,
                "speaker_results": speaker_awards, "record": record, "tab_card": tab_card,
            }),
        ));
    }
    Ok(seasons
        .into_iter()
        .rev()
        .map(|(season, mut results)| {
            results.sort_by(|a, b| a.0.cmp(&b.0));
            json!({"season": season, "season_display": format_season(&season),
               "tournaments": results.into_iter().map(|(_, value)| value).collect::<Vec<_>>()})
        })
        .collect())
}

fn lite_person(person: &Value) -> Value {
    json!({"id": person["id"], "name": person["name"], "school_id": person["school_id"],
           "school_name": person["school_name"], "url": person["url"]})
}

enum History {
    Team,
    Speaker {
        table: &'static str,
        division: i64,
        markers: usize,
    },
}

struct HistoryScope<'a> {
    debater_id: i64,
    debater: &'a Value,
    redacted_teams: &'a HashSet<i64>,
    events: &'a ProfileEvents,
    origin: &'a str,
}

async fn history(
    conn: &mut AnyConnection,
    board: History,
    scope: &HistoryScope<'_>,
    teams: &mut Teams,
) -> Result<Vec<Value>, ApiError> {
    let (table, cols, relation, slots) = match board {
        History::Team => (
            "core_toty",
            "CAST(t.id AS BIGINT) AS team_id, t.name AS team_name,",
            "JOIN core_team t ON t.id = b.team_id JOIN core_team_debaters td ON td.team_id = t.id AND td.debater_id = $1",
            5,
        ),
        History::Speaker { table, markers, .. } => (table, "", "", markers),
    };
    let restriction = if matches!(board, History::Team) {
        ""
    } else {
        "WHERE b.debater_id = $1"
    };
    let mut joins = String::new();
    let mut columns = String::new();
    for marker in &MARKER_NAMES[..slots] {
        columns.push_str(&format!(
            ", CAST(b.marker_{marker} AS DOUBLE PRECISION) AS points_{marker},
            CAST(m_{marker}.id AS BIGINT) AS id_{marker}, m_{marker}.name AS name_{marker},
            CAST(m_{marker}.date AS TEXT) AS date_{marker}"
        ));
        joins.push_str(&format!(
            " LEFT JOIN core_tournament m_{marker} ON m_{marker}.id = b.tournament_{marker}_id"
        ));
    }
    let query = format!(
        "SELECT {cols} CAST(b.id AS BIGINT) AS id, b.season, CAST(b.place AS BIGINT) AS place,
        CAST(b.points AS DOUBLE PRECISION) AS points,
        CAST(CASE WHEN b.tied THEN 1 ELSE 0 END AS BIGINT) AS tied {columns}
        FROM {table} b {relation} {joins} {restriction} ORDER BY b.place, b.season"
    );
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(scope.debater_id)
        .fetch_all(&mut *conn)
        .await?;
    let mut history = Vec::new();
    for row in rows {
        let entity = match board {
            History::Team => {
                let id = row.try_get::<i64, _>("team_id")?;
                if scope.redacted_teams.contains(&id) {
                    continue;
                }
                let people = teams.load(conn, id, scope.origin).await?;
                json!({"team": Teams::lite(id, row.try_get("team_name")?, people, scope.origin)})
            }
            History::Speaker { .. } => {
                let school = match scope.debater["school_id"].as_i64() {
                    Some(id) => lite_school(
                        id,
                        scope.debater["school_name"].as_str().unwrap_or_default(),
                        scope.origin,
                    ),
                    None => Value::Null,
                };
                json!({"debater": lite_person(scope.debater), "school": school})
            }
        };
        let mut markers = Vec::new();
        for (index, marker) in MARKER_NAMES[..slots].iter().enumerate() {
            let points = row.try_get::<f64, _>(format!("points_{marker}").as_str())?;
            let id = row.try_get::<Option<i64>, _>(format!("id_{marker}").as_str())?;
            let Some(id) = id.filter(|_| points != 0.0) else {
                continue;
            };
            if let History::Speaker { division, .. } = board
                && scope
                    .events
                    .hidden_speaker_tournaments
                    .get(&division)
                    .is_some_and(|hidden| hidden.contains(&id))
            {
                continue;
            }
            let name = row.try_get::<&str, _>(format!("name_{marker}").as_str())?;
            markers.push(json!({
                "slot": index+1, "points": points,
                "tournament": {"id": id, "name": name, "display": name.split(" (").next().unwrap_or_default(),
                    "date": row.try_get::<Option<&str>, _>(format!("date_{marker}").as_str())?,
                    "url": format!("{}/core/tournaments/{id}", scope.origin)}
            }));
        }
        let season = row.try_get::<&str, _>("season")?;
        let place = row.try_get::<i64, _>("place")?;
        let mut payload = json!({
            "id": row.try_get::<i64, _>("id")?, "season": season, "season_display": format_season(season),
            "place": place, "place_display": place_display(place),
            "points": row.try_get::<f64, _>("points")?, "tied": row.try_get::<i64, _>("tied")? != 0,
            "markers": markers,
        });
        if let (Some(target), Value::Object(entity)) = (payload.as_object_mut(), entity) {
            target.extend(entity);
        }
        history.push(payload);
    }
    Ok(history)
}

async fn load(
    state: &AppState,
    id: i64,
    user: &User,
    origin: &str,
    max: Option<i64>,
    path: &str,
) -> Result<Value, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let query = PERSON
        .replace("{debater}", DEBATER_COLUMNS)
        .replace("{school}", SCHOOL_COLUMNS);
    let data = sqlx::query(AssertSqlSafe(query))
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(ApiError::NotFound("Debater not found"))?;
    let debater = person_payload(&data, origin)?;
    let first_season = data.try_get::<Option<&str>, _>("first_season")?;
    let latest_season = data.try_get::<Option<&str>, _>("latest_season")?;
    let is_dino = debater["status_code"].as_i64() == Some(2);
    let paradigm = data
        .try_get::<Option<&str>, _>("paradigm")?
        .filter(|url| !url.is_empty() && user.can_view_private_videos);
    let mut contacts = json!({
        "paradigm_url": paradigm,
        "to_outreach": data.try_get::<i64, _>("outreach")? != 0,
        "judge_outreach": is_dino && data.try_get::<i64, _>("judge")? != 0,
    });
    if let Some(regions) = debater["regions"]
        .as_array()
        .filter(|regions| !regions.is_empty())
    {
        contacts["regions"] = json!(
            regions
                .iter()
                .map(|region| {
                    let code = region.as_str().unwrap_or_default();
                    json!({"value": code, "label": crate::schools::region_label(code)})
                })
                .collect::<Vec<_>>()
        );
    }
    let aliases = if let Some(alias) = data.try_get::<Option<i64>, _>("alias_group_id")? {
        let query = ALIASES
            .replace("{debater}", DEBATER_COLUMNS)
            .replace("{school}", SCHOOL_COLUMNS);
        sqlx::query(AssertSqlSafe(query))
            .bind(alias)
            .bind(id)
            .fetch_all(&mut *tx)
            .await?
            .iter()
            .map(|row| person_payload(row, origin))
            .collect::<Result<Vec<_>, _>>()?
    } else {
        Vec::new()
    };
    let events = events(&mut tx, id).await?;
    let tie_breaker = if origin.is_empty() { "td.id" } else { "t.id" };
    let sql = TEAMS.replace("{tie_breaker}", tie_breaker);
    let participation = sqlx::query(AssertSqlSafe(sql))
        .bind(id)
        .fetch_all(&mut *tx)
        .await?;
    let teams_ids = participation
        .iter()
        .map(|row| row.try_get("id"))
        .collect::<Result<Vec<i64>, _>>()?;
    let mut teams = Teams::new(&mut tx, &teams_ids, origin).await?;
    let mut season_summaries = summaries(&mut tx, &events, &debater, origin, &mut teams).await?;
    let redacted_teams = events
        .redacted_pairs
        .iter()
        .map(|(team, _)| *team)
        .collect::<HashSet<_>>();
    let scope = HistoryScope {
        debater_id: id,
        debater: &debater,
        redacted_teams: &redacted_teams,
        events: &events,
        origin,
    };
    let mut toty = history(&mut tx, History::Team, &scope, &mut teams).await?;
    let mut soty = history(
        &mut tx,
        History::Speaker {
            table: "core_soty",
            division: 1,
            markers: 6,
        },
        &scope,
        &mut teams,
    )
    .await?;
    let mut noty = history(
        &mut tx,
        History::Speaker {
            table: "core_noty",
            division: 0,
            markers: 5,
        },
        &scope,
        &mut teams,
    )
    .await?;
    let mut teams_payload = Vec::new();
    for row in participation {
        let team = row.try_get::<i64, _>("id")?;
        if redacted_teams.contains(&team) {
            continue;
        }
        let people = teams.load(&mut tx, team, origin).await?;
        teams_payload.push(json!({
            "team": Teams::serialize(team, row.try_get("name")?, people, origin)?,
            "tournament_count": row.try_get::<i64, _>("tournament_count")?,
            "toty_points": row.try_get::<Option<f64>, _>("points")?.map_or_else(|| json!(0), |points| json!(points)),
        }));
    }
    let tournaments = sqlx::query_scalar::<_, i64>(
        "
        SELECT DISTINCT CAST(tournament_id AS BIGINT) AS id FROM core_video
        WHERE $1 IN (pm_id, mg_id, lo_id, mo_id)
        ORDER BY id
    ",
    )
    .bind(id)
    .fetch_all(&mut *tx)
    .await?;
    let mut video_rows = Vec::new();
    for tournament_id in tournaments {
        let tournament = tournament(&mut tx, tournament_id, origin).await?;
        video_rows.extend(videos(&mut tx, &tournament, origin, user, Some(id)).await?);
    }
    video_rows.sort_by_key(|video| video["id"].as_i64());
    tx.commit().await?;
    if let Some(max) = max {
        for results in [
            &mut toty,
            &mut soty,
            &mut noty,
            &mut teams_payload,
            &mut season_summaries,
        ] {
            results.truncate(max as usize);
        }
    }
    Ok(json!({
        "debater": debater, "first_season": first_season, "latest_season": latest_season,
        "is_dino": is_dino, "contact_preferences": contacts, "also_debated_under": aliases,
        "teams": teams_payload, "standings": {"toty": toty, "soty": soty, "noty": noty},
        "season_summaries": season_summaries, "videos": video_rows,
        "links": {"self": proxy_url(origin, path)},
    }))
}

pub(crate) async fn detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    Extension(user): Extension<User>,
) -> Response {
    if !is_django_integer(&id) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Ok(id) = id.parse() else {
        return json_error(StatusCode::NOT_FOUND, "Debater not found");
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let max = limit(uri.query().unwrap_or_default());
    let path = uri
        .path_and_query()
        .map(|path| path.as_str())
        .unwrap_or_default();
    match load(&state, id, &user, &origin, max, path).await {
        Ok(result) => api_response(Json(result)),
        Err(ApiError::NotFound(detail)) => json_error(StatusCode::NOT_FOUND, detail),
        Err(_) => json_error(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error."),
    }
}

pub(crate) async fn detail_options(Path(id): Path<String>) -> Response {
    if !is_django_integer(&id) {
        return StatusCode::NOT_FOUND.into_response();
    }
    options(
        "Debater Detail Api",
        "Return a debater's public profile, standings history, and tournament results.",
    )
}

fn partner(team: &Value, id: i64) -> Value {
    team["debaters"]
        .as_array()
        .and_then(|people| {
            people
                .iter()
                .find(|person| person["id"].as_i64() != Some(id))
        })
        .cloned()
        .unwrap_or_default()
}

pub(crate) async fn page(
    State(state): State<AppState>,
    Path(raw_id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(id) = raw_id
        .parse::<i64>()
        .ok()
        .filter(|_| is_django_integer(&raw_id))
    else {
        return StatusCode::NOT_FOUND.into_response();
    };
    render_page(&state, id, &uri, &headers, None).await
}

pub(crate) async fn render_page(
    state: &AppState,
    id: i64,
    uri: &axum::http::Uri,
    headers: &HeaderMap,
    editing: Option<minijinja::Value>,
) -> Response {
    let Ok(user) = session::user(&state.pool, headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let data = match load(state, id, &user, "", None, uri.path()).await {
        Ok(data) => data,
        Err(ApiError::NotFound(_)) => return StatusCode::NOT_FOUND.into_response(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let mut object = data["debater"].clone();
    for field in ["first_season", "latest_season"] {
        object[field] = json!(data[field].as_str().map(format_season).unwrap_or_default());
    }
    object["is_dino"] = data["is_dino"].clone();
    let contacts = &data["contact_preferences"];
    object["paradigm"] = contacts["paradigm_url"].clone();
    object["dino_to_contact_opt_in"] = contacts["to_outreach"].clone();
    object["dino_judge_contact_opt_in"] = contacts["judge_outreach"].clone();
    let query: HashMap<_, _> =
        url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes()).collect();
    let all = query.contains_key("all");
    let requested_season = query
        .get("season")
        .map(|value| value.as_ref())
        .filter(|value| !value.is_empty());
    let mut season_choices = Vec::new();
    let mut eligible = Vec::new();
    for season in data["season_summaries"].as_array().into_iter().flatten() {
        let year = season["season"].as_str().unwrap_or_default();
        let tournaments: Vec<_> = season["tournaments"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|event| {
                all || ["team_results", "speaker_results"].iter().any(|key| {
                    event[*key]
                        .as_array()
                        .is_some_and(|awards| !awards.is_empty())
                })
            })
            .collect();
        if !tournaments.is_empty() {
            eligible.push((year, tournaments));
            if year
                .parse::<i32>()
                .is_ok_and(|year| (2004..=state.current_season).contains(&year))
            {
                season_choices.push((year, season["season_display"].as_str().unwrap_or_default()));
            }
        }
    }
    let default_season = state.current_season.to_string();
    let current_season = eligible
        .first()
        .map(|(year, _)| requested_season.unwrap_or(year))
        .unwrap_or(&default_season);
    let mut results = Vec::new();
    for (_, events) in eligible.iter().filter(|(year, _)| *year == current_season) {
        for event in events {
            let mut awards = Vec::new();
            for (kind, field) in [("team", "team_results"), ("speaker", "speaker_results")] {
                awards.extend(
                    event[field]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .map(|award| json!([kind, award])),
                );
            }
            results.push(json!({
                "id": event["tournament"]["id"],
                "tournament": event["tournament"],
                "team": event["team"],
                "partner": partner(&event["team"], id),
                "data": awards,
                "tab_card": state.debater_tab_cards.then_some(&event["tab_card"]),
            }));
        }
    }
    let teams: Vec<_> = data["teams"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|team| {
            let mut entry = team["team"].clone();
            entry["partner"] = partner(&entry, id);
            entry
        })
        .collect();
    let mut videos = Vec::new();
    let profile_videos = data["videos"].as_array();
    for role in ["pm", "lo", "mg", "mo"] {
        for video in profile_videos
            .into_iter()
            .flatten()
            .filter(|video| video["participants"][role]["id"].as_i64() == Some(id))
        {
            let mut item = video.clone();
            for participant in ["pm", "lo", "mg", "mo"] {
                item[participant] = video["participants"][participant].clone();
            }
            item["tags"] = json!(
                video["tags"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|name| json!({"name": name}))
                    .collect::<Vec<_>>()
            );
            videos.push(item);
        }
    }
    let owner = if let Some(user_id) = user.id {
        sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_debater WHERE id = $1 AND user_id = $2",
        )
        .bind(id)
        .bind(user_id)
        .fetch_optional(&state.pool)
        .await
        .ok()
        .flatten()
        .is_some()
    } else {
        false
    };
    let mut buttons = Vec::new();
    for (permission, name, suffix, class) in [
        ("remove_debater", "Delete", "delete", "btn-danger"),
        ("change_debater", "Edit", "edit", "btn-info"),
    ] {
        if session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false)
        {
            buttons.push(
                json!({"name":name, "class":class, "href":format!("/core/debaters/{id}/{suffix}")}),
            );
        }
    }
    let partner_breakdown = if state.debater_partner_chart {
        crate::exports::partner_breakdown(&state.pool, id)
            .await
            .unwrap_or_default()
    } else {
        None
    };
    let manage_team = user.is_staff
        && session::has_permission(&state.pool, &user, "change_teamresult")
            .await
            .unwrap_or(false);
    let manage_speaker = user.is_staff
        && session::has_permission(&state.pool, &user, "change_speakerresult")
            .await
            .unwrap_or(false);
    let common = crate::web::base_context(state, &user, uri);
    let values = minijinja::context! {
        object, buttons, results, teams, videos, can_edit_profile => owner || user.is_superuser,
        partner_breakdown, perms => json!({"core":{"change_teamresult":manage_team, "change_speakerresult":manage_speaker}}),
        can_download_tab_cards_csv => crate::exports::can_download(&state.pool,&user,id).await.unwrap_or(false),
        seasons => season_choices, current_season,
        also_debated_under => data["also_debated_under"].clone(),
        totys => data["standings"]["toty"].clone(),
        sotys => data["standings"]["soty"].clone(),
        notys => data["standings"]["noty"].clone(),
    };
    let template = if editing.is_some() {
        "debaters/update.html"
    } else {
        "debaters/detail.html"
    };
    let bindings = match editing {
        Some(form) => minijinja::context! {..form,..values,..common},
        None => minijinja::context! {..values,..common},
    };
    match crate::web::render(template, bindings) {
        Ok(html) => (
            [(axum::http::header::VARY, "Cookie")],
            axum::response::Html(html),
        )
            .into_response(),
        Err(error) => {
            eprintln!("Debater profile template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
