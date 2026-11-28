use std::{collections::BTreeMap, env, fmt::Write as _, fs, io, path::Path, sync::LazyLock};

use adotenv::{Dialect, Severity, parse_with};
use regex::{Captures, Regex};

// Python's dotenv interpolation runs on the decoded value, including single-quoted values.
static REFERENCE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\$\{([^}:]*)(?::-([^}]*))?\}").expect("valid dotenv reference pattern")
});

fn resolved_values(
    source: &str,
    process: &BTreeMap<String, String>,
) -> BTreeMap<String, Option<String>> {
    let (document, diagnostics) = parse_with(source, Dialect::PYTHON_DOTENV);
    for diagnostic in diagnostics
        .iter()
        .filter(|item| item.severity() == Severity::Error)
    {
        eprintln!(
            "Could not parse environment file statement at line {}.",
            diagnostic.line()
        );
    }
    let mut values = BTreeMap::<String, Option<String>>::new();
    for entry in document.entries() {
        let value = entry.value().map(|value| {
            REFERENCE
                .replace_all(value, |matched: &Captures<'_>| {
                    let name = &matched[1];
                    match (process.get(name), values.get(name)) {
                        (Some(value), _) | (None, Some(Some(value))) => value.clone(),
                        (None, Some(None)) => String::new(),
                        (None, None) => matched
                            .get(2)
                            .map_or("", |default| default.as_str())
                            .to_owned(),
                    }
                })
                .into_owned()
        });
        values.insert(entry.key().to_owned(), value);
    }
    values
}

fn render_assignments(
    values: BTreeMap<String, Option<String>>,
    assigned: impl Fn(&str) -> bool,
) -> io::Result<String> {
    let mut output = String::new();
    for (key, value) in values {
        let Some(value) = value else { continue };
        // Match the installer's key alphabet. Every Black Rod setting uses this alphabet.
        if !key.starts_with(|ch: char| ch == '_' || ch.is_ascii_alphabetic())
            || !key
                .bytes()
                .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, b'_' | b'.'))
            || assigned(&key)
        {
            continue;
        }
        if value.contains('\0') {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid environment file value.",
            ));
        }
        let escaped = value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('$', "\\$")
            .replace('\n', "\\n");
        writeln!(output, "{key}=\"{escaped}\"").expect("writing to memory cannot fail");
    }
    Ok(output)
}

pub(super) fn load(path: &Path) -> io::Result<()> {
    if env::var("PYTHON_DOTENV_DISABLED").is_ok_and(|value| {
        matches!(
            value.to_ascii_lowercase().as_str(),
            "1" | "true" | "t" | "yes" | "y"
        )
    }) {
        return Ok(());
    }
    let source = match fs::read_to_string(path) {
        Ok(source) => source,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(io::Error::new(
                error.kind(),
                "Could not read the Black Rod environment file.",
            ));
        }
    };
    let process = env::vars_os()
        .filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?)))
        .collect();
    let values = resolved_values(&source, &process);
    let assignments = render_assignments(values, |key| env::var_os(key).is_some())?;
    // Apply already-resolved assignments before starting the runtime. The installer never sees
    // the original file, so it cannot reinterpret its quoted values or variable references.
    dotenvy::from_read(assignments.as_bytes()).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "Could not apply the Black Rod environment file.",
        )
    })
}

pub(super) fn positive_number(name: &str, default: u32) -> io::Result<u32> {
    let Some(value) = env::var_os(name) else {
        return Ok(default);
    };
    value
        .to_str()
        .and_then(|value| value.parse::<std::num::NonZeroU32>().ok())
        .map(std::num::NonZeroU32::get)
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{name} must be a positive integer."),
            )
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_django_values_through_environment_installation() {
        let source = concat!(
            "ENV_SOURCE=file\n",
            "EMPTY=\n",
            "BARE\n",
            "SECRET_KEY=synthetic$ENV_SOURCE\\key stays intact # comment\n",
            "DATABASE_PASSWORD=\"quoted\\tvalue\\\\path\\$ENV_SOURCE\"\n",
            "SENTRY_DSN='${ENV_SOURCE}/${MISSING:-fallback}/${EMPTY:-fallback}/${BARE:-fallback}'\n",
            "LITERAL='line one\nline two'\n",
            "DUPLICATE=first\nDUPLICATE=last\n",
        );
        let process = BTreeMap::from([("ENV_SOURCE".to_owned(), "process".to_owned())]);
        let values = resolved_values(source, &process);
        let assignments = render_assignments(values, |key| process.contains_key(key)).unwrap();
        let installed = dotenvy::from_read_iter(assignments.as_bytes())
            .collect::<Result<BTreeMap<_, _>, _>>()
            .unwrap();
        assert_eq!(
            installed.get("SECRET_KEY").unwrap(),
            r"synthetic$ENV_SOURCE\key stays intact"
        );
        assert_eq!(
            installed.get("DATABASE_PASSWORD").unwrap(),
            "quoted\tvalue\\path\\$ENV_SOURCE"
        );
        assert_eq!(installed.get("SENTRY_DSN").unwrap(), "process/fallback//");
        assert_eq!(installed.get("LITERAL").unwrap(), "line one\nline two");
        assert_eq!(installed.get("DUPLICATE").unwrap(), "last");
        assert!(!installed.contains_key("ENV_SOURCE"));
        assert!(!installed.contains_key("BARE"));
    }
}
