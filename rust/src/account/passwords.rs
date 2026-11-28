use std::{
    collections::{HashMap, HashSet},
    io::Read,
    sync::LazyLock,
};

use axum::{
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use flate2::read::GzDecoder;
use minijinja::context;
use sqlx::Row;

use super::{Errors, Fields, redirect, render, visit};
use crate::{ApiError, AppState, flash, login, password, session};

const CHANGE: &str = "/accounts/password/change/";
const SET: &str = "/accounts/password/set/";

static COMMON: LazyLock<HashSet<String>> = LazyLock::new(|| {
    // Django's bundled list preserves the validation behavior of existing forms.
    let bytes = include_bytes!("../../assets/common-passwords.txt.gz");
    let mut contents = String::new();
    GzDecoder::new(bytes.as_slice())
        .read_to_string(&mut contents)
        .expect("common password list");
    contents
        .lines()
        .map(|word| word.trim().to_owned())
        .collect()
});

fn similarity(password: &str, value: &str) -> bool {
    let lower = value.to_lowercase();
    lower
        .split(|ch: char| !ch.is_alphanumeric() && ch != '_')
        .chain(std::iter::once(lower.as_str()))
        .filter(|part| !part.is_empty())
        .any(|part| {
            let password_length = password.chars().count();
            let part_length = part.chars().count();
            if 2 * password_length.min(part_length) * 10 < 7 * (password_length + part_length) {
                return false;
            }
            let mut remaining = HashMap::<char, usize>::new();
            for character in part.chars() {
                *remaining.entry(character).or_default() += 1;
            }
            let matches = password
                .chars()
                .filter(|character| {
                    if let Some(count) = remaining.get_mut(character).filter(|count| **count > 0) {
                        *count -= 1;
                        true
                    } else {
                        false
                    }
                })
                .count();
            20 * matches >= 7 * (password_length + part_length)
        })
}

pub(super) fn validation(fields: &Fields, attributes: [&str; 4]) -> Errors {
    let mut errors = Errors::new();
    let first = fields.get("password1").map_or("", String::as_str);
    let second = fields.get("password2").map_or("", String::as_str);
    if first.is_empty() {
        errors.insert("password1", vec!["This field is required.".to_owned()]);
    } else {
        let mut validation = Vec::new();
        let lower = first.to_lowercase();
        for (attribute, label) in
            attributes
                .into_iter()
                .zip(["username", "first name", "last name", "email address"])
        {
            if !attribute.is_empty() && similarity(&lower, attribute) {
                validation.push(format!("The password is too similar to the {label}."));
                break;
            }
        }
        if first.chars().count() < 8 {
            validation.push(
                "This password is too short. It must contain at least 8 characters.".to_owned(),
            );
        }
        if COMMON.contains(lower.trim()) {
            validation.push("This password is too common.".to_owned());
        }
        static DIGITS: LazyLock<regex::Regex> = LazyLock::new(|| {
            regex::Regex::new(r"^[\p{Nd}²³¹⁰⁴-⁹₀-₉]+$").expect("numeric passwords")
        });
        if DIGITS.is_match(first) {
            validation.push("This password is entirely numeric.".to_owned());
        }
        if !validation.is_empty() {
            errors.insert("password1", validation);
        }
    }
    if second.is_empty() {
        errors.insert("password2", vec!["This field is required.".to_owned()]);
    } else if !errors.contains_key("password1") && first != second {
        errors.insert(
            "password2",
            vec!["You must type the same password each time.".to_owned()],
        );
    }
    errors
}

pub(super) async fn new_hash(password: &str) -> Result<String, ApiError> {
    let bytes = password.as_bytes().to_owned();
    password::run_blocking(move || password::encode(&bytes))
        .await
        .map_err(|_| ApiError::Serialization)?
        .map_err(|_| ApiError::Serialization)
}

pub(crate) async fn change(State(state): State<AppState>, request: Request) -> Response {
    match change_page(&state, request).await {
        Ok(response) => response,
        Err(error) => {
            eprintln!("Account password form failed: {error:?}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

async fn change_page(state: &AppState, request: Request) -> Result<Response, ApiError> {
    let setting = request.uri().path() == SET;
    let user = session::user(&state.pool, request.headers(), state.secret.as_deref()).await?;
    let Some(id) = user.id else {
        return Ok(login::redirect_to_login(request.uri()));
    };
    let row = sqlx::query(
        "SELECT username,email,first_name,last_name,password FROM core_user WHERE id=$1",
    )
    .bind(id)
    .fetch_one(&state.pool)
    .await?;
    let original: &str = row.try_get("password")?;
    if setting != original.starts_with('!') {
        return Ok(redirect(if setting { CHANGE } else { SET }));
    }
    let path = if setting { SET } else { CHANGE };
    let title = if setting {
        "Set Password"
    } else {
        "Change Password"
    };
    let label = if setting { "Password" } else { "New Password" };
    let visit = match visit(state, request).await {
        Ok(visit) => visit,
        Err(response) => return Ok(*response),
    };
    let mut errors = Errors::new();
    if visit.fields.is_some()
        && super::limits::consume(state, "change_password", &super::limits::user(id), 5, 60)
            .await
            .is_none()
    {
        return Ok(super::limits::denied());
    }
    if let Some(fields) = &visit.fields {
        errors = validation(
            fields,
            [
                row.try_get("username")?,
                row.try_get("first_name")?,
                row.try_get("last_name")?,
                row.try_get("email")?,
            ],
        );
        let mut compare_against = original.to_owned();
        if !setting {
            let provided = fields
                .get("oldpassword")
                .map_or("", String::as_str)
                .as_bytes()
                .to_owned();
            if provided.is_empty() {
                errors.insert("oldpassword", vec!["This field is required.".to_owned()]);
            } else {
                let current = compare_against.clone();
                let verified = password::run_blocking(move || {
                    password::verify_and_upgrade(&provided, Some(&current))
                })
                .await
                .map_err(|_| ApiError::Serialization)?;
                if let Some(Some(upgrade)) = verified {
                    // Django may upgrade the current digest while validating the form.
                    let updated =
                        sqlx::query("UPDATE core_user SET password=$1 WHERE id=$2 AND password=$3")
                            .bind(&upgrade)
                            .bind(id)
                            .bind(&compare_against)
                            .execute(&state.pool)
                            .await?;
                    if updated.rows_affected() != 1 {
                        return Ok(StatusCode::CONFLICT.into_response());
                    }
                    compare_against = upgrade;
                } else if verified.is_none() {
                    errors.insert(
                        "oldpassword",
                        vec!["Please type your current password.".to_owned()],
                    );
                }
            }
        }
        if errors.is_empty() {
            let secret = state.secret.as_deref().ok_or(ApiError::Serialization)?;
            let session = session::StoredSession::read(&state.pool, &visit.headers, secret)
                .await?
                .ok_or(ApiError::Serialization)?;
            let hash = new_hash(&fields["password1"]).await?;
            let mut tx = state.pool.begin().await?;
            let changed = sqlx::query(
                "UPDATE core_user SET password=$1 WHERE id=$2 AND is_active AND password=$3",
            )
            .bind(&hash)
            .bind(id)
            .bind(&compare_against)
            .execute(&mut *tx)
            .await?;
            if changed.rows_affected() != 1 {
                return Ok(StatusCode::CONFLICT.into_response());
            }
            let (key, age) = session.rotate_password(&mut tx, secret, &hash).await?;
            tx.commit().await?;
            let mut response = redirect(if visit.next.is_empty() {
                CHANGE
            } else {
                &visit.next
            });
            login::set_cookie(&mut response, "sessionid", &key, age, visit.secure(), true);
            let notice = if setting {
                "Password successfully set."
            } else {
                "Password successfully changed."
            };
            flash::push(
                &mut response,
                &visit.headers,
                secret,
                25,
                notice,
                visit.secure(),
            );
            return Ok(response);
        }
    }
    render(
        state,
        &user,
        "account/password.html",
        &visit,
        context! {
            title, heading=>title, action=>path, old_password=>!setting, password_label=>label, errors
        },
    )
}
