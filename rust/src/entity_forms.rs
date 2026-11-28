use std::collections::BTreeMap;

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    response::{Html, IntoResponse, Response},
};
use minijinja::context;
use serde_json::json;
use sqlx::{AnyPool, AssertSqlSafe, Row};

use crate::{ApiError, AppState, claims, is_django_integer, login, session, web};

type Fields = BTreeMap<&'static str, String>;
type Errors = BTreeMap<&'static str, String>;

fn validate_text(errors: &mut Errors, name: &'static str, value: &str, max: usize, required: bool) {
    if value.is_empty() && required {
        errors.insert(name, "This field is required.".to_owned());
    } else if value.chars().count() > max {
        errors.insert(
            name,
            format!(
                "Ensure this value has at most {max} characters (it has {}).",
                value.chars().count()
            ),
        );
    } else if value.contains('\0') {
        errors.insert(name, "Null characters are not allowed.".to_owned());
    }
}

async fn school(
    pool: &AnyPool,
    fields: &Fields,
    errors: &mut Errors,
    included: bool,
    id: Option<i64>,
) -> Result<Option<i64>, ApiError> {
    validate_text(errors, "name", &fields["name"], 64, true);
    validate_text(errors, "short_name", &fields["short_name"], 64, true);
    if !errors.is_empty() {
        return Ok(None);
    }
    let saved = if let Some(id) = id {
        sqlx::query_scalar::<_,i64>("UPDATE core_school SET name=$1,short_name=$2,included_in_oty=$3
            WHERE id=$4 AND NOT EXISTS (SELECT 1 FROM core_school WHERE name=$1 AND id<>$4) RETURNING CAST(id AS BIGINT)")
            .bind(&fields["name"]).bind(&fields["short_name"]).bind(included).bind(id).fetch_optional(pool).await
    } else {
        sqlx::query_scalar::<_,i64>("INSERT INTO core_school (name,short_name,included_in_oty,temporary,synthetic)
            VALUES ($1,$2,$3,FALSE,FALSE) ON CONFLICT (name) DO NOTHING RETURNING CAST(id AS BIGINT)")
            .bind(&fields["name"]).bind(&fields["short_name"]).bind(included).fetch_optional(pool).await
    };
    let id = match saved {
        Ok(id) => id,
        Err(error)
            if error
                .as_database_error()
                .is_some_and(|db| db.is_unique_violation()) =>
        {
            None
        }
        Err(error) => return Err(error.into()),
    };
    if id.is_none() {
        errors.insert("name", "School with this Name already exists.".to_owned());
    }
    Ok(id)
}

async fn debater(
    state: &AppState,
    fields: &Fields,
    errors: &mut Errors,
    choices: &mut BTreeMap<&'static str, String>,
    debater_id: Option<i64>,
) -> Result<Option<i64>, ApiError> {
    validate_text(errors, "first_name", &fields["first_name"], 32, true);
    validate_text(errors, "last_name", &fields["last_name"], 32, false);
    let mut ids = BTreeMap::new();
    for (field, sql) in [
        (
            "school",
            "SELECT name FROM core_school WHERE id=$1 AND NOT temporary",
        ),
        (
            "alias_group",
            "SELECT label FROM core_debateraliasgroup WHERE id=$1",
        ),
        (
            "existing_debater",
            "SELECT TRIM(first_name || ' ' || last_name) FROM core_debater WHERE id=$1 AND NOT temporary AND NOT synthetic",
        ),
    ] {
        let value = &fields[field];
        if value.is_empty() {
            if field == "school" {
                errors.insert(field, "This field is required.".to_owned());
            }
            continue;
        }
        let label = if let Ok(id) = value.parse::<i64>() {
            ids.insert(field, id);
            sqlx::query_scalar::<_, String>(sql)
                .bind(id)
                .fetch_optional(&state.pool)
                .await?
        } else {
            None
        };
        if let Some(label) = label {
            choices.insert(field, label);
        } else {
            errors.insert(
                field,
                "Select a valid choice. That choice is not one of the available choices."
                    .to_owned(),
            );
        }
    }
    if !errors.is_empty() {
        return Ok(None);
    }
    let first = claims::edit::sanitize_name(&fields["first_name"], "Removed");
    let last = claims::edit::sanitize_name(&fields["last_name"], "");
    let (empty_region, normalized_region) =
        if state.pool.connect_options().database_url.scheme() == "sqlite" {
            (
                "'[]'",
                "CASE WHEN json_type(region)='text' THEN CASE WHEN json_extract(region,'$')=''
            THEN '[]' ELSE json_array(json_extract(region,'$')) END ELSE region END",
            )
        } else {
            (
            "CAST('[]' AS JSONB)",
            "CASE WHEN jsonb_typeof(region)='string' THEN CASE WHEN region=CAST('\"\"' AS JSONB)
            THEN CAST('[]' AS JSONB) ELSE jsonb_build_array(region) END ELSE region END",
        )
        };
    let id = if let Some(id) = debater_id {
        let query = format!(
            "UPDATE core_debater SET first_name=$1,last_name=$2,school_id=$3,alias_group_id=$4,
            dino_judge_contact_opt_in=(status=2 AND dino_judge_contact_opt_in),
            region=CASE WHEN dino_to_contact_opt_in OR (status=2 AND dino_judge_contact_opt_in)
                THEN {normalized_region} ELSE {empty_region} END
            WHERE id=$5 AND NOT synthetic AND NOT temporary RETURNING CAST(id AS BIGINT)"
        );
        let mut tx = state.pool.begin().await?;
        let updated = sqlx::query_scalar::<_, i64>(AssertSqlSafe(query))
            .bind(first)
            .bind(last)
            .bind(ids.get("school"))
            .bind(ids.get("alias_group"))
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?;
        if updated.is_some() {
            claims::edit::team_names(&mut tx, id).await?;
        }
        tx.commit().await?;
        updated
    } else {
        let sql=format!("INSERT INTO core_debater (first_name,last_name,school_id,alias_group_id,first_season,
            latest_season,status,dino_to_contact_opt_in,dino_judge_contact_opt_in,region,elo_manual_opt,temporary,synthetic)
            VALUES ($1,$2,$3,$4,$5,$5,1,FALSE,FALSE,{empty_region},'',FALSE,FALSE) RETURNING CAST(id AS BIGINT)");
        Some(
            sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql))
                .bind(first)
                .bind(last)
                .bind(ids.get("school"))
                .bind(ids.get("alias_group"))
                .bind(state.current_season.to_string())
                .fetch_one(&state.pool)
                .await?,
        )
    };
    crate::search::invalidate(state).await;
    Ok(id)
}

