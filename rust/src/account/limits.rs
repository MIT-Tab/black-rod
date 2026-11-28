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
const MAX_ATTEMPT_WINDOW: Duration = Duration::from_secs(300);

pub(crate) fn cache() -> Limits {
    Limits::builder()
        // Recent attempts stay in the cache across the bucket's creation window.
        .time_to_idle(MAX_ATTEMPT_WINDOW)
        .max_capacity(8192)
        .build()
}

pub(super) fn ip(state: &AppState, request: &Request) -> String {
    let peer = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|source| source.0.ip());
    let address = if peer.is_some() && peer == state.trusted_proxy {
        // The trusted immediate proxy appends the address of its client.
        request
            .headers()
            .get_all("x-forwarded-for")
            .iter()
            .next_back()
            .and_then(|header| header.to_str().ok())
            .and_then(|header| header.rsplit(',').next())
            .and_then(|address| address.trim().parse::<IpAddr>().ok())
            .or(peer)
    } else {
        peer
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use sqlx::any::{AnyPoolOptions, install_default_drivers};

    #[tokio::test]
    async fn fallback_buckets_expire_after_inactivity() {
        let limits = cache();
        assert_eq!(limits.policy().time_to_idle(), Some(MAX_ATTEMPT_WINDOW));
        assert_eq!(limits.policy().time_to_live(), None);
    }

    #[tokio::test]
    async fn client_address_requires_a_matching_immediate_proxy() {
        install_default_drivers();
        let pool = AnyPoolOptions::new()
            .connect_lazy("sqlite::memory:")
            .unwrap();
        let state = AppState::new(pool, 2026).with_trusted_proxy("127.0.0.1".parse().unwrap());
        let mut request = Request::builder()
            .header("x-forwarded-for", "198.51.100.1, 203.0.113.27")
            .body(Body::empty())
            .unwrap();
        request
            .extensions_mut()
            .insert(ConnectInfo("192.0.2.5:1234".parse::<SocketAddr>().unwrap()));
        assert_eq!(ip(&state, &request), "ip:192.0.2.5");
        request
            .extensions_mut()
            .insert(ConnectInfo("127.0.0.1:1234".parse::<SocketAddr>().unwrap()));
        assert_eq!(ip(&state, &request), "ip:203.0.113.27");
        request
            .headers_mut()
            .insert("x-forwarded-for", "198.51.100.1, invalid".parse().unwrap());
        assert_eq!(ip(&state, &request), "ip:127.0.0.1");
    }
}
