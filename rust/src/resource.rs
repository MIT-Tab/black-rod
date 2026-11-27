use axum::{
    extract::{OriginalUri, Path, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::{Value, json};
use sqlx::{Row, any::AnyRow};

use crate::{AppState, is_django_integer, login, session, team_profile::read_transaction, web};

pub(crate) const TYPES: &[(&str, &str)] = &[
    ("case", "Case"),
    ("lecture", "Lecture"),
    ("slidedeck", "Slide Deck"),
    ("writeup", "Write-up"),
    ("other", "Other"),
];

pub(crate) fn type_label(value: &str) -> &str {
    TYPES
        .iter()
        .find(|(key, _)| *key == value)
        .map_or(value, |(_, label)| *label)
}

pub(crate) fn created_date(value: &str) -> String {
    value
        .get(..10)
        .and_then(|date| chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d").ok())
        .map(|date| date.format("%B %d, %Y").to_string())
        .unwrap_or_default()
}

const DETAIL: &str = "
    SELECT r.title, r.resource_type, r.content_link, r.description, r.usage_permissions,
        CAST(r.created_at AS TEXT) AS created,
        CAST(r.viewing_permission AS BIGINT) AS permission,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM core_resource_authors a JOIN core_debater d ON d.id = a.debater_id
            WHERE a.resource_id = r.id AND d.user_id = CAST($2 AS BIGINT) AND NOT d.synthetic AND NOT d.temporary)
            THEN 1 ELSE 0 END AS BIGINT) AS author
    FROM core_resource r WHERE r.id = $1
";
pub(crate) const AUTHORS: &str = "
    SELECT CAST(d.id AS BIGINT) AS id, TRIM(d.first_name || ' ' || d.last_name) AS name
    FROM core_resource_authors a JOIN core_debater d ON d.id = a.debater_id
    WHERE a.resource_id = $1 AND NOT d.synthetic AND NOT d.temporary ORDER BY d.id
";
pub(crate) const TAGS: &str = "
    SELECT tag.name, tag.slug FROM core_resourcetag tag JOIN core_taggedresource item ON item.tag_id = tag.id
    JOIN django_content_type ct ON ct.id = item.content_type_id
    WHERE ct.app_label = 'core' AND ct.model = 'resource' AND item.object_id = $1 ORDER BY item.id
";

fn object(row: &AnyRow, id: i64, authors: Vec<Value>, tags: Vec<Value>) -> Value {
    let permission: i64 = row.get("permission");
    let permission = match permission {
        0 => json!("Public (Discoverable by Google search)"),
        1 => json!("Requires Login"),
        _ => json!(permission),
    };
    let title: &str = row.get("title");
    let resource_type = type_label(row.get("resource_type"));
    json!({
        "id":id, "title":title, "name":format!("{title} ({resource_type})"),
        "resource_type": resource_type,
        "created_at": created_date(row.get("created")),
        "viewing_permission": permission,
        "content_link": row.get::<&str, _>("content_link"),
        "description": row.get::<&str, _>("description"),
        "usage_permissions": row.get::<&str, _>("usage_permissions"),
        "authors": authors, "tags": tags,
    })
}

pub(crate) async fn detail(
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
    let Ok(user) = session::user(&state.pool, &headers, state.secret.as_deref()).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(mut tx) = read_transaction(&state.pool).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let record = match sqlx::query(DETAIL)
        .bind(id)
        .bind(user.id)
        .fetch_optional(&mut *tx)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        Err(error) => {
            eprintln!("Resource lookup failed: {error}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let is_author = record.get::<i64, _>("author") != 0;
    let permission: i64 = record.get("permission");
    let may_view = user.is_superuser
        || permission == 0
        || (permission == 1
            && user.is_authenticated
            && (user.can_view_private_videos || is_author));
    if !may_view {
        return if user.is_authenticated {
            web::permission_denied(&state, &user, &uri)
        } else {
            let mut response = login::redirect_to_login(&uri);
            response
                .headers_mut()
                .insert(header::VARY, "Cookie".parse().unwrap());
            response
        };
    }
    let Ok(authors) = sqlx::query(AUTHORS).bind(id).fetch_all(&mut *tx).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    let Ok(tags) = sqlx::query(TAGS).bind(id).fetch_all(&mut *tx).await else {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    };
    if tx.commit().await.is_err() {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let authors = authors
        .iter()
        .map(|author| {
            let id: i64 = author.get("id");
            json!({"name": author.get::<&str, _>("name"), "url": format!("/core/debaters/{id}")})
        })
        .collect();
    let tags = tags
        .iter()
        .map(|tag| json!({"name": tag.get::<&str,_>("name"), "slug": tag.get::<&str,_>("slug")}))
        .collect();
    let mut buttons = Vec::new();
    for (perm, name, action, class) in [
        ("delete_resource", "Delete", "delete", "btn-danger"),
        ("change_resource", "Edit", "edit", "btn-info"),
    ] {
        if session::has_permission(&state.pool, &user, perm)
            .await
            .unwrap_or(false)
        {
            buttons.push(json!({"name":name, "href":format!("/core/resources/{id}/{action}"), "class":class}));
        }
    }
    let object = object(&record, id, authors, tags);
    let common = web::base_context(&state, &user, &uri);
    match web::render(
        "resources/detail.html",
        context! {object, buttons, is_author, ..common},
    ) {
        Ok(html) => ([(header::VARY, "Cookie")], Html(html)).into_response(),
        Err(error) => {
            eprintln!("Resource detail template failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn tag(Path(slug): Path<String>) -> Response {
    let mut url = url::form_urlencoded::Serializer::new(String::new());
    url.append_pair("tags", &slug);
    (
        StatusCode::FOUND,
        [(
            header::LOCATION,
            format!("/core/resources/?{}", url.finish()),
        )],
    )
        .into_response()
}
