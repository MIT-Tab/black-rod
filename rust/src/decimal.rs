use rust_decimal::Decimal;

pub(crate) fn validate(
    raw: &str,
    precision: Option<usize>,
    places: Option<usize>,
) -> Result<Decimal, String> {
    let raw = raw.trim();
    let number = Decimal::from_str_exact(raw)
        .or_else(|_| Decimal::from_scientific(raw))
        .map_err(|_| "Enter a number.".to_owned())?;
    let scale = number.scale() as usize;
    let digits = scale.max(
        number
            .mantissa()
            .unsigned_abs()
            .checked_ilog10()
            .map_or(1, |leading| leading as usize + 1),
    );
    if let Some(max) = precision.filter(|max| digits > *max) {
        return Err(format!(
            "Ensure that there are no more than {max} digits in total."
        ));
    }
    if let Some(places) = places {
        if scale > places {
            return Err(format!(
                "Ensure that there are no more than {places} decimal places."
            ));
        }
        if let Some(max) = precision.and_then(|max| max.checked_sub(places))
            && digits - scale > max
        {
            return Err(format!(
                "Ensure that there are no more than {max} digits before the decimal point."
            ));
        }
    }
    Ok(number)
}
