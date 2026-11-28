use std::{fmt::Write as _, io};

use serde::Serialize;
use serde_json::{Serializer, ser::Formatter};

struct Spaced;

impl Formatter for Spaced {
    fn write_number_str<W: ?Sized + io::Write>(
        &mut self,
        out: &mut W,
        number: &str,
    ) -> io::Result<()> {
        Canonical.write_number_str(out, number)
    }

    fn begin_array_value<W: ?Sized + io::Write>(
        &mut self,
        out: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if !first {
            out.write_all(b", ")?;
        }
        Ok(())
    }

    fn begin_object_key<W: ?Sized + io::Write>(
        &mut self,
        out: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if !first {
            out.write_all(b", ")?;
        }
        Ok(())
    }

    fn begin_object_value<W: ?Sized + io::Write>(&mut self, out: &mut W) -> io::Result<()> {
        out.write_all(b": ")
    }
}

pub(crate) fn ascii(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    for ch in value.chars() {
        if ch.is_ascii() && ch != '\u{007f}' {
            output.push(ch);
        } else {
            let mut units = [0; 2];
            for unit in ch.encode_utf16(&mut units) {
                let _ = write!(output, "\\u{unit:04x}");
            }
        }
    }
    output
}

struct Canonical;

impl Formatter for Canonical {
    fn write_number_str<W: ?Sized + io::Write>(
        &mut self,
        out: &mut W,
        number: &str,
    ) -> io::Result<()> {
        if number.contains(['.', 'e', 'E'])
            && let Ok(float) = number.parse::<f64>()
        {
            if float.is_infinite() {
                return out.write_all(if float.is_sign_negative() {
                    b"-Infinity"
                } else {
                    b"Infinity"
                });
            }
            let mut buffer = ryu::Buffer::new();
            let repr = buffer.format_finite(float);
            if let Some((mantissa, exp)) = repr.split_once('e')
                && let Ok(exp) = exp.parse::<i32>()
            {
                return write!(out, "{mantissa}e{exp:+03}");
            }
            let (sign, magnitude) = repr
                .strip_prefix('-')
                .map_or(("", repr), |value| ("-", value));
            // Python uses scientific notation one decade earlier than Ryū.
            if let Some(digits) = magnitude.strip_prefix("0.0000")
                && let Some((first, rest)) = digits.split_at_checked(1)
            {
                write!(out, "{sign}{first}")?;
                if !rest.is_empty() {
                    write!(out, ".{rest}")?;
                }
                return out.write_all(b"e-05");
            }
            return out.write_all(repr.as_bytes());
        }
        out.write_all(if number == "-0" {
            b"0"
        } else {
            number.as_bytes()
        })
    }
}

pub(crate) fn canonical(value: &serde_json::Value) -> String {
    let mut output = Vec::new();
    value
        .serialize(&mut Serializer::with_formatter(&mut output, Canonical))
        .expect("a JSON value can be written to memory");
    ascii(std::str::from_utf8(&output).expect("JSON is UTF-8"))
}

pub(crate) fn encode<T: ?Sized + Serialize>(value: &T) -> Result<String, serde_json::Error> {
    let mut output = Vec::new();
    value.serialize(&mut Serializer::with_formatter(&mut output, Spaced))?;
    Ok(ascii(std::str::from_utf8(&output).expect("JSON is UTF-8")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uses_djangos_sqlite_spacing_and_ascii_json() {
        let value = serde_json::json!([{"name": "Été 🌌", "score": 42}]);
        assert_eq!(
            encode(&value).unwrap(),
            r#"[{"name": "\u00c9t\u00e9 \ud83c\udf0c", "score": 42}]"#
        );
    }

    #[test]
    fn spaced_json_canonicalizes_numbers_like_python_dumps() {
        for raw in [
            r#"{"speaks":26.50,"tiny":0.000010000,"big":1E16,"zero":-0,"integer_value":4e0}"#,
            r#"{"tiny":1e-05,"zero":0,"speaks":26.5,"integer_value":4.0,"big":10000000000000000.0}"#,
        ] {
            let value: serde_json::Value = serde_json::from_str(raw).unwrap();
            assert_eq!(
                encode(&value).unwrap(),
                r#"{"big": 1e+16, "integer_value": 4.0, "speaks": 26.5, "tiny": 1e-05, "zero": 0}"#
            );
        }
    }
}
