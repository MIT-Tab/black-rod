use std::time::Duration;

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode, header},
};
use black_rod::{AppState, PrivateTokens, app};
use http_body_util::BodyExt;
use serde_json::Value;
use sqlx::{
    AnyPool,
    any::{AnyPoolOptions, install_default_drivers},
};
use tower::ServiceExt;

async fn fixture() -> AnyPool {
    install_default_drivers();
    let pool = AnyPoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .unwrap();
    sqlx::query(
        "CREATE TABLE core_school (
            id BIGINT PRIMARY KEY, name TEXT NOT NULL, included_in_oty BOOLEAN, temporary BOOLEAN
        )",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "CREATE TABLE core_debater (
            id BIGINT PRIMARY KEY, school_id BIGINT, latest_season TEXT, email TEXT, synthetic BOOLEAN, temporary BOOLEAN
        )",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO core_school VALUES (7, 'Harvard', TRUE, FALSE), (8, 'Temporary', FALSE, TRUE)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("INSERT INTO core_debater VALUES (1, 7, '2024', 'fixture@example.test', FALSE, FALSE), (2, 8, '2026', 'hidden@example.test', TRUE, FALSE)")
        .execute(&pool)
        .await
        .unwrap();
    pool
}

async fn school_list(router: &Router, host: &str) -> Value {
    let response = router
        .clone()
        .oneshot(
            Request::get("/api/schools/")
                .header(header::HOST, host)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn public_directory_keeps_successes_cached_and_partitions_origins() {
    let pool = fixture().await;
    let router = app(AppState::new(pool.clone(), 2026));
    let first = school_list(&router, "localhost").await;
    assert_eq!(first["count"], 1);
    assert_eq!(
        first["schools"][0]["api_url"],
        "http://localhost/api/schools/7/detail/"
    );

    sqlx::query("UPDATE core_school SET name = 'New name' WHERE id = 7")
        .execute(&pool)
        .await
        .unwrap();

    assert_eq!(school_list(&router, "localhost").await, first);
    let other_origin = school_list(&router, "127.0.0.1").await;
    assert_eq!(other_origin["schools"][0]["name"], "New name");
    assert_eq!(
        other_origin["schools"][0]["url"],
        "http://127.0.0.1/core/schools/7"
    );
}

#[tokio::test]
async fn cache_can_be_disabled_for_measurement() {
    let pool = fixture().await;
    let router = app(AppState::with_cache_lifetime(
        pool.clone(),
        2026,
        Duration::ZERO,
    ));
    let first = school_list(&router, "localhost").await;
    assert_eq!(first["count"], 1);

    sqlx::query("UPDATE core_debater SET latest_season = '2023' WHERE id = 1")
        .execute(&pool)
        .await
        .unwrap();
    assert_eq!(school_list(&router, "localhost").await["count"], 0);
}

#[tokio::test]
async fn absolute_links_use_the_configured_origin() {
    let state = AppState::new(fixture().await, 2026);
    assert!(
        state
            .clone()
            .with_public_origin("https://host.test/subpath")
            .is_err()
    );
    assert!(
        state
            .clone()
            .with_public_origin("https://user@host.test")
            .is_err()
    );
    let router = app(state.with_public_origin("https://host.test").unwrap());
    let result = school_list(&router, "localhost").await;
    assert_eq!(
        result["schools"][0]["api_url"],
        "https://host.test/api/schools/7/detail/"
    );
}

async fn private_mittab(router: &Router, token: Option<&str>, input: Value) -> (StatusCode, Value) {
    let mut request = Request::post("/api/private/debater-emails/")
        .header(header::HOST, "localhost")
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        request = request.header(header::AUTHORIZATION, token);
    }
    let response = router
        .clone()
        .oneshot(request.body(Body::from(input.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&body).unwrap())
}

#[tokio::test]
async fn private_lookup_requires_bearer_and_hides_synthetic_contact_details() {
    use serde_json::json;

    let state = AppState::new(fixture().await, 2026);
    let disabled = app(state.clone());
    let enabled = app(state.with_private_tokens(PrivateTokens::new("", "current, old")));
    for token in [
        None,
        Some("old"),
        Some("bearer old"),
        Some("Bearer invalid"),
    ] {
        let (status, _) = private_mittab(&enabled, token, json!({"debater_ids":[1]})).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
    }
    let (status, _) = private_mittab(
        &disabled,
        Some("Bearer current"),
        json!({"debater_ids":[1]}),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, body) = private_mittab(
        &enabled,
        Some("Bearer old"),
        json!({"debater_ids":[1,2,3,"1"]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body,
        json!({"debaters":[
            {"id":1, "email":"fixture@example.test"},
            {"id":2, "email":null},
            {"id":3, "email":null}
        ]})
    );
}

#[tokio::test]
async fn private_lookup_applies_the_bound_after_deduplication() {
    use serde_json::json;

    let router = app(
        AppState::new(fixture().await, 2026).with_private_tokens(PrivateTokens::new("", "key"))
    );
    let token = Some("Bearer key");
    for (input, message) in [
        (json!({"debater_ids": "1"}), "debater_ids must be a list."),
        (
            json!({"debater_ids":[1, null]}),
            "debater_ids must contain only integers.",
        ),
        (
            json!({"debater_ids":(1..=201).collect::<Vec<_>>()}),
            "At most 200 debater ids may be requested.",
        ),
    ] {
        let (status, data) = private_mittab(&router, token, input).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(data["detail"], message);
    }
    let (status, data) = private_mittab(&router, token, json!({"debater_ids":vec![1; 250]})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(data["debaters"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn append_slash_redirects_only_to_registered_routes() {
    let router = app(AppState::new(fixture().await, 2026));
    for (path, expected) in [
        (
            "/api/debaters/%37/detail?limit=2",
            Some("/api/debaters/7/detail/?limit=2"),
        ),
        (
            "/core/my-profile?source=legacy",
            Some("/core/my-profile/?source=legacy"),
        ),
        ("/api/debaters/not-a-number/detail", None),
        ("/api/debaters/not-a-number/detail/", None),
        ("/api/tournaments/not-a-number/detail/", None),
        ("/core/missing-endpoint", None),
    ] {
        let response = router
            .clone()
            .oneshot(
                Request::get(path)
                    .header(header::HOST, "localhost")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        if let Some(location) = expected {
            assert_eq!(response.status(), StatusCode::MOVED_PERMANENTLY, "{path}");
            assert_eq!(response.headers()[header::LOCATION], location);
        } else {
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
            assert!(!response.headers().contains_key(header::LOCATION));
        }
    }
}

#[tokio::test]
async fn reading_an_editor_upload_does_not_reserve_an_image_decoder() {
    use axum::body::Bytes;
    use tokio::{sync::mpsc, time::timeout};
    use tokio_stream::wrappers::ReceiverStream;

    const TOKEN: &str = "abcdefghijklmnopqrstuvwxyzABCDEF";
    const PATH: &str = "/summernote/upload_attachment/";
    const TYPE: &str = "multipart/form-data; boundary=editor-fixture";
    let router = app(AppState::new(fixture().await, 2026));
    let request = |body| {
        Request::post(PATH)
            .header(header::HOST, "localhost")
            .header(header::CONTENT_TYPE, TYPE)
            .header(header::COOKIE, format!("csrftoken={TOKEN}"))
            .header("x-csrftoken", TOKEN)
            .body(body)
            .unwrap()
    };
    let prefix = "--editor-fixture\r\nContent-Disposition: form-data; name=\"files\"; filename=\"fixture.png\"\r\n\r\n";
    let mut partial_uploads = Vec::new();
    for _ in 0..2 {
        let (sender, receiver) = mpsc::channel::<Result<Bytes, std::io::Error>>(1);
        sender
            .send(Ok(Bytes::from_static(prefix.as_bytes())))
            .await
            .unwrap();
        let service = router.clone();
        let incomplete = request(Body::from_stream(ReceiverStream::new(receiver)));
        let task = tokio::spawn(service.oneshot(incomplete));
        drop(
            timeout(Duration::from_secs(5), sender.reserve())
                .await
                .unwrap()
                .unwrap(),
        );
        partial_uploads.push((sender, task));
    }

    let body = Body::from(format!("{prefix}not an image\r\n--editor-fixture--\r\n"));
    let rejected = timeout(Duration::from_secs(5), router.oneshot(request(body)))
        .await
        .expect("a complete upload should reach validation while other requests are still sending")
        .unwrap();
    assert_eq!(rejected.status(), StatusCode::BAD_REQUEST);
    let contents = rejected.into_body().collect().await.unwrap().to_bytes();
    let error: Value = serde_json::from_slice(&contents).unwrap();
    assert!(
        error["message"]
            .as_str()
            .unwrap()
            .starts_with("Upload a valid image.")
    );
    for (sender, task) in partial_uploads {
        task.abort();
        drop(sender);
    }
}

#[tokio::test]
async fn llm_browsing_preserves_a_successful_large_api_response() {
    let pool = fixture().await;
    let school_name = format!("{}large-response-marker", "x".repeat(32 * 1024 * 1024));
    sqlx::query("UPDATE core_school SET name=$1 WHERE id=7")
        .bind(school_name)
        .execute(&pool)
        .await
        .unwrap();
    let router = app(AppState::new(pool, 2026));
    let response = router
        .oneshot(
            Request::get("/llm/?endpoint=/api/schools/")
                .header(header::HOST, "localhost")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let html = response.into_body().collect().await.unwrap().to_bytes();
    assert!(html.len() > 32 * 1024 * 1024);
    assert!(String::from_utf8_lossy(&html).contains("large-response-marker"));
}

#[tokio::test]
async fn head_negotiates_the_same_public_api_representation_without_a_body() {
    let router = app(AppState::new(fixture().await, 2026));
    for accept in ["text/html", "application/json; indent=4"] {
        let request = |method| {
            Request::builder()
                .method(method)
                .uri("/api/schools/")
                .header(header::HOST, "localhost")
                .header(header::ACCEPT, accept)
                .body(Body::empty())
                .unwrap()
        };
        let get = router.clone().oneshot(request("GET")).await.unwrap();
        let head = router.clone().oneshot(request("HEAD")).await.unwrap();
        assert_eq!(head.status(), StatusCode::OK);
        for name in [header::CONTENT_TYPE, header::VARY, header::ALLOW] {
            assert_eq!(
                head.headers().get(&name),
                get.headers().get(&name),
                "{name}"
            );
        }
        let length: usize = head.headers()[header::CONTENT_LENGTH]
            .to_str()
            .unwrap()
            .parse()
            .unwrap();
        if accept == "text/html" {
            assert!(
                head.headers()[header::SET_COOKIE]
                    .to_str()
                    .unwrap()
                    .starts_with("csrftoken=")
            );
            assert!(length > 1000);
        } else {
            let get = get.into_body().collect().await.unwrap().to_bytes();
            assert_eq!(length, get.len());
            assert!(String::from_utf8_lossy(&get).contains("\n    "));
        }
        assert!(
            head.into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .is_empty()
        );
    }
}
