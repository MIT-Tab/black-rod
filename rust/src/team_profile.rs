pub(crate) mod edit;

use std::collections::{HashMap, HashSet};

use axum::{
    Json,
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{AnyConnection, AssertSqlSafe, Row, any::AnyRow};

use crate::{
    ApiError, AppState, api_response, is_django_integer, json_error, options,
    schedule::{TOURNAMENT_COLUMNS, Tournament, proxy_url, season_display},
    school_profile::limit,
    schools::{DEBATER_COLUMNS, Debater, School},
    standings::place_display,
};

pub(crate) const SCHOOL_COLUMNS: &str = "CAST(s.id AS BIGINT) AS school_id, s.name AS school_name,
    CAST(CASE WHEN s.included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included";
const TEAM: &str = "SELECT name FROM core_team WHERE id = $1";
const MEMBERS: &str = "
    SELECT {columns}, {school}, CAST(td.team_id AS BIGINT) AS member_team_id
    FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id
    LEFT JOIN core_school s ON s.id = d.school_id
    WHERE td.team_id IN ({teams}) AND NOT d.synthetic AND NOT d.temporary
    ORDER BY td.team_id, d.id
";
const ROUNDS: &str = "
    SELECT CAST(r.id AS BIGINT) AS id, CAST(r.round_number AS BIGINT) AS round_number,
           CAST(r.tournament_id AS BIGINT) AS tournament_id,
           CAST(r.victor AS BIGINT) AS victor, CAST(r.gov_id AS BIGINT) AS gov_id,
           CAST(r.opp_id AS BIGINT) AS opp_id, o.name AS opponent_name,
           CAST((SELECT COUNT(DISTINCT COALESCE(d.school_id, -1))
                 FROM core_team_debaters td JOIN core_debater d ON d.id = td.debater_id
                 WHERE td.team_id = o.id AND NOT d.synthetic AND NOT d.temporary) AS BIGINT) AS affiliations
    FROM core_round r
    JOIN core_team o ON o.id = CASE WHEN r.gov_id = $1 THEN r.opp_id ELSE r.gov_id END
    WHERE (r.gov_id = $1 OR r.opp_id = $1)
        AND (CAST($2 AS BIGINT) IS NULL OR r.tournament_id = $2)
    ORDER BY r.round_number
";
const STATS: &str = "
    SELECT CAST(rs.round_id AS BIGINT) AS round_id, CAST(rs.debater_id AS BIGINT) AS debater_id,
           CAST(rs.speaks AS DOUBLE PRECISION) AS speaks, CAST(rs.ranks AS DOUBLE PRECISION) AS ranks,
           rs.debater_role
    FROM core_roundstats rs JOIN core_round r ON r.id = rs.round_id
    WHERE (r.gov_id = $1 OR r.opp_id = $1) AND (rs.debater_id = $2 OR rs.debater_id = $3)
        AND (CAST($4 AS BIGINT) IS NULL OR r.tournament_id = $4)
    ORDER BY rs.id
";
const AWARDS: &str = "
    SELECT CAST(r.id AS BIGINT) AS id, CAST(r.tournament_id AS BIGINT) AS tournament_id,
           CAST(r.place AS BIGINT) AS place, CAST(r.type_of_place AS BIGINT) AS division,
           CAST(CASE WHEN r.ghost_points THEN 1 ELSE 0 END AS BIGINT) AS ghost,
           CAST(CASE WHEN EXISTS (SELECT 1 FROM core_teamresult_hidden_from_debaters h
                WHERE h.teamresult_id = r.id) THEN 1 ELSE 0 END AS BIGINT) AS hidden
    FROM core_teamresult r WHERE r.team_id = $1
";
const TOURNAMENTS: &str = "
    SELECT {columns}, CAST(t.num_rounds AS BIGINT) AS prelims
    FROM core_tournament t LEFT JOIN core_school s ON s.id = t.host_id
    WHERE EXISTS (SELECT 1 FROM core_teamresult r WHERE r.tournament_id = t.id AND r.team_id = $1)
        OR EXISTS (SELECT 1 FROM core_round r WHERE r.tournament_id = t.id
                   AND (r.gov_id = $1 OR r.opp_id = $1))
";
const MARKERS: [&str; 5] = ["one", "two", "three", "four", "five"];

#[derive(Serialize)]
pub(crate) struct Team<'a> {
    id: i64,
    name: &'a str,
    hybrid: bool,
    debaters: &'a [Value],
    url: String,
    api_url: String,
}

impl<'a> Team<'a> {
    pub(crate) fn new(
        id: i64,
        name: &'a str,
        hybrid: bool,
        debaters: &'a [Value],
        origin: &str,
    ) -> Self {
        Self {
            id,
            name,
            hybrid,
            debaters,
            url: format!("{origin}/core/teams/{id}"),
            api_url: format!("{origin}/api/teams/{id}/detail/"),
        }
    }
}

pub(crate) fn school(row: &AnyRow, origin: &str) -> Result<Option<School>, sqlx::Error> {
    match row.try_get("school_id")? {
        Some(id) => Ok(Some(School::new(
            id,
            row.try_get("school_name")?,
            row.try_get::<i64, _>("included")? != 0,
            origin,
        ))),
        None => Ok(None),
    }
}

pub(crate) async fn members(
    conn: &mut AnyConnection,
    team: i64,
    origin: &str,
) -> Result<Vec<Value>, ApiError> {
    Ok(members_for(conn, &[team], origin)
        .await?
        .remove(&team)
        .unwrap_or_default())
}

pub(crate) async fn members_for(
    conn: &mut AnyConnection,
    teams: &[i64],
    origin: &str,
) -> Result<HashMap<i64, Vec<Value>>, ApiError> {
    let mut members = teams
        .iter()
        .map(|id| (*id, Vec::new()))
        .collect::<HashMap<_, _>>();
    for group in teams.chunks(200) {
        let placeholders = (1..=group.len())
            .map(|position| format!("${position}"))
            .collect::<Vec<_>>()
            .join(",");
        let query = MEMBERS
            .replace("{columns}", DEBATER_COLUMNS)
            .replace("{school}", SCHOOL_COLUMNS)
            .replace("{teams}", &placeholders);
        let mut statement = sqlx::query(AssertSqlSafe(query));
        for id in group {
            statement = statement.bind(id);
        }
        for row in statement.fetch_all(&mut *conn).await? {
            let team: i64 = row.try_get("member_team_id")?;
            let school = school(&row, origin)?;
            members
                .entry(team)
                .or_default()
                .push(serde_json::to_value(Debater::from_row(
                    &row,
                    origin,
                    school.as_ref(),
                )?)?);
        }
    }
    Ok(members)
}

fn round_result(gov_won: bool, opp_won: bool, victor: i64) -> &'static str {
    if gov_won || opp_won {
        return match victor {
            6 => "AW",
            3 | 4 => "WF",
            _ => "W",
        };
    }
    match victor {
        5 => "AL",
        3.. => "LF",
        _ => "L",
    }
}

