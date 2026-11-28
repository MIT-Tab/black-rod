use std::collections::BTreeMap;

use serde_json::{Value, json};
use serde_pickle::{
    DeOptions, HashableValue, Value as Pickle, value::from_value, value_from_slice, value_to_writer,
};

use super::profiles::Dashboard;
use crate::cache::python_value;

const MODELS: &str = "core.utils.elo_runtime_engine.models";

fn scalar(output: &mut Vec<u8>, value: &Pickle) -> Result<(), serde_pickle::Error> {
    let begin = output.len();
    value_to_writer(output, value, serde_pickle::SerOptions::new().proto_v2())?;
    // Embed the value without its separate protocol header and stop opcode.
    output.drain(begin..begin + 2);
    output.pop();
    Ok(())
}

fn fields(
    output: &mut Vec<u8>,
    name: &str,
    row: &serde_json::Map<String, Value>,
) -> Result<(), serde_pickle::Error> {
    // GLOBAL, EMPTY_TUPLE, NEWOBJ, NONE, EMPTY_DICT, MARK. The instance
    // uses Python slots, stored as (None, fields) before BUILD.
    output.extend_from_slice(format!("c{MODELS}\n{name}\n").as_bytes());
    output.extend_from_slice(b")\x81N}(");
    for (key, value) in row {
        if key == "elo_display" {
            continue;
        }
        scalar(output, &Pickle::String(key.clone()))?;
        if key == "ranking_rows" {
            output.extend_from_slice(b"](");
            if let Some(rows) = value.as_array() {
                for row in rows.iter().filter_map(Value::as_object) {
                    fields(output, "DebaterRankingRow", row)?;
                }
            }
            output.push(b'e'); // APPENDS
        } else {
            scalar(output, &python_value(value))?;
        }
    }
    output.extend_from_slice(b"u\x86b"); // SETITEMS, TUPLE2, BUILD
    Ok(())
}

pub(super) fn encode(result: &Dashboard, settings: Option<(&str, &str)>) -> Option<Vec<u8>> {
    let mut document = serde_json::to_value(result).ok()?;
    for row in document.get_mut("ranking_rows")?.as_array_mut()? {
        let names = row
            .get("schools")?
            .as_array()?
            .iter()
            .filter_map(|school| school.get("name")?.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        row.as_object_mut()?
            .insert("school_name".to_owned(), json!(names));
    }
    let mut output = Vec::new();
    output.extend_from_slice(b"\x80\x02}(");
    scalar(&mut output, &Pickle::String("result".to_owned())).ok()?;
    fields(&mut output, "EloRunResult", document.as_object()?).ok()?;
    if let Some((compute, display)) = settings {
        scalar(&mut output, &Pickle::String("compute_signature".to_owned())).ok()?;
        scalar(&mut output, &Pickle::String(compute.to_owned())).ok()?;
        scalar(&mut output, &Pickle::String("display_signature".to_owned())).ok()?;
        scalar(&mut output, &Pickle::String(display.to_owned())).ok()?;
    }
    output.extend_from_slice(b"u.");
    Some(output)
}

fn slots(value: Pickle) -> Option<BTreeMap<HashableValue, Pickle>> {
    let Pickle::Tuple(mut values) = value else {
        return None;
    };
    if values.len() != 2 || values.first() != Some(&Pickle::None) {
        return None;
    }
    match values.pop()? {
        Pickle::Dict(data) => Some(data),
        _ => None,
    }
}

pub(super) fn decode(data: &[u8]) -> Option<Dashboard> {
    let options = DeOptions::new().keep_restore_state();
    let Pickle::Dict(mut cache) = value_from_slice(data, options).ok()? else {
        return None;
    };
    let mut result = slots(cache.remove(&HashableValue::String("result".to_owned()))?)?;
    let ranking = HashableValue::String("ranking_rows".to_owned());
    let Pickle::List(rows) = result.remove(&ranking)? else {
        return None;
    };
    let rows = rows
        .into_iter()
        .map(|row| {
            let mut attributes = slots(row)?;
            let elo = match attributes.get(&HashableValue::String("elo".to_owned()))? {
                Pickle::F64(elo) if elo.is_finite() => *elo,
                _ => return None,
            };
            attributes.insert(
                HashableValue::String("elo_display".to_owned()),
                Pickle::String(super::profiles::display_elo(elo)),
            );
            Some(Pickle::Dict(attributes))
        })
        .collect::<Option<Vec<_>>>()?;
    result.insert(ranking, Pickle::List(rows));
    from_value(Pickle::Dict(result)).ok()
}
