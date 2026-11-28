use std::{
    io,
    net::{IpAddr, Ipv6Addr, SocketAddr},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::{ConnectInfo, Request},
    http::StatusCode,
    response::{Html, IntoResponse, Response},
};
use tokio::sync::Mutex;

use crate::{AppState, cache::Memcached};

pub(crate) type Limits = moka::future::Cache<String, Arc<Mutex<Bucket>>>;

#[derive(Default)]
pub(crate) struct Bucket {
    attempts: Vec<f64>,
    pending: Vec<f64>,
    rollbacks: Vec<f64>,
}
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

fn record_attempt(attempts: &mut Vec<f64>, now: f64, limit: usize, ttl: u32) -> Option<f64> {
    attempts.retain(|recent| *recent > now - f64::from(ttl));
    if attempts.len() >= limit {
        return None;
    }
    let mut time = now;
    while attempts.contains(&time) {
        time = time.next_up();
    }
    attempts.insert(0, time);
    Some(time)
}

async fn update_shared<T>(
    cache: &Memcached,
    key: &str,
    local: &mut Bucket,
    now: f64,
    ttl: u32,
    mut update: impl FnMut(&mut Vec<f64>) -> Option<T>,
) -> io::Result<Option<T>> {
    const MAX_RETRIES: usize = 16;
    let oldest = now - f64::from(ttl);
    local.pending.retain(|time| *time > oldest);
    local.rollbacks.retain(|time| *time > oldest);
    let reconcile = !local.pending.is_empty() || !local.rollbacks.is_empty();
    for _ in 0..MAX_RETRIES {
        let (mut attempts, version) = match cache.read_versioned(key).await? {
            Some((1, data, version)) => (
                serde_pickle::from_slice(&data, Default::default()).map_err(io::Error::other)?,
                Some(version),
            ),
            None => (Vec::new(), None),
            Some(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Invalid rate limit value",
                ));
            }
        };
        if reconcile {
            attempts.retain(|time| *time > oldest && !local.rollbacks.contains(time));
            for time in &local.pending {
                if !attempts.contains(time) {
                    attempts.push(*time);
                }
            }
            attempts.sort_unstable_by(|left, right| right.total_cmp(left));
        }
        let result = update(&mut attempts);
        if result.is_none() && !reconcile {
            local.attempts = attempts;
            return Ok(None);
        }
        let bytes =
            serde_pickle::to_vec(&attempts, Default::default()).map_err(io::Error::other)?;
        if cache.compare_and_swap(key, 1, &bytes, ttl, version).await? {
            local.attempts = attempts;
            local.pending.clear();
            local.rollbacks.clear();
            return Ok(result);
        }
    }
    Ok(None)
}

fn epoch_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
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
        .get_with(key.clone(), async {
            Arc::new(Mutex::new(Bucket::default()))
        })
        .await;
    let mut local = cell.lock().await;
    let now = epoch_seconds();
    if let Some(cache) = &state.memcached
        && let Ok(result) = update_shared(cache, &key, &mut local, now, ttl, |attempts| {
            record_attempt(attempts, now, limit, ttl)
        })
        .await
    {
        return result.map(|time| Attempt { key, time, ttl });
    }
    let time = record_attempt(&mut local.attempts, now, limit, ttl)?;
    if state.memcached.is_some() {
        local.pending.push(time);
    }
    Some(Attempt { key, time, ttl })
}

impl Attempt {
    pub(super) async fn rollback(self, state: &AppState) {
        let cell = state
            .account_limits
            .get_with(self.key.clone(), async {
                Arc::new(Mutex::new(Bucket::default()))
            })
            .await;
        let mut local = cell.lock().await;
        local.pending.retain(|time| *time != self.time);
        local.attempts.retain(|time| *time != self.time);
        if let Some(cache) = &state.memcached {
            local.rollbacks.push(self.time);
            let _ = update_shared(
                cache,
                &self.key,
                &mut local,
                epoch_seconds(),
                self.ttl,
                |_| None::<()>,
            )
            .await;
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