pub(crate) struct Card {
    pub(crate) wins: i64,
    pub(crate) rounds: Vec<Value>,
}

pub(crate) async fn cards(
    conn: &mut AnyConnection,
    team: i64,
    people: &[Value],
    origin: &str,
    only_tournament: Option<i64>,
) -> Result<HashMap<i64, Card>, ApiError> {
    let round_rows = sqlx::query(ROUNDS)
        .bind(team)
        .bind(only_tournament)
        .fetch_all(&mut *conn)
        .await?;
    let endpoints = [people.first(), people.last()];
    let ids = endpoints.map(|person| person.and_then(|person| person["id"].as_i64()));
    let mut scores = HashMap::new();
    if ids[0].is_some() && !round_rows.is_empty() {
        let rows = sqlx::query(STATS)
            .bind(team)
            .bind(ids[0])
            .bind(ids[1])
            .bind(only_tournament)
            .fetch_all(conn)
            .await?;
        for row in rows {
            let key = (
                row.try_get::<i64, _>("round_id")?,
                row.try_get::<i64, _>("debater_id")?,
            );
            if let std::collections::hash_map::Entry::Vacant(entry) = scores.entry(key) {
                entry.insert(json!({
                    "role": row.try_get::<Option<&str>, _>("debater_role")?.unwrap_or_default().to_uppercase(),
                    "speaks": row.try_get::<Option<f64>, _>("speaks")?,
                    "ranks": row.try_get::<Option<f64>, _>("ranks")?,
                }));
            }
        }
    }
    let mut cards = HashMap::new();
    for row in round_rows {
        let victor = row.try_get::<i64, _>("victor")?;
        let gov = row.try_get::<i64, _>("gov_id")?;
        let opp = row.try_get::<i64, _>("opp_id")?;
        let gov_won = gov == team && matches!(victor, 1 | 3 | 6);
        let opp_won = opp == team && matches!(victor, 2 | 4 | 6);
        let card = cards
            .entry(row.try_get::<i64, _>("tournament_id")?)
            .or_insert_with(|| Card {
                wins: 0,
                rounds: Vec::new(),
            });
        card.wins += i64::from(gov_won) + i64::from(opp_won);
        let round_id: i64 = row.try_get("id")?;
        let stats = endpoints
            .into_iter()
            .flatten()
            .filter_map(|person| {
                let id = person["id"].as_i64()?;
                let mut payload = scores.get(&(round_id, id))?.clone();
                payload["debater"] = person.clone();
                Some(payload)
            })
            .collect::<Vec<_>>();
        let side = if gov == team { "OPP" } else { "GOV" };
        let opponent = Team::new(
            if gov == team { opp } else { gov },
            row.try_get("opponent_name")?,
            row.try_get::<i64, _>("affiliations")? == 2,
            &[],
            origin,
        );
        card.rounds.push(json!({
            "round_id": round_id,
            "round_number": row.try_get::<i64, _>("round_number")?,
            "opponent": opponent,
            "opponent_side": side,
            "result": round_result(gov_won, opp_won, victor),
            "stats": stats,
        }));
    }
    Ok(cards)
}

