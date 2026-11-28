use std::{cell::RefCell, future::Future};

use axum::http::{Method, StatusCode};
use sentry::protocol::{Event, Exception};

tokio::task_local! {
    static CAUSE: RefCell<Option<Exception>>;
}

pub(crate) async fn with_cause<T>(request: impl Future<Output = T>) -> (T, Option<Exception>) {
    CAUSE
        .scope(RefCell::new(None), async {
            let response = request.await;
            (response, CAUSE.with(RefCell::take))
        })
        .await
}

fn remember(kind: &str, detail: Option<String>) {
    // Capture classifications and the original call stack without values from a
    // database row, HTTP body, or template context.
    let _ = CAUSE.try_with(|cause| {
        let mut cause = cause.borrow_mut();
        if cause.is_none() {
            *cause = Some(Exception {
                ty: kind.to_owned(),
                value: detail,
                stacktrace: sentry::integrations::backtrace::current_stacktrace(),
                ..Default::default()
            });
        }
    });
}

pub(crate) fn remember_database(error: &sqlx::Error) {
    use sqlx::Error;
    let (name, code) = match error {
        Error::Database(error) => {
            let code = error.code().filter(|code| {
                !code.is_empty()
                    && code.len() <= 5
                    && code.bytes().all(|byte| byte.is_ascii_alphanumeric())
            });
            ("DatabaseError", code.map(|code| code.into_owned()))
        }
        Error::RowNotFound => ("DatabaseRowNotFound", None),
        Error::PoolTimedOut => ("DatabasePoolTimeout", None),
        Error::PoolClosed => ("DatabasePoolClosed", None),
        Error::ColumnDecode { .. } | Error::Decode(_) => ("DatabaseDecodeError", None),
        Error::ColumnNotFound(_) | Error::ColumnIndexOutOfBounds { .. } => {
            ("DatabaseColumnError", None)
        }
        Error::Io(_) | Error::Tls(_) | Error::Protocol(_) => ("DatabaseConnectionError", None),
        _ => ("DatabaseQueryError", None),
    };
    remember(name, code);
}

pub(crate) fn remember_json(error: &serde_json::Error) {
    remember("JsonError", Some(format!("{:?}", error.classify())));
}

pub(crate) fn remember_template(kind: minijinja::ErrorKind) {
    remember("TemplateError", Some(format!("{kind:?}")));
}

pub(crate) fn event(
    route: &str,
    method: &Method,
    status: StatusCode,
    cause: Option<Exception>,
) -> Event<'static> {
    Event {
        level: sentry::Level::Error,
        transaction: Some(route.to_owned()),
        message: Some(format!("HTTP {} in {route}", status.as_u16())),
        tags: [
            ("http.method".to_owned(), method.to_string()),
            ("http.status_code".to_owned(), status.as_u16().to_string()),
        ]
        .into_iter()
        .collect(),
        exception: cause.into_iter().collect::<Vec<_>>().into(),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn server_reports_distinguish_causes_without_database_values() {
        let (_, failure) = with_cause(async {
            remember_database(&sqlx::Error::ColumnNotFound(
                "private fixture value".to_owned(),
            ));
        })
        .await;
        let (_, timeout) = with_cause(async {
            remember_database(&sqlx::Error::PoolTimedOut);
        })
        .await;
        let route = "/core/debaters/{id}";
        let failure = event(
            route,
            &Method::GET,
            StatusCode::INTERNAL_SERVER_ERROR,
            failure,
        );
        let timeout = event(
            route,
            &Method::GET,
            StatusCode::INTERNAL_SERVER_ERROR,
            timeout,
        );

        assert_eq!(failure.transaction.as_deref(), Some(route));
        assert_eq!(failure.tags["http.status_code"], "500");
        assert_eq!(failure.exception.values[0].ty, "DatabaseColumnError");
        assert_eq!(timeout.exception.values[0].ty, "DatabasePoolTimeout");
        assert!(
            !serde_json::to_string(&failure)
                .unwrap()
                .contains("private fixture value")
        );
    }
}
