use std::{
    io,
    path::{Path, PathBuf},
};

use chrono::{SecondsFormat, Utc};
use serde_json::{Value, json};
use tokio::{fs, io::AsyncWriteExt, sync::Mutex};

static WRITING: Mutex<()> = Mutex::const_new(());

pub(in crate::admin) fn enabled() -> bool {
    std::env::var("ENV").is_ok_and(|mode| mode.trim().eq_ignore_ascii_case("development"))
}

pub(in crate::admin) fn path(root: &Path) -> PathBuf {
    std::env::var("ROUND_AMENDMENTS_FILE")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("apda/round-amendments.local.json"))
}

pub(in crate::admin) async fn append(
    root: &Path,
    action: &Value,
) -> Result<Option<PathBuf>, String> {
    if !enabled() {
        return Ok(None);
    }
    let _lock = WRITING.lock().await;
    let path = path(root);
    let parent = path
        .parent()
        .filter(|dir| !dir.as_os_str().is_empty())
        .unwrap_or(std::path::Path::new("."));
    fs::create_dir_all(parent)
        .await
        .map_err(|error| error.to_string())?;
    let mut doc = match fs::read(&path).await {
        Ok(data) => serde_json::from_slice::<Value>(&data).map_err(|_| {
            format!(
                "Existing amendment file is not valid JSON: {}",
                path.display()
            )
        })?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => json!({"actions":[]}),
        Err(error) => return Err(error.to_string()),
    };
    let fields = doc
        .as_object_mut()
        .ok_or("Existing amendment file must contain a top-level JSON object.")?;
    let list = fields.entry("actions").or_insert(json!([]));
    if list.is_null() {
        *list = json!([]);
    }
    list.as_array_mut()
        .ok_or("Existing amendment file must contain an 'actions' list.")?
        .push(action.clone());
    fields.insert(
        "last_updated_at".into(),
        json!(Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)),
    );
    let contents = crate::django_json::ascii(
        &serde_json::to_string_pretty(&doc).map_err(|error| error.to_string())?,
    );
    let temp = parent.join(format!(
        ".round-amendment-{}.tmp",
        uuid::Uuid::new_v4().simple()
    ));
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let result = async {
        let mut file = options.open(&temp).await?;
        file.write_all(contents.as_bytes()).await?;
        file.write_all(b"\n").await?;
        file.sync_all().await?;
        fs::rename(&temp, &path).await
    }
    .await;
    if let Err(error) = result {
        let _ = fs::remove_file(&temp).await;
        return Err(error.to_string());
    }
    Ok(Some(path))
}
