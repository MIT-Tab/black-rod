mod auth;
mod autocomplete;
mod exchange;
mod form;
mod listing;
mod passwords;
mod records;
mod schema;

use std::collections::HashSet;

use axum::{
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::{Value, context};
use serde::Serialize;
use sqlx::Row;

use crate::{
    ApiError, AppState, flash, login,
    session::{self, User},
    web,
};
use schema::{Application, Model, schema};

pub(crate) use auth::{login as sign_in, logout};

const HOME: &str = "/admin/";

fn cap_first(source: &str) -> String {
    let mut characters = source.chars();
    if let Some(first) = characters.next() {
        first.to_uppercase().chain(characters).collect()
    } else {
        String::new()
    }
}

struct Viewer {
    user: User,
    username: String,
    short_name: String,
    usable: bool,
    permissions: HashSet<(String, String)>,
}

impl Viewer {
    async fn load(state: &AppState, headers: &HeaderMap) -> Result<Self, ApiError> {
        let user = session::user(&state.pool, headers, state.secret.as_deref()).await?;
        let Some(id) = user.id else {
            return Ok(Self {
                user,
                username: String::new(),
                short_name: String::new(),
                usable: false,
                permissions: HashSet::new(),
            });
        };
        let row = sqlx::query("SELECT username,first_name,password FROM core_user WHERE id=$1")
            .bind(id)
            .fetch_one(&state.pool)
            .await?;
        let username: String = row.try_get("username")?;
        let first: String = row.try_get("first_name")?;
        let password: &str = row.try_get("password")?;
        let permissions = if user.is_staff && !user.is_superuser {
            sqlx::query_as::<_,(String,String)>("SELECT ct.app_label, p.codename
                FROM auth_permission p JOIN django_content_type ct ON ct.id=p.content_type_id
                WHERE EXISTS(SELECT 1 FROM core_user_user_permissions u WHERE u.user_id=$1 AND u.permission_id=p.id)
                OR EXISTS(SELECT 1 FROM core_user_groups u JOIN auth_group_permissions g ON g.group_id=u.group_id
                    WHERE u.user_id=$1 AND g.permission_id=p.id)").bind(id).fetch_all(&state.pool).await?
                .into_iter().collect()
        } else {
            HashSet::new()
        };
        Ok(Self {
            user,
            short_name: if first.is_empty() {
                username.clone()
            } else {
                first
            },
            username,
            usable: !password.starts_with('!'),
            permissions,
        })
    }

    fn staff(&self) -> bool {
        self.user.is_authenticated && self.user.is_staff
    }

    fn allows(&self, model: &Model, action: &str) -> bool {
        if !self.staff() || !model.allowed.get(action).copied().unwrap_or_default() {
            return false;
        }
        self.user.is_superuser
            || (!model.restricted
                && self
                    .permissions
                    .contains(&(model.app().to_owned(), format!("{action}_{}", model.slug()))))
    }

    fn read(&self, model: &Model) -> bool {
        self.allows(model, "view") || self.allows(model, "change")
    }

    fn applications(&self) -> Vec<ApplicationLink> {
        schema()
            .apps
            .iter()
            .filter_map(|app| {
                let models = app
                    .models
                    .iter()
                    .filter_map(|model| {
                        let readable = self.read(model);
                        let add = self.allows(model, "add");
                        if !readable && !add && !self.allows(model, "delete") {
                            return None;
                        }
                        let url = model.url();
                        Some(ModelLink {
                            name: cap_first(&model.plural),
                            object_name: &model.object_name,
                            admin_url: readable.then(|| url.clone()),
                            add_url: add.then(|| format!("{url}add/")),
                            view_only: !self.allows(model, "change"),
                        })
                    })
                    .collect::<Vec<_>>();
                (!models.is_empty()).then(|| ApplicationLink {
                    app_label: &app.label,
                    name: &app.name,
                    app_url: app.url(),
                    models,
                })
            })
            .collect()
    }
}

#[derive(Clone, Serialize)]
struct ApplicationLink {
    app_label: &'static str,
    name: &'static str,
    app_url: String,
    models: Vec<ModelLink>,
}

#[derive(Clone, Serialize)]
struct ModelLink {
    name: String,
    object_name: &'static str,
    admin_url: Option<String>,
    add_url: Option<String>,
    view_only: bool,
}

struct AdminRequest<'a> {
    state: &'a AppState,
    headers: &'a HeaderMap,
    uri: &'a Uri,
    viewer: Viewer,
}

impl AdminRequest<'_> {
    fn origin(&self) -> Option<String> {
        self.state.origin(self.headers)
    }
    fn secure(&self) -> bool {
        self.origin()
            .is_some_and(|origin| origin.starts_with("https://"))
    }

    fn view(&self, name: &str, bindings: Value) -> Result<Response, ApiError> {
        let schema = schema();
        let apps = self.viewer.applications();
        let notices = self
            .state
            .secret
            .as_deref()
            .map(|secret| flash::alerts(self.headers, secret))
            .unwrap_or_default();
        let (secret, token) =
            login::csrf_tokens(self.headers).map_err(|_| ApiError::Serialization)?;
        let current_path = self.uri.path();
        let is_popup = login::form_values(self.uri.query().unwrap_or_default().as_bytes())
            .contains_key("_popup");
        let source = web::render(name, context! {
            user=>&self.viewer.user, has_permission=>self.viewer.staff(), short_name=>&self.viewer.short_name,
            username=>&self.viewer.username, has_password=>self.viewer.usable, csrf=>token,
            site_title=>&schema.title, site_header=>&schema.header, view_site_url=>"/",
            available_apps=>&apps, current_path, is_popup,
            is_nav_sidebar_enabled=>true, messages=>&notices, ..bindings,
        }).map_err(|reason|{
            eprintln!("Django admin template {name} failed: {reason}");
            ApiError::Serialization
        })?;
        let mut result = (
            [
                (header::VARY, "Cookie"),
                (
                    header::CACHE_CONTROL,
                    "max-age=0, no-cache, no-store, must-revalidate, private",
                ),
            ],
            Html(source),
        )
            .into_response();
        login::set_csrf_cookie(&mut result, &secret, self.secure());
        if !notices.is_empty() {
            flash::clear(&mut result, self.secure());
        }
        Ok(result)
    }

    async fn index(&self, app: Option<&Application>) -> Result<Response, ApiError> {
        let apps = self.viewer.applications();
        if let Some(app) = app {
            let selected = apps
                .into_iter()
                .filter(|entry| entry.app_label == app.label)
                .collect::<Vec<_>>();
            if selected.is_empty() {
                return Ok(StatusCode::NOT_FOUND.into_response());
            }
            return self.view("site_admin/app_index.html",context! {
                title=>format!("{} administration",app.name),app_list=>selected,app_label=>&app.label
            });
        }
        let mut log = Vec::new();
        for row in sqlx::query("SELECT CAST(l.action_flag AS BIGINT) AS kind,l.object_id,l.object_repr,ct.app_label,ct.model
            FROM django_admin_log l LEFT JOIN django_content_type ct ON ct.id=l.content_type_id
            WHERE l.user_id=$1 ORDER BY l.action_time DESC,l.id DESC LIMIT 10").bind(self.viewer.user.id)
            .fetch_all(&self.state.pool).await?
        {
            let kind:i64 = row.try_get("kind")?;
            let pk:Option<&str> = row.try_get("object_id")?;
            let app:Option<&str> = row.try_get("app_label")?;
            let model:Option<&str> = row.try_get("model")?;
            let spec = app.zip(model).and_then(|(app,model)|schema().model(app,model));
            let url = spec.zip(pk).map(|(spec,pk)| format!("{}{}/change/",spec.url(),schema::quote(pk)));
            log.push(context! {
                is_addition=>kind==1,is_change=>kind==2,is_deletion=>kind==3,
                get_admin_url=>url, object_repr=>row.try_get::<&str,_>("object_repr")?,
                model=>spec.map(|model|cap_first(&model.singular))
            });
        }
        self.view(
            "site_admin/index.html",
            context! {
                title=>&schema().index,app_list=>apps,admin_log=>log
            },
        )
    }
}

fn admin_login(uri: &Uri) -> Response {
    let next = uri
        .path_and_query()
        .map(|part| part.as_str())
        .unwrap_or(HOME);
    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("next", next)
        .finish()
        .replace("%2F", "/");
    redirect(&format!("/admin/login/?{query}"))
}

fn redirect(url: &str) -> Response {
    let Some(header) = HeaderValue::from_str(url).ok() else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    (StatusCode::FOUND, [(header::LOCATION, header)]).into_response()
}

pub(crate) async fn dispatch(State(state): State<AppState>, request: Request) -> Response {
    match endpoint(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Django admin request failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn endpoint(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let (parts, body) = request.into_parts();
    let viewer = Viewer::load(state, &parts.headers).await?;
    if !viewer.staff() {
        return Ok(admin_login(&parts.uri));
    }
    let headers = parts.headers.clone();
    let uri = parts.uri.clone();
    let admin = AdminRequest {
        state,
        headers: &headers,
        uri: &uri,
        viewer,
    };
    let path = parts.uri.path().strip_prefix(HOME).unwrap_or_default();
    let bare = path.trim_end_matches('/');
    let segments = bare.split('/').collect::<Vec<_>>();
    if !parts.uri.path().ends_with('/') && parts.method == Method::GET {
        return Ok((
            StatusCode::MOVED_PERMANENTLY,
            [(header::LOCATION, format!("{}/", parts.uri.path()))],
        )
            .into_response());
    }
    match segments.as_slice() {
        [""] => admin.index(None).await,
        ["password_change"] => passwords::change(&admin, None, parts.method, body).await,
        ["password_change", "done"] => passwords::done(&admin),
        ["core", "user", id, "password"] => match id.parse::<i64>() {
            Ok(id) => passwords::change(&admin, Some(id), parts.method, body).await,
            Err(_) => Ok(StatusCode::NOT_FOUND.into_response()),
        },
        ["autocomplete"] => autocomplete::endpoint(&admin).await,
        ["r", content, id] => {
            let (Ok(content), Ok(id)) = (content.parse::<i64>(), id.parse::<i64>()) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            autocomplete::view_on_site(&admin, content, id).await
        }
        ["jsi18n"] => Ok((
            [(header::CONTENT_TYPE, "text/javascript; charset=utf-8")],
            include_str!("../schema/admin_jsi18n.js"),
        )
            .into_response()),
        [app] if schema().app(app).is_some() => admin.index(schema().app(app)).await,
        [app, model, "export"] => {
            let Some(model) = schema().model(app, model) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            exchange::export_page(&admin, model, parts.method, body).await
        }
        [app, model, "import"] => {
            let Some(model) = schema().model(app, model) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            exchange::preview(&admin, model, Request::from_parts(parts, body)).await
        }
        [app, model, "process_import"] => {
            let Some(model) = schema().model(app, model) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            exchange::confirm(&admin, model, parts.method, body).await
        }
        [app, model, "add"] => {
            let Some(model) = schema().model(app, model) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            form::page(&admin, model, None, parts.method, body).await
        }
        [app, model, id, "history"] => {
            let (Some(model), Ok(id)) = (schema().model(app, model), id.parse::<i64>()) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            records::history(&admin, model, id).await
        }
        [app, model, id, "delete"] => {
            let (Some(model), Ok(id)) = (schema().model(app, model), id.parse::<i64>()) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            records::delete_entry(&admin, model, id, parts.method, body).await
        }
        [app, model, id]
            if id.bytes().all(|byte| byte.is_ascii_digit())
                && schema().model(app, model).is_some() =>
        {
            Ok((
                StatusCode::MOVED_PERMANENTLY,
                [(
                    header::LOCATION,
                    format!("/admin/{app}/{model}/{id}/change/"),
                )],
            )
                .into_response())
        }
        [app, model, id, "change"] => {
            let (Some(model), Ok(id)) = (schema().model(app, model), id.parse::<i64>()) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            form::page(&admin, model, Some(id), parts.method, body).await
        }
        [app, model] => {
            let Some(model) = schema().model(app, model) else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            listing::page(&admin, model, parts.method, body).await
        }
        _ => Ok(StatusCode::NOT_FOUND.into_response()),
    }
}
