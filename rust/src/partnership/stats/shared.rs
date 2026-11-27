use std::sync::Arc;

use serde_pickle::{DeOptions, HashableValue, Value, value_from_slice};
use uuid::Uuid;

use crate::{ApiError, AppState, cache::Memcached};

use super::{CACHE_TTL, compute};

const PREFIX: &str = ":1:public-partnership-stats:v3";
const GENERATION: &str = ":1:public-partnership-stats:v3:generation";
const PICKLE: u32 = 1;
const TEXT: u32 = 16;
const PYTHON_TRUE: &[u8] = b"\x80\x05\x88.";

fn uuid_pickle(uuid: Uuid) -> Vec<u8> {
    let mut bytes = b"\x80\x02cuuid\nUUID\n)\x81}X\x03\x00\x00\x00int\x8a\x11".to_vec();
    bytes.extend_from_slice(&uuid.as_u128().to_le_bytes());
    bytes.extend_from_slice(b"\x00sb.");
    bytes
}

fn uuid_value(bytes: &[u8]) -> Option<Uuid> {
    let Value::Dict(mut fields) =
        value_from_slice(bytes, DeOptions::new().keep_restore_state()).ok()?
    else {
        return None;
    };
    let value = match fields.remove(&HashableValue::String("int".to_owned()))? {
        Value::Int(number) => number.to_string().parse::<u128>().ok()?,
        Value::I64(number) => number.try_into().ok()?,
        _ => return None,
    };
    Some(Uuid::from_u128(value))
}

async fn existing_generation(cache: &Memcached) -> Option<Uuid> {
    let (flags, data) = cache.read_raw(GENERATION).await?;
    (flags == PICKLE).then(|| uuid_value(&data)).flatten()
}

async fn generation(cache: &Memcached) -> Option<Uuid> {
    if let Some(current) = existing_generation(cache).await {
        return Some(current);
    }
    let proposed = Uuid::new_v4();
    if cache
        .write_raw(GENERATION, PICKLE, &uuid_pickle(proposed), 0, false)
        .await
    {
        return Some(proposed);
    }
    existing_generation(cache).await
}

fn fragment_key(generation: Uuid) -> String {
    format!("{PREFIX}:{}", generation.simple())
}

fn fragment(flags: u32, data: Vec<u8>) -> Option<Arc<str>> {
    let text = match flags {
        TEXT => String::from_utf8(data).ok()?,
        PICKLE => match value_from_slice(&data, DeOptions::new().keep_restore_state()).ok()? {
            Value::String(text) => text,
            Value::Tuple(mut arguments) if arguments.len() == 1 => match arguments.pop()? {
                Value::String(text) => text,
                _ => return None,
            },
            _ => return None,
        },
        _ => return None,
    };
    Some(text.into())
}

async fn read_fragment(cache: &Memcached, key: &str) -> Option<Arc<str>> {
    let (flags, data) = cache.read_raw(key).await?;
    fragment(flags, data)
}

pub(super) enum Fragment {
    Unavailable,
    Pending,
    Ready(Result<Arc<str>, ApiError>),
}

pub(super) async fn results(state: &AppState, cache: &Memcached) -> Fragment {
    let Some(generation) = generation(cache).await else {
        return Fragment::Unavailable;
    };
    let key = fragment_key(generation);
    if let Some(html) = read_fragment(cache, &key).await {
        return Fragment::Ready(Ok(html));
    }
    let lock = format!("{key}:lock");
    if !cache
        .write_raw(&lock, PICKLE, PYTHON_TRUE, 120, false)
        .await
    {
        if let Some(html) = read_fragment(cache, &key).await {
            return Fragment::Ready(Ok(html));
        }
        return if cache.read_raw(&lock).await.is_some() {
            Fragment::Pending
        } else {
            Fragment::Unavailable
        };
    }
    let cache = cache.clone();
    let state = state.clone();
    let pending = tokio::spawn(async move {
        let result = if let Some(html) = read_fragment(&cache, &key).await {
            Ok(html)
        } else {
            let result = compute(&state).await;
            if let Ok(html) = &result {
                cache
                    .write_raw(
                        &key,
                        TEXT,
                        html.as_bytes(),
                        CACHE_TTL.as_secs() as u32,
                        true,
                    )
                    .await;
            }
            result
        };
        cache.delete_raw(&lock).await;
        result
    });
    Fragment::Ready(pending.await.unwrap_or(Err(ApiError::Serialization)))
}

pub(super) async fn clear(cache: &Memcached) {
    let previous = generation(cache).await;
    if cache
        .write_raw(GENERATION, PICKLE, &uuid_pickle(Uuid::new_v4()), 0, true)
        .await
        && let Some(previous) = previous
    {
        cache.delete_raw(&fragment_key(previous)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_django_memcached_pickles() {
        let expected = Uuid::parse_str("95130f94-fb3a-41aa-87f4-e18f33208401").unwrap();
        let django_uuid = hex::decode("80059531000000000000008c0475756964948c04555549449493942981947d948c03696e74948a11018420338fe1f487aa413afb940f13950073622e").unwrap();
        assert_eq!(uuid_value(&django_uuid), Some(expected));
        assert_eq!(uuid_value(&uuid_pickle(expected)), Some(expected));
        let django_safe_string = hex::decode("8005953c000000000000008c17646a616e676f2e7574696c732e73616665737472696e67948c0a53616665537472696e679493948c0b3c623e746573743c2f623e94859481942e").unwrap();
        assert_eq!(
            fragment(PICKLE, django_safe_string).as_deref(),
            Some("<b>test</b>")
        );
        assert_eq!(
            fragment(TEXT, b"<b>test</b>".to_vec()).as_deref(),
            Some("<b>test</b>")
        );
    }
}