async fn history(
    conn: &mut AnyConnection,
    team: &Team<'_>,
    hidden: &HashSet<i64>,
    origin: &str,
    max: Option<i64>,
) -> Result<Vec<Value>, ApiError> {
    let mut columns = Vec::new();
    let mut joins = Vec::new();
    for marker in MARKERS {
        columns.push(format!(
            "CAST(b.marker_{marker} AS DOUBLE PRECISION) AS points_{marker},
            CAST(m_{marker}.id AS BIGINT) AS id_{marker}, m_{marker}.name AS name_{marker},
            CAST(m_{marker}.date AS TEXT) AS date_{marker}"
        ));
        joins.push(format!(
            "LEFT JOIN core_tournament m_{marker} ON m_{marker}.id = b.tournament_{marker}_id"
        ));
    }
    let query = format!(
        "
        SELECT CAST(b.id AS BIGINT) AS id, b.season, CAST(b.place AS BIGINT) AS place,
               CAST(b.points AS DOUBLE PRECISION) AS points,
               CAST(CASE WHEN b.tied THEN 1 ELSE 0 END AS BIGINT) AS tied, {}
        FROM core_toty b {} WHERE b.team_id = $1 ORDER BY b.points DESC, b.season LIMIT $2
    ",
        columns.join(", "),
        joins.join(" ")
    );
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(team.id)
        .bind(max.unwrap_or(i64::MAX))
        .fetch_all(conn)
        .await?;
    let people = team
        .debaters
        .iter()
        .map(|person| {
            json!({
                "id": person["id"], "name": person["name"], "url": person["url"],
                "school_id": person["school_id"], "school_name": person["school_name"],
            })
        })
        .collect::<Vec<_>>();
    let lite_team = json!({"id": team.id, "name": team.name, "debaters": people, "url": team.url});
    let mut result = Vec::new();
    for row in rows {
        let mut markers = Vec::new();
        for (index, marker) in MARKERS.into_iter().enumerate() {
            let points: f64 = row.try_get(format!("points_{marker}").as_str())?;
            let id: Option<i64> = row.try_get(format!("id_{marker}").as_str())?;
            if let Some(id) = id.filter(|id| points != 0.0 && !hidden.contains(id)) {
                let name: &str = row.try_get(format!("name_{marker}").as_str())?;
                markers.push(json!({
                    "slot": index+1, "points": points,
                    "tournament": {"id": id, "name": name, "display": name.split(" (").next().unwrap_or_default(),
                        "date": row.try_get::<&str, _>(format!("date_{marker}").as_str())?,
                        "url": format!("{origin}/core/tournaments/{id}"),
                    }
                }));
            }
        }
        let place: i64 = row.try_get("place")?;
        let season: &str = row.try_get("season")?;
        result.push(json!({
            "id": row.try_get::<i64, _>("id")?,
            "season": season,
            "season_display": season.parse().map_or_else(|_| season.to_owned(), season_display),
            "place": place,
            "place_display": place_display(place),
            "tied": row.try_get::<i64, _>("tied")? != 0,
            "points": row.try_get::<f64, _>("points")?,
            "team": lite_team,
            "markers": markers,
        }));
    }
    Ok(result)
}

