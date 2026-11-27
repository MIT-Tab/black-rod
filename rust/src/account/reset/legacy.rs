use axum::http::{HeaderMap, header};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use minijinja::Value;

use super::*;
use crate::{account::Visit, password};

const CHANGE: &str = "/auth/password_change/";
const CHANGE_DONE: &str = "/auth/password_change/done/";
const RESET: &str = "/auth/password_reset/";
const RESET_DONE: &str = "/auth/password_reset/done/";
const COMPLETE: &str = "/auth/reset/done/";
const CONFIRM: &str = "/auth/reset/";
const TOKEN_KEY: &str = "_password_reset_token";

pub(crate) async fn page(State(state): State<AppState>, request: Request) -> Response {
    match dispatch(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Legacy password request failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn dispatch(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let path = request.uri().path().to_owned();
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    if matches!(path.as_str(), CHANGE | CHANGE_DONE) && !user.is_authenticated {
        let next = request
            .uri()
            .path_and_query()
            .map_or(path.as_str(), |uri| uri.as_str());
        let query = url::form_urlencoded::Serializer::new(String::new())
            .append_pair("next", next)
            .finish()
            .replace("%2F", "/");
        return Ok(redirect(&format!("/auth/login?{query}")));
    }
    let visit = match visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    let response = match path.as_str() {
        CHANGE => change(state, &user, &visit).await?,
        CHANGE_DONE => view(
            state,
            &user,
            &visit,
            "Password change successful",
            "change_done",
            context! {},
        )?,
        RESET => reset(state, &user, &visit).await?,
        RESET_DONE => view(
            state,
            &user,
            &visit,
            "Password reset sent",
            "reset_done",
            context! {},
        )?,
        COMPLETE => view(
            state,
            &user,
            &visit,
            "Password reset complete",
            "reset_complete",
            context! {},
        )?,
        _ => confirm(state, &user, &visit, &path).await?,
    };
    Ok(response)
}

fn view(
    state: &AppState,
    user: &session::User,
    visit: &Visit,
    title: &str,
    mode: &str,
    extra: Value,
) -> Result<Response, ApiError> {
    let mut response = render(
        state,
        user,
        "account/legacy_passwords.html",
        visit,
        context! {title,mode,site_title=>"Django site admin",site_header=>"Django administration",..extra},
    )?;
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        "max-age=0, no-cache, no-store, must-revalidate, private"
            .parse()
            .expect("cache policy"),
    );
    Ok(response)
}

fn session_cookie(response: &mut Response, key: &str, visit: &Visit, keep: bool) {
    login::set_cookie(
        response,
        "sessionid",
        key,
        keep.then_some(login::SESSION_AGE),
        visit.secure(),
        true,
    );
}

async fn reset(
    state: &AppState,
    user: &session::User,
    visit: &Visit,
) -> Result<Response, ApiError> {
    let mut errors = Errors::new();
    let mut email = "";
    if let Some(fields) = &visit.fields {
        email = fields.get("email").map_or("", String::as_str).trim();
        if email.is_empty() {
            errors.insert("email", vec!["This field is required.".into()]);
        } else if !email_syntax(email) {
            errors.insert("email", vec!["Enter a valid email address.".into()]);
        } else {
            send_mail(state, &visit.origin, email).await?;
            return Ok(redirect(RESET_DONE));
        }
    }
    view(
        state,
        user,
        visit,
        "Password reset",
        "reset",
        context! {errors,email},
    )
}

async fn send_mail(state: &AppState, origin: &str, email: &str) -> Result<(), ApiError> {
    let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
    let targets = sqlx::query_scalar::<_, i64>(
        "SELECT CAST(id AS BIGINT) FROM core_user
         WHERE is_active AND LOWER(email)=LOWER($1) AND substr(password,1,1)<>'!' ORDER BY id",
    )
    .bind(email)
    .fetch_all(&state.pool)
    .await?;
    if targets.is_empty() {
        return Ok(());
    }
    let site =
        sqlx::query_as::<_, (String, String)>("SELECT name,domain FROM django_site WHERE id=1")
            .fetch_optional(&state.pool)
            .await?;
    let link_origin = site.as_ref().map_or_else(
        || origin.to_owned(),
        |(_, domain)| {
            let protocol = if origin.starts_with("https://") {
                "https"
            } else {
                "http"
            };
            format!("{protocol}://{domain}")
        },
    );
    let name = site.as_ref().map_or(origin, |(name, _)| name.as_str());
    for id in targets {
        if let Some(person) = ResetUser::read(state, id).await? {
            let uid = URL_SAFE_NO_PAD.encode(id.to_string());
            let token = person.token_with_emails(Utc::now().timestamp() - EPOCH, secret, false);
            let _ = mail::send_with_subject(&person.email, &format!("Password reset on {name}"), format!(
                "\nYou're receiving this email because you requested a password reset for your user account at {name}.\n\nPlease go to the following page and choose a new password:\n\n{link_origin}{CONFIRM}{uid}/{token}/\n\nIn case you’ve forgotten, you are: {}\n\nThanks for using our site!\n\nThe {name} team\n\n",
                person.username,
            )).await;
        }
    }
    Ok(())
}

fn forms(fields: &super::super::Fields, person: &ResetUser) -> Errors {
    let values = [
        (
            "password1".to_owned(),
            fields.get("new_password1").cloned().unwrap_or_default(),
        ),
        (
            "password2".to_owned(),
            fields.get("new_password2").cloned().unwrap_or_default(),
        ),
    ]
    .into_iter()
    .collect();
    let mut errors = passwords::validation(
        &values,
        [&person.username, &person.first, &person.last, &person.email],
    );
    [
        ("password1", "new_password1"),
        ("password2", "new_password2"),
    ]
    .into_iter()
    .filter_map(|(source, target)| {
        errors.remove(source).map(|messages| {
            let messages = messages
                .into_iter()
                .map(|text| {
                    if text.contains("didn’t match") || text.contains("didn't match") {
                        "The two password fields didn’t match.".to_owned()
                    } else {
                        text
                    }
                })
                .collect();
            (target, messages)
        })
    })
    .collect()
}

async fn verify_old(
    fields: &super::super::Fields,
    person: &ResetUser,
    errors: &mut Errors,
) -> Result<(), ApiError> {
    let provided = fields.get("old_password").cloned().unwrap_or_default();
    if provided.is_empty() {
        errors.insert("old_password", vec!["This field is required.".into()]);
    } else {
        let original = person.hash.clone();
        let valid = tokio::task::spawn_blocking(move || {
            password::verify_and_upgrade(provided.as_bytes(), Some(&original)).is_some()
        })
        .await
        .map_err(|_| ApiError::Serialization)?;
        if !valid {
            errors.insert(
                "old_password",
                vec!["Your old password was entered incorrectly. Please enter it again.".into()],
            );
        }
    }
    Ok(())
}

async fn change(
    state: &AppState,
    user: &session::User,
    visit: &Visit,
) -> Result<Response, ApiError> {
    let person = ResetUser::read(state, user.id.ok_or(ApiError::Serialization)?)
        .await?
        .ok_or(ApiError::Serialization)?;
    let mut errors = Errors::new();
    if let Some(fields) = &visit.fields {
        errors = forms(fields, &person);
        verify_old(fields, &person, &mut errors).await?;
        if errors.is_empty() {
            let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
            let session = storage(state, &visit.headers, secret)
                .await?
                .ok_or(ApiError::Serialization)?;
            let hash = passwords::new_hash(&fields["new_password1"]).await?;
            let mut tx = state.pool.begin().await?;
            if !set_password(&mut tx, &person, &hash).await? {
                return Ok(StatusCode::CONFLICT.into_response());
            }
            let (id, remember) = session.rotate_password(&mut tx, secret, &hash).await?;
            tx.commit().await?;
            let mut response = redirect(CHANGE_DONE);
            session_cookie(&mut response, &id, visit, remember);
            return Ok(response);
        }
    }
    view(
        state,
        user,
        visit,
        "Password change",
        "change",
        context! {errors},
    )
}

async fn storage(
    state: &AppState,
    headers: &HeaderMap,
    secret: &[u8],
) -> Result<Option<session::StoredSession>, ApiError> {
    Ok(session::StoredSession::read(&state.pool, headers, secret).await?)
}

async fn set_password(
    conn: &mut sqlx::AnyConnection,
    person: &ResetUser,
    hash: &str,
) -> Result<bool, ApiError> {
    Ok(
        sqlx::query("UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3")
            .bind(hash)
            .bind(person.id)
            .bind(&person.hash)
            .execute(conn)
            .await?
            .rows_affected()
            == 1,
    )
}

async fn confirm(
    state: &AppState,
    user: &session::User,
    visit: &Visit,
    path: &str,
) -> Result<Response, ApiError> {
    let (uid, candidate) = path
        .trim_start_matches(CONFIRM)
        .trim_end_matches('/')
        .split_once('/')
        .unwrap_or_default();
    let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
    let id = URL_SAFE_NO_PAD
        .decode(uid)
        .ok()
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .and_then(|number| number.parse::<i64>().ok());
    let person = if let Some(id) = id {
        ResetUser::read(state, id).await?
    } else {
        None
    };
    let staged = candidate == "set-password";
    let mut session = storage(state, &visit.headers, secret).await?;
    let token = if staged {
        session
            .as_ref()
            .and_then(|data| data.string(TOKEN_KEY))
            .unwrap_or_default()
    } else {
        candidate.to_owned()
    };
    let Some(person) = person.filter(|person| person.valid_token(&token, secret, false)) else {
        return view(
            state,
            user,
            visit,
            "Password reset unsuccessful",
            "confirm",
            context! {},
        );
    };
    if !staged {
        let mut response = redirect(&format!("{CONFIRM}{uid}/set-password/"));
        if let Some(mut session) = session {
            session.set_string(TOKEN_KEY, &token)?;
            if !session.save(&state.pool, secret).await? {
                return Ok(StatusCode::CONFLICT.into_response());
            }
        } else {
            let key =
                session::create_anonymous(&state.pool, secret, &json!({TOKEN_KEY:token})).await?;
            session_cookie(&mut response, &key, visit, true);
        }
        return Ok(response);
    }
    let mut errors = Errors::new();
    if let Some(fields) = &visit.fields {
        errors = forms(fields, &person);
        if errors.is_empty() {
            let hash = passwords::new_hash(&fields["new_password1"]).await?;
            let mut tx = state.pool.begin().await?;
            if !set_password(&mut tx, &person, &hash).await? {
                return Ok(StatusCode::CONFLICT.into_response());
            }
            if let Some(mut session) = session.take() {
                session.remove(TOKEN_KEY);
                if !session.save_on(&mut tx, secret).await? {
                    return Ok(StatusCode::CONFLICT.into_response());
                }
            }
            tx.commit().await?;
            return Ok(redirect(COMPLETE));
        }
    }
    view(
        state,
        user,
        visit,
        "Enter new password",
        "confirm",
        context! {errors,valid=>true,username=>person.username},
    )
}
