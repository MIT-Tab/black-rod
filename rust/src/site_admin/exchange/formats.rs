use std::io::{BufRead, BufReader, Cursor, Read, Seek, Write};

use calamine::{Data, DataType, Ods, Range, Reader, Xls, Xlsx};
use scraper::{Html, Selector};
use serde_json::{Map, Value, json};
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

type Rows = Vec<Vec<Value>>;

pub(super) fn content_type(kind: &str) -> &'static str {
    match kind {
        "csv" => "text/csv",
        "tsv" => "text/tab-separated-values",
        "json" => "application/json",
        "yaml" => "text/yaml",
        "html" => "text/html",
        "xls" => "application/vnd.ms-excel",
        "xlsx" => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ods" => "application/vnd.oasis.opendocument.spreadsheet",
        _ => "application/octet-stream",
    }
}

pub(super) fn text(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(value) => value.clone(),
        Value::Bool(value) => if *value { "True" } else { "False" }.to_owned(),
        value => value.to_string(),
    }
}

fn csv(kind: &str, mut input: impl BufRead) -> Result<Vec<Map<String, Value>>, String> {
    if input
        .fill_buf()
        .map_err(|error| error.to_string())?
        .starts_with(&[239, 187, 191])
    {
        input.consume(3);
    }
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(if kind == "csv" { b',' } else { b'\t' })
        .has_headers(false)
        .from_reader(input);
    mapped_rows(reader.records().map(|record| {
        record
            .map(|row| row.iter().map(|cell| json!(cell)).collect())
            .map_err(|error| error.to_string())
    }))
}

fn spreadsheet(range: Range<Data>) -> Rows {
    range
        .rows()
        .map(|row| {
            row.iter()
                .map(|cell| match cell {
                    Data::Empty => Value::Null,
                    Data::Bool(flag) => json!(flag),
                    Data::Int(number) => json!(number),
                    Data::Float(number)
                        if number.fract() == 0.0
                            && *number >= i64::MIN as f64
                            && *number < i64::MAX as f64 =>
                    {
                        json!(*number as i64)
                    }
                    Data::Float(number) => json!(number),
                    Data::DateTime(_) => cell
                        .as_datetime()
                        .map(|value| json!(value.format("%Y-%m-%d %H:%M:%S").to_string()))
                        .unwrap_or(Value::Null),
                    _ => json!(cell.to_string()),
                })
                .collect()
        })
        .collect()
}

fn html(bytes: &[u8]) -> Result<Rows, String> {
    let input = std::str::from_utf8(bytes).map_err(|error| error.to_string())?;
    let doc = Html::parse_document(input);
    let table = Selector::parse("table").expect("table");
    let row = Selector::parse("tr").expect("row");
    let cell = Selector::parse("td, th").expect("table cells");
    let Some(table) = doc.select(&table).next() else {
        return Err("No <table> found in input HTML".to_owned());
    };
    Ok(table
        .select(&row)
        .map(|row| {
            row.select(&cell)
                .map(|cell| json!(cell.text().collect::<String>()))
                .collect()
        })
        .collect())
}

fn yaml_value(value: &yaml_rust2::Yaml) -> Value {
    use yaml_rust2::Yaml;
    match value {
        Yaml::Integer(value) => json!(value),
        Yaml::Real(value) => value
            .parse::<f64>()
            .ok()
            .map_or_else(|| json!(value), |value| json!(value)),
        Yaml::String(value) => json!(value),
        Yaml::Boolean(value) => json!(value),
        Yaml::Array(values) => Value::Array(values.iter().map(yaml_value).collect()),
        Yaml::Hash(values) => Value::Object(
            values
                .iter()
                .filter_map(|(key, value)| Some((key.as_str()?.to_owned(), yaml_value(value))))
                .collect(),
        ),
        _ => Value::Null,
    }
}

fn map_rows(input: Value) -> Result<Vec<Map<String, Value>>, String> {
    let Value::Array(list) = input else {
        return Err("Expected a list of rows.".to_owned());
    };
    list.into_iter()
        .map(|row| match row {
            Value::Object(values) => Ok(values),
            _ => Err("Expected each row to be a mapping.".to_owned()),
        })
        .collect()
}