pub(crate) async fn read_transaction(
    pool: &sqlx::AnyPool,
) -> Result<sqlx::Transaction<'static, sqlx::Any>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    if pool.connect_options().database_url.scheme() != "sqlite" {
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .execute(&mut *tx)
            .await?;
    }
    Ok(tx)
}

async fn load(
    state: &AppState,
    id: i64,
    origin: &str,
    max: Option<i64>,
    path: &str,
) -> Result<Value, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let name = sqlx::query_scalar::<_, String>(TEAM)
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(ApiError::NotFound("Team not found"))?;
    let roster = members(&mut tx, id, origin).await?;
    let hybrid = roster
        .iter()
        .map(|person| person["school_id"].as_i64())
        .collect::<HashSet<_>>()
        .len()
        == 2;
    let team = Team::new(id, &name, hybrid, &roster, origin);
    let awards = sqlx::query(AWARDS).bind(id).fetch_all(&mut *tx).await?;
    let mut hidden = HashSet::new();
    let mut shown: HashMap<i64, Vec<Value>> = HashMap::new();
    let query = TOURNAMENTS.replace("{columns}", TOURNAMENT_COLUMNS);
    let rows = sqlx::query(AssertSqlSafe(query))
        .bind(id)
        .fetch_all(&mut *tx)
        .await?;
    let mut tournaments = HashMap::new();
    let mut prelims = HashMap::new();
    for row in rows {
        let tournament = Tournament::from_row(&row, origin)?;
        prelims.insert(tournament.id, row.try_get::<i64, _>("prelims")?);
        tournaments.insert(tournament.id, tournament);
    }
    for row in awards {
        let tournament = row.try_get::<i64, _>("tournament_id")?;
        if row.try_get::<i64, _>("hidden")? != 0 {
            hidden.insert(tournament);
            continue;
        }
        let place: i64 = row.try_get("place")?;
        let division: i64 = row.try_get("division")?;
        let division_label = match division {
            0 => json!("Novice"),
            1 => json!("Varsity"),
            _ => json!(division),
        };
        shown.entry(tournament).or_default().push(json!({
            "id": row.try_get::<i64, _>("id")?, "place": place,
            "place_display": if place > 0 { place_display(place) } else { String::new() },
            "type": division_label, "type_code": division, "ghost_points": row.try_get::<i64, _>("ghost")? != 0,
            "team": team, "tournament": tournaments[&tournament],
        }));
    }
    let cards = cards(&mut tx, id, &roster, origin, None).await?;
    let mut performances = Vec::new();
    for (tournament_id, tournament) in tournaments {
        let awards = shown.remove(&tournament_id).unwrap_or_default();
        let card = cards.get(&tournament_id);
        if awards.is_empty() && (card.is_none() || hidden.contains(&tournament_id)) {
            continue;
        }
        let record = card
            .map(|card| format!("{} - {}", card.wins, prelims[&tournament_id] - card.wins))
            .unwrap_or_default();
        let rounds = card.map(|card| card.rounds.as_slice()).unwrap_or_default();
        let sort_key = (tournament.date.clone(), tournament.id);
        for award in if awards.is_empty() {
            vec![Value::Null]
        } else {
            awards
        } {
            performances.push((
                sort_key.clone(),
                json!({
                    "kind": if award.is_null() {"participation"} else {"award"},
                    "team_result": award,
                    "record": record,
                    "tab_card": rounds,
                    "tournament": tournament,
                }),
            ));
        }
    }
    performances.sort_by(|(a, _), (b, _)| a.cmp(b));
    let history = history(&mut tx, &team, &hidden, origin, max).await?;
    tx.commit().await?;
    Ok(json!({
        "team": team, "toty_history": history,
        "tournaments": performances.into_iter().take(max.unwrap_or(i64::MAX) as usize).map(|(_, value)| value).collect::<Vec<_>>(),
        "links": {"self": proxy_url(origin, path)},
    }))
}

