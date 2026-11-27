use std::sync::Arc;

use serde_pickle::{HashableValue, Value as Pickle};
use sqlx::AnyPool;

use super::{FRESH, Live, Payload, STALE};
use crate::{
    ApiError,
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
    pool: &AnyPool,
    season: i32,
    cache: &Memcached,
    key: &str,
    lock_held: bool,
) -> Result<Arc<Payload>, ApiError> {
    let result = live.rebuild(pool, season, Some(cache)).await;
    if lock_held {
        cache.delete_raw(&format!("{key}:lock")).await;
    }
    if let Ok(payload) = &result
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
    result
}

pub(super) async fn active(
    live: &Arc<Live>,
    pool: &AnyPool,
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
        let pool = pool.clone();
        let cache = cache.clone();
        tokio::spawn(async move {
            let _guard = guard;
            if cache
                .write_raw(&format!("{key}:lock"), 2, b"1", LOCK_SECONDS, false)
                .await
            {
                let _ = refresh(&live, &pool, season, &cache, &key, true).await;
            }
        });
    }
    stale.map_or_else(Vec::new, |payload| payload.active_sources.clone())
}

pub(super) async fn data(
    live: &Live,
    pool: &AnyPool,
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
    refresh(live, pool, season, cache, &key, locked).await
}

pub(super) async fn invalidate(cache: &Memcached, season: i32) {
    let key = key(season);
    let stale = format!("{key}:stale");
    tokio::join!(cache.delete_raw(&key), cache.delete_raw(&stale));
}