pub(super) fn read(
    kind: &str,
    mut input: impl Read + Seek,
) -> Result<Vec<Map<String, Value>>, String> {
    let rows = match kind {
        "csv" | "tsv" => return csv(kind, BufReader::new(input)),
        "html" => {
            let mut bytes = Vec::new();
            input
                .read_to_end(&mut bytes)
                .map_err(|error| error.to_string())?;
            html(&bytes)?
        }
        "json" => {
            return map_rows(
                serde_json::from_reader(BufReader::new(input))
                    .map_err(|error| error.to_string())?,
            );
        }
        "yaml" => {
            let mut content = String::new();
            input
                .read_to_string(&mut content)
                .map_err(|error| error.to_string())?;
            let yaml = yaml_rust2::YamlLoader::load_from_str(&content)
                .map_err(|error| error.to_string())?;
            return map_rows(yaml.first().map_or(Value::Null, yaml_value));
        }
        "xls" => spreadsheet(
            Xls::new(BufReader::new(input))
                .map_err(|error| error.to_string())?
                .worksheet_range_at(0)
                .ok_or("The workbook has no sheets.")?
                .map_err(|error| error.to_string())?,
        ),
        "xlsx" => {
            let index = crate::motion::import::worksheet_index(&mut input);
            input.rewind().map_err(|error| error.to_string())?;
            spreadsheet(
                Xlsx::new(BufReader::new(input))
                    .map_err(|error| error.to_string())?
                    .worksheet_range_at(index)
                    .ok_or("The workbook has no sheets.")?
                    .map_err(|error| error.to_string())?,
            )
        }
        "ods" => spreadsheet(
            Ods::new(BufReader::new(input))
                .map_err(|error| error.to_string())?
                .worksheet_range_at(0)
                .ok_or("The workbook has no sheets.")?
                .map_err(|error| error.to_string())?,
        ),
        _ => return Err("Select a valid format.".to_owned()),
    };
    mapped_rows(rows.into_iter().map(Ok))
}

fn mapped_rows(
    mut rows: impl Iterator<Item = Result<Vec<Value>, String>>,
) -> Result<Vec<Map<String, Value>>, String> {
    let headers = rows
        .next()
        .ok_or("The file has no rows.")??
        .iter()
        .map(text)
        .collect::<Vec<_>>();
    rows.map(|row| {
        let row = row?;
        if row.len() > headers.len() {
            return Err("A data row has more cells than the header.".to_owned());
        }
        Ok(headers
            .iter()
            .cloned()
            .zip(row.into_iter().chain(std::iter::repeat(Value::Null)))
            .collect())
    })
    .collect()
}

fn xml_text(output: &mut String, input: &str) {
    for value in input.chars() {
        match value {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            value if value < ' ' && !matches!(value, '\t' | '\n' | '\r') => output.push('\u{FFFD}'),
            value => output.push(value),
        }
    }
}

fn html_table(headers: &[String], rows: &Rows) -> Vec<u8> {
    let mut output = "<table><thead><tr>".to_owned();
    for header in headers {
        output.push_str("<th>");
        xml_text(&mut output, header);
        output.push_str("</th>");
    }
    output.push_str("</tr></thead><tbody>");
    for row in rows {
        output.push_str("<tr>");
        for value in row {
            output.push_str("<td>");
            xml_text(&mut output, &text(value));
            output.push_str("</td>");
        }
        output.push_str("</tr>");
    }
    output.push_str("</tbody></table>");
    output.into_bytes()
}

fn sheet(kind: &str, headers: &[String], rows: &Rows) -> Result<Vec<u8>, String> {
    use xls::{Cell, Workbook};
    let mut workbook = Workbook::new();
    let worksheet = workbook.sheet_mut(0).expect("initial sheet");
    let (max_rows, max_cols) = if kind == "xls" {
        (65_536, 256)
    } else {
        (1_048_576, 16_384)
    };
    if rows.len() >= max_rows || headers.len() > max_cols {
        return Err(format!("The data exceeds the {kind} workbook dimensions."));
    }
    worksheet.name = "Tablib Dataset".to_owned();
    for (index, header) in headers.iter().enumerate() {
        worksheet.set(0, index as u32, Cell::Text(header.clone()));
    }
    for (row, values) in rows.iter().enumerate() {
        for (column, value) in values.iter().enumerate() {
            let cell = match value {
                Value::Null => continue,
                Value::Number(number) => Cell::Number(number.as_f64().unwrap_or_default()),
                Value::Bool(flag) => Cell::Bool(*flag),
                value => Cell::Text(text(value)),
            };
            worksheet.set((row + 1) as u32, column as u32, cell);
        }
    }
    let mut result = Cursor::new(Vec::new());
    let written = if kind == "xls" {
        xls::core::xls::write(&workbook, &mut result)
    } else {
        xls::core::xlsx::write(&workbook, &mut result)
    };
    written.map_err(|error| error.to_string())?;
    Ok(result.into_inner())
}

