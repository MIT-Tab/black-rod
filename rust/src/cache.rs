use std::{
    collections::HashMap,
    io,
    net::SocketAddr,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use axum::body::Bytes;
use moka::Expiry;
use ring::digest;
use serde_json::Value as Json;
use serde_pickle::{HashableValue, Value as Pickle};
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

struct CacheValue {
    flags: u32,
    data: Vec<u8>,
    version: Option<u64>,
}

impl Memcached {
    pub(crate) fn new(address: SocketAddr) -> Self {
        Self(address)
    }

    pub(crate) fn key(namespace: &str, origin: &str, endpoint: impl std::fmt::Debug) -> String {
        let identity = format!("{namespace}\0{origin}\0{endpoint:?}");
        let hash = digest::digest(&digest::SHA256, identity.as_bytes());
        format!("black-rod:rust:v1:{}", hex::encode(hash))
    }

    async fn read_counter_checked(&self, key: &str) -> io::Result<Option<u32>> {
        let Some((flags, value)) = self.read_raw_checked(key).await? else {
            return Ok(None);
        };
        let count = (flags == 2)
            .then(|| std::str::from_utf8(&value).ok()?.parse().ok())
            .flatten()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "Invalid cache counter"))?;
        Ok(Some(count))
    }

    pub(crate) async fn quota_count(&self, key: &str) -> Result<u32, crate::ApiError> {
        self.read_counter_checked(key)
            .await
            .map(Option::unwrap_or_default)
            .map_err(|_| crate::ApiError::QuotaUnavailable)
    }

    // Callers hold the account's database lock while reserving the slot.
    pub(crate) async fn commit_with_quota(
        &self,
        mut tx: sqlx::Transaction<'static, sqlx::Any>,
        key: &str,
        prior: u32,
    ) -> Result<(), crate::ApiError> {
        // Check deferred PostgreSQL constraints while the transaction still
        // owns the lock and before the external quota changes.
        if tx.backend_name() == "PostgreSQL" {
            sqlx::query("SET CONSTRAINTS ALL IMMEDIATE")
                .execute(&mut *tx)
                .await?;
        }
        let cache = self.clone();
        let key = key.to_owned();
        // Finish the reservation and commit even if the client disconnects.
        let result = tokio::spawn(async move {
            let reserved = (prior + 1).to_string();
            if !cache
                .update_raw_checked(&key, 2, reserved.as_bytes(), 86_400)
                .await
                .is_ok_and(|stored| stored)
            {
                return None;
            }
            let result = tx.commit().await;
            if let Err(error) = &result
                && error
                    .as_database_error()
                    .and_then(|error| error.code())
                    .is_some_and(|code| {
                        code.starts_with("23") || matches!(code.as_ref(), "40001" | "40P01" | "787")
                    })
                && let Ok(Some((2, data, version))) = cache.read_versioned(&key).await
                && data == reserved.as_bytes()
            {
                // Refund a definite rejection if the slot is unchanged. A
                // failed connection can leave the outcome of COMMIT unknown.
                let _ = cache
                    .compare_and_swap(&key, 2, prior.to_string().as_bytes(), 86_400, Some(version))
                    .await;
            }
            Some(result)
        })
        .await
        .map_err(|error| {
            eprintln!("Quota commit task failed: {error}");
            crate::ApiError::Database
        })?;
        Ok(result.ok_or(crate::ApiError::QuotaUnavailable)??)
    }

    pub(crate) async fn generation(&self, key: &str) -> Option<uuid::Uuid> {
        let decode = |(flags, bytes): (u32, Vec<u8>)| {
            (flags == 0)
                .then(|| uuid::Uuid::from_slice(&bytes).ok())
                .flatten()
        };
        if let Some(record) = self.read_raw_checked(key).await.ok()? {
            return decode(record);
        }
        let proposed = uuid::Uuid::new_v4();
        if self.write_raw(key, 0, proposed.as_bytes(), 0, false).await {
            return Some(proposed);
        }
        self.read_raw(key).await.and_then(decode)
    }

    pub(crate) async fn advance_generation(&self, key: &str) -> bool {
        if self
            .write_raw(key, 0, uuid::Uuid::new_v4().as_bytes(), 0, true)
            .await
        {
            return true;
        }
        self.invalidate_raw(key).await;
        false
    }

    pub(crate) async fn advance_counter(&self, key: &str, seconds: u32) -> bool {
        if self.write_raw(key, 2, b"1", seconds, false).await {
            return true;
        }
        if !valid_key(key) {
            return false;
        }
        let advanced = timeout(SERVICE_TIMEOUT, async {
            let stream = TcpStream::connect(self.0).await?;
            let mut reader = BufReader::new(stream);
            reader
                .get_mut()
                .write_all(format!("incr {key} 1\r\n").as_bytes())
                .await?;
            let mut reply = String::new();
            (&mut reader).take(40).read_line(&mut reply).await?;
            Ok::<_, io::Error>(reply.trim_end().parse::<u64>().is_ok())
        })
        .await
        .is_ok_and(|result| result.unwrap_or_default());
        if advanced {
            let _ = self.touch_raw(key, seconds).await;
        }
        advanced
    }

    pub(crate) async fn read_raw(&self, key: &str) -> Option<(u32, Vec<u8>)> {
        self.read_raw_checked(key).await.ok().flatten()
    }

    // Writers need to distinguish an absent key from an unavailable or invalid read.
    pub(crate) async fn read_raw_checked(&self, key: &str) -> io::Result<Option<(u32, Vec<u8>)>> {
        let item = self.fetch_raw(key, false).await?;
        Ok(item.map(|item| (item.flags, item.data)))
    }

    pub(crate) async fn read_versioned(
        &self,
        key: &str,
    ) -> io::Result<Option<(u32, Vec<u8>, u64)>> {
        let item = self.fetch_raw(key, true).await?;
        Ok(item.and_then(|item| Some((item.flags, item.data, item.version?))))
    }

    pub(crate) async fn read_many_bytes(&self, keys: &[String]) -> io::Result<Vec<Vec<u8>>> {
        if keys.is_empty() {
            return Ok(Vec::new());
        }
        if !keys.iter().all(|key| valid_key(key)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid cache key",
            ));
        }
        let mut values = HashMap::with_capacity(keys.len());
        let mut reader = timeout(SERVICE_TIMEOUT, async {
            let mut socket = TcpStream::connect(self.0).await?;
            socket
                .write_all(format!("get {}\r\n", keys.join(" ")).as_bytes())
                .await?;
            Ok::<_, io::Error>(BufReader::new(socket))
        })
        .await
        .map_err(io::Error::other)??;
        while let Some((key, item)) = timeout(SERVICE_TIMEOUT, Self::read_value(&mut reader, false))
            .await
            .map_err(io::Error::other)??
        {
            if item.flags != 0
                || values.insert(key, item.data).is_some()
                || values.len() > keys.len()
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Invalid cache response",
                ));
            }
        }
        keys.iter()
            .map(|key| {
                values
                    .remove(key)
                    .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Missing cache entry"))
            })
            .collect()
    }

    async fn read_value(
        reader: &mut BufReader<TcpStream>,
        versioned: bool,
    ) -> io::Result<Option<(String, CacheValue)>> {
        let malformed = || io::Error::new(io::ErrorKind::InvalidData, "Invalid cache response");
        let mut header = Vec::new();
        (&mut *reader)
            .take(300)
            .read_until(b'\n', &mut header)
            .await?;
        if header == b"END\r\n" {
            return Ok(None);
        }
        let line = std::str::from_utf8(&header)
            .ok()
            .and_then(|line| line.strip_suffix("\r\n"))
            .ok_or_else(malformed)?;
        let mut fields = line.split_ascii_whitespace();
        if fields.next() != Some("VALUE") {
            return Err(malformed());
        }
        let key = fields.next().ok_or_else(malformed)?.to_owned();
        let flags = fields
            .next()
            .and_then(|flags| flags.parse::<u32>().ok())
            .ok_or_else(malformed)?;
        let length = fields
            .next()
            .and_then(|length| length.parse::<usize>().ok())
            .filter(|length| *length <= MAX_VALUE)
            .ok_or_else(malformed)?;
        let version = if versioned {
            Some(
                fields
                    .next()
                    .and_then(|version| version.parse().ok())
                    .ok_or_else(malformed)?,
            )
        } else {
            None
        };
        if fields.next().is_some() {
            return Err(malformed());
        }
        let mut data = vec![0; length];
        reader.read_exact(&mut data).await?;
        let mut terminator = [0; 2];
        reader.read_exact(&mut terminator).await?;
        if terminator != *b"\r\n" {
            return Err(malformed());
        }
        Ok(Some((
            key,
            CacheValue {
                flags,
                data,
                version,
            },
        )))
    }

    async fn fetch_raw(&self, key: &str, versioned: bool) -> io::Result<Option<CacheValue>> {
        if !valid_key(key) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid cache key",
            ));
        }
        timeout(SERVICE_TIMEOUT, async {
            let stream = TcpStream::connect(self.0).await?;
            let mut reader = BufReader::new(stream);
            let command = if versioned { "gets" } else { "get" };
            reader
                .get_mut()
                .write_all(format!("{command} {key}\r\n").as_bytes())
                .await?;
            let Some((received, value)) = Self::read_value(&mut reader, versioned).await? else {
                return Ok(None);
            };
            if received != key || Self::read_value(&mut reader, versioned).await?.is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Invalid cache response",
                ));
            }
            Ok(Some(value))
        })
        .await
        .map_err(io::Error::other)?
    }

    async fn reply(&self, command: &str, body: Option<&[u8]>) -> io::Result<Vec<u8>> {
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
            Ok(response)
        })
        .await
        .map_err(io::Error::other)?
    }

    async fn send(&self, command: &str, body: Option<&[u8]>, expected: &[u8]) -> bool {
        self.reply(command, body)
            .await
            .is_ok_and(|reply| reply == expected)
    }

    pub(crate) async fn write_raw(
        &self,
        key: &str,
        flags: u32,
        body: &[u8],
        seconds: u32,
        replace: bool,
    ) -> bool {
        self.write_raw_checked(key, flags, body, seconds, replace)
            .await
            .unwrap_or(false)
    }

    // A definitive rejection is safe to roll back. Transport errors may arrive after a write.
    pub(crate) async fn write_raw_checked(
        &self,
        key: &str,
        flags: u32,
        body: &[u8],
        seconds: u32,
        replace: bool,
    ) -> io::Result<bool> {
        let operation = if replace { "set" } else { "add" };
        self.store_raw(operation, key, flags, body, seconds).await
    }

    // Memcached can remove an existing item when a SET is rejected on allocation.
    // REPLACE updates an existing item without that behavior. ADD covers a miss.
    pub(crate) async fn update_raw_checked(
        &self,
        key: &str,
        flags: u32,
        body: &[u8],
        seconds: u32,
    ) -> io::Result<bool> {
        if self.store_raw("replace", key, flags, body, seconds).await? {
            return Ok(true);
        }
        self.store_raw("add", key, flags, body, seconds).await
    }

    async fn store_raw(
        &self,
        operation: &str,
        key: &str,
        flags: u32,
        body: &[u8],
        seconds: u32,
    ) -> io::Result<bool> {
        if !valid_key(key) || body.len() > MAX_VALUE {
            return Ok(false);
        }
        let command = format!("{operation} {key} {flags} {seconds} {}\r\n", body.len());
        let reply = self.reply(&command, Some(body)).await?;
        match reply.as_slice() {
            b"STORED\r\n" => Ok(true),
            b"NOT_STORED\r\n" => Ok(false),
            reply
                if reply.ends_with(b"\r\n")
                    && (reply.starts_with(b"SERVER_ERROR ")
                        || reply.starts_with(b"CLIENT_ERROR ")) =>
            {
                Ok(false)
            }
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid cache write response",
            )),
        }
    }

    pub(crate) async fn compare_and_swap(
        &self,
        key: &str,
        flags: u32,
        body: &[u8],
        seconds: u32,
        version: Option<u64>,
    ) -> io::Result<bool> {
        let Some(version) = version else {
            return self
                .write_raw_checked(key, flags, body, seconds, false)
                .await;
        };
        if !valid_key(key) || body.len() > MAX_VALUE {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid cache item",
            ));
        }
        let command = format!("cas {key} {flags} {seconds} {} {version}\r\n", body.len());
        match self.reply(&command, Some(body)).await?.as_slice() {
            b"STORED\r\n" => Ok(true),
            b"EXISTS\r\n" | b"NOT_FOUND\r\n" => Ok(false),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid cache CAS response",
            )),
        }
    }

    pub(crate) async fn delete_raw(&self, key: &str) -> bool {
        valid_key(key)
            && self
                .send(&format!("delete {key}\r\n"), None, b"DELETED\r\n")
                .await
    }

    pub(crate) async fn invalidate_raw(&self, key: &str) {
        if !valid_key(key)
            || !self
                .reply(&format!("delete {key}\r\n"), None)
                .await
                .is_ok_and(|reply| matches!(reply.as_slice(), b"DELETED\r\n" | b"NOT_FOUND\r\n"))
        {
            eprintln!("Could not invalidate shared cache key {key}");
        }
    }

    pub(crate) async fn touch_raw(&self, key: &str, seconds: u32) -> io::Result<bool> {
        if !valid_key(key) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Invalid cache key",
            ));
        }
        match self
            .reply(&format!("touch {key} {seconds}\r\n"), None)
            .await?
            .as_slice()
        {
            b"TOUCHED\r\n" => Ok(true),
            b"NOT_FOUND\r\n" => Ok(false),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid cache touch response",
            )),
        }
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

