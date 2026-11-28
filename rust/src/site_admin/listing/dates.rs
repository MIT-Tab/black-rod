use std::collections::BTreeSet;

use chrono::{Datelike, NaiveDate};
use minijinja::{Value, context};
use sqlx::AssertSqlSafe;

use super::{AdminRequest, ApiError, Parameters, query_string};

const YEAR: &str = "date_set__year";
const MONTH: &str = "date_set__month";
const DAY: &str = "date_set__day";

pub(super) async fn navigation(
    admin: &AdminRequest<'_>,
    table: &str,
    condition: &str,
    values: &[String],
    params: &Parameters,
) -> Result<Value, ApiError> {
    let query =
        format!("SELECT DISTINCT CAST(t0.date_set AS TEXT) FROM {table}{condition} ORDER BY 1");
    let mut dates = sqlx::query_scalar::<_, Option<String>>(AssertSqlSafe(query));
    for value in values {
        dates = dates.bind(value);
    }
    let dates = dates
        .fetch_all(&admin.state.pool)
        .await?
        .into_iter()
        .flatten()
        .filter_map(|value| NaiveDate::parse_from_str(&value, "%Y-%m-%d").ok())
        .collect::<Vec<_>>();
    let mut query = params.clone();
    query.retain(|key| key != "p" && !key.starts_with("date_set__"));
    let link = |year: Option<i32>, month: Option<u32>, day: Option<u32>| {
        let mut next = query.clone();
        for (name, value) in [
            (YEAR, year.map(|year| year.to_string())),
            (MONTH, month.map(|month| month.to_string())),
            (DAY, day.map(|day| day.to_string())),
        ] {
            if let Some(value) = value {
                next.insert(name.to_owned(), value);
            }
        }
        query_string(&next)
    };
    let lookup_year = params.get(YEAR).and_then(|value| value.parse::<i32>().ok());
    let lookup_month = params
        .get(MONTH)
        .and_then(|value| value.parse::<u32>().ok());
    let lookup_day = params.get(DAY).and_then(|value| value.parse::<u32>().ok());
    let mut year = lookup_year;
    let mut month = lookup_month;
    if year.is_none()
        && month.is_none()
        && lookup_day.is_none()
        && let (Some(first), Some(last)) = (dates.first(), dates.last())
        && first.year() == last.year()
    {
        year = Some(first.year());
        if first.month() == last.month() {
            month = Some(first.month());
        }
    }
    let (back, choices) = match (year, month, lookup_day) {
        (Some(year), Some(month), Some(day)) => {
            let Some(day) = NaiveDate::from_ymd_opt(year, month, day) else {
                return Ok(Value::UNDEFINED);
            };
            (
                Some(context! {link=>link(Some(year),Some(month),None),title=>day.format("%B %Y").to_string()}),
                vec![context! {title=>day.format("%B %-d").to_string()}],
            )
        }
        (Some(year), Some(month), _) => (
            Some(context! {link=>link(Some(year),None,None),title=>year.to_string()}),
            dates.into_iter().map(|date| context! {
                link=>link(Some(year),Some(month),Some(date.day())),title=>date.format("%B %-d").to_string()
            }).collect(),
        ),
        (Some(year), _, _) => (
            Some(context! {link=>link(None,None,None),title=>"All dates"}),
            dates.into_iter().map(|day| day.month()).collect::<BTreeSet<_>>().into_iter()
                .filter_map(|month|NaiveDate::from_ymd_opt(year,month,1))
                .map(|month| context! {link=>link(Some(year),Some(month.month()),None),title=>month.format("%B %Y").to_string()})
                .collect(),
        ),
        _ => (
            None,
            dates.into_iter().map(|day|day.year()).collect::<BTreeSet<_>>().into_iter()
                .map(|year|context! {link=>link(Some(year),None,None),title=>year.to_string()})
                .collect(),
        ),
    };
    Ok(context! {back,choices})
}
