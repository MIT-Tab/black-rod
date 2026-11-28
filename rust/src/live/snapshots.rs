use std::{
    collections::HashMap,
    io::{self, Write},
};

use flate2::write::ZlibDecoder;
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

pub(crate) async fn source_index(cache: &Memcached) -> io::Result<HashMap<String, Source>> {
    let Some((flags, data)) = cache.read_raw_checked(INDEX).await? else {
        return Ok(HashMap::new());
    };
    if flags != 1 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid snapshot index flags",
        ));
    }
    let mut index = serde_pickle::from_slice::<HashMap<String, Source>>(&data, Default::default())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid snapshot index"))?;
    let now = chrono::Utc::now().timestamp() as f64;
    index.retain(|_, source| {
        source
            .missing_since
            .is_none_or(|date| now - date < RETENTION)
    });
    Ok(index)
}

pub(super) async fn missing(
    cache: &Memcached,
    active: &HashMap<String, Value>,
) -> HashMap<String, Value> {
    let mut sources = source_index(cache)
        .await
        .unwrap_or_default()
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
    // Each cache read has its own deadline. Unlike remote requests, retained
    // snapshots must all be attempted before publishing the rebuilt standings.
    while let Some(value) = waiting.join_next().await {
        if let Ok(Some((url, payload))) = value {
            result.insert(url, payload);
        }
        if let Some(source) = sources.next() {
            load(&mut waiting, source);
        }
    }
    result
}

pub(crate) async fn cached_source(
    cache: &Memcached,
    key: &str,
    chunks: &[String],
) -> Option<Value> {
    let prefix = format!("mittab-import:{key}:");
    if !chunks.iter().all(|chunk| chunk.starts_with(&prefix)) {
        return None;
    }
    let keys = chunks
        .iter()
        .map(|chunk| format!(":1:{chunk}"))
        .collect::<Vec<_>>();
    let compressed = cache.read_many_bytes(&keys).await.ok()?;
    tokio::task::spawn_blocking(move || {
        let mut decoder = ZlibDecoder::new(Vec::new());
        for chunk in compressed {
            decoder.write_all(&chunk).ok()?;
        }
        let decoded = decoder.finish().ok()?;
        serde_json::from_slice(&decoded).ok()
    })
    .await
    .ok()?
}

#[cfg(test)]
mod tests {
    use flate2::{Compression, write::ZlibEncoder};
    use serde_json::json;
    use tokio::{
        io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
        net::TcpListener,
    };

    use super::*;

    async fn serve_get(listener: &TcpListener, records: &[(String, u32, &[u8])]) {
        let (connection, _) = listener.accept().await.unwrap();
        let mut reader = BufReader::new(connection);
        let mut command = String::new();
        reader.read_line(&mut command).await.unwrap();
        let requested = records
            .iter()
            .map(|(key, _, _)| key.as_str())
            .collect::<Vec<_>>()
            .join(" ");
        assert_eq!(command, format!("get {requested}\r\n"));
        let stream = reader.get_mut();
        for (key, flags, data) in records.iter().rev() {
            stream
                .write_all(format!("VALUE {key} {flags} {}\r\n", data.len()).as_bytes())
                .await
                .unwrap();
            stream.write_all(data).await.unwrap();
            stream.write_all(b"\r\n").await.unwrap();
        }
        stream.write_all(b"END\r\n").await.unwrap();
    }

    #[tokio::test]
    async fn fetches_all_retained_chunks_together_in_manifest_order() {
        let chunks: Vec<_> = (0..180)
            .map(|number| format!("mittab-import:fixture:generation:{number}"))
            .collect();
        let sources = HashMap::from([(
            "fixture",
            Source {
                url: "http://fixture.example".into(),
                name: "Fixture Tournament".into(),
                updated_at: None,
                missing_since: None,
                chunks: chunks.clone(),
            },
        )]);
        let index = serde_pickle::to_vec(&sources, Default::default()).unwrap();
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        encoder
            .write_all(br#"{"tournament":{"name":"Fixture Tournament"}}"#)
            .unwrap();
        let compressed = encoder.finish().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let cache = Memcached::new(listener.local_addr().unwrap());
        let fixture = tokio::spawn(async move {
            let indexed = chunks
                .iter()
                .enumerate()
                .map(|(part, key)| {
                    let length = compressed.len();
                    (
                        format!(":1:{key}"),
                        0,
                        &compressed
                            [part * length / chunks.len()..(part + 1) * length / chunks.len()],
                    )
                })
                .collect::<Vec<_>>();
            serve_get(&listener, &[(INDEX.to_owned(), 1, index.as_slice())]).await;
            serve_get(&listener, &indexed).await;
        });
        let result = missing(&cache, &HashMap::new()).await;
        assert_eq!(result["http://fixture.example"]["_cached_only"], true);
        fixture.await.unwrap();
    }

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
            serve_get(&listener, &[(INDEX.to_owned(), 1, index.as_slice())]).await;
            serve_get(
                &listener,
                &[
                    (
                        ":1:mittab-import:fixture:generation:0".to_owned(),
                        0,
                        &compressed[..5],
                    ),
                    (
                        ":1:mittab-import:fixture:generation:5".to_owned(),
                        0,
                        &compressed[5..],
                    ),
                ],
            )
            .await;
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
