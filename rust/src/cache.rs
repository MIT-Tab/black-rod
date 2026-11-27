use std::{
    io,
    net::SocketAddr,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use axum::body::Bytes;
use moka::Expiry;
use ring::digest;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::TcpStream,
    time::timeout,
};

const SERVICE_TIMEOUT: Duration = Duration::from_millis(100);
const MAX_VALUE: usize = 1024 * 1024;

#[derive(Clone)]
pub(crate) struct Memcached(SocketAddr);

impl Memcached {
    pub(crate) fn new(address: SocketAddr) -> Self {
        Self(address)
    }

    pub(crate) fn key(namespace: &str, origin: &str, endpoint: impl std::fmt::Debug) -> String {
        let identity = format!("{namespace}\0{origin}\0{endpoint:?}");
        let hash = digest::digest(&digest::SHA256, identity.as_bytes());
        format!("black-rod:rust:v1:{}", hex::encode(hash))
    }

    pub(crate) async fn get(&self, key: &str) -> Option<CachedBody> {
        let result = timeout(SERVICE_TIMEOUT, async {
            let stream = TcpStream::connect(self.0).await?;
            let mut reader = BufReader::new(stream);
            reader
                .get_mut()
                .write_all(format!("get {key}\r\n").as_bytes())
                .await?;
            let mut header = Vec::new();
            // Bound the line independently from the value size advertised by the server.
            (&mut reader)
                .take(256)
                .read_until(b'\n', &mut header)
                .await?;
            let line = std::str::from_utf8(&header)
                .ok()
                .and_then(|line| line.strip_suffix("\r\n"));
            let Some(line) = line else {
                return Ok(None);
            };
            let mut fields = line.split_ascii_whitespace();
            if fields.next() != Some("VALUE")
                || fields.next() != Some(key)
                || fields.next() != Some("0")
            {
                return Ok(None);
            }
            let length = fields
                .next()
                .and_then(|length| length.parse::<usize>().ok());
            let Some(length) =
                length.filter(|value| (size_of::<u64>()..=MAX_VALUE).contains(value))
            else {
                return Ok(None);
            };
            let mut expiry = [0; 8];
            reader.read_exact(&mut expiry).await?;
            let lifetime =
                Duration::from_secs(u64::from_be_bytes(expiry).saturating_sub(epoch_seconds()));
            if lifetime.is_zero() {
                return Ok(None);
            }
            let mut bytes = vec![0; length - size_of::<u64>()];
            reader.read_exact(&mut bytes).await?;
            let mut terminator = [0; 7];
            reader.read_exact(&mut terminator).await?;
            if terminator != *b"\r\nEND\r\n" {
                return Ok(None);
            }
            Ok::<_, io::Error>(Some(CachedBody {
                bytes: Bytes::from(bytes),
                lifetime,
            }))
        })
        .await;
        result.ok()?.ok()?
    }

    pub(crate) async fn set(&self, key: &str, body: &Bytes, ttl: Duration) {
        let length = body.len() + size_of::<u64>();
        if ttl.is_zero() || length > MAX_VALUE {
            return;
        }
        // Memcached interprets durations larger than 30 days as Unix timestamps.
        let seconds = ttl.as_secs().clamp(1, 30 * 24 * 60 * 60);
        let expires = epoch_seconds().saturating_add(seconds).to_be_bytes();
        let _ = timeout(SERVICE_TIMEOUT, async {
            let mut stream = TcpStream::connect(self.0).await?;
            stream
                .write_all(format!("set {key} 0 {seconds} {length}\r\n").as_bytes())
                .await?;
            stream.write_all(&expires).await?;
            stream.write_all(body).await?;
            stream.write_all(b"\r\n").await?;
            let mut response = [0u8; 8];
            stream.read_exact(&mut response).await?;
            Ok::<_, io::Error>(())
        })
        .await;
    }
}

fn epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

#[derive(Clone)]
pub(crate) struct CachedBody {
    pub(crate) bytes: Bytes,
    pub(crate) lifetime: Duration,
}

pub(crate) struct BodyExpiry;

impl<K> Expiry<K, CachedBody> for BodyExpiry {
    fn expire_after_create(
        &self,
        _key: &K,
        body: &CachedBody,
        _instant: Instant,
    ) -> Option<Duration> {
        Some(body.lifetime)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;

    #[tokio::test]
    async fn records_use_memcached_ascii_commands_and_keep_remaining_time() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let key = Memcached::key("directory", "https://results.test", ("school", 1));
        let server_key = key.clone();
        let task = tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            let mut stream = BufReader::new(socket);
            let mut header = String::new();
            stream.read_line(&mut header).await.unwrap();
            assert_eq!(header, format!("set {server_key} 0 300 10\r\n"));
            let mut body = [0; 12];
            stream.read_exact(&mut body).await.unwrap();
            assert_eq!(&body[8..], b"{}\r\n");
            stream.get_mut().write_all(b"STORED\r\n").await.unwrap();

            let (socket, _) = listener.accept().await.unwrap();
            let mut stream = BufReader::new(socket);
            header.clear();
            stream.read_line(&mut header).await.unwrap();
            assert_eq!(header, format!("get {server_key}\r\n"));
            stream
                .get_mut()
                .write_all(format!("VALUE {server_key} 0 10\r\n").as_bytes())
                .await
                .unwrap();
            stream.get_mut().write_all(&body).await.unwrap();
            stream.get_mut().write_all(b"END\r\n").await.unwrap();
        });
        let cache = Memcached::new(address);
        cache
            .set(&key, &Bytes::from_static(b"{}"), Duration::from_secs(300))
            .await;
        let body = cache.get(&key).await.unwrap();
        assert_eq!(body.bytes, "{}");
        assert!(body.lifetime > Duration::from_secs(298));
        assert!(body.lifetime <= Duration::from_secs(300));
        task.await.unwrap();
    }
}
