use chrono::{Datelike, NaiveDate};

fn four_digit_year(value: &str) -> bool {
    value.len() == 4 && value.bytes().all(|byte| byte.is_ascii_digit())
}

pub(crate) fn numeric(value: &str) -> Option<NaiveDate> {
    let value = value.trim();
    let result = if let Some((month_day, year)) = value.rsplit_once('/') {
        if four_digit_year(year) {
            NaiveDate::parse_from_str(value, "%m/%d/%Y")
        } else if year.len() == 2 && year.bytes().all(|byte| byte.is_ascii_digit()) {
            let short = year.parse::<u16>().ok()?;
            let year = if short < 69 { 2000 } else { 1900 } + short;
            NaiveDate::parse_from_str(&format!("{month_day}/{year}"), "%m/%d/%Y")
        } else {
            return None;
        }
    } else {
        let (year, _) = value.split_once('-')?;
        if !four_digit_year(year) {
            return None;
        }
        NaiveDate::parse_from_str(value, "%Y-%m-%d")
    };
    result.ok().filter(|date| date.year() != 0)
}

pub(crate) fn form(value: &str) -> Option<NaiveDate> {
    numeric(value).or_else(|| {
        let value = value.trim();
        let (_, year) = value.rsplit_once(' ')?;
        if !four_digit_year(year) {
            return None;
        }
        [
            "%b %d %Y",
            "%b %d, %Y",
            "%d %b %Y",
            "%d %b, %Y",
            "%B %d %Y",
            "%B %d, %Y",
            "%d %B %Y",
            "%d %B, %Y",
        ]
        .into_iter()
        .find_map(|format| NaiveDate::parse_from_str(value, format).ok())
        .filter(|date| date.year() != 0)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numeric_dates_respect_djangos_year_width_and_century_boundary() {
        for (value, expected) in [
            ("2/14/26", "2026-02-14"),
            ("2/14/2026", "2026-02-14"),
            ("11/27/68", "2068-11-27"),
            ("11/27/69", "1969-11-27"),
        ] {
            assert_eq!(numeric(value).unwrap().to_string(), expected);
        }
        for value in ["2/14/2", "2/14/026", "26-2-14", "0000-02-14", "2/29/26"] {
            assert!(numeric(value).is_none(), "{value}");
        }
    }
}
