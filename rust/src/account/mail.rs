use std::{sync::LazyLock, time::Duration};

use lettre::{AsyncSmtpTransport, AsyncTransport, Message, Tokio1Executor, message::Mailbox};

use crate::{ApiError, AppState};

static TRANSPORT: LazyLock<Option<AsyncSmtpTransport<Tokio1Executor>>> = LazyLock::new(|| {
    let target =
        std::env::var("BLACK_ROD_SMTP_URL").unwrap_or_else(|_| "smtp://localhost:25".to_owned());
    AsyncSmtpTransport::<Tokio1Executor>::from_url(&target)
        .ok()
        .map(|options| options.timeout(Some(Duration::from_secs(10))).build())
});

pub(super) async fn site(state: &AppState) -> Result<String, ApiError> {
    Ok(
        sqlx::query_scalar::<_, String>("SELECT name FROM django_site WHERE id=1")
            .fetch_optional(&state.pool)
            .await?
            .unwrap_or_else(|| "example.com".to_owned()),
    )
}

pub(super) async fn send(
    to: &str,
    site: &str,
    subject: &str,
    body: String,
) -> Result<(), ApiError> {
    let from =
        std::env::var("BLACK_ROD_FROM_EMAIL").unwrap_or_else(|_| "webmaster@localhost".to_owned());
    let address = |value: &str| -> Result<Mailbox, ApiError> {
        value.parse().map_err(|_| ApiError::Serialization)
    };
    let message = Message::builder()
        .from(address(&from)?)
        .to(address(to)?)
        .subject(format!("[{site}] {subject}"))
        .body(body)
        .map_err(|_| ApiError::Serialization)?;
    let transport = TRANSPORT.as_ref().ok_or(ApiError::Serialization)?;
    transport
        .send(message)
        .await
        .map_err(|_| ApiError::Serialization)?;
    Ok(())
}
