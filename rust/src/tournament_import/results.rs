mod forms;
mod persist;
mod review;
pub(crate) mod standings;

use axum::{
    Json,
    body::to_bytes,
    extract::{OriginalUri, Request, State},
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};
use sqlx::Row;
use url::{Url, form_urlencoded};

use super::{Error, source};
use crate::{
    ApiError, AppState, flash, login,
    session::{self, User},
    web,
};
use forms::{FormValues, Panel, ResultRow, TABS, Tab};

const PAGE: &str = "/core/tournaments/data_entry";

async fn requester(
    state: &AppState,
    uri: &Uri,
    headers: &HeaderMap,
) -> Result<User, Box<Response>> {
    let user = session::user(&state.pool, headers, state.secret.as_deref())
        .await
        .map_err(|_| Box::new(StatusCode::INTERNAL_SERVER_ERROR.into_response()))?;
    if !user.is_authenticated {
        return Err(Box::new(login::redirect_to_login(uri)));
    }
    if !session::has_permission(&state.pool, &user, "change_tournament")
        .await
        .map_err(|_| Box::new(StatusCode::INTERNAL_SERVER_ERROR.into_response()))?
    {
        return Err(Box::new(web::permission_denied(state, &user, uri)));
    }
    Ok(user)
}

fn safe_url(input: &str) -> Option<String> {
    let input = input.trim();
    if input.is_empty() {
        return Some(String::new());
    }
    let input = if input.contains("://") {
        input.to_owned()
    } else {
        format!("http://{input}")
    };
    if input.chars().count() > 2048 {
        return None;
    }
    Url::parse(&input)
        .ok()
        .filter(|url| {
            matches!(url.scheme(), "http" | "https" | "ftp" | "ftps")
                && url.host_str().is_some_and(|host| {
                    host == "localhost" || host.contains('.') || host.starts_with('[')
                })
        })
        .map(|url| url.to_string())
}

struct Render<'a> {
    state: &'a AppState,
    user: &'a User,
    uri: &'a Uri,
    headers: &'a HeaderMap,
    origin: &'a str,
}

impl Render<'_> {
    fn page(&self, name: &str, fields: minijinja::Value) -> Result<Response, Error> {
        let (cookie, csrf) =
            login::csrf_tokens(self.headers).map_err(|_| ApiError::Serialization)?;
        let common = web::base_context(self.state, self.user, self.uri);
        let security = context! {csrf};
        let page = web::render(name, context! {..fields,..security,..common}).map_err(|error| {
            eprintln!("Tournament results template failed: {error}");
            ApiError::Serialization
        })?;
        let mut response = ([(header::VARY, "Cookie")], Html(page)).into_response();
        login::set_csrf_cookie(&mut response, &cookie, self.origin.starts_with("https://"));
        Ok(response)
    }
}

