use std::{
    net::{IpAddr, Ipv6Addr, SocketAddr},
    sync::Arc,
    time::Duration,
};

use axum::{
    extract::{ConnectInfo, Request},
    http::StatusCode,
    response::{Html, IntoResponse, Response},
};
use tokio::sync::Mutex;

use crate::AppState;

pub(crate) type Limits = moka::future::Cache<String, Arc<Mutex<Vec<f64>>>>;

pub(crate) fn cache() -> Limits {
    Limits::builder()
        .time_to_live(Duration::from_secs(300))
        .max_capacity(8192)
        .build()
}

pub(super) fn ip(request: &Request) -> String {
    let address = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|source| source.0.ip())
        .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::LOCALHOST));
    let address = match address {
        IpAddr::V6(address) => IpAddr::V6(Ipv6Addr::from(u128::from(address) & (u128::MAX << 64))),
        address => address,
    };
    format!("ip:{address}")
}

pub(super) fn user(id: i64) -> String {
    format!("user:{id}")
}

pub(super) fn digest(value: &str) -> String {
    hex::encode(ring::digest::digest(
        &ring::digest::SHA256,
        value.as_bytes(),
    ))
}

#[derive(Clone)]
pub(super) struct Attempt {
    key: String,
    time: f64,
    ttl: u32,
}

pub(super) async fn consume(
    state: &AppState,
    action: &str,
    subject: &str,
    limit: usize,
    ttl: u32,
) -> Option<Attempt> {
    let key = format!(":1:allauth:rl:{action}:{subject}");
    let cell = state
        .account_limits
        .get_with(key.clone(), async { Arc::new(Mutex::new(Vec::new())) })
        .await;
    let mut local = cell.lock().await;
    if let Some(cache) = &state.memcached
        && let Some((1, data)) = cache.read_raw(&key).await
        && let Ok(shared) = serde_pickle::from_slice::<Vec<f64>>(&data, Default::default())
    {
        *local = shared;
    }
    let time = chrono::Utc::now().timestamp_micros() as f64 / 1_000_000.0;
    local.retain(|recent| *recent > time - f64::from(ttl));
    if local.len() >= limit {
        return None;
    }
    local.insert(0, time);
    if let Some(cache) = &state.memcached
        && let Ok(data) = serde_pickle::to_vec(&*local, Default::default())
    {
        let _ = cache.write_raw(&key, 1, &data, ttl, true).await;
    }
    Some(Attempt { key, time, ttl })
}

impl Attempt {
    pub(super) async fn rollback(self, state: &AppState) {
        let Some(cell) = state.account_limits.get(&self.key).await else {
            return;
        };
        let mut local = cell.lock().await;
        if let Some(cache) = &state.memcached
            && let Some((1, data)) = cache.read_raw(&self.key).await
            && let Ok(shared) = serde_pickle::from_slice::<Vec<f64>>(&data, Default::default())
        {
            *local = shared;
        }
        local.retain(|value| *value != self.time);
        if let Some(cache) = &state.memcached
            && let Ok(data) = serde_pickle::to_vec(&*local, Default::default())
        {
            let _ = cache.write_raw(&self.key, 1, &data, self.ttl, true).await;
        }
    }
}

pub(super) fn denied() -> Response {
    (
        StatusCode::TOO_MANY_REQUESTS,
        Html(
            "<html><head><title>Too Many Requests</title></head><body>
            <h1>429 Too Many Requests</h1>
            <p>You have sent too many requests. Please try again later.</p></body></html>",
        ),
    )
        .into_response()
}
