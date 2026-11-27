use serde_json::Value;

pub(crate) fn metadata_ids(metadata: &Value, key: &str) -> Option<Vec<i64>> {
    metadata[key]
        .as_array()?
        .iter()
        .map(|value| {
            value
                .as_i64()
                .or_else(|| value.as_str()?.trim().parse().ok())
                .or_else(|| {
                    value
                        .as_f64()
                        .filter(|number| number.is_finite())
                        .map(|number| number as i64)
                })
                .or_else(|| value.as_bool().map(i64::from))
        })
        .collect()
}

pub(crate) fn metadata_winner(metadata: &Value, victor: i64) -> Option<usize> {
    match metadata["winner"]
        .as_str()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "a" => Some(0),
        "b" => Some(1),
        _ => match victor {
            1 | 3 => Some(0),
            2 | 4 => Some(1),
            _ => None,
        },
    }
}

pub(crate) fn metadata_elimination(metadata: &Value, stage: &str) -> bool {
    match metadata["stage"]
        .as_str()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "outround" => true,
        "prelim" => false,
        _ => matches!(
            stage.trim().to_ascii_lowercase().as_str(),
            "outround" | "elim"
        ),
    }
}