async fn load_event(state: &AppState, id: i64) -> Result<Option<(Value, i64)>, Error> {
    let event = sqlx::query(
        "SELECT name,CAST(qual_type AS BIGINT) AS kind FROM core_tournament WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(&state.pool)
    .await?;
    Ok(event
        .map(|row| {
            Ok::<_, sqlx::Error>((
                json!({"id":id,"name":row.try_get::<&str,_>("name")?}),
                row.try_get("kind")?,
            ))
        })
        .transpose()?)
}

async fn render_entry(
    render: &Render<'_>,
    event: Value,
    mut panels: Vec<Panel>,
    autoquals: Vec<i64>,
    mut autoqual_errors: Vec<String>,
    submitted: bool,
    review: Option<&review::Review>,
) -> Result<Response, Error> {
    let (people, schools, eligible_autoquals) = {
        let mut conn = render.state.pool.acquire().await?;
        (
            forms::labels(&mut conn, &panels, &autoquals, review.is_some()).await?,
            review::school_labels(&mut conn, &panels).await?,
            forms::labels(&mut conn, &[], &autoquals, false).await?,
        )
    };
    if submitted {
        let error = "Select a valid choice. That choice is not one of the available choices.";
        for panel in &mut panels {
            let choices = if panel.tab.kind == "school" {
                &schools
            } else {
                &people
            };
            let max = if panel.tab.kind == "school" { 64 } else { 32 };
            for row in panel.rows.iter_mut().filter(|row| !row.deleted) {
                if [row.first, row.second]
                    .into_iter()
                    .flatten()
                    .any(|id| !choices.contains_key(&id.to_string()))
                    || row
                        .school
                        .is_some_and(|id| !schools.contains_key(&id.to_string()))
                {
                    row.errors.push(error.to_owned());
                }
                if matches!(panel.tab.kind, "school" | "debater")
                    && (row.name.chars().count() > max || row.last_name.chars().count() > max)
                {
                    row.errors
                        .push(format!("Ensure this value has at most {max} characters."));
                }
            }
        }
        if autoquals
            .iter()
            .any(|id| !eligible_autoquals.contains_key(&id.to_string()))
        {
            autoqual_errors.push(error.to_owned());
        }
        if panels.iter().all(Panel::valid) && autoqual_errors.is_empty() {
            let id = event["id"].as_i64().ok_or(ApiError::Serialization)?;
            return match persist::store(render.state, id, &mut panels, &autoquals, review).await {
                Ok(skipped) => {
                    let mut response = (
                        StatusCode::FOUND,
                        [(header::LOCATION, format!("/core/tournaments/{id}"))],
                    )
                        .into_response();
                    if !skipped.is_empty()
                        && let Some(secret) = render.state.secret.as_deref()
                    {
                        let summary = super::bundle::Summary {
                            skipped,
                            ..Default::default()
                        };
                        let (level, message) = summary.flash();
                        flash::push(
                            &mut response,
                            render.headers,
                            secret,
                            level,
                            &message,
                            render.origin.starts_with("https://"),
                        );
                    }
                    Ok(response)
                }
                Err(Error::Invalid(reason)) => Ok((
                    StatusCode::BAD_REQUEST,
                    format!("No results were saved: {reason}"),
                )
                    .into_response()),
                Err(Error::Database(ApiError::Constraint)) => Ok((
                    StatusCode::BAD_REQUEST,
                    "No results were saved: conflicting placements.",
                )
                    .into_response()),
                Err(error) => Err(error),
            };
        }
    }
    let error = panels.iter().find(|panel| !panel.valid());
    let active_tab = error
        .or_else(|| {
            panels.iter().find(|panel| {
                !panel.rows.is_empty() || matches!(panel.tab.kind, "team" | "speaker")
            })
        })
        .map_or("varsity_teams", |panel| panel.tab.key);
    let error_message = error.map(|panel| {
        format!(
            "Please fix the errors in the {} tab before continuing.",
            panel.tab.label
        )
    });
    let selected_api_tabs = if review.is_some() {
        TABS.iter().map(|tab| tab.key).collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let shown = |kind| {
        panels.iter().any(|panel| {
            panel.tab.kind == kind && (!panel.rows.is_empty() || !panel.errors.is_empty())
        })
    };
    render.page("tournaments/data_entry.html",context!{tournament=>event,panels,people,schools,autoquals,autoqual_errors,
        active_tab,error_message,has_api_data=>review.is_some(),show_school_tab=>shown("school"),show_debater_tab=>shown("debater"),
        review_token=>review.map_or("",|item|item.token.as_str()),current_api_url=>review.map_or("",|item|item.url.as_str()),
        selected_api_tabs,import_counts_from_api=>review.is_some()})
}

async fn handle(state: &AppState, request: Request) -> Result<Response, Error> {
    let uri = request.uri().clone();
    let user = match requester(state, &uri, request.headers()).await {
        Ok(user) => user,
        Err(response) => return Ok(*response),
    };
    let (parts, body) = request.into_parts();
    let Some(origin) = state.origin(&parts.headers) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let given = form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<FormValues>();
    let submits = parts.method == Method::POST;
    let content = if submits {
        match to_bytes(body, 2_621_440).await {
            Ok(data) => data,
            Err(_) => return Ok(StatusCode::PAYLOAD_TOO_LARGE.into_response()),
        }
    } else {
        Default::default()
    };
    let parameters = form_urlencoded::parse(&content)
        .into_owned()
        .collect::<FormValues>();
    if submits
        && !login::csrf_valid(
            &parts.headers,
            parameters.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        )
    {
        return Ok(login::csrf_failure());
    }
    let raw = given
        .get("tournament")
        .filter(|value| !value.is_empty())
        .or_else(|| parameters.get("tournament"));
    let Some(id) = raw.and_then(|value| value.trim().parse::<i64>().ok()) else {
        return Ok(StatusCode::BAD_REQUEST.into_response());
    };
    let Some((event, kind)) = load_event(state, id).await? else {
        return Ok(StatusCode::NOT_FOUND.into_response());
    };
    let values = if submits { &parameters } else { &given };
    let supplied = values.get("api_url").map_or("", String::as_str).trim();
    let render = Render {
        state,
        user: &user,
        uri: &uri,
        headers: &parts.headers,
        origin: &origin,
    };
    if uri.path().ends_with("/setup") {
        let url = safe_url(supplied);
        if submits && let Some(url) = &url {
            let mut params = form_urlencoded::Serializer::new(String::new());
            params.append_pair("tournament", &id.to_string());
            if !url.is_empty() {
                params.append_pair("api_url", url);
                for tab in TABS {
                    params.append_pair(&format!("import_{}", tab.key), "1");
                }
                params.append_pair("import_counts", "1");
            }
            let location = format!("{PAGE}?{}", params.finish());
            return Ok((StatusCode::FOUND, [(header::LOCATION, location)]).into_response());
        }
        return render.page(
            "tournaments/data_entry_setup.html",
            context! {tournament=>event,api_url=>supplied,
            error=>(submits && url.is_none()).then_some("Enter a valid URL.")},
        );
    }
    let token = given
        .get("review")
        .filter(|value| !value.is_empty())
        .or_else(|| parameters.get("review").filter(|value| !value.is_empty()));
    let user_id = user.id.ok_or(ApiError::Serialization)?;
    let mut reviewed = None;
    if !supplied.is_empty() {
        let result = async {
            let url = supplied.trim_end_matches('/');
            if let Some(token) = token {
                let data = source::load_review(state, token, id, user_id).await?;
                let mappings = source::review_mappings(state, token).await;
                reviewed = Some(review::Review {
                    token: token.clone(),
                    url: url.to_owned(),
                    data,
                    mappings,
                });
                return Ok(None);
            }
            if submits {
                return Err(super::invalid(
                    "Open a new import review before saving API results.",
                ));
            }
            let data = source::fresh_or_cached(state, url).await?;
            if data["import_ready"] != true || !super::truthy(&data["bundle"]) {
                return Err(super::invalid(
                    "This tournament's results are not available yet.",
                ));
            }
            let token = source::save_review(state, id, user_id, data).await?;
            Ok(Some(source::review_url(id, &token, url)))
        }
        .await;
        match result {
            Ok(Some(location)) => {
                return Ok((StatusCode::FOUND, [(header::LOCATION, location)]).into_response());
            }
            Ok(None) => (),
            Err(Error::Invalid(reason)) => {
                return Ok((StatusCode::BAD_REQUEST, reason).into_response());
            }
            Err(error) => return Err(error),
        }
    }
    let mut errors = Vec::new();
    let (panels, autoquals) = if submits {
        let mut people = form_urlencoded::parse(&content)
            .filter(|(key, _)| key == "autoqual_debaters")
            .filter_map(|(_, value)| match value.parse::<i64>() {
                Ok(id) if id > 0 => Some(id),
                _ => {
                    errors.push(
                        "Select a valid choice. That choice is not one of the available choices."
                            .to_owned(),
                    );
                    None
                }
            })
            .collect::<Vec<_>>();
        people.sort_unstable();
        people.dedup();
        if !people.is_empty() && !matches!(kind, 1..=10 | 15 | 16) {
            errors
                .push("Choose an autoqual tournament type before entering recipients.".to_owned());
        }
        (forms::submitted(&parameters, reviewed.is_some()), people)
    } else {
        let panels = if let Some(review) = &mut reviewed {
            review::seed(state, review, id).await?
        } else {
            forms::current(&mut *state.pool.acquire().await?, id).await?
        };
        let mut conn = state.pool.acquire().await?;
        let autoquals = sqlx::query_scalar::<_, i64>(
            "SELECT CAST(debater_id AS BIGINT) FROM core_tournamentautoqual
            WHERE tournament_id=$1 ORDER BY id",
        )
        .bind(id)
        .fetch_all(&mut *conn)
        .await?;
        (panels, autoquals)
    };
    render_entry(
        &render,
        event,
        panels,
        autoquals,
        errors,
        submits,
        reviewed.as_ref(),
    )
    .await
}

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    match handle(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Tournament results request failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn new_form(OriginalUri(uri): OriginalUri, method: Method) -> Response {
    if !matches!(method, Method::GET | Method::HEAD) {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"Invalid request"})),
        )
            .into_response();
    }
    let query = form_urlencoded::parse(uri.query().unwrap_or_default().as_bytes())
        .into_owned()
        .collect::<FormValues>();
    let index = query
        .get("form_index")
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or_default();
    let kind = query.get("form_type").map_or("team", String::as_str);
    let default = match kind {
        "team" => ("2", "Team", "team"),
        "school" => ("0", "School", "school"),
        "debater" => ("1", "Debater", "debater"),
        _ => ("3", "Speaker", "speaker"),
    };
    let ghost = query
        .get("has_ghost_points")
        .is_some_and(|value| matches!(value.as_str(), "1" | "true" | "True"));
    let panel = Tab {
        key: default.0,
        kind: default.2,
        ghost,
        ..TABS[0]
    };
    let row = ResultRow {
        order: index + 1,
        scoring: true,
        ..Default::default()
    };
    let item_name = query
        .get("item_name")
        .filter(|name| !name.is_empty())
        .map_or(default.1, String::as_str);
    match web::render(
        "tournaments/includes/result_row_ajax.html",
        context! {panel,row,people=>FormValues::new(),schools=>FormValues::new(),
        form_index=>index,place_number=>index+1,item_name},
    ) {
        Ok(html) => Json(json!({"html":html})).into_response(),
        Err(error) => {
            eprintln!("Tournament results AJAX form failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}
