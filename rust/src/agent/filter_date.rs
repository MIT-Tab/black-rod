use std::sync::LazyLock;

use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Timelike};
use regex::Regex;

static DATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}$").unwrap());
static ISO_DATETIME: LazyLock<Regex> = LazyLock::new(|| {
    // Python 3.10's fromisoformat accepts a single separator and reduced time precision.
    Regex::new(concat!(
        r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(?s:.)([0-9]{2})",
        r"(?::([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{3}(?:[0-9]{3})?))?)?)?",
        r"([+-][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{6})?)?)?$",
    ))
    .unwrap()
});
static DATETIME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"^([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})[T ]([0-9]{1,2}):([0-9]{1,2})",
        r"(?::([0-9]{1,2})(?:[.,]([0-9]{1,6})[0-9]{0,6})?)?",
        r"\s*(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?$",
    ))
    .unwrap()
});

fn calendar_date(raw: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(raw, "%Y-%m-%d")
        .ok()
        .filter(|date| date.year() > 0)
}

fn invalid_date(raw: &str) -> String {
    format!("['“{raw}” value has the correct format (YYYY-MM-DD) but it is an invalid date.']")
}

pub(super) fn date(raw: &str) -> Result<String, String> {
    let raw_date = raw.strip_suffix('\n').unwrap_or(raw);
    if DATE.is_match(raw_date) {
        calendar_date(raw_date)
            .map(|date| date.to_string())
            .ok_or_else(|| invalid_date(raw))
    } else {
        Err(format!(
            "['“{raw}” value has an invalid date format. It must be in YYYY-MM-DD format.']"
        ))
    }
}

fn micros(raw: &str) -> Option<u32> {
    Some(raw.parse::<u32>().ok()? * 10_u32.pow(6_usize.checked_sub(raw.len())? as u32))
}

fn offset(raw: &str) -> Option<TimeDelta> {
    if raw == "Z" {
        return Some(TimeDelta::zero());
    }
    let hour = raw[1..3].parse::<i64>().ok()?;
    let mut remainder = raw[3..].trim_start_matches(':').split(':');
    let minute = remainder
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or("0")
        .parse::<i64>()
        .ok()?;
    let second = remainder.next().unwrap_or("0");
    let (second, fraction) = second.split_once('.').unwrap_or((second, "0"));
    let seconds = (hour * 60 + minute) * 60 + second.parse::<i64>().ok()?;
    if seconds >= 86_400 {
        return None;
    }
    let total = 1_000_000 * seconds
        + if seconds == 0 {
            0
        } else {
            i64::from(micros(fraction)?)
        };
    Some(TimeDelta::microseconds(if raw.starts_with('-') {
        -total
    } else {
        total
    }))
}

fn parse_datetime(raw: &str) -> Result<(NaiveDateTime, TimeDelta), String> {
    let trimmed = raw.strip_suffix('\n').unwrap_or(raw);
    if DATE.is_match(trimmed) {
        return calendar_date(trimmed)
            .map(|date| (date.and_time(NaiveTime::MIN), TimeDelta::zero()))
            .ok_or_else(|| invalid_date(raw));
    }
    let Some(parts) = ISO_DATETIME
        .captures(raw)
        .or_else(|| DATETIME.captures(trimmed))
    else {
        return Err(format!(
            "['“{raw}” value has an invalid format. It must be in YYYY-MM-DD HH:MM[:ss[.uuuuuu]][TZ] format.']"
        ));
    };

    let parsed = (|| {
        let date = calendar_date(&parts[1])?;
        let part = |index| parts.get(index).map_or("0", |value| value.as_str());
        let time = NaiveTime::from_hms_micro_opt(
            part(2).parse().ok()?,
            part(3).parse().ok()?,
            part(4).parse().ok()?,
            micros(part(5))?,
        )?;
        let offset = if let Some(value) = parts.get(6) {
            offset(value.as_str())?
        } else {
            TimeDelta::zero()
        };
        Some((NaiveDateTime::new(date, time), offset))
    })();
    parsed.ok_or_else(|| format!(
        "['“{raw}” value has the correct format (YYYY-MM-DD HH:MM[:ss[.uuuuuu]][TZ]) but it is an invalid date/time.']"
    ))
}

pub(super) fn datetime(raw: &str, sqlite: bool) -> Result<String, String> {
    let (local, offset) = parse_datetime(raw)?;
    if sqlite {
        let instant = local - offset;
        let format = if instant.nanosecond() != 0 {
            "%Y-%m-%d %H:%M:%S%.6f"
        } else {
            "%Y-%m-%d %H:%M:%S"
        };
        Ok(instant.format(format).to_string())
    } else {
        // Preserve the supplied offset for PostgreSQL's own timestamp validation.
        let sign = if offset < TimeDelta::zero() { '-' } else { '+' };
        let total = offset.num_seconds().abs();
        let micros = offset.subsec_micros().abs();
        let (hour, minute, second) = (total / 3600, total / 60 % 60, total % 60);
        let mut encoded = format!(
            "{}{sign}{hour:02}:{minute:02}",
            local.format("%Y-%m-%d %H:%M:%S%.6f")
        );
        use std::fmt::Write;
        if second != 0 || micros != 0 {
            write!(encoded, ":{second:02}").expect("writing to String");
        }
        if micros != 0 {
            write!(encoded, ".{micros:06}").expect("writing to String");
        }
        Ok(encoded)
    }
}
