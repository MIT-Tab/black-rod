pub(crate) mod edit;

use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
};

use axum::{
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use chrono::NaiveDateTime;
use minijinja::context;
use serde_json::{Value, json};
use sqlx::{AnyConnection, Row};

use crate::{
    ApiError, AppState,
    directory::TOURNAMENT_TYPES,
    flash, login,
    session::User,
    team_profile::{members_for, read_transaction},
    web,
};

pub(crate) const BASE: &str = "/core/admin/tournament-audit";
const TYPES: [i64; 9] = [0, 7, 8, 9, 10, 15, 12, 13, 14];
const SORTS: [(&str, &str); 14] = [
    ("season", "season_sort"),
    ("date", "date"),
    ("tournament", "tournament_name"),
    ("type", "qual_type_label"),
    ("teams", "recorded_team_count"),
    ("prelims", "recorded_num_rounds"),
    ("recorded_in", "recorded_prelim"),
    ("expected_in", "expected_prelim"),
    ("in_pct", "prelim_pct"),
    ("recorded_out", "recorded_outround"),
    ("expected_out", "expected_outround"),
    ("out_pct", "outround_pct"),
    ("total_pct", "total_pct"),
    ("imports", "linked_import_count"),
];

pub(crate) fn detail_url(id: i64) -> String {
    format!("{BASE}/{id}/")
}

fn type_name(kind: i64) -> String {
    TOURNAMENT_TYPES
        .iter()
        .find(|(id, _)| kind == *id)
        .map_or_else(|| kind.to_string(), |(_, label)| (*label).to_owned())
}

fn percent(count: i64, total: i64) -> f64 {
    if total <= 0 {
        100.0
    } else {
        ((1_000.0 * count as f64) / total as f64).round_ties_even() / 10.0
    }
}

async fn audit(conn: &mut AnyConnection, id: Option<i64>) -> Result<Vec<Value>, ApiError> {
    let rows = sqlx::query(
        "SELECT CAST(t.id AS BIGINT) AS id,t.name,t.season, CAST(t.date AS TEXT) AS date,
        CAST(t.qual_type AS BIGINT) AS kind,CAST(t.num_rounds AS BIGINT) AS rounds,
        CAST(t.num_teams AS BIGINT) AS teams,CAST(t.num_novice_teams AS BIGINT) AS novice,
        (SELECT COUNT(*) FROM core_teamresult a WHERE a.tournament_id=t.id AND a.place>0
            AND a.type_of_place=CASE WHEN t.qual_type=9 THEN 0 ELSE 1 END) AS places,
        (SELECT COUNT(*) FROM core_round r WHERE r.tournament_id=t.id
            AND (LOWER(TRIM(r.stage))='outround' OR r.round_number>t.num_rounds)) AS outs,
        (SELECT COUNT(*) FROM core_round r WHERE r.tournament_id=t.id) AS total,
        (SELECT COUNT(*) FROM core_tournamentimport i WHERE i.tournament_id=t.id) AS imports
        FROM core_tournament t WHERE (CAST($1 AS BIGINT) IS NULL OR t.id=$1)
        AND t.qual_type IN (0,7,8,9,10,15,12,13,14) ORDER BY t.date,t.id",
    )
    .bind(id)
    .fetch_all(conn)
    .await?;
    let mut data = Vec::new();
    for row in rows {
        let season: &str = row.try_get("season")?;
        let Some(start) = season
            .trim()
            .split('-')
            .next()
            .and_then(|value| value.trim().parse::<i32>().ok())
            .filter(|value| *value >= 2017)
        else {
            continue;
        };
        let kind = row.try_get::<i64, _>("kind")?;
        let count = row.try_get::<i64, _>("teams")?;
        let novice = row.try_get::<i64, _>("novice")?;
        let teams = if kind == 9 && novice > 0 {
            novice
        } else if count > 0 {
            count
        } else {
            novice.max(0)
        };
        if teams < 1 {
            continue;
        }
        let rounds = row.try_get::<i64, _>("rounds")?;
        let expected_prelim = teams * if kind == 8 { 7 } else { rounds.max(0) } / 2;
        let expected_out = (row.try_get::<i64, _>("places")? - 1).max(0);
        let outs = row.try_get::<i64, _>("outs")?;
        let total = row.try_get::<i64, _>("total")?;
        let id = row.try_get::<i64, _>("id")?;
        data.push(json!({
            "tournament_id":id,"tournament_name":row.try_get::<&str,_>("name")?,
            "season_display":if season.chars().all(|value|value.is_ascii_digit()) {crate::schedule::season_display(start)} else{season.to_owned()},
            "season_sort":start,"date":row.try_get::<&str,_>("date")?,"qual_type_label":type_name(kind),
            "public_tournament_url":format!("/core/tournaments/{id}"),"audit_detail_url":detail_url(id),
            "recorded_team_count":teams,"recorded_num_rounds":rounds,"recorded_prelim":total-outs,"expected_prelim":expected_prelim,
            "prelim_pct":percent(total-outs,expected_prelim),"recorded_outround":outs,"expected_outround":expected_out,
            "outround_pct":percent(outs,expected_out),"total_pct":percent(total,expected_prelim+expected_out),
            "linked_import_count":row.try_get::<i64,_>("imports")?,
        }));
    }
    Ok(data)
}

async fn pipeline(state: &AppState, uri: &Uri) -> Result<minijinja::Value, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let mut rows = audit(&mut tx, None).await?;
    tx.commit().await?;
    let params = url::form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<BTreeMap<_, _>>();
    let get = |key| params.get(key).map_or("", String::as_str).trim();
    let requested = get("sort").to_ascii_lowercase();
    let descending = get("dir").eq_ignore_ascii_case("desc");
    let incomplete = get("incomplete_only") == "1";
    let is_incomplete = |row: &Value| {
        row["total_pct"]
            .as_f64()
            .is_some_and(|percent| percent < 100.0)
    };
    if incomplete {
        rows.retain(is_incomplete);
    }
    if let Some((_, field)) = SORTS.iter().find(|(name, _)| *name == requested) {
        rows.sort_by(|left, right| {
            let values = (&left[*field], &right[*field]);
            let cmp = match values {
                (Value::String(a), Value::String(b)) => a.to_lowercase().cmp(&b.to_lowercase()),
                (Value::Number(a), Value::Number(b)) => a
                    .as_f64()
                    .partial_cmp(&b.as_f64())
                    .unwrap_or(Ordering::Equal),
                _ => Ordering::Equal,
            };
            let ordering = cmp
                .then_with(|| left["date"].as_str().cmp(&right["date"].as_str()))
                .then_with(|| {
                    left["tournament_id"]
                        .as_i64()
                        .cmp(&right["tournament_id"].as_i64())
                });
            if descending {
                ordering.reverse()
            } else {
                ordering
            }
        });
    }
    let headers = SORTS
        .into_iter()
        .map(|(key, _)| {
            let active = key == requested;
            let (next, indicator) = match (active, descending) {
                (true, true) => ("asc", " [desc]"),
                (true, false) => ("desc", " [asc]"),
                _ => ("asc", ""),
            };
            let prefix = if incomplete { "incomplete_only=1&" } else { "" };
            (
                key,
                json!({"url":format!("?{prefix}sort={key}&dir={next}"),"indicator":indicator}),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let incomplete_count = rows.iter().filter(|row| is_incomplete(row)).count();
    let summary = json!({
        "total_tournaments":rows.len(),"incomplete_tournaments":incomplete_count,
        "complete_tournaments":rows.len()-incomplete_count,
        "linked_imports":rows.iter().filter_map(|row|row["linked_import_count"].as_i64()).sum::<i64>(),
    });
    Ok(
        context! {rows,summary,show_incomplete_only=>incomplete,sort_headers=>headers,included_type_labels=>TYPES.map(type_name)},
    )
}

fn label(origin: &str, imported: bool) -> String {
    if origin.is_empty() {
        return if imported { "Imported" } else { "Manual" }.to_owned();
    }
    origin
        .split('_')
        .map(|word| {
            let mut chars = word.chars();
            chars.next().map_or_else(String::new, |first| {
                first
                    .to_uppercase()
                    .chain(chars.flat_map(char::to_lowercase))
                    .collect::<String>()
            })
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn team_name(debaters: &[Value], fallback: &str) -> String {
    let school = |index: usize| {
        debaters[index]["school"]["name"]
            .as_str()
            .unwrap_or("Unaffiliated")
    };
    match debaters {
        [first, last] => {
            let home = if school(0) == school(1) {
                school(0).to_owned()
            } else {
                format!("{} / {}", school(0), school(1))
            };
            let person = |row: &Value| {
                format!(
                    "{} {}",
                    row["first_name"].as_str().unwrap_or_default(),
                    row["last_name"].as_str().unwrap_or_default()
                )
            };
            format!("{home} {} and {}", person(first), person(last))
        }
        [first, ..] => format!(
            "{} {} {}",
            school(0),
            first["first_name"].as_str().unwrap_or_default(),
            first["last_name"].as_str().unwrap_or_default()
        ),
        [] => fallback.to_owned(),
    }
}

async fn detail(state: &AppState, id: i64) -> Result<minijinja::Value, ApiError> {
    let mut tx = read_transaction(&state.pool).await?;
    let event = sqlx::query(
        "SELECT name,season,CAST(date AS TEXT) AS date FROM core_tournament WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ApiError::NotFound("Tournament not found."))?;
    let tournament = json!({"id":id,"name":event.try_get::<&str,_>("name")?,"season":event.try_get::<&str,_>("season")?,
        "date":event.try_get::<&str,_>("date")?,"url":format!("/core/tournaments/{id}")});
    let audit_row = audit(&mut tx, Some(id)).await?.into_iter().next();
    let rounds=sqlx::query("SELECT CAST(r.id AS BIGINT) AS id,CAST(r.gov_id AS BIGINT) AS gov_id,CAST(r.opp_id AS BIGINT) AS opp_id,
        CAST(r.round_number AS BIGINT) AS number,r.stage,r.round_label,r.import_origin,CAST(r.victor AS BIGINT) AS victor,
        CAST(r.metadata AS TEXT) AS metadata,g.name AS gov_name,o.name AS opp_name,
        CAST(CASE WHEN m.id IS NOT NULL THEN 1 ELSE 0 END AS BIGINT) AS imported
        FROM core_round r JOIN core_team g ON g.id=r.gov_id JOIN core_team o ON o.id=r.opp_id
        LEFT JOIN core_importedroundmetadata m ON m.round_id=r.id WHERE r.tournament_id=$1 ORDER BY r.stage,r.round_number,r.id")
        .bind(id).fetch_all(&mut *tx).await?;
    let mut teams = BTreeSet::new();
    for round in &rounds {
        teams.insert(round.try_get::<i64, _>("gov_id")?);
        teams.insert(round.try_get::<i64, _>("opp_id")?);
    }
    let people = members_for(&mut tx, &teams.into_iter().collect::<Vec<_>>(), "").await?;
    let mut round_rows = Vec::new();
    let mut imported_round_count = 0;
    for round in rounds {
        let pk = round.try_get::<i64, _>("id")?;
        let origin = round.try_get::<&str, _>("import_origin")?.trim();
        let has_meta = round.try_get::<i64, _>("imported")? == 1;
        if has_meta || (!origin.is_empty() && !origin.eq_ignore_ascii_case("manual")) {
            imported_round_count += 1;
        }
        let stage = round.try_get::<&str, _>("stage")?;
        let metadata = serde_json::from_str::<Value>(round.try_get("metadata")?)?;
        let victor = round.try_get::<i64, _>("victor")?;
        let result = [
            "UNKNOWN",
            "GOV",
            "OPP",
            "GOV via Forfeit",
            "OPP via Forfeit",
            "ALL DROP",
            "ALL WIN",
            "BYE",
        ];
        let victor = result
            .get(victor as usize)
            .map_or_else(|| victor.to_string(), |value| (*value).to_owned());
        round_rows.push(json!({
            "id":pk,"stage_label":match stage {"prelim"=>"Prelim","outround"=>"Outround",_=>stage},
            "round_number":round.try_get::<i64,_>("number")?,"canonical_round_name":round.try_get::<&str,_>("round_label")?.trim(),
            "source_round_name":metadata["source_round_name"].as_str().unwrap_or_default().trim(),
            "gov_name":team_name(people.get(&round.try_get("gov_id")?).map_or(&[][..],Vec::as_slice),round.try_get("gov_name")?),
            "opp_name":team_name(people.get(&round.try_get("opp_id")?).map_or(&[][..],Vec::as_slice),round.try_get("opp_name")?),
            "victor_label":victor,"origin_label":label(origin,has_meta),
            "modify_url":format!("{BASE}/{id}/round/{pk}/"),"delete_url":format!("{BASE}/{id}/round/{pk}/delete/"),
        }));
    }
    let mut import_rows = Vec::new();
    for row in sqlx::query("SELECT CAST(i.id AS BIGINT) AS id,i.import_type,i.original_file_name,i.source_hash,
        CAST(i.imported_at AS TEXT) AS imported_at,
        (SELECT COUNT(*) FROM core_importedroundmetadata_sources s WHERE s.tournamentimport_id=i.id) AS links
        FROM core_tournamentimport i WHERE i.tournament_id=$1 ORDER BY i.imported_at DESC,i.id DESC")
        .bind(id).fetch_all(&mut *tx).await?{
        let source=row.try_get::<i64,_>("id")?;
        let import_type=row.try_get::<&str,_>("import_type")?;
        let date=row.try_get::<&str,_>("imported_at")?;
        let date=NaiveDateTime::parse_from_str(date.split('+').next().unwrap_or(date),"%Y-%m-%d %H:%M:%S%.f")
            .map_or_else(|_|date.to_owned(),|date|date.format("%Y-%m-%d %H:%M").to_string());
        import_rows.push(json!({
            "id":source,"import_type_label":match import_type {"file_backup"=>"File Backup","forum_post"=>"Forum Post",
                "mittab_bundle"=>"Mit-Tab Bundle","db_inference"=>"Inferred From DB","other"=>"Other",_=>import_type},
            "original_file_name":row.try_get::<&str,_>("original_file_name")?,"source_hash":row.try_get::<&str,_>("source_hash")?,
            "imported_at":date,"linked_round_count":row.try_get::<i64,_>("links")?,
            "delete_url":format!("{BASE}/{id}/imports/{source}/delete/"),"move_url":format!("{BASE}/{id}/imports/{source}/move/"),
        }));
    }
    tx.commit().await?;
    let manual_round_count = round_rows.len() - imported_round_count;
    Ok(
        context! {tournament,audit_row,round_rows,import_rows,imported_round_count,manual_round_count,
        add_round_url=>format!("{BASE}/{id}/round/new/"),round_amendment_recording=>super::recording_context()},
    )
}

pub(super) fn render(
    state: &AppState,
    user: &User,
    uri: &Uri,
    headers: &HeaderMap,
    template: &str,
    fields: minijinja::Value,
) -> Response {
    let common = web::base_context(state, user, uri);
    let secure = state
        .origin(headers)
        .is_some_and(|url| url.starts_with("https://"));
    let Ok((cookie, csrf)) = login::csrf_tokens(headers) else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let messages = state
        .secret
        .as_deref()
        .map(|secret| flash::alerts(headers, secret))
        .unwrap_or_default();
    let security = context! {csrf,messages};
    let html = match web::render(template, context! {..fields,..security,..common}) {
        Ok(html) => html,
        Err(error) => {
            eprintln!("Tournament audit template failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let mut response = ([(header::VARY, "Cookie")], Html(html)).into_response();
    login::set_csrf_cookie(&mut response, &cookie, secure);
    if !messages.is_empty() {
        flash::clear(&mut response, secure);
    }
    response
}

async fn page(state: &AppState, uri: &Uri, headers: &HeaderMap, id: Option<i64>) -> Response {
    let user = match super::require(state, uri, headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let (template, data) = if let Some(id) = id {
        (
            "admin/tournament_audit_detail.html",
            detail(state, id).await,
        )
    } else {
        (
            "admin/tournament_pipeline_audit.html",
            pipeline(state, uri).await,
        )
    };
    match data {
        Ok(data) => render(state, &user, uri, headers, template, data),
        Err(ApiError::NotFound(_)) => StatusCode::NOT_FOUND.into_response(),
        Err(error) => {
            eprintln!("Tournament audit query failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn overview(
    State(state): State<AppState>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    page(&state, &uri, &headers, None).await
}

pub(crate) async fn event(
    State(state): State<AppState>,
    Path(id): Path<u64>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Ok(id) = i64::try_from(id) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    page(&state, &uri, &headers, Some(id)).await
}
