//! Django's timestamped signing format for existing sessions and signed account links.

use std::io::{Read, Write};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::Utc;
use flate2::{Compression, read::ZlibDecoder, write::ZlibEncoder};
use ring::{digest, hmac};
use serde_json::Value;

const MAX_SIGNED_PAYLOAD_SIZE: u64 = 1024 * 1024;

pub(crate) fn key(salt: &[u8], secret: &[u8]) -> hmac::Key {
    let mut hash = digest::Context::new(&digest::SHA256);
    hash.update(salt);
    hash.update(secret);
    hmac::Key::new(hmac::HMAC_SHA256, hash.finish().as_ref())
}

pub(crate) fn decode_signed(data: &str, secret: &[u8], salt: &[u8]) -> Option<Value> {
    if data.len() as u64 > MAX_SIGNED_PAYLOAD_SIZE {
        return None;
    }
    let (payload, _) = signed_payload(data, secret, salt)?;
    let bytes = payload_bytes(payload, MAX_SIGNED_PAYLOAD_SIZE)?;
    serde_json::from_slice(&bytes).ok()
}

pub(super) fn decode_database_session(data: &str, secret: &[u8], salt: &[u8]) -> Option<Vec<u8>> {
    let (payload, _) = signed_payload(data, secret, salt)?;
    payload_bytes(payload, u64::MAX)
}

fn signed_payload<'a>(data: &'a str, secret: &[u8], salt: &[u8]) -> Option<(&'a str, &'a str)> {
    let (signed, signature) = data.rsplit_once(':')?;
    let signature = URL_SAFE_NO_PAD.decode(signature).ok()?;
    hmac::verify(&key(salt, secret), signed.as_bytes(), &signature).ok()?;
    signed.rsplit_once(':')
}

fn payload_bytes(payload: &str, max_bytes: u64) -> Option<Vec<u8>> {
    let contents = if let Some(payload) = payload.strip_prefix('.') {
        let compressed = URL_SAFE_NO_PAD.decode(payload).ok()?;
        let mut inflated = Vec::new();
        ZlibDecoder::new(compressed.as_slice())
            .take(max_bytes.saturating_add(1))
            .read_to_end(&mut inflated)
            .ok()?;
        inflated
    } else {
        URL_SAFE_NO_PAD.decode(payload).ok()?
    };
    (contents.len() as u64 <= max_bytes).then_some(contents)
}

fn timestamp(mut time: u64) -> String {
    const ALPHABET: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    let mut encoded = Vec::new();
    loop {
        encoded.push(char::from(ALPHABET[(time % 62) as usize]));
        time /= 62;
        if time == 0 {
            break;
        }
    }
    encoded.into_iter().rev().collect()
}

pub(crate) fn encode_signed(payload: &str, secret: &[u8], salt: &[u8]) -> String {
    sign_payload(&URL_SAFE_NO_PAD.encode(payload), secret, salt)
}

pub(crate) fn encode_compressed(
    payload: &str,
    secret: &[u8],
    salt: &[u8],
) -> std::io::Result<String> {
    let mut compressor = ZlibEncoder::new(Vec::new(), Compression::default());
    compressor.write_all(payload.as_bytes())?;
    let compressed = compressor.finish()?;
    Ok(if compressed.len() + 1 < payload.len() {
        sign_payload(
            &format!(".{}", URL_SAFE_NO_PAD.encode(compressed)),
            secret,
            salt,
        )
    } else {
        encode_signed(payload, secret, salt)
    })
}

fn sign_payload(payload: &str, secret: &[u8], salt: &[u8]) -> String {
    let signed = format!("{payload}:{}", timestamp(Utc::now().timestamp() as u64));
    let signature = URL_SAFE_NO_PAD.encode(hmac::sign(&key(salt, secret), signed.as_bytes()));
    format!("{signed}:{signature}")
}

pub(crate) fn decode_timed<T: serde::de::DeserializeOwned>(
    signed: &str,
    secret: &[u8],
    salt: &[u8],
    max_age: i64,
) -> Option<T> {
    decode_timed_with_limits(
        signed,
        secret,
        salt,
        max_age,
        MAX_SIGNED_PAYLOAD_SIZE as usize,
        MAX_SIGNED_PAYLOAD_SIZE,
    )
}

pub(crate) fn decode_timed_with_limits<T: serde::de::DeserializeOwned>(
    signed: &str,
    secret: &[u8],
    salt: &[u8],
    max_age: i64,
    max_signed_bytes: usize,
    max_payload_bytes: u64,
) -> Option<T> {
    if signed.len() > max_signed_bytes {
        return None;
    }
    let (payload, timestamp) = signed_payload(signed, secret, salt)?;
    let timestamp = timestamp.bytes().try_fold(0_i64, |acc, byte| {
        let digit = match byte {
            b'0'..=b'9' => byte - b'0',
            b'A'..=b'Z' => byte - b'A' + 10,
            b'a'..=b'z' => byte - b'a' + 36,
            _ => return None,
        };
        acc.checked_mul(62)?.checked_add(i64::from(digit))
    })?;
    if Utc::now().timestamp().saturating_sub(timestamp) > max_age {
        return None;
    }
    serde_json::from_slice(&payload_bytes(payload, max_payload_bytes)?).ok()
}
