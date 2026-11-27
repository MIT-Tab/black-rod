use std::{collections::HashMap, io::Read, time::Duration};

use flate2::read::ZlibDecoder;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::task::JoinSet;

use crate::cache::Memcached;

pub(crate) const INDEX: &str = ":1:mittab-import:index:v1";
pub(crate) const RETENTION: f64 = 7.0 * 24.0 * 3600.0;

#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct Source {
    pub(crate) url: String,
    pub(crate) name: String,
    #[serde(default)]
    pub(crate) updated_at: Option<String>,
    pub(crate) missing_since: Option<f64>,
    pub(crate) chunks: Vec<String>,
}

pub(crate) async fn source_index(cache: &Memcached) -> HashMap<String, Source> {
    let Some((1, data)) = cache.read_raw(INDEX).await else {
        return HashMap::new();
    };
    let Ok(index) = serde_pickle::from_slice::<HashMap<String, Source>>(&data, Default::default())
    else {
        return HashMap::new();
    };
    let now = chrono::Utc::now().timestamp() as f64;
    index
        .into_iter()
        .filter(|(_, source)| {
            source
                .missing_since
                .is_none_or(|date| now - date < RETENTION)
        })
        .collect()
}

pub(super) async fn missing(
    cache: &Memcached,
    active: &HashMap<String, Value>,
) -> HashMap<String, Value> {
    let mut sources = source_index(cache)
        .await
        .into_iter()
        .filter(|(_, source)| !active.contains_key(&source.url));
    let mut waiting = JoinSet::new();
    let load = |waiting: &mut JoinSet<_>, (key, source): (String, Source)| {
        let cache = cache.clone();
        waiting.spawn(async move {
            let response = cached_source(&cache, &key, &source.chunks).await;
            response.and_then(|mut value| {
                let payload = value.as_object_mut()?;
                payload.remove("bundle");
                payload.remove("new_debater_data");
                payload.insert("_cached_only".into(), Value::Bool(true));
                Some((source.url, value))
            })
        });
    };
    for source in sources.by_ref().take(8) {
        load(&mut waiting, source);
    }
    let mut result = HashMap::new();
    let _ = tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(value) = waiting.join_next().await {
            if let Ok(Some((url, payload))) = value {
                result.insert(url, payload);
            }
            if let Some(source) = sources.next() {
                load(&mut waiting, source);
            }
        }
    })
    .await;
    result
}

pub(crate) async fn cached_source(
    cache: &Memcached,
    key: &str,
    chunks: &[String],
) -> Option<Value> {
    let prefix = format!("mittab-import:{key}:");
    let mut compressed = Vec::new();
    for chunk in chunks {
        if !chunk.starts_with(&prefix) {
            return None;
        }
        let (0, data) = cache.read_raw(&format!(":1:{chunk}")).await? else {
            return None;
        };
        compressed.extend_from_slice(&data);
    }
    tokio::task::spawn_blocking(move || {
        let mut decoder = ZlibDecoder::new(compressed.as_slice());
        let mut decoded = Vec::new();
        decoder.read_to_end(&mut decoded).ok()?;
        serde_json::from_slice(&decoded).ok()
    })
    .await
    .ok()?
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use flate2::{Compression, write::ZlibEncoder};
    use serde_json::json;
    use tokio::{
        io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
        net::TcpListener,
    };

    use super::*;

    #[tokio::test]
    async fn reads_python_cache_index_and_reassembles_compressed_chunks() {
        // Protocol 5 pickle produced by the Django cache's Python serializer.
        let index = hex::decode(concat!(
            "800595de000000000000007d948c0766697874757265947d94288c0375726c948c16687474703a2f2f6669",
            "78747572652e6578616d706c65948c046e616d65948c124669787475726520546f75726e616d656e74948c",
            "0a757064617465645f6174948c19323032352d30312d30315430303a30303a30302b30303a3030948c0d6d",
            "697373696e675f73696e6365944e8c066368756e6b73945d94288c226d69747461622d696d706f72743a66",
            "6978747572653a67656e65726174696f6e3a30948c226d69747461622d696d706f72743a66697874757265",
            "3a67656e65726174696f6e3a35946575732e",
        )).unwrap();
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(br#"{"tournament":{"name":"Fixture Tournament"},"bundle":{},"new_debater_data":[]}"#).unwrap();
        let compressed = encoder.finish().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let cache = Memcached::new(listener.local_addr().unwrap());
        let fixture = tokio::spawn(async move {
            for (key, flags, value) in [
                (INDEX, 1, index.as_slice()),
                (":1:mittab-import:fixture:generation:0", 0, &compressed[..5]),
                (":1:mittab-import:fixture:generation:5", 0, &compressed[5..]),
            ] {
                let (connection, _) = listener.accept().await.unwrap();
                let mut reader = BufReader::new(connection);
                let mut command = String::new();
                reader.read_line(&mut command).await.unwrap();
                assert_eq!(command, format!("get {key}\r\n"));
                let stream = reader.get_mut();
                stream
                    .write_all(format!("VALUE {key} {flags} {}\r\n", value.len()).as_bytes())
                    .await
                    .unwrap();
                stream.write_all(value).await.unwrap();
                stream.write_all(b"\r\nEND\r\n").await.unwrap();
            }
        });
        let result = missing(&cache, &HashMap::new()).await;
        assert_eq!(
            result.get("http://fixture.example"),
            Some(&json!({
                "tournament":{"name":"Fixture Tournament"}, "_cached_only":true,
            }))
        );
        fixture.await.unwrap();
    }
}
