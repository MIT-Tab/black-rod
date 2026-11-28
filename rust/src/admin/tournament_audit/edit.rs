use std::collections::{BTreeMap, BTreeSet};

use axum::{
    extract::{Path, Request, State},
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use minijinja::context;
use rust_decimal::Decimal;
use serde_json::{Map, Value, json};
use sqlx::{AnyConnection, Row};

use crate::{
    ApiError, AppState,
    admin::{
        self,
        amendment::{self, Error, record, round::SLOTS},
    },
    agent::journal::TABLES,
    flash, login,
    team_profile::members_for,
};

use super::{BASE, detail_url, render};

const STAGES: [(i64, &str); 7] = [
    (2, "Final"),
    (4, "Semifinal"),
    (8, "Quarterfinal"),
    (16, "Octafinal"),
    (32, "Double Octafinal"),
    (64, "Triple Octafinal"),
    (128, "Quadruple Octafinal"),
];
const VICTORS: [&str; 8] = [
    "UNKNOWN",
    "GOV",
    "OPP",
    "GOV via Forfeit",
    "OPP via Forfeit",
    "ALL DROP",
    "ALL WIN",
    "BYE",
];

fn field(name: &str, kind: &str, required: bool, value: &str) -> Value {
    json!({"name":name,"kind":kind,"required":required,"value":value,"choices":[],"errors":[],"step":"","min":null,"limit":0})
}

fn select(name: &str, value: &str, choices: &[(&str, &str)], required: bool) -> Value {
    let mut item = field(name, "select", required, value);
    item["choices"] = json!(choices);
    item
}

async fn event(conn: &mut AnyConnection, id: i64) -> Result<Option<Value>, Error> {
    let row = sqlx::query("SELECT name,season FROM core_tournament WHERE id=$1")
        .bind(id)
        .fetch_optional(conn)
        .await?;
    row.map(|row|Ok(json!({"id":id,"name":row.try_get::<&str,_>("name")?,"season":row.try_get::<&str,_>("season")?}))).transpose()
}

type Inputs = BTreeMap<String, String>;

#[derive(Default)]
struct Aggregate {
    role: String,
    speaker_name: String,
    scores: [Vec<Decimal>; 2],
}

fn display(value: &Value) -> String {
    if value.is_null() {
        String::new()
    } else {
        value
            .as_str()
            .map_or_else(|| value.to_string(), str::to_owned)
    }
}

async fn initial(conn: &mut AnyConnection, round: &Value) -> Result<Inputs, Error> {
    let meta = &round["metadata"];
    let mut values = Inputs::new();
    for (field, value) in [
        ("canonical_round_name", &round["round_label"]),
        ("source_round_name", &meta["source_round_name"]),
        ("stage", &round["stage"]),
        ("outround_stage", &round["elim_size"]),
        ("round_number", &round["round_number"]),
        ("victor", &round["victor"]),
    ] {
        values.insert(field.to_owned(), display(value));
    }
    let id = amendment::integer(&round["id"]).ok_or(ApiError::Serialization)?;
    let imported = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_importedroundmetadata WHERE round_id=$1",
    )
    .bind(id)
    .fetch_optional(&mut *conn)
    .await?;
    let aliases = if let Some(id) = imported {
        TABLES["core.ImportedRoundMetadata"].get(conn, id).await?
    } else {
        Value::Null
    };
    let raw=sqlx::query("SELECT CAST(debater_id AS BIGINT) AS id,debater_role,
        CAST(speaks AS TEXT) AS speaks,CAST(ranks AS TEXT) AS ranks,CAST(metadata AS TEXT) AS metadata
        FROM core_roundstats WHERE round_id=$1 ORDER BY score_index,id").bind(id).fetch_all(&mut *conn).await?;
    let mut stats = BTreeMap::<i64, Aggregate>::new();
    for row in raw {
        let id = row.try_get("id")?;
        let stat = stats.entry(id).or_insert_with(|| Aggregate {
            role: row
                .try_get::<Option<String>, _>("debater_role")
                .ok()
                .flatten()
                .unwrap_or_default(),
            speaker_name: row
                .try_get::<&str, _>("metadata")
                .ok()
                .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                .map(|metadata| amendment::text(&metadata["speaker_name"]))
                .unwrap_or_default(),
            ..Default::default()
        });
        for (index, key) in ["speaks", "ranks"].into_iter().enumerate() {
            if let Some(score) = row
                .try_get::<Option<&str>, _>(key)?
                .and_then(|value| value.parse::<Decimal>().ok())
            {
                stat.scores[index].push(score);
            }
        }
    }
    let sides = [
        amendment::integer(&round["gov_id"]).ok_or(ApiError::Serialization)?,
        amendment::integer(&round["opp_id"]).ok_or(ApiError::Serialization)?,
    ];
    let members = members_for(conn, &sides, "").await?;
    for (index, (slot, alias_field)) in SLOTS.into_iter().enumerate() {
        let side = index / 2;
        let ordered_key = if side == 0 {
            "team_a_ids"
        } else {
            "team_b_ids"
        };
        let name_key = if side == 0 {
            "team_a_names"
        } else {
            "team_b_names"
        };
        let order = meta[ordered_key]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter_map(amendment::integer)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let mut roster = members[&sides[side]]
            .iter()
            .filter_map(|person| person["id"].as_i64())
            .collect::<Vec<_>>();
        roster.sort_by_key(|id| {
            (
                order
                    .iter()
                    .position(|value| value == id)
                    .unwrap_or(usize::MAX),
                *id,
            )
        });
        let alias = if let Some(id) = amendment::integer(&aliases[alias_field]) {
            TABLES["core.DebaterAlias"].get(conn, id).await?
        } else {
            Value::Null
        };
        let identity =
            amendment::integer(&alias["debater_id"]).or_else(|| roster.get(index % 2).copied());
        let person = if let Some(id) = identity {
            values.insert(format!("{slot}_debater"), id.to_string());
            sqlx::query_scalar::<_, String>(
                "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1",
            )
            .bind(id)
            .fetch_optional(&mut *conn)
            .await?
            .unwrap_or_default()
        } else {
            String::new()
        };
        let source = amendment::text(&alias["source_name"]);
        let stat = identity.and_then(|id| stats.get(&id));
        let raw_source = meta[name_key]
            .as_array()
            .and_then(|rows| {
                rows.iter()
                    .filter_map(Value::as_str)
                    .filter(|name| !name.trim().is_empty())
                    .nth(index % 2)
            })
            .unwrap_or_default();
        let from_stats = stat.map_or("", |row| row.speaker_name.as_str());
        let source = [source.as_str(), from_stats, raw_source, &person]
            .into_iter()
            .find(|name| !name.is_empty())
            .unwrap_or_default();
        values.insert(format!("{slot}_source_name"), source.to_owned());
        if let Some(stat) = stat {
            values.insert(format!("{slot}_role"), stat.role.clone());
            for (index, key) in ["speaks", "ranks"].into_iter().enumerate() {
                let scores = &stat.scores[index];
                if !scores.is_empty() {
                    let score = scores.iter().sum::<Decimal>() / Decimal::from(scores.len());
                    values.insert(format!("{slot}_{key}"), format!("{:.4}", score.round_dp(4)));
                }
            }
        }
    }
    Ok(values)
}

async fn person_choice(
    conn: &mut AnyConnection,
    id: i64,
) -> Result<Option<(String, Option<i64>)>, Error> {
    let row=sqlx::query("SELECT TRIM(first_name || ' ' || last_name) AS name,CAST(school_id AS BIGINT) AS school,
        CAST(CASE WHEN temporary THEN 1 ELSE 0 END AS BIGINT) AS temporary FROM core_debater WHERE id=$1")
        .bind(id).fetch_optional(conn).await?;
    row.map(|row| {
        let mut name = row.try_get::<String, _>("name")?;
        if row.try_get::<i64, _>("temporary")? == 1 {
            name.push_str(" (New)");
        }
        Ok((name, row.try_get("school")?))
    })
    .transpose()
}

fn failure(field: &mut Value, message: impl Into<String>) {
    if let Some(rows) = field["errors"].as_array_mut() {
        rows.push(json!(message.into()));
    }
}

struct Form {
    fields: BTreeMap<String, Value>,
    slots: Vec<Value>,
    names: Vec<String>,
    people: Vec<i64>,
}

impl Form {
    async fn build(
        conn: &mut AnyConnection,
        values: &Inputs,
        submitted: bool,
    ) -> Result<Self, Error> {
        let get = |name: &str| values.get(name).map_or("", String::as_str).trim();
        let mut fields = BTreeMap::new();
        for (name, limit) in [("canonical_round_name", 32), ("source_round_name", 128)] {
            let mut item = field(name, "text", false, get(name));
            item["limit"] = json!(limit);
            if submitted && get(name).chars().count() > limit {
                failure(
                    &mut item,
                    format!("Ensure this value has at most {limit} characters."),
                );
            }
            fields.insert(name.to_owned(), item);
        }
        let mut number = field("round_number", "number", false, get("round_number"));
        number["min"] = json!(1);
        if submitted
            && !get("round_number").is_empty()
            && !get("round_number")
                .parse::<i64>()
                .is_ok_and(|value| value >= 1)
        {
            failure(
                &mut number,
                "Ensure this value is greater than or equal to 1.",
            );
        }
        fields.insert("round_number".into(), number);
        let mut stage = select(
            "stage",
            get("stage"),
            &[("prelim", "Prelim"), ("outround", "Outround")],
            true,
        );
        if submitted && !matches!(get("stage"), "prelim" | "outround") {
            failure(&mut stage, "Select a valid choice.");
        }
        fields.insert("stage".into(), stage);
        let mut elim = field("outround_stage", "select", false, get("outround_stage"));
        elim["choices"] = json!(
            std::iter::once(("".to_owned(), "---------"))
                .chain(
                    STAGES
                        .into_iter()
                        .map(|(value, label)| (value.to_string(), label))
                )
                .collect::<Vec<_>>()
        );
        if submitted
            && !get("outround_stage").is_empty()
            && !get("outround_stage")
                .parse::<i64>()
                .is_ok_and(|value| STAGES.iter().any(|&(size, _)| size == value))
        {
            failure(&mut elim, "Select a valid choice.");
        }
        fields.insert("outround_stage".into(), elim);
        let mut victor = field("victor", "select", true, get("victor"));
        victor["choices"] = json!(
            VICTORS
                .into_iter()
                .enumerate()
                .map(|(id, label)| (id.to_string(), label))
                .collect::<Vec<_>>()
        );
        if submitted
            && !get("victor")
                .parse::<usize>()
                .is_ok_and(|value| value < VICTORS.len())
        {
            failure(&mut victor, "Select a valid choice.");
        }
        fields.insert("victor".into(), victor);
        let mut slots = Vec::new();
        let mut roster = Vec::new();
        let mut names = Vec::new();
        let mut ids = BTreeSet::new();
        let mut roles = [BTreeSet::new(), BTreeSet::new()];
        for (index, (key, _)) in SLOTS.into_iter().enumerate() {
            let label = ["Gov 1", "Gov 2", "Opp 1", "Opp 2"][index];
            let input = |suffix: &str| format!("{key}_{suffix}");
            let field_name = input("debater");
            let mut debater = field(&field_name, "person", true, get(&field_name));
            let id = get(&field_name).parse::<i64>().ok();
            let person = if let Some(id) = id {
                person_choice(conn, id).await?
            } else {
                None
            };
            let mut choices = vec![("".to_owned(), "---------".to_owned())];
            if let Some((name, school)) = &person {
                choices.push((get(&field_name).to_owned(), name.clone()));
                if submitted {
                    if !ids.insert(id) {
                        failure(
                            &mut debater,
                            "Each ballot slot must use a different debater.",
                        );
                    } else if school.is_none() {
                        failure(
                            &mut debater,
                            "Selected debaters need a school so a canonical team can be built.",
                        );
                    }
                }
            } else if submitted {
                failure(
                    &mut debater,
                    if get(&field_name).is_empty() {
                        "This field is required."
                    } else {
                        "Select a valid choice."
                    },
                );
            }
            debater["choices"] = json!(choices);
            let field_name = input("source_name");
            let mut source_name = field(&field_name, "text", false, get(&field_name));
            source_name["limit"] = json!(128);
            if submitted && get(&field_name).chars().count() > 128 {
                failure(
                    &mut source_name,
                    "Ensure this value has at most 128 characters.",
                );
            }
            names.push(if get(&field_name).is_empty() {
                person
                    .map(|(name, _)| name.trim_end_matches(" (New)").to_owned())
                    .unwrap_or_default()
            } else {
                get(&field_name).to_owned()
            });
            roster.push(id.unwrap_or_default());
            let pairs = if index < 2 {
                [("", "---------"), ("PM", "PM"), ("MG", "MG")]
            } else {
                [("", "---------"), ("LO", "LO"), ("MO", "MO")]
            };
            let field_name = input("role");
            let mut role = select(&field_name, get(&field_name), &pairs, false);
            if submitted && !pairs.iter().any(|(value, _)| *value == get(&field_name)) {
                failure(&mut role, "Select a valid choice.");
            } else if submitted
                && !get(&field_name).is_empty()
                && !roles[index / 2].insert(get(&field_name).to_owned())
            {
                failure(&mut role, "Each side can only use a role once.");
            }
            let score = |kind: &str| {
                let field_name = input(kind);
                let raw = get(&field_name);
                let mut item = field(&field_name, "number", false, raw);
                item["step"] = json!("0.0001");
                if submitted && !raw.is_empty() {
                    match raw.parse::<Decimal>() {
                        Err(_) => failure(&mut item, "Enter a number."),
                        Ok(value) if value.scale() > 4 => failure(
                            &mut item,
                            "Ensure that there are no more than 4 decimal places.",
                        ),
                        Ok(value) if value.abs() >= Decimal::from(100) => failure(
                            &mut item,
                            "Ensure that there are no more than 2 digits before the decimal point.",
                        ),
                        _ => (),
                    }
                }
                item
            };
            slots.push(
                json!({"label":label,"debater":debater,"source_name":source_name,"role":role,
                "speaks":score("speaks"),"ranks":score("ranks")}),
            );
        }
        Ok(Self {
            fields,
            slots,
            names,
            people: roster,
        })
    }
    fn valid(&self) -> bool {
        let valid = |item: &Value| item["errors"].as_array().is_none_or(Vec::is_empty);
        self.fields.values().all(valid)
            && self.slots.iter().all(|row| {
                row.as_object()
                    .into_iter()
                    .flatten()
                    .all(|(_, field)| valid(field))
            })
    }
}

async fn action(
    conn: &mut AnyConnection,
    event: i64,
    round: &Value,
    form: &Form,
    values: &Inputs,
) -> Result<Value, Error> {
    let get = |name: &str| values.get(name).map_or("", String::as_str).trim();
    let mut metadata = round["metadata"].as_object().cloned().unwrap_or_default();
    let stage = get("stage");
    let number = get("round_number").parse::<i64>().unwrap_or_default();
    let size = get("outround_stage")
        .parse::<i64>()
        .ok()
        .filter(|_| stage == "outround");
    let label = get("canonical_round_name");
    let canonical = if label.is_empty() {
        if let Some((_, stage)) = STAGES.iter().find(|(value, _)| size == Some(*value)) {
            (*stage).to_owned()
        } else {
            format!(
                "{}{}",
                if stage == "outround" { "E" } else { "P" },
                if number == 0 {
                    "?".to_owned()
                } else {
                    number.to_string()
                }
            )
        }
    } else {
        label.to_owned()
    };
    if get("source_round_name").is_empty() {
        metadata.remove("source_round_name");
    } else {
        metadata.insert("source_round_name".into(), json!(get("source_round_name")));
    }
    for (key, value) in [
        ("round_label", json!(canonical)),
        ("stage", json!(stage)),
        ("team_a_ids", json!(&form.people[..2])),
        ("team_b_ids", json!(&form.people[2..])),
        ("team_a_names", json!(&form.names[..2])),
        ("team_b_names", json!(&form.names[2..])),
    ] {
        metadata.insert(key.into(), value);
    }
    let id = amendment::integer(&round["id"]);
    let mut key = amendment::text(&round["import_key"]);
    if id.is_none() && key.trim().is_empty() && record::enabled() {
        let token = uuid::Uuid::new_v4().simple().to_string();
        key = format!("manual-amendment-{}", &token[..24]);
    }
    let origin = round["import_origin"]
        .as_str()
        .filter(|value| !value.is_empty())
        .unwrap_or("manual");
    let mut stats = Vec::new();
    let mut existing = BTreeMap::new();
    if let Some(id) = id {
        for row in sqlx::query(
            "SELECT debater_role,source_status,CAST(metadata AS TEXT) AS metadata
            FROM core_roundstats WHERE round_id=$1 AND debater_role IS NOT NULL ORDER BY id",
        )
        .bind(id)
        .fetch_all(&mut *conn)
        .await?
        {
            existing
                .entry(
                    row.try_get::<String, _>("debater_role")?
                        .trim()
                        .to_ascii_uppercase(),
                )
                .or_insert((
                    row.try_get::<String, _>("source_status")?,
                    serde_json::from_str::<Value>(row.try_get("metadata")?)?,
                ));
        }
    }
    let mut imported = Map::new();
    for (index, (slot, _)) in SLOTS.into_iter().enumerate() {
        let role = get(&format!("{slot}_role"));
        let (status, meta) = existing.get(role).map_or(("", None), |(status, meta)| {
            (status.as_str(), meta.as_object())
        });
        let mut meta = meta.cloned().unwrap_or_default();
        meta.insert("speaker_name".into(), json!(form.names[index]));
        let score = |kind| {
            let name = format!("{slot}_{kind}");
            let value = get(&name);
            (!value.is_empty() && stage != "outround").then(|| value.to_owned())
        };
        stats.push(json!({"debater_id":form.people[index],"score_index":1,"speaks":score("speaks"),"ranks":score("ranks"),
            "debater_role":if stage=="outround"{""}else{role},"source_status":status,"metadata":meta}));
        imported.insert(
            slot.to_owned(),
            json!({"debater_id":form.people[index],"source_name":form.names[index]}),
        );
    }
    let imported_already = id.is_some()
        && sqlx::query_scalar::<_, i64>(
            "SELECT CAST(id AS BIGINT) FROM core_importedroundmetadata WHERE round_id=$1",
        )
        .bind(id)
        .fetch_optional(conn)
        .await?
        .is_some();
    let mut action = json!({"type":if id.is_some(){"update_round"}else{"create_round"},"tournament_id":event,"round_number":number,
        "stage":stage,"elim_size":size,"round_label":canonical,"victor":get("victor").parse::<i64>().unwrap_or_default(),
        "metadata":metadata,"import_origin":origin,"import_key":key,
        "gov":{"debater_ids":&form.people[..2]},"opp":{"debater_ids":&form.people[2..]},"stats":stats});
    if let Some(id) = id {
        action["round_id"] = json!(id);
    }
    if imported_already || !origin.eq_ignore_ascii_case("manual") {
        action["imported_metadata"] = json!(imported);
    }
    Ok(action)
}

fn pair(path: &str) -> Option<(i64, Option<i64>)> {
    let suffix = path
        .strip_prefix(BASE)?
        .strip_prefix('/')?
        .trim_end_matches('/');
    let mut segments = suffix.split('/');
    let event = segments.next()?.parse::<i64>().ok()?;
    if segments.next() != Some("round") {
        return None;
    }
    let round = match segments.next()? {
        "new" => None,
        raw => Some(raw.parse().ok()?),
    };
    Some((event, round))
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    let uri = request.uri().clone();
    let headers = request.headers().clone();
    let user = match admin::require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let Some((id, round_id)) = pair(uri.path()) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let secure = origin.starts_with("https://");
    let submitted = request.method() == Method::POST;
    let result=async {
        let mut tx=state.pool.begin().await?;
        let Some(tournament)=event(&mut tx,id).await? else{return Ok(StatusCode::NOT_FOUND.into_response());};
        let round=if let Some(round_id)=round_id {
            let row=TABLES["core.Round"].get(&mut tx,round_id).await?;
            if amendment::integer(&row["tournament_id"])!=Some(id){return Ok(StatusCode::NOT_FOUND.into_response());}
            row
        }else{Value::Null};
        let values=if submitted {
            let values=match login::parse_form(&headers,request.into_body(),2_621_440).await {
                Ok(values)=>values.into_iter().collect::<Inputs>(),
                Err(status)=>return Ok(status.into_response()),
            };
            if !login::csrf_valid(&headers,values.get("csrfmiddlewaretoken").map(String::as_str),&origin){
                return Ok(login::csrf_failure());
            }
            values
        }else if round_id.is_some(){initial(&mut tx,&round).await?}else{Inputs::new()};
        let form=Form::build(&mut tx,&values,submitted).await?;
        if submitted && form.valid(){
            let mut action=action(&mut tx,id,&round,&form,&values).await?;
            amendment::execute(&mut tx,std::slice::from_ref(&action),state.current_season,user.id,&json!({"source":"tournament_audit_round_edit"})).await?;
            tx.commit().await?;
            amendment::invalidate(&state).await;
            let mut response=(StatusCode::FOUND,[(header::LOCATION,detail_url(id))]).into_response();
            if let Some(secret)=state.secret.as_deref(){
                flash::push(&mut response,&headers,secret,25,
                    &format!("Saved round {} for {}.",amendment::text(&action["round_label"]),amendment::text(&tournament["name"])),secure);
            }
            if !amendment::text(&action["import_key"]).is_empty() && let Some(values)=action.as_object_mut(){values.remove("round_id");}
            notice_record(&state,&headers,&mut response,&action,secure,"Round was saved").await;
            return Ok(response);
        }
        tx.rollback().await?;
        let bindings=context!{tournament,round_obj=>round_id,fields=>form.fields,rows=>form.slots,
            errors=>Vec::<String>::new(),audit_detail_url=>detail_url(id),round_amendment_recording=>admin::recording_context()};
        Ok(render(&state,&user,&uri,&headers,"admin/tournament_round_ballot.html",bindings))
    }.await;
    result.unwrap_or_else(|error: Error| {
        eprintln!("Tournament round form failed: {}", error.message());
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}

async fn notice_record(
    state: &AppState,
    headers: &axum::http::HeaderMap,
    response: &mut Response,
    action: &Value,
    secure: bool,
    status: &str,
) {
    let note = match record::append(action).await {
        Ok(Some(path)) => Some((
            20,
            format!(
                "Recorded {} amendment to {}.",
                amendment::text(&action["type"]),
                path.display()
            ),
        )),
        Err(error) => Some((
            30,
            format!("{status} but amendment recording failed: {error}"),
        )),
        Ok(None) => None,
    };
    if let Some((level, message)) = note
        && let Some(secret) = state.secret.as_deref()
    {
        flash::push(response, headers, secret, level, &message, secure);
    }
}

pub(crate) async fn change(
    State(state): State<AppState>,
    Path((tournament, object)): Path<(i64, i64)>,
    request: Request,
) -> Response {
    let uri = request.uri().clone();
    let headers = request.headers().clone();
    let user = match admin::require(&state, &uri, &headers).await {
        Ok(user) => user,
        Err(response) => return *response,
    };
    let Some(origin) = state.origin(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let secure = origin.starts_with("https://");
    let fields = match login::parse_form(&headers, request.into_body(), 2_621_440).await {
        Ok(fields) => fields,
        Err(status) => return status.into_response(),
    };
    if !login::csrf_valid(
        &headers,
        fields.get("csrfmiddlewaretoken").map(String::as_str),
        &origin,
    ) {
        return login::csrf_failure();
    }
    let import = uri.path().contains("/imports/");
    let move_import = uri.path().ends_with("/move/");
    let result = async {
        let mut tx = state.pool.begin().await?;
        let Some(event) = event(&mut tx, tournament).await? else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        let table = if import {
            "core.TournamentImport"
        } else {
            "core.Round"
        };
        let row = TABLES[table].get(&mut tx, object).await?;
        if amendment::integer(&row["tournament_id"]) != Some(tournament) {
            return Ok(StatusCode::NOT_FOUND.into_response());
        }
        let mut response = (
            StatusCode::FOUND,
            [(header::LOCATION, detail_url(tournament))],
        )
            .into_response();
        let flash = |response: &mut Response, level, message: &str| {
            if let Some(secret) = state.secret.as_deref() {
                flash::push(response, &headers, secret, level, message, secure);
            }
        };
        let event_name = amendment::text(&event["name"]);
        let (action, message, status) = if move_import {
            let target = fields
                .get(&format!("import-{object}-target_tournament"))
                .and_then(|value| value.parse::<i64>().ok())
                .filter(|&id| id != tournament);
            let destination = if let Some(target) = target {
                self::event(&mut tx, target).await?
            } else {
                None
            };
            let Some((id, destination)) = target.zip(destination) else {
                flash(
                    &mut response,
                    40,
                    "Please choose a valid target tournament.",
                );
                return Ok(response);
            };
            (
                json!({"type":"move_tournament_import","id":object,"target_tournament_id":id}),
                format!(
                    "Moved tournament import {object} from {event_name} to {}.",
                    amendment::text(&destination["name"])
                ),
                "Tournament import was moved",
            )
        } else if import {
            (
                json!({"type":"delete_tournament_import","id":object}),
                format!("Deleted tournament import {object} from {event_name}."),
                "Tournament import was deleted",
            )
        } else {
            let label = amendment::text(&row["round_label"]);
            let label = if label.is_empty() {
                object.to_string()
            } else {
                label
            };
            let key = amendment::text(&row["import_key"]);
            let action = if key.trim().is_empty() {
                json!({"type":"delete_round","round_id":object})
            } else {
                json!({"type":"delete_round","tournament_id":tournament,"import_key":key})
            };
            (
                action,
                format!("Deleted round {label} from {event_name}."),
                "Round was deleted",
            )
        };
        amendment::execute(
            &mut tx,
            std::slice::from_ref(&action),
            state.current_season,
            user.id,
            &json!({"source":action["type"]}),
        )
        .await?;
        tx.commit().await?;
        amendment::invalidate(&state).await;
        flash(&mut response, 25, &message);
        notice_record(&state, &headers, &mut response, &action, secure, status).await;
        Ok(response)
    }
    .await;
    result.unwrap_or_else(|error: Error| {
        eprintln!("Tournament audit update failed: {}", error.message());
        StatusCode::BAD_REQUEST.into_response()
    })
}
