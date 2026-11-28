mod catalog;
mod generate;
mod near_miss;
pub(crate) mod practice;
mod scoring;

use std::collections::{BTreeMap, HashSet};

use axum::{
    Json,
    extract::{OriginalUri, Path, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use chrono::{Datelike, Days, FixedOffset, NaiveDate, Utc};
use minijinja::context;
use scoring::{Board, CELLS, Guess, breakdown, person_key, solved};
use serde_json::{Value, json};
use sqlx::{Any, AnyConnection, AnyPool, AssertSqlSafe, Executor, Row};
use tokio_stream::StreamExt;

use crate::{ApiError, AppState, flash, login, session, web};

const BOARD: &str = "
    SELECT CAST(id AS BIGINT) AS id, CAST(date AS TEXT) AS day, CAST(rows AS TEXT) AS rows,
        CAST(columns AS TEXT) AS columns, CAST(answers AS TEXT) AS answers,
        CAST(aliases AS TEXT) AS aliases, CAST(clue_memberships AS TEXT) AS memberships
    FROM core_immaculategridboard WHERE CAST(date AS TEXT) = $1 AND replaced_at IS NULL
";
const PLAY: &str = "
    SELECT CAST(guesses AS TEXT) FROM core_immaculategridplay WHERE board_id = $1 AND user_id = $2
";
const RANKINGS: &str = "
    SELECT CAST(b.date AS TEXT) AS day, CAST(p.user_id AS BIGINT) AS id,
           CAST(p.guesses AS TEXT) AS guesses, u.username, u.first_name, u.last_name
    FROM core_immaculategridplay p JOIN core_immaculategridboard b ON b.id = p.board_id
    JOIN core_user u ON u.id = p.user_id
    WHERE CAST(b.date AS TEXT) BETWEEN $1 AND $2 AND b.replaced_at IS NULL
";
const PLAYER: &str = "
    SELECT CAST(p.guesses AS TEXT) AS guesses, u.username, u.first_name, u.last_name
    FROM core_immaculategridplay p JOIN core_user u ON p.user_id = u.id
    WHERE p.board_id = $1 AND p.user_id = $2
";
const PERSON: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, CAST(d.alias_group_id AS BIGINT) AS alias_group,
        TRIM(d.first_name || ' ' || d.last_name) AS name, COALESCE(s.name, '') AS school
    FROM core_debater d LEFT JOIN core_school s ON s.id = d.school_id
    WHERE NOT d.temporary AND NOT d.synthetic
";

pub(super) fn game_day() -> NaiveDate {
    Utc::now()
        .with_timezone(&FixedOffset::west_opt(5 * 3600).unwrap())
        .date_naive()
}

fn week_start(day: NaiveDate) -> NaiveDate {
    day - Days::new(u64::from(day.weekday().num_days_from_monday()))
}

fn uncached(mut response: Response) -> Response {
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("max-age=0, no-cache, no-store, must-revalidate, private"),
    );
    response
}

fn render(
    state: &AppState,
    user: &session::User,
    uri: &Uri,
    template: &str,
    data: Value,
    status: StatusCode,
) -> Response {
    let common = web::base_context(state, user, uri);
    let data = minijinja::Value::from_serialize(data);
    match web::render(template, context! { ..data, ..common }) {
        Ok(html) => uncached((status, [(header::VARY, "Cookie")], Html(html)).into_response()),
        Err(error) => {
            eprintln!("Immaculate Grid template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn board<'a>(
    executor: impl Executor<'a, Database = Any>,
    day: NaiveDate,
) -> Result<Option<Board>, ApiError> {
    let row = sqlx::query(BOARD)
        .bind(day.to_string())
        .fetch_optional(executor)
        .await?;
    let Some(row) = row else { return Ok(None) };
    let id: i64 = row.try_get("id")?;
    let grid = Board {
        id: id as u128,
        date: day,
        rows: serde_json::from_str(row.try_get("rows")?)?,
        columns: serde_json::from_str(row.try_get("columns")?)?,
        answers: serde_json::from_str(row.try_get("answers")?)?,
        aliases: serde_json::from_str(row.try_get("aliases")?)?,
        clue_memberships: serde_json::from_str(row.try_get("memberships")?)?,
    };
    Ok(grid.valid_shape().then_some(grid))
}

async fn visible_on(conn: &mut AnyConnection, board: &Board) -> Result<bool, ApiError> {
    let subjects: HashSet<_> = board
        .rows
        .iter()
        .chain(&board.columns)
        .filter_map(|clue| clue.subject.as_deref())
        .collect();
    for subject in subjects {
        let Some(prefix) = subject.get(..1) else {
            return Ok(false);
        };
        let Ok(id) = subject[1..].parse::<i64>() else {
            return Ok(false);
        };
        let field = match prefix {
            "d" => "d.id",
            "a" => "d.alias_group_id",
            _ => return Ok(false),
        };
        let query = format!("
            SELECT CAST(CASE WHEN EXISTS (
                SELECT 1 FROM core_debater d WHERE {field} = $1 AND NOT d.synthetic AND NOT d.temporary
                AND NOT EXISTS (
                    SELECT 1 FROM core_immaculategridhiddendebater h JOIN core_debater hd ON hd.id = h.debater_id
                    WHERE hd.id = d.id OR (hd.alias_group_id IS NOT NULL AND hd.alias_group_id = d.alias_group_id)
                )
            ) THEN 1 ELSE 0 END AS BIGINT)
        ");
        if sqlx::query_scalar::<_, i64>(AssertSqlSafe(query))
            .bind(id)
            .fetch_one(&mut *conn)
            .await?
            == 0
        {
            return Ok(false);
        }
    }
    Ok(true)
}

async fn clues_visible(pool: &AnyPool, board: &Board) -> Result<bool, ApiError> {
    let mut conn = pool.acquire().await?;
    visible_on(&mut conn, board).await
}

async fn guesses(pool: &AnyPool, board: u128, user: i64) -> Result<Vec<Guess>, ApiError> {
    let play = sqlx::query_scalar::<_, String>(PLAY)
        .bind(board as i64)
        .bind(user)
        .fetch_optional(pool)
        .await?;
    Ok(play
        .as_deref()
        .map(serde_json::from_str)
        .transpose()?
        .unwrap_or_default())
}

fn player_name(row: &sqlx::any::AnyRow, id: i64) -> Result<String, sqlx::Error> {
    let first: &str = row.try_get("first_name")?;
    let last: &str = row.try_get("last_name")?;
    let name = format!("{first} {last}");
    if !name.trim().is_empty() {
        return Ok(name.trim().to_owned());
    }
    let username: &str = row.try_get("username")?;
    Ok(if username.contains('@') {
        format!("Player {id}")
    } else {
        username.to_owned()
    })
}

fn score_row(id: i64, name: &str, day: &str, guesses: &[Guess]) -> Value {
    let mut row = serde_json::to_value(breakdown(guesses)).expect("finite grid score");
    row["id"] = json!(id);
    row["name"] = json!(name);
    row["day"] = json!(day);
    row
}

fn rank(rows: &mut [Value], viewer: i64) {
    rows.sort_by(|a, b| {
        b["score"]
            .as_i64()
            .cmp(&a["score"].as_i64())
            .then_with(|| {
                a["name"]
                    .as_str()
                    .unwrap_or_default()
                    .to_lowercase()
                    .cmp(&b["name"].as_str().unwrap_or_default().to_lowercase())
            })
            .then_with(|| a["id"].as_i64().cmp(&b["id"].as_i64()))
            .then_with(|| a["day"].as_str().cmp(&b["day"].as_str()))
    });
    let mut last = None;
    let mut place = 0;
    for (position, row) in rows.iter_mut().enumerate() {
        let score = row["score"].as_i64();
        if score != last {
            place = position + 1;
            last = score;
        }
        row["rank"] = json!(place);
        row["is_viewer"] = json!(row["id"].as_i64() == Some(viewer));
    }
}

struct Rankings {
    today: Vec<Value>,
    week: Vec<Value>,
    days: Vec<String>,
    all_time: Vec<Value>,
}

async fn rankings(
    pool: &AnyPool,
    day: NaiveDate,
    viewer: i64,
    all_time: bool,
) -> Result<Rankings, ApiError> {
    let start = week_start(day);
    let dates: Vec<_> = (0..=day.weekday().num_days_from_monday())
        .map(|offset| (start + Days::new(u64::from(offset))).to_string())
        .collect();
    let monday = start.to_string();
    let end = day.to_string();
    let begin = if all_time { "0001-01-01" } else { &monday };
    let mut rows = sqlx::query(RANKINGS).bind(begin).bind(&end).fetch(pool);
    let mut result = Rankings {
        today: Vec::new(),
        week: Vec::new(),
        days: dates,
        all_time: Vec::new(),
    };
    let mut weekly = BTreeMap::<i64, (String, BTreeMap<String, i64>)>::new();
    while let Some(row) = rows.next().await {
        let row = row?;
        let play: Vec<Guess> = serde_json::from_str(row.try_get("guesses")?)?;
        if play.is_empty() {
            continue;
        }
        let id = row.try_get::<i64, _>("id")?;
        let name = player_name(&row, id)?;
        let date: &str = row.try_get("day")?;
        let item = score_row(id, &name, date, &play);
        if date == end {
            result.today.push(item.clone());
        }
        if date >= monday.as_str() {
            weekly
                .entry(id)
                .or_insert_with(|| (name, BTreeMap::new()))
                .1
                .insert(date.to_owned(), item["score"].as_i64().unwrap_or_default());
        }
        if all_time && solved(&play).len() == CELLS {
            result.all_time.push(item);
            rank(&mut result.all_time, viewer);
            result.all_time.truncate(10);
        }
    }
    for (id, (name, scores)) in weekly {
        let daily_scores: Vec<_> = result
            .days
            .iter()
            .map(|day| (day, scores.get(day)))
            .collect();
        result
            .week
            .push(json!({"id":id, "name":name, "daily_scores":daily_scores,
            "score":scores.values().sum::<i64>()}));
    }
    rank(&mut result.today, viewer);
    rank(&mut result.week, viewer);
    Ok(result)
}

async fn current_board(state: &AppState, day: NaiveDate) -> Result<Option<Board>, ApiError> {
    let board = board(&state.pool, day).await?;
    if let Some(board) = board
        && clues_visible(&state.pool, &board).await?
    {
        return Ok(Some(board));
    }
    Ok(None)
}

async fn generate_daily(state: &AppState, day: NaiveDate) -> Result<Option<Board>, ApiError> {
    if let Some(existing) = current_board(state, day).await? {
        return Ok(Some(existing));
    }
    let sqlite = state.pool.connect_options().database_url.scheme() == "sqlite";
    let mut tx = state.pool.begin().await?;
    let date = day.to_string();
    sqlx::query(
        "INSERT INTO core_sitesetting (key, value) VALUES ('immaculate_grid_generation', $1)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
    )
    .bind(&date)
    .execute(&mut *tx)
    .await?;
    if let Some(existing) = board(&mut *tx, day).await?
        && visible_on(&mut tx, &existing).await?
    {
        tx.commit().await?;
        return Ok(Some(existing));
    }
    let first = (day - Days::new(100)).to_string();
    let last = (day + Days::new(100)).to_string();
    let rows = sqlx::query("
        SELECT CAST(date AS TEXT) AS date, CAST(rows AS TEXT) AS rows, CAST(columns AS TEXT) AS columns
        FROM core_immaculategridboard WHERE CAST(date AS TEXT) BETWEEN $1 AND $2 AND CAST(date AS TEXT) <> $3
    ").bind(&first).bind(&last).bind(&date).fetch_all(&mut *tx).await?;
    let history = rows
        .iter()
        .map(|row| {
            Ok(generate::HistoryBoard {
                date: row
                    .try_get::<&str, _>("date")?
                    .parse()
                    .map_err(|_| ApiError::InvalidStoredDate)?,
                rows: serde_json::from_str(row.try_get("rows")?)?,
                columns: serde_json::from_str(row.try_get("columns")?)?,
            })
        })
        .collect::<Result<Vec<_>, ApiError>>()?;
    let catalog = catalog::load(&mut tx, day, &[]).await?;
    let built = tokio::task::spawn_blocking(move || generate::build(day, catalog, &history, None))
        .await
        .map_err(|_| ApiError::Serialization)?;
    let Some(mut next) = built else {
        return Ok(None);
    };
    if !visible_on(&mut tx, &next).await? {
        return Ok(None);
    }
    sqlx::query(
        "UPDATE core_immaculategridboard SET replaced_at = CURRENT_TIMESTAMP
        WHERE CAST(date AS TEXT) = $1 AND replaced_at IS NULL",
    )
    .bind(&date)
    .execute(&mut *tx)
    .await?;
    let json_sql = if sqlite {
        "INSERT INTO core_immaculategridboard (date, rows, columns, answers, aliases, clue_memberships)
         VALUES ($1,$2,$3,$4,$5,$6) RETURNING CAST(id AS BIGINT)"
    } else {
        "INSERT INTO core_immaculategridboard (date, rows, columns, answers, aliases, clue_memberships)
         VALUES (CAST(CAST($1 AS TEXT) AS DATE), CAST(CAST($2 AS TEXT) AS JSONB),
            CAST(CAST($3 AS TEXT) AS JSONB), CAST(CAST($4 AS TEXT) AS JSONB), CAST(CAST($5 AS TEXT) AS JSONB),
            CAST(CAST($6 AS TEXT) AS JSONB)) RETURNING CAST(id AS BIGINT)"
    };
    let memberships = next
        .clue_memberships
        .iter()
        .map(|(key, people)| {
            let mut people: Vec<_> = people.iter().collect();
            people.sort();
            (key, people)
        })
        .collect::<BTreeMap<_, _>>();
    let id = sqlx::query_scalar::<_, i64>(json_sql)
        .bind(&date)
        .bind(serde_json::to_string(&next.rows)?)
        .bind(serde_json::to_string(&next.columns)?)
        .bind(serde_json::to_string(&next.answers)?)
        .bind(serde_json::to_string(&next.aliases)?)
        .bind(serde_json::to_string(&memberships)?)
        .fetch_one(&mut *tx)
        .await?;
    next.id = id as u128;
    tx.commit().await?;
    Ok(Some(next))
}

async fn daily_data(
    state: &AppState,
    day: NaiveDate,
    user: i64,
) -> Result<Option<Value>, ApiError> {
    let Some(board) = generate_daily(state, day).await? else {
        return Ok(None);
    };
    let guesses = guesses(&state.pool, board.id, user).await?;
    let ranks = rankings(&state.pool, day, user, false).await?;
    let mut result = scoring::grid(&board, &guesses, false);
    result["today_leaders"] = json!(ranks.today.into_iter().take(3).collect::<Vec<_>>());
    result["week_leaders"] = json!(ranks.week.into_iter().take(3).collect::<Vec<_>>());
    result["week_days"] = json!(ranks.days);
    Ok(Some(result))
}

pub(crate) async fn daily(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(user_id) = user.id else {
        return uncached(login::redirect_to_login(&uri));
    };
    let Some(secret) = state.secret.as_deref() else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let date = game_day();
    let (mut data, status) = match daily_data(&state, date, user_id).await {
        Ok(Some(data)) => (data, StatusCode::OK),
        Ok(None) => (
            json!({"day": date.to_string()}),
            StatusCode::SERVICE_UNAVAILABLE,
        ),
        Err(error) => {
            eprintln!("Daily grid failed: {error:?}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let Ok((csrf, token)) = login::csrf_tokens(&headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let secure = state
        .origin(&headers)
        .is_some_and(|value| value.starts_with("https://"));
    let messages = flash::messages(&headers, secret);
    data["csrf"] = json!(token);
    data["messages"] = json!(messages);
    let open = data["rows"].is_array();
    let mut response = render(
        &state,
        &user,
        &uri,
        "immaculate_grid/grid.html",
        data,
        status,
    );
    if open {
        login::set_csrf_cookie(&mut response, &csrf, secure);
        if !messages.is_empty() {
            flash::clear(&mut response, secure);
        }
    }
    response
}

pub(crate) async fn leaderboard(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(id) = user.id else {
        return uncached(login::redirect_to_login(&uri));
    };
    let day = game_day();
    let Ok(rankings) = rankings(&state.pool, day, id, true).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(active_board) = board(&state.pool, day).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let mut can_view = false;
    if let Some(board) = active_board {
        let Ok(guesses) = guesses(&state.pool, board.id, id).await else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        can_view = solved(&guesses).len() == CELLS;
    }
    render(
        &state,
        &user,
        &uri,
        "immaculate_grid/leaderboard.html",
        json!({
            "day":day.to_string(), "today_leaders": rankings.today, "week_leaders": rankings.week,
            "week_days": rankings.days, "all_time_leaders": rankings.all_time, "can_view_boards": can_view,
        }),
        StatusCode::OK,
    )
}

pub(crate) async fn player_board(
    State(state): State<AppState>,
    Path(player): Path<i64>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(viewer) = user.id else {
        return uncached(login::redirect_to_login(&uri));
    };
    let today = game_day();
    let filter = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(key, _)| key == "day")
        .last();
    let day = filter.map_or_else(|| Ok(today), |(_, day)| day.parse::<NaiveDate>());
    let Ok(day) = day else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if !(week_start(today)..=today).contains(&day) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let board = match current_board(&state, day).await {
        Ok(Some(board)) => board,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    if day == today {
        let Ok(viewer_guesses) = guesses(&state.pool, board.id, viewer).await else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        if solved(&viewer_guesses).len() != CELLS {
            return uncached(
                (
                    StatusCode::FORBIDDEN,
                    [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
                    "Solve all nine squares on today's board to view other players' boards.",
                )
                    .into_response(),
            );
        }
    }
    let row = match sqlx::query(PLAYER)
        .bind(board.id as i64)
        .bind(player)
        .fetch_optional(&state.pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let Ok(play) = serde_json::from_str::<Vec<Guess>>(row.get("guesses")) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if play.is_empty() {
        return uncached(
            (
                StatusCode::FOUND,
                [(header::LOCATION, "/immaculate-grid/leaderboard/")],
            )
                .into_response(),
        );
    }
    let mut data = scoring::grid(&board, &play, false);
    let Ok(name) = player_name(&row, player) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    data["player_name"] = json!(name);
    data["read_only"] = json!(true);
    data["own_board"] = json!(viewer == player);
    if viewer != player {
        data["miss_names"] = Value::Null;
    }
    render(
        &state,
        &user,
        &uri,
        "immaculate_grid/player_board.html",
        data,
        StatusCode::OK,
    )
}

fn like(value: &str) -> String {
    value
        .replace('!', "!!")
        .replace('%', "!%")
        .replace('_', "!_")
}

async fn search_names(pool: &AnyPool, query: &str) -> Result<Vec<Value>, ApiError> {
    let words: Vec<_> = query.split_whitespace().collect();
    let mut sql = format!("SELECT * FROM ({PERSON}) p WHERE 1=1");
    for index in 0..words.len() {
        sql.push_str(&format!(
            " AND LOWER(p.name) LIKE LOWER(${}) ESCAPE '!'",
            index + 1
        ));
    }
    sql.push_str(&format!(
        " ORDER BY CASE WHEN LOWER(p.name) = LOWER(${}) THEN 0
        WHEN LOWER(p.name) LIKE LOWER(${}) ESCAPE '!' THEN 1 ELSE 2 END, LOWER(p.name), p.id",
        words.len() + 1,
        words.len() + 2
    ));
    let mut statement = sqlx::query(AssertSqlSafe(sql));
    for word in words {
        statement = statement.bind(format!("%{}%", like(word)));
    }
    let mut rows = statement
        .bind(query)
        .bind(format!("{}%", like(query)))
        .fetch(pool);
    let mut people = HashSet::new();
    let mut results = Vec::new();
    while let Some(row) = rows.next().await {
        let row = row?;
        let id = row.try_get("id")?;
        let person = person_key(id, row.try_get("alias_group")?);
        if people.insert(person.clone()) {
            results.push(
                json!({"id":id, "person":person, "name":row.try_get::<&str,_>("name")?,
                "school":row.try_get::<&str,_>("school")?}),
            );
        }
        if results.len() == 12 {
            break;
        }
    }
    Ok(results)
}

pub(crate) async fn search(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if !user.is_authenticated {
        return uncached(login::redirect_to_login(&uri));
    }
    let query: String = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .filter(|(key, _)| key == "q")
        .last()
        .map(|(_, value)| value.split_whitespace().collect::<Vec<_>>().join(" "))
        .unwrap_or_default()
        .chars()
        .take(80)
        .collect();
    let results = if query.chars().count() < 2 {
        Vec::new()
    } else {
        match search_names(&state.pool, &query).await {
            Ok(results) => results,
            Err(error) => {
                eprintln!("Grid search failed: {error:?}");
                return StatusCode::INTERNAL_SERVER_ERROR.into_response();
            }
        }
    };
    uncached(Json(json!({"results":results})).into_response())
}

async fn pick_guess(
    pool: &AnyPool,
    board: &Board,
    guesses: &[Guess],
    debater: i64,
    cell: usize,
    attempts: usize,
) -> Result<Result<Guess, &'static str>, ApiError> {
    let candidate = sqlx::query(AssertSqlSafe(format!("{PERSON} AND d.id = $1")))
        .bind(debater)
        .fetch_optional(pool)
        .await?;
    let Some(candidate) = candidate else {
        return Ok(Err(
            "Choose a square and select a debater from the search results.",
        ));
    };
    let group = candidate.try_get::<Option<i64>, _>("alias_group")?;
    let profiles = if let Some(group) = group {
        sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_debater WHERE alias_group_id = $1",
        )
        .bind(group)
        .fetch_all(pool)
        .await?
        .into_iter()
        .collect()
    } else {
        HashSet::from([debater])
    };
    let mut people = HashSet::from([person_key(debater, group)]);
    for id in &profiles {
        people.insert(person_key(*id, None));
        if let Some(person) = board.aliases.get(id) {
            people.insert(person.clone());
        }
    }
    if let Some(issue) = scoring::guess_issue(guesses, attempts, cell, &people, &profiles) {
        return Ok(Err(issue));
    }
    let person = board.person(debater, group);
    let score = board.answers[cell].get(&person).copied();
    Ok(Ok(Guess {
        cell,
        person,
        debater_id: Some(debater),
        score,
        name: candidate.try_get("name")?,
    }))
}

async fn write_guess(
    pool: &AnyPool,
    board: u128,
    user: i64,
    existing: Option<String>,
    guesses: Vec<Guess>,
) -> Result<bool, ApiError> {
    let sqlite = pool.connect_options().database_url.scheme() == "sqlite";
    // Django's SQLite JSONField equality compares the serialized text.
    let document = if sqlite {
        crate::django_json::encode(&guesses)?
    } else {
        serde_json::to_string(&guesses)?
    };
    let value = if sqlite {
        "$3"
    } else {
        "CAST(CAST($3 AS TEXT) AS JSONB)"
    };
    let sql = if existing.is_some() {
        format!("UPDATE core_immaculategridplay SET guesses = {value}
            WHERE board_id = $1 AND user_id = $2 AND CAST(guesses AS TEXT) = $4
            AND EXISTS (SELECT 1 FROM core_immaculategridboard b WHERE b.id = $1 AND b.replaced_at IS NULL)")
    } else {
        format!("INSERT INTO core_immaculategridplay (board_id, user_id, guesses)
            SELECT $1, $2, {value} FROM core_immaculategridboard b WHERE b.id = $1 AND b.replaced_at IS NULL
            ON CONFLICT (board_id, user_id) DO NOTHING")
    };
    let query = sqlx::query(AssertSqlSafe(sql))
        .bind(board as i64)
        .bind(user)
        .bind(document);
    let query = if let Some(old) = existing {
        query.bind(old)
    } else {
        query
    };
    Ok(query.execute(pool).await?.rows_affected() == 1)
}

fn redirect_with_message(
    path: &'static str,
    message: Option<&str>,
    headers: &HeaderMap,
    secret: &[u8],
    secure: bool,
) -> Response {
    let mut response = uncached((StatusCode::FOUND, [(header::LOCATION, path)]).into_response());
    if let Some(message) = message {
        flash::push(&mut response, headers, secret, 20, message, secure);
    }
    response
}

pub(crate) async fn guess(State(state): State<AppState>, request: Request) -> Response {
    let Ok(user) = session::user(&state.pool, request.headers(), state.secret.as_deref()).await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(user_id) = user.id else {
        return uncached(login::redirect_to_login(request.uri()));
    };
    let Some(origin) = state.origin(request.headers()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let Some(secret) = state.secret.as_deref() else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let (parts, body) = request.into_parts();
    let form = match login::parse_form(&parts.headers, body, 128 * 1024).await {
        Ok(fields) => fields,
        Err(status) => return status.into_response(),
    };
    let field = |name| form.get(name).map(String::as_str).unwrap_or_default();
    if !login::csrf_valid(
        &parts.headers,
        form.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return login::csrf_failure();
    }
    let redirect = |message| {
        redirect_with_message(
            "/immaculate-grid/",
            message,
            &parts.headers,
            secret,
            origin.starts_with("https://"),
        )
    };
    let day = game_day();
    let (Ok(input_day), Ok(id), Ok(cell), Ok(attempts), Ok(debater)) = (
        field("day").parse::<NaiveDate>(),
        field("board").parse::<u128>(),
        field("cell").parse::<usize>(),
        field("attempts").parse::<usize>(),
        field("debater").parse::<i64>(),
    ) else {
        return redirect(Some(
            "Choose a square and select a debater from the search results.",
        ));
    };
    if cell >= CELLS || id == 0 {
        return redirect(Some(
            "Choose a square and select a debater from the search results.",
        ));
    }
    if day != input_day {
        return redirect(Some("A new daily grid is ready."));
    }
    let board = match current_board(&state, day).await {
        Ok(Some(board)) if board.id == id => board,
        Ok(_) => {
            return redirect(Some(
                "This grid has changed. Choose a square on the latest board.",
            ));
        }
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let Ok(existing) = sqlx::query_scalar::<_, String>(PLAY)
        .bind(id as i64)
        .bind(user_id)
        .fetch_optional(&state.pool)
        .await
    else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let mut guesses = match existing
        .as_deref()
        .map(serde_json::from_str::<Vec<Guess>>)
        .transpose()
    {
        Ok(play) => play.unwrap_or_default(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    match pick_guess(&state.pool, &board, &guesses, debater, cell, attempts).await {
        Ok(Ok(guess)) => guesses.push(guess),
        Ok(Err(issue)) => return redirect(Some(issue)),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
    if day != game_day() {
        return redirect(Some("A new daily grid is ready."));
    }
    match write_guess(&state.pool, board.id, user_id, existing, guesses).await {
        Ok(true) => redirect(None),
        Ok(false) => redirect(Some("Your grid was updated. Choose a square to continue.")),
        Err(error) => {
            eprintln!("Daily grid update failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn reveal(
    pool: &AnyPool,
    board: &Board,
    guesses: &[Guess],
    cell: usize,
) -> Result<Value, ApiError> {
    let scores = &board.answers[cell];
    let best = scores.values().copied().max();
    let mut ids = board
        .aliases
        .iter()
        .filter(|(_, person)| scores.contains_key(*person))
        .map(|(id, _)| *id)
        .collect::<Vec<_>>();
    ids.sort_unstable();
    let mut viewed = HashSet::new();
    let mut options = Vec::new();
    for ids in ids.chunks(200) {
        let params = (1..=ids.len())
            .map(|index| format!("${index}"))
            .collect::<Vec<_>>()
            .join(", ");
        let mut query = sqlx::query(AssertSqlSafe(format!(
            "{PERSON} AND d.id IN ({params}) ORDER BY d.id"
        )));
        for id in ids {
            query = query.bind(*id);
        }
        for row in query.fetch_all(pool).await? {
            let id: i64 = row.try_get("id")?;
            if viewed.insert(person_key(id, row.try_get("alias_group")?))
                && let Some(score) = board.aliases.get(&id).and_then(|person| scores.get(person))
            {
                options.push(json!({"name":row.try_get::<&str,_>("name")?, "school":row.try_get::<&str,_>("school")?,
                    "score":score, "tier":scoring::tier(*score, best)}));
            }
        }
    }
    options.sort_by(|a, b| {
        b["score"].as_i64().cmp(&a["score"].as_i64()).then_with(|| {
            a["name"]
                .as_str()
                .unwrap_or_default()
                .to_lowercase()
                .cmp(&b["name"].as_str().unwrap_or_default().to_lowercase())
        })
    });
    let clues = [
        &board.rows[cell / scoring::WIDTH],
        &board.columns[cell % scoring::WIDTH],
    ];
    let guesses: Vec<_> = guesses
        .iter()
        .filter(|guess| guess.cell == cell && guess.score.is_none())
        .collect();
    let approximate = board.clue_memberships.is_empty() && !guesses.is_empty();
    let thresholds = clues.map(|clue| near_miss::threshold(&clue.key));
    let needed: Vec<_> = thresholds
        .iter()
        .zip(clues)
        .filter_map(|(threshold, clue)| {
            let (topic, _) = threshold.as_ref()?;
            (approximate
                || guesses.iter().any(|guess| {
                    !board
                        .clue_memberships
                        .get(&clue.key)
                        .is_some_and(|people| people.contains(&guess.person))
                }))
            .then_some((*topic).to_owned())
        })
        .collect();
    let catalog = if approximate || !needed.is_empty() {
        let mut conn = pool.acquire().await?;
        Some(catalog::load(&mut conn, board.date, &needed).await?)
    } else {
        None
    };
    let misses: Vec<_> = guesses
        .iter()
        .map(|guess| {
            let current = catalog
                .as_ref()
                .and_then(|catalog| guess.debater_id.and_then(|id| catalog.aliases.get(&id)))
                .unwrap_or(&guess.person);
            let matches = clues.map(|clue| {
                if approximate {
                    catalog
                        .as_ref()
                        .and_then(|catalog| catalog.clues.get(&clue.key))
                        .map(|category| category.people.contains(current))
                } else {
                    board
                        .clue_memberships
                        .get(&clue.key)
                        .map(|people| people.contains(&guess.person))
                }
            });
            let mut miss = json!({"name":guess.name, "matches":matches});
            if let Some(catalog) = &catalog
                && !needed.is_empty()
            {
                let details = std::array::from_fn::<_, 2, _>(|index| {
                    (matches[index] == Some(false))
                        .then(|| near_miss::explain(&catalog.progress, thresholds[index], current))
                        .flatten()
                });
                if details.iter().any(Option::is_some) {
                    miss["details"] = json!(details);
                }
            }
            miss
        })
        .collect();
    Ok(
        json!({"answers":options, "misses":misses, "clues":clues.map(|clue| &clue.label), "approximate":approximate}),
    )
}

fn json_error(status: StatusCode, error: &str) -> Response {
    uncached((status, Json(json!({"error":error}))).into_response())
}

pub(crate) async fn answers(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Some(user_id) = user.id else {
        return uncached(login::redirect_to_login(&uri));
    };
    let fields = login::form_values(uri.query().unwrap_or_default().as_bytes());
    let field = |name| fields.get(name).map(String::as_str).unwrap_or_default();
    let (Ok(day), Ok(id), Ok(cell)) = (
        field("day").parse::<NaiveDate>(),
        field("board").parse::<u128>(),
        field("cell").parse::<usize>(),
    ) else {
        return json_error(StatusCode::BAD_REQUEST, "Choose a grid and a square.");
    };
    if id == 0 || cell >= CELLS {
        return json_error(StatusCode::BAD_REQUEST, "Choose a grid and a square.");
    }
    let infinite = field("mode") == "infinite";
    let (board, guesses) = if infinite {
        let Some(secret) = state.secret.as_deref() else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        let stored = match session::StoredSession::read(&state.pool, &headers, secret).await {
            Ok(Some(stored)) => stored,
            Ok(None) => return json_error(StatusCode::NOT_FOUND, "This grid is unavailable."),
            Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        };
        match practice::play(&stored) {
            Some(play) if play.board.id == id && play.board.date == day => {
                (play.board, play.guesses)
            }
            _ => return json_error(StatusCode::NOT_FOUND, "This grid is unavailable."),
        }
    } else {
        let board = match board(&state.pool, day).await {
            Ok(Some(board)) if board.id == id => board,
            Ok(_) => return json_error(StatusCode::NOT_FOUND, "This grid is unavailable."),
            Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        };
        let Ok(guesses) = guesses(&state.pool, board.id, user_id).await else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        (board, guesses)
    };
    if solved(&guesses).len() != CELLS {
        return json_error(
            StatusCode::FORBIDDEN,
            "Solve all nine squares to view valid answers.",
        );
    }
    match clues_visible(&state.pool, &board).await {
        Ok(true) => {}
        Ok(false) => {
            return json_error(StatusCode::SERVICE_UNAVAILABLE, "This grid is unavailable.");
        }
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
    match reveal(&state.pool, &board, &guesses, cell).await {
        Ok(data) => uncached(Json(data).into_response()),
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}