fn ods(headers: &[String], rows: &Rows) -> Result<Vec<u8>, String> {
    const TYPE: &str = "application/vnd.oasis.opendocument.spreadsheet";
    let mut result = ZipWriter::new(Cursor::new(Vec::new()));
    let formats = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    let mut write = || -> Result<(), Box<dyn std::error::Error>> {
        result.start_file(
            "mimetype",
            SimpleFileOptions::default().compression_method(CompressionMethod::Stored),
        )?;
        result.write_all(TYPE.as_bytes())?;
        result.start_file("META-INF/manifest.xml", formats)?;
        result.write_all(br#"<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet" manifest:version="1.2"/>
<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>"#)?;
        result.start_file("content.xml", formats)?;
        result.write_all(br#"<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2"><office:body><office:spreadsheet><table:table table:name="Tablib Dataset">"#)?;
        let headings = headers.iter().map(|text| json!(text)).collect::<Vec<_>>();
        for values in std::iter::once(&headings).chain(rows) {
            let mut row = "<table:table-row>".to_owned();
            for value in values {
                match value {
                    Value::Null=>row.push_str("<table:table-cell/>"),
                    Value::Number(number)=>row.push_str(&format!(r#"<table:table-cell office:value-type="float" office:value="{number}"><text:p>{number}</text:p></table:table-cell>"#)),
                    value=>{
                        row.push_str(r#"<table:table-cell office:value-type="string"><text:p>"#);
                        xml_text(&mut row,&text(value));
                        row.push_str("</text:p></table:table-cell>");
                    }
                }
            }
            row.push_str("</table:table-row>");
            result.write_all(row.as_bytes())?;
        }
        result.write_all(
            b"</table:table></office:spreadsheet></office:body></office:document-content>",
        )?;
        Ok(())
    };
    write().map_err(|error| error.to_string())?;
    result
        .finish()
        .map(Cursor::into_inner)
        .map_err(|error| error.to_string())
}

pub(super) fn write(kind: &str, headers: &[String], rows: &Rows) -> Result<Vec<u8>, String> {
    match kind {
        "csv" | "tsv" => {
            let mut output = csv::WriterBuilder::new()
                .delimiter(if kind == "csv" { b',' } else { b'\t' })
                .terminator(csv::Terminator::CRLF)
                .from_writer(Vec::new());
            output
                .write_record(headers)
                .map_err(|error| error.to_string())?;
            for row in rows {
                output
                    .write_record(row.iter().map(text))
                    .map_err(|error| error.to_string())?;
            }
            output.into_inner().map_err(|error| error.to_string())
        }
        "html" => Ok(html_table(headers, rows)),
        "xls" | "xlsx" => sheet(kind, headers, rows),
        "ods" => ods(headers, rows),
        "json" | "yaml" => {
            let data = rows
                .iter()
                .map(|row| {
                    headers
                        .iter()
                        .cloned()
                        .zip(row.iter().cloned())
                        .collect::<Map<String, Value>>()
                })
                .collect::<Vec<_>>();
            let json = serde_json::to_string(&data).map_err(|error| error.to_string())?;
            if kind == "json" {
                return Ok(json.into_bytes());
            }
            let values =
                yaml_rust2::YamlLoader::load_from_str(&json).map_err(|error| error.to_string())?;
            let mut result = String::new();
            yaml_rust2::YamlEmitter::new(&mut result)
                .dump(&values[0])
                .map_err(|error| error.to_string())?;
            Ok(result.into_bytes())
        }
        _ => Err("Select a valid format.".to_owned()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_admin_format_preserves_headers_and_cell_data() {
        let headers = ["id", "name", "short_name", "season"]
            .map(str::to_owned)
            .to_vec();
        let rows = vec![
            vec![
                json!(42),
                json!("Équipe A & B"),
                json!("A, B"),
                json!("2026"),
            ],
            vec![
                json!(43),
                json!("Line 1\nLine 2"),
                json!("<example>"),
                json!("2004"),
            ],
        ];
        for format in ["csv", "xls", "xlsx", "tsv", "ods", "json", "yaml", "html"] {
            let data = write(format, &headers, &rows)
                .unwrap_or_else(|error| panic!("{format} export: {error}"));
            let observed = read(format, Cursor::new(&data))
                .unwrap_or_else(|error| panic!("{format} import: {error}"));
            let observed = observed
                .iter()
                .map(|row| {
                    headers
                        .iter()
                        .map(|header| text(&row[header]))
                        .collect::<Vec<_>>()
                })
                .collect::<Vec<_>>();
            let expected = rows
                .iter()
                .map(|row| row.iter().map(text).collect::<Vec<_>>())
                .collect::<Vec<_>>();
            assert_eq!(expected, observed, "{format}");
        }
    }

    #[test]
    fn imports_preserve_blank_values_and_reject_oversized_rows() {
        let input = b"id,name,included_in_oty\r\n1,First School,1\r\n,New School,0\r\n";
        let parsed = read("csv", Cursor::new(input)).unwrap();
        assert_eq!(parsed[0]["id"], json!("1"));
        assert_eq!(parsed[1]["id"], json!(""));
        assert_eq!(parsed[1]["included_in_oty"], json!("0"));
        assert!(read("tsv", Cursor::new(b"id\tname\n1\tSchool\tUnexpected")).is_err());
        assert!(read("json", Cursor::new(b"{}")).is_err());
    }
}
