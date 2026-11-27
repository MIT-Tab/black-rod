use std::sync::LazyLock;

use chrono::{
    DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, SecondsFormat, Timelike,
    Utc,
};
use regex::Regex;

static DATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}$").unwrap());
static DATETIME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"^([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})[T ]([0-9]{1,2}:[0-9]{1,2})",
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

fn offset(raw: &str) -> Option<FixedOffset> {
    if raw == "Z" {
        return FixedOffset::east_opt(0);
    }
    let hour = raw[1..3].parse::<i32>().ok()?;
    let minute = raw[3..].trim_start_matches(':');
    let minute = if minute.is_empty() {
        0
    } else {
        minute.parse::<i32>().ok()?
    };
    let seconds = (hour * 60 + minute) * 60;
    if raw.starts_with('-') {
        FixedOffset::west_opt(seconds)
    } else {
        FixedOffset::east_opt(seconds)
    }
}

fn parse_datetime(raw: &str) -> Result<DateTime<Utc>, String> {
    let trimmed = raw.strip_suffix('\n').unwrap_or(raw);
    if DATE.is_match(trimmed) {
        return calendar_date(trimmed)
            .map(|date| date.and_time(NaiveTime::MIN).and_utc())
            .ok_or_else(|| invalid_date(raw));
    }
    let Some(parts) = DATETIME.captures(trimmed) else {
        return Err(format!(
            "['“{raw}” value has an invalid format. It must be in YYYY-MM-DD HH:MM[:ss[.uuuuuu]][TZ] format.']"
        ));
    };

    let parsed = (|| {
        let date = calendar_date(&parts[1])?;
        let seconds = parts.get(3).map_or("0", |value| value.as_str());
        let fraction = parts.get(4).map_or("0", |value| value.as_str());
        let time = NaiveTime::parse_from_str(
            &format!("{}:{seconds}.{fraction}", &parts[2]),
            "%H:%M:%S%.f",
        )
        .ok()?;
        if time.nanosecond() >= 1_000_000_000 {
            return None;
        }
        let local = NaiveDateTime::new(date, time);
        match parts.get(5) {
            None => Some(local.and_utc()),
            Some(value) => Some(
                local
                    .and_local_timezone(offset(value.as_str())?)
                    .single()?
                    .to_utc(),
            ),
        }
    })();
    parsed.ok_or_else(|| format!(
        "['“{raw}” value has the correct format (YYYY-MM-DD HH:MM[:ss[.uuuuuu]][TZ]) but it is an invalid date/time.']"
    ))
}

pub(super) fn datetime(raw: &str, sqlite: bool) -> Result<String, String> {
    let instant = parse_datetime(raw)?;
    let fractional = instant.timestamp_subsec_micros() != 0;
    if sqlite {
        let format = if fractional {
            "%Y-%m-%d %H:%M:%S%.6f"
        } else {
            "%Y-%m-%d %H:%M:%S"
        };
        Ok(instant.format(format).to_string())
    } else {
        let seconds = if fractional {
            SecondsFormat::Micros
        } else {
            SecondsFormat::Secs
        };
        Ok(instant.to_rfc3339_opts(seconds, true))
    }
}
