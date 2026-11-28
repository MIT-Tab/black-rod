use std::{fs, path::Path, process::Command};

use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

#[tokio::test]
async fn commands_load_release_configuration_from_another_working_directory() {
    let sandbox = tempfile::tempdir().unwrap();
    let release = sandbox.path().join("release");
    let caller = sandbox.path().join("caller");
    fs::create_dir_all(release.join("apda/assets")).unwrap();
    fs::create_dir_all(release.join("rust/static/vendor")).unwrap();
    fs::create_dir_all(&caller).unwrap();
    let original = Path::new(env!("CARGO_BIN_EXE_black-rod"));
    let binary = release.join(original.file_name().unwrap());
    if fs::hard_link(original, &binary).is_err() {
        fs::copy(original, &binary).unwrap();
    }

    let db = release.join("fixture-$BLACK_ROD_FIXTURE_SUFFIX.sqlite3");
    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(&db)
            .create_if_missing(true),
    )
    .await
    .unwrap();
    sqlx::raw_sql(
        "CREATE TABLE core_user (id INTEGER, username TEXT, email TEXT);
         CREATE TABLE core_generatedcode (code TEXT, user_id INTEGER);
         INSERT INTO core_user VALUES (1, 'example', 'sample@example.test');
         INSERT INTO core_generatedcode VALUES ('fixture', 1);",
    )
    .execute(&pool)
    .await
    .unwrap();
    pool.close().await;
    let config = format!(
        "ENV=development\nDATABASE_DIRECTORY={}\nBLACK_ROD_DATABASE_URL=sqlite://${{DATABASE_DIRECTORY}}/fixture-$BLACK_ROD_FIXTURE_SUFFIX.sqlite3?mode=ro\n",
        release.display()
    );
    fs::write(release.join(".env"), &config).unwrap();
    fs::write(caller.join(".env"), "BLACK_ROD_DATABASE_URL=broken\n").unwrap();

    let export = |filename, override_config: Option<&Path>| {
        let mut command = Command::new(&binary);
        command
            .current_dir(&caller)
            .env_remove("BLACK_ROD_RELEASE_ROOT")
            .env_remove("BLACK_ROD_DATABASE_URL")
            .env_remove("DATABASE_DIRECTORY")
            .env_remove("WEBPACK_STATS_FILE")
            .env_remove("DOT_ENV_FILE")
            .env("ENV", "development")
            .env("BLACK_ROD_BIND", "127.0.0.1:0")
            .env("BLACK_ROD_DATABASE_POOL_SIZE", "2")
            .env("BLACK_ROD_DATABASE_ACQUIRE_TIMEOUT_SECONDS", "3")
            .env("LIVE_MITTAB_DIRECTORY_URL", "")
            .env("BLACK_ROD_FIXTURE_SUFFIX", "must-not-expand")
            .args(["export_generated_codes", "--output", filename]);
        if let Some(config) = override_config {
            command.env("DOT_ENV_FILE", config);
        }
        let result = command.output().unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert_eq!(
            fs::read(caller.join(filename)).unwrap(),
            b"code,username,email\r\nfixture,example,sample@example.test\r\n"
        );
    };
    export("from-release.csv", None);
    assert!(!release.join("from-release.csv").exists());

    let override_config = sandbox.path().join("test.env");
    fs::write(override_config.as_path(), config).unwrap();
    fs::write(release.join(".env"), "BLACK_ROD_DATABASE_URL=broken\n").unwrap();
    export("from-override.csv", Some(&override_config));
}
