use std::sync::Arc;

use serde_pickle::{HashableValue, Value as Pickle};

use super::{FRESH, Live, Payload, STALE};
use crate::{
    ApiError, AppState,
    cache::{Memcached, python_value},
};

const LOCK_SECONDS: u32 = 60;

fn key(season: i32) -> String {
    format!(":1:live-standings:v4:{season}")
}

async fn load(cache: &Memcached, key: &str) -> Option<Arc<Payload>> {
    let (1, bytes) = cache.read_raw(key).await? else {
        return None;
    };
    serde_pickle::from_slice(&bytes, Default::default())
        .ok()
        .map(Arc::new)
}

fn serialize(payload: &Payload) -> Result<Vec<u8>, serde_pickle::Error> {
    let fields = [
        ("season", Pickle::String(payload.season.clone())),
        ("boards", python_value(&payload.boards)),
        (
            "active_sources",
            Pickle::List(
                payload
                    .active_sources
                    .iter()
                    .cloned()
                    .map(Pickle::String)
                    .collect(),
            ),
        ),
    ];
    let value = Pickle::Dict(
        fields
            .into_iter()
            .map(|(key, value)| (HashableValue::String(key.to_owned()), value))
            .collect(),
    );
    serde_pickle::value_to_vec(&value, Default::default())
}

async fn refresh(
    live: &Live,
    state: &AppState,
    season: i32,
    cache: &Memcached,
    key: &str,
    lock_held: bool,
) -> Result<Arc<Payload>, ApiError> {
    if lock_held && let Some(found) = load(cache, key).await {
        cache.delete_raw(&format!("{key}:lock")).await;
        return Ok(found);
    }
    let result = live.rebuild(state, season).await;
    if lock_held {
        publish_and_unlock(cache, key, result.as_deref().ok()).await;
    }
    result
}

async fn publish_and_unlock(cache: &Memcached, key: &str, payload: Option<&Payload>) {
    if let Some(payload) = payload
        && let Ok(bytes) = serialize(payload)
    {
        cache
            .write_raw(key, 1, &bytes, FRESH.as_secs() as u32, true)
            .await;
        cache
            .write_raw(
                &format!("{key}:stale"),
                1,
                &bytes,
                STALE.as_secs() as u32,
                true,
            )
            .await;
    }
    cache.delete_raw(&format!("{key}:lock")).await;
}

pub(super) async fn active(
    live: &Arc<Live>,
    state: &AppState,
    season: i32,
    cache: &Memcached,
) -> Vec<String> {
    let key = key(season);
    if let Some(found) = load(cache, &key).await {
        return found.active_sources.clone();
    }
    let stale = load(cache, &format!("{key}:stale")).await;
    if let Ok(guard) = live.refreshing.clone().try_lock_owned() {
        let live = live.clone();
        let state = state.clone();
        let cache = cache.clone();
        tokio::spawn(async move {
            let _guard = guard;
            if cache
                .write_raw(&format!("{key}:lock"), 2, b"1", LOCK_SECONDS, false)
                .await
            {
                let _ = refresh(&live, &state, season, &cache, &key, true).await;
            }
        });
    }
    stale.map_or_else(Vec::new, |payload| payload.active_sources.clone())
}

pub(super) async fn data(
    live: &Live,
    state: &AppState,
    season: i32,
    cache: &Memcached,
) -> Result<Arc<Payload>, ApiError> {
    let key = key(season);
    if let Some(found) = load(cache, &key).await {
        return Ok(found);
    }
    let stale = load(cache, &format!("{key}:stale")).await;
    if live.refreshing.try_lock().is_err()
        && let Some(stale) = stale.as_ref()
    {
        return Ok(stale.clone());
    }
    let _guard = live.refreshing.lock().await;
    if let Some(found) = load(cache, &key).await {
        return Ok(found);
    }
    let locked = cache
        .write_raw(&format!("{key}:lock"), 2, b"1", LOCK_SECONDS, false)
        .await;
    if !locked && let Some(stale) = stale {
        return Ok(stale);
    }
    refresh(live, state, season, cache, &key, locked).await
}

pub(super) async fn invalidate(cache: &Memcached, season: i32) {
    let key = key(season);
    let stale = format!("{key}:stale");
    tokio::join!(cache.delete_raw(&key), cache.delete_raw(&stale));
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{
        io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
        net::TcpListener,
    };

    #[tokio::test]
    async fn live_refresh_releases_the_lock_after_both_publications() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let cache = Memcached::new(listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            for key in ["test-live", "test-live:stale"] {
                let (stream, _) = listener.accept().await.unwrap();
                let mut stream = BufReader::new(stream);
                let mut header = String::new();
                stream.read_line(&mut header).await.unwrap();
                let fields: Vec<_> = header.split_ascii_whitespace().collect();
                assert_eq!(&fields[..3], &["set", key, "1"]);
                let size = fields[4].parse::<usize>().unwrap();
                let mut body = vec![0; size + 2];
                stream.read_exact(&mut body).await.unwrap();
                stream.get_mut().write_all(b"STORED\r\n").await.unwrap();
            }
            let (stream, _) = listener.accept().await.unwrap();
            let mut stream = BufReader::new(stream);
            let mut line = String::new();
            stream.read_line(&mut line).await.unwrap();
            assert_eq!(line, "delete test-live:lock\r\n");
            stream.get_mut().write_all(b"DELETED\r\n").await.unwrap();
        });
        let payload = Payload {
            season: "2025".to_owned(),
            boards: serde_json::json!({}),
            active_sources: Vec::new(),
        };
        publish_and_unlock(&cache, "test-live", Some(&payload)).await;
        server.await.unwrap();
    }
}