// JSON numbers use a private Serde representation to preserve arbitrary precision.
// Encode ordinary live standings values as Python primitives for Django’s shared caches.
pub(crate) fn python_value(value: &Json) -> Pickle {
    match value {
        Json::Null => Pickle::None,
        Json::Bool(value) => Pickle::Bool(*value),
        Json::String(value) => Pickle::String(value.clone()),
        Json::Array(values) => Pickle::List(values.iter().map(python_value).collect()),
        Json::Object(fields) => Pickle::Dict(
            fields
                .iter()
                .map(|(key, value)| (HashableValue::String(key.clone()), python_value(value)))
                .collect(),
        ),
        Json::Number(number) => {
            if let Some(value) = number.as_i64() {
                Pickle::I64(value)
            } else if let Some(value) = number.as_u64() {
                Pickle::Int(value.into())
            } else {
                Pickle::F64(
                    number
                        .as_f64()
                        .expect("shared cache records use finite numbers"),
                )
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;

    #[tokio::test]
    async fn checked_reads_distinguish_miss_from_protocol_and_transport_failure() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let cache = Memcached::new(listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            for reply in [
                b"END\r\n".as_slice(),
                b"SERVER_ERROR unavailable\r\n",
                b"VALUE other 0 1\r\nx\r\nEND\r\n",
                b"VALUE index 1 3\r\nx",
            ] {
                let (socket, _) = listener.accept().await.unwrap();
                let mut stream = BufReader::new(socket);
                let mut command = String::new();
                stream.read_line(&mut command).await.unwrap();
                assert_eq!(command, "get index\r\n");
                stream.get_mut().write_all(reply).await.unwrap();
            }
        });
        assert!(cache.read_raw_checked("index").await.unwrap().is_none());
        for _ in 0..3 {
            assert!(cache.read_raw_checked("index").await.is_err());
        }
        server.await.unwrap();
    }

    #[tokio::test]
    async fn checked_writes_distinguish_rejection_from_lost_acknowledgement() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let cache = Memcached::new(listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            for reply in [
                b"STORED\r\n".as_slice(),
                b"SERVER_ERROR object too large for cache\r\n",
                b"",
            ] {
                let (socket, _) = listener.accept().await.unwrap();
                let mut stream = BufReader::new(socket);
                let mut command = String::new();
                stream.read_line(&mut command).await.unwrap();
                assert_eq!(command, "set index 1 0 3\r\n");
                let mut payload = [0; 5];
                stream.read_exact(&mut payload).await.unwrap();
                assert_eq!(&payload, b"abc\r\n");
                stream.get_mut().write_all(reply).await.unwrap();
            }
        });
        assert!(
            cache
                .write_raw_checked("index", 1, b"abc", 0, true)
                .await
                .unwrap()
        );
        assert!(
            !cache
                .write_raw_checked("index", 1, b"abc", 0, true)
                .await
                .unwrap()
        );
        assert!(
            cache
                .write_raw_checked("index", 1, b"abc", 0, true)
                .await
                .is_err()
        );
        server.await.unwrap();
    }

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