pub(crate) async fn detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    if !is_django_integer(&id) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Ok(id) = id.parse() else {
        return json_error(StatusCode::NOT_FOUND, "Team not found");
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let max = limit(uri.query().unwrap_or_default());
    let path = uri
        .path_and_query()
        .map(|path| path.as_str())
        .unwrap_or_default();
    match load(&state, id, &origin, max, path).await {
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
        "Team Detail Api",
        "Expose a team's roster, TOTY history, and tournament performances.",
    )
}

pub(crate) async fn page(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(id) = id.parse::<i64>().ok().filter(|_| is_django_integer(&id)) else {
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
    use crate::{session, web};
    use axum::{http::header, response::Html};
    use minijinja::context;

    let exists = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_team WHERE id = $1 AND NOT synthetic",
    )
    .bind(id)
    .fetch_optional(&state.pool)
    .await;
    match exists {
        Ok(Some(_)) => (),
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(error) => {
            let _ = ApiError::from(error);
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    }
    let data = load(state, id, "", None, "").await;
    let Ok(data) = data else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(user) = session::user(&state.pool, headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let all = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .any(|(name, _)| name == "all");
    let mut buttons = Vec::new();
    for (permission, name, suffix, class) in [
        ("remove_team", "Delete", "delete", "btn-danger"),
        ("change_team", "Edit", "edit", "btn-info"),
    ] {
        if session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false)
        {
            buttons.push(
                json!({"name":name, "href":format!("/core/teams/{id}/{suffix}"), "class":class}),
            );
        }
    }
    let rounds = data["tournaments"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let results = rounds
        .iter()
        .filter(|row| row["kind"] == "award")
        .chain(rounds.iter().filter(|row| all && row["kind"] != "award"))
        .map(|row| {
            json!({
                "type":row["kind"],"result":row["team_result"],"record":row["record"],
                "tab_card":row["tab_card"],"tournament":row["tournament"],
            })
        })
        .collect::<Vec<_>>();
    let common = web::base_context(state, &user, uri);
    let specific = context! {
        object => &data["team"], visible_debaters => &data["team"]["debaters"],
        totys => &data["toty_history"], buttons, results, current_season => "",
    };
    let (template, bindings) = match editing {
        Some(form) => ("teams/update.html", context! {..form,..common,..specific}),
        None => ("teams/detail.html", context! {..common,..specific}),
    };
    match web::render(template, bindings) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Team template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn result_labels_keep_byes_forfeits_and_double_results_consistent() {
        assert_eq!(round_result(true, false, 3), "WF");
        assert_eq!(round_result(false, false, 4), "LF");
        assert_eq!(round_result(false, false, 5), "AL");
        assert_eq!(round_result(true, true, 6), "AW");
        assert_eq!(round_result(false, false, 7), "LF");
    }
}
