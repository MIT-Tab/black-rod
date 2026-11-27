pub(crate) mod edit;

use axum::{
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};
use sqlx::{AnyConnection, Row};

use crate::{
    ApiError, AppState, is_django_integer, login,
    session::{self, User},
    team_profile::read_transaction,
    tournament_profile::{debater_by_id, labels, tournament},
    web,
};

fn permitted(user: &User, permissions: i64) -> bool {
    user.can_view_video || permissions == 0 || (permissions == 1 && user.can_view_private_videos)
}

fn embed_link(link: &str, password: &str) -> Option<String> {
    if link.contains("youtube") && !link.contains("playlist") {
        return url::Url::parse(link)
            .ok()?
            .query_pairs()
            .find(|(key, _)| key == "v")
            .map(|(_, id)| format!("https://www.youtube.com/embed/{id}"));
    }
    if link.contains("vimeo") && password.is_empty() {
        return link
            .rsplit('/')
            .next()
            .map(|id| format!("https://player.vimeo.com/video/{id}"));
    }
    None
}

async fn metadata(conn: &mut AnyConnection, id: i64) -> Result<Value, ApiError> {
    let row = sqlx::query(
        "
        SELECT CAST(tournament_id AS BIGINT) AS tournament_id, CAST(round AS BIGINT) AS round,
               CAST(permissions AS BIGINT) AS permissions,
               CAST(pm_id AS BIGINT) AS pm_id, CAST(mg_id AS BIGINT) AS mg_id,
               CAST(lo_id AS BIGINT) AS lo_id, CAST(mo_id AS BIGINT) AS mo_id,
               link, password, description, \"case\"
        FROM core_video WHERE id = $1
    ",
    )
    .bind(id)
    .fetch_one(&mut *conn)
    .await?;
    let bits = labels(row.try_get("round")?, row.try_get("permissions")?);
    let tournament =
        serde_json::to_value(tournament(&mut *conn, row.try_get("tournament_id")?, "").await?)?;
    let name = format!(
        "{} / {} / {}",
        tournament["name"].as_str().unwrap_or_default(),
        tournament["season_display"].as_str().unwrap_or_default(),
        bits.0
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| bits.0.to_string())
    );
    let tags = sqlx::query_scalar::<_, String>(
        "
        SELECT t.name FROM taggit_tag t JOIN taggit_taggeditem ti ON ti.tag_id = t.id
        JOIN django_content_type ct ON ct.id = ti.content_type_id
        WHERE ct.app_label = 'core' AND ct.model = 'video' AND ti.object_id = $1 ORDER BY ti.id
    ",
    )
    .bind(id)
    .fetch_all(&mut *conn)
    .await?;
    let mut object = json!({
        "id":id, "name":name, "tournament":tournament, "round":bits.0, "permissions":bits.1,
        "link":row.try_get::<&str,_>("link")?, "password":row.try_get::<&str,_>("password")?,
        "description":row.try_get::<&str,_>("description")?, "case":row.try_get::<&str,_>("case")?,
        "tags":tags.into_iter().map(|name|json!({"name":name})).collect::<Vec<_>>(),
    });
    for role in ["pm", "mg", "lo", "mo"] {
        object[role] =
            debater_by_id(&mut *conn, row.try_get(format!("{role}_id").as_str())?, "").await?;
    }
    Ok(object)
}

pub(crate) async fn detail(
    State(state): State<AppState>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(id) = id.parse::<i64>().ok().filter(|_| is_django_integer(&id)) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(mut tx) = read_transaction(&state.pool).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let result = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(permissions AS BIGINT) FROM core_video WHERE id = $1",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await;
    let permissions = match result {
        Ok(Some(permissions)) => permissions,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    if !permitted(&user, permissions) {
        let mut response = if user.is_authenticated {
            web::permission_denied(&state, &user, &uri)
        } else {
            login::redirect_to_login(&uri)
        };
        response
            .headers_mut()
            .insert(header::VARY, header::HeaderValue::from_static("Cookie"));
        return response;
    }
    let Ok(object) = metadata(&mut tx, id).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if tx.commit().await.is_err() {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let embed_link = embed_link(
        object["link"].as_str().unwrap_or_default(),
        object["password"].as_str().unwrap_or_default(),
    );
    let mut buttons = Vec::new();
    for (permission, name, suffix, class) in [
        ("delete_video", "Delete", "delete", "btn-danger"),
        ("change_video", "Edit", "edit", "btn-info"),
    ] {
        if session::has_permission(&state.pool, &user, permission)
            .await
            .unwrap_or(false)
        {
            buttons.push(
                json!({"name":name,"href":format!("/core/videos/{id}/{suffix}"),"class":class}),
            );
        }
    }
    let common = web::base_context(&state, &user, &uri);
    let specific = context! {object, buttons, embed => embed_link.is_some(), embed_link};
    match web::render("videos/detail.html", context! {..common,..specific}) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Video template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn tag(Path(name): Path<String>) -> Response {
    let name = name.replace(' ', "%20");
    (
        StatusCode::FOUND,
        [(header::LOCATION, format!("/core/videos/?tags={name}"))],
    )
        .into_response()
}
