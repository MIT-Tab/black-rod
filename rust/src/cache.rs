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

fn valid_key(key: &str) -> bool {
    !key.is_empty() && key.len() <= 250 && key.bytes().all(|byte| byte.is_ascii_graphic())
}

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

    pub(crate) async fn read_raw(&self, key: &str) -> Option<(u32, Vec<u8>)> {
        if !valid_key(key) {
            return None;
        }
        let result = timeout(SERVICE_TIMEOUT, async {
            let stream = TcpStream::connect(self.0).await?;
            let mut reader = BufReader::new(stream);
            reader
                .get_mut()
                .write_all(format!("get {key}\r\n").as_bytes())
                .await?;
            let mut header = Vec::new();
            (&mut reader)
                .take(300)
                .read_until(b'\n', &mut header)
                .await?;
            let line = std::str::from_utf8(&header)
                .ok()
                .and_then(|line| line.strip_suffix("\r\n"));
            let Some(line) = line else {
                return Ok(None);
            };
            let mut fields = line.split_ascii_whitespace();
            if fields.next() != Some("VALUE") || fields.next() != Some(key) {
                return Ok(None);
            }
            let Some(flags) = fields.next().and_then(|flags| flags.parse::<u32>().ok()) else {
                return Ok(None);
            };
            let length = fields
                .next()
                .and_then(|length| length.parse::<usize>().ok());
            let Some(length) = length.filter(|length| *length <= MAX_VALUE) else {
                return Ok(None);
            };
            let mut bytes = vec![0; length];
            reader.read_exact(&mut bytes).await?;
            let mut terminator = [0; 7];
            reader.read_exact(&mut terminator).await?;
            if terminator != *b"\r\nEND\r\n" {
                return Ok(None);
            }
            Ok::<_, io::Error>(Some((flags, bytes)))
        })
        .await;
        result.ok()?.ok()?
    }

    async fn send(&self, command: &str, body: Option<&[u8]>, expected: &[u8]) -> bool {
        timeout(SERVICE_TIMEOUT, async {
            let stream = TcpStream::connect(self.0).await?;
            let mut reader = BufReader::new(stream);
            reader.get_mut().write_all(command.as_bytes()).await?;
            if let Some(body) = body {
                reader.get_mut().write_all(body).await?;
                reader.get_mut().write_all(b"\r\n").await?;
            }
            let mut response = Vec::with_capacity(24);
            (&mut reader)
                .take(300)
                .read_until(b'\n', &mut response)
                .await?;
            Ok::<_, io::Error>(response == expected)
        })
        .await
        .is_ok_and(|result| result.unwrap_or_default())
    }

    pub(crate) async fn write_raw(
        &self,
        key: &str,
        flags: u32,
        body: &[u8],
        seconds: u32,
        replace: bool,
    ) -> bool {
        if !valid_key(key) || body.len() > MAX_VALUE {
            return false;
        }
        let operation = if replace { "set" } else { "add" };
        let command = format!("{operation} {key} {flags} {seconds} {}\r\n", body.len());
        self.send(&command, Some(body), b"STORED\r\n").await
    }

    pub(crate) async fn delete_raw(&self, key: &str) -> bool {
        valid_key(key)
            && self
                .send(&format!("delete {key}\r\n"), None, b"DELETED\r\n")
                .await
    }

    pub(crate) async fn touch_raw(&self, key: &str, seconds: u32) -> bool {
        valid_key(key)
            && self
                .send(&format!("touch {key} {seconds}\r\n"), None, b"TOUCHED\r\n")
                .await
    }

    pub(crate) async fn get(&self, key: &str) -> Option<CachedBody> {
        let (flags, bytes) = self.read_raw(key).await?;
        if flags != 0 {
            return None;
        }
        let expiry = bytes.first_chunk::<8>()?;
        let lifetime =
            Duration::from_secs(u64::from_be_bytes(*expiry).saturating_sub(epoch_seconds()));
        (!lifetime.is_zero()).then(|| CachedBody {
            bytes: Bytes::from(bytes).slice(8..),
            lifetime,
        })
    }

    pub(crate) async fn set(&self, key: &str, body: &Bytes, ttl: Duration) {
        let length = body.len() + size_of::<u64>();
        if ttl.is_zero() || length > MAX_VALUE {
            return;
        }
        // Memcached interprets durations larger than 30 days as Unix timestamps.
        let seconds = ttl.as_secs().clamp(1, 30 * 24 * 60 * 60);
        let mut contents = Vec::with_capacity(length);
        contents.extend_from_slice(&epoch_seconds().saturating_add(seconds).to_be_bytes());
        contents.extend_from_slice(body);
        self.write_raw(key, 0, &contents, seconds as u32, true)
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
