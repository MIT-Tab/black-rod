use std::{fmt::Write as _, io};

use serde::Serialize;
use serde_json::{Serializer, ser::Formatter};

struct Spaced;

impl Formatter for Spaced {
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
        if ch.is_ascii() {
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
}