async fn page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let uri = request.uri().clone();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if !user.is_authenticated {
        return Ok(login::redirect_to_login(&uri));
    }
    let is_school = uri.path().starts_with("/core/schools/");
    let id = match uri.path().strip_suffix("/edit") {
        Some(prefix) => {
            let raw = prefix.rsplit('/').next().unwrap_or_default();
            if !is_django_integer(raw) {
                return Ok(StatusCode::NOT_FOUND.into_response());
            }
            let Ok(id) = raw.parse::<i64>() else {
                return Ok(StatusCode::NOT_FOUND.into_response());
            };
            Some(id)
        }
        None => None,
    };
    let permission = match (is_school, id) {
        (true, None) => "add_school",
        (true, Some(_)) => "change_school",
        (false, None) => "add_debater",
        (false, Some(_)) => "change_debater",
    };
    if !session::has_permission(&state.pool, &user, permission).await? {
        return Ok(web::permission_denied(state, &user, &uri));
    }
    let mut values = Fields::new();
    let mut errors = Errors::new();
    let mut choices = BTreeMap::new();
    let mut included = true;
    if let Some(id) = id {
        let sql = if is_school {
            "SELECT name,short_name,CAST(CASE WHEN included_in_oty THEN 1 ELSE 0 END AS BIGINT) AS included FROM core_school WHERE id=$1 AND NOT temporary"
        } else {
            "SELECT d.first_name,d.last_name,CAST(d.school_id AS TEXT) AS school,CAST(d.alias_group_id AS TEXT) AS alias_group,
                s.name AS school_name,g.label AS alias_label FROM core_debater d
                LEFT JOIN core_school s ON s.id=d.school_id LEFT JOIN core_debateraliasgroup g ON g.id=d.alias_group_id
                WHERE d.id=$1 AND NOT d.synthetic AND NOT d.temporary"
        };
        let Some(row) = sqlx::query(sql)
            .bind(id)
            .fetch_optional(&state.pool)
            .await?
        else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        if is_school {
            for field in ["name", "short_name"] {
                values.insert(
                    field,
                    row.try_get::<Option<String>, _>(field)?.unwrap_or_default(),
                );
            }
            included = row.try_get::<i64, _>("included")? != 0;
        } else {
            for field in ["first_name", "last_name", "school", "alias_group"] {
                values.insert(
                    field,
                    row.try_get::<Option<String>, _>(field)?.unwrap_or_default(),
                );
            }
            for (field, source) in [("school", "school_name"), ("alias_group", "alias_label")] {
                if let Some(label) = row.try_get::<Option<String>, _>(source)? {
                    choices.insert(field, label);
                }
            }
        }
    }
    let (parts, body) = request.into_parts();
    let secure = state
        .origin(&parts.headers)
        .is_some_and(|url| url.starts_with("https://"));
    if parts.method == Method::POST {
        let submitted = match login::parse_form(&parts.headers, body, 2_621_440).await {
            Ok(fields) => fields,
            Err(status) => return Ok(status.into_response()),
        };
        let Some(origin) = state.origin(&parts.headers) else {
            return Ok(StatusCode::BAD_REQUEST.into_response());
        };
        if !login::csrf_valid(
            &parts.headers,
            submitted.get("csrfmiddlewaretoken").map(String::as_str),
            &origin,
        ) {
            return Ok(login::csrf_failure());
        }
        let inputs = if is_school {
            &["name", "short_name"][..]
        } else {
            &[
                "first_name",
                "last_name",
                "school",
                "alias_group",
                "existing_debater",
                "tournament_id",
                "school_name",
            ][..]
        };
        for name in inputs {
            values.insert(
                *name,
                submitted
                    .get(*name)
                    .map_or("", String::as_str)
                    .trim()
                    .to_owned(),
            );
        }
        included = submitted
            .get("included_in_oty")
            .is_some_and(|input| !input.is_empty() && !input.eq_ignore_ascii_case("false"));
        let result = if is_school {
            school(&state.pool, &values, &mut errors, included, id).await?
        } else {
            debater(state, &values, &mut errors, &mut choices, id).await?
        };
        if let Some(id) = result {
            if is_school {
                crate::search::invalidate(state).await;
            }
            if !is_school && submitted.contains_key("ajax") {
                return Ok(Html(id.to_string()).into_response());
            }
            let kind = if is_school { "schools" } else { "debaters" };
            return Ok((
                StatusCode::FOUND,
                [(header::LOCATION, format!("/core/{kind}/{id}"))],
            )
                .into_response());
        }
    }
    let Ok((csrf, token)) = login::csrf_tokens(&parts.headers) else {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    };
    let bindings =
        context! {csrf=>token,fields=>values,errors,included,choices,is_school,messages=>json!([])};
    let mut response = if let Some(id) = id {
        if is_school {
            crate::school_profile::render_page(state, id, &uri, &parts.headers, Some(bindings))
                .await
        } else {
            crate::debater_profile::render_page(state, id, &uri, &parts.headers, Some(bindings))
                .await
        }
    } else {
        let common = web::base_context(state, &user, &uri);
        let html = web::render("core/create_entity.html", context! {..common,..bindings}).map_err(
            |error| {
                eprintln!("Create entity template failed: {error}");
                ApiError::Serialization
            },
        )?;
        ([(header::VARY, "Cookie")], Html(html)).into_response()
    };
    login::set_csrf_cookie(&mut response, &csrf, secure);
    Ok(response)
}

pub(crate) async fn endpoint(State(state): State<AppState>, request: Request) -> Response {
    page(&state, request).await.unwrap_or_else(|error| {
        eprintln!("Create entity failed: {error:?}");
        StatusCode::INTERNAL_SERVER_ERROR.into_response()
    })
}
