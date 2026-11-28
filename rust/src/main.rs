use std::{
    env,
    net::SocketAddr,
    path::{Path, PathBuf},
    time::Duration,
};

use black_rod::{AppState, COMMAND_HELP, Live, OAuth, PrivateTokens, app};
use sqlx::any::{AnyPoolOptions, install_default_drivers};
use tokio::net::TcpListener;

fn release_root() -> std::io::Result<Option<PathBuf>> {
    if let Some(path) = env::var_os("BLACK_ROD_RELEASE_ROOT") {
        return PathBuf::from(path).canonicalize().map(Some);
    }
    let executable = env::current_exe()?.canonicalize()?;
    let working_directory = env::current_dir()?;
    Ok(executable
        .ancestors()
        .skip(1)
        .chain([working_directory.as_path()])
        .find(|root| root.join("apda/assets").is_dir() && root.join("rust/static/vendor").is_dir())
        .map(Path::to_path_buf))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = release_root()?;
    if let Some(config) = env::var_os("DOT_ENV_FILE")
        .map(PathBuf::from)
        .or_else(|| root.as_ref().map(|root| root.join(".env")))
    {
        match dotenvy::from_path(config) {
            Ok(()) => (),
            Err(error) if error.not_found() => (),
            Err(_) => return Err("Could not read the Black Rod environment file.".into()),
        }
    }
    let _monitoring = if env::var("ENV").as_deref() == Ok("production") {
        let dsn = env::var("SENTRY_DSN").unwrap_or_default();
        if dsn.is_empty() {
            None
        } else {
            let dsn = dsn
                .parse::<sentry::types::Dsn>()
                .map_err(|_| "Invalid Sentry DSN.")?;
            Some(sentry::init((
                dsn,
                sentry::ClientOptions::new()
                    .maybe_release(sentry::release_name!())
                    .environment("production"),
            )))
        }
    } else {
        None
    };
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(run(root))
}

async fn run(root: Option<PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let command = args.next().unwrap_or_else(|| "serve".into());
    let args = args.collect::<Vec<_>>();
    if matches!(command.as_str(), "--help" | "-h" | "help") {
        print!("{COMMAND_HELP}");
        return Ok(());
    }
    install_default_drivers();
    let production = env::var("ENV").is_ok_and(|mode| mode == "production");
    let production_web = production && command == "serve";
    let secret = env::var("SECRET_KEY")
        .ok()
        .or_else(|| (env::var("ENV").as_deref() == Ok("development")).then(|| "abcd".to_owned()));
    if production_web && secret.as_deref().is_none_or(|key| key.trim().is_empty()) {
        return Err("Set SECRET_KEY before serving production requests.".into());
    }
    let season = match env::var("BLACK_ROD_CURRENT_SEASON") {
        Ok(season) => season
            .parse::<i32>()
            .map_err(|_| "Set BLACK_ROD_CURRENT_SEASON to the tournament season's start year.")?,
        Err(_) if production => {
            return Err(
                "Set BLACK_ROD_CURRENT_SEASON before starting the production runtime.".into(),
            );
        }
        Err(_) => 2026,
    };
    let enabled = |name| {
        env::var(name)
            .is_ok_and(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
    };
    let address: SocketAddr = env::var("BLACK_ROD_BIND")
        .or_else(|_| env::var("PORT").map(|port| format!("0.0.0.0:{port}")))
        .unwrap_or_else(|_| "127.0.0.1:9090".into())
        .parse()?;
    let proxy = env::var("BLACK_ROD_TRUSTED_PROXY_IP")
        .ok()
        .map(|proxy| proxy.parse())
        .transpose()?;
    if production_web
        && address.ip().is_loopback()
        && proxy.is_none()
        && !enabled("BLACK_ROD_DIRECT_CONNECTIONS")
    {
        return Err("Set BLACK_ROD_TRUSTED_PROXY_IP to the immediate reverse proxy's address, or set BLACK_ROD_DIRECT_CONNECTIONS=1 for direct requests.".into());
    }
    let database = database_url(production)?;
    let pool = AnyPoolOptions::new()
        .max_connections(8)
        .after_connect(|connection, _| {
            Box::pin(async move {
                if connection.backend_name() == "PostgreSQL" {
                    sqlx::query("SET TIME ZONE 'UTC'")
                        .execute(connection)
                        .await?;
                }
                Ok(())
            })
        })
        .connect(&database)
        .await?;
    let cache_lifetime = env::var("BLACK_ROD_CACHE_TTL_SECONDS")
        .map(|value| value.parse())
        .unwrap_or(Ok(300))?;
    let mut state =
        AppState::with_cache_lifetime(pool, season, Duration::from_secs(cache_lifetime));
    if let Some(root) = &root {
        state = state.with_release_root(root);
    }
    state = state.with_debater_profile_features(
        enabled("ENABLE_DEBATER_PROFILE_TAB_CARDS"),
        enabled("ENABLE_DEBATER_PARTNER_PIE_CHART"),
    );
    state = state.with_tournament_tab_card_search(enabled("ENABLE_TOURNAMENT_TAB_CARD_SEARCH"));
    if let Some(origin) = env::var("BLACK_ROD_PUBLIC_ORIGIN")
        .ok()
        .or_else(|| production.then(|| "https://results.apda.online".to_owned()))
    {
        state = state.with_public_origin(&origin)?;
    }
    if let Some(hosts) = env::var("BLACK_ROD_ALLOWED_HOSTS")
        .ok()
        .or_else(|| production.then(|| "50.116.54.146,results.apda.online".to_owned()))
    {
        state = state.with_allowed_hosts(&hosts);
    }
    if let Some(proxy) = proxy {
        state = state.with_trusted_proxy(proxy);
    }
    if let Some(secret) = secret {
        state = state.with_session_secret(secret);
    }
    let stats = env::var_os("WEBPACK_STATS_FILE")
        .map(PathBuf::from)
        .or_else(|| root.as_ref().map(|root| root.join("webpack-stats.json")));
    if production_web {
        let stats =
            stats.ok_or("Set BLACK_ROD_RELEASE_ROOT or WEBPACK_STATS_FILE for this release.")?;
        state = state.with_production_webpack_stats(&stats)?;
    } else if let Some(stats) = stats {
        if enabled("BLACK_ROD_WATCH_WEBPACK") {
            state = state.watch_webpack_stats(&stats);
        } else if stats.is_file() {
            state = state.with_webpack_stats(&stats)?;
        }
    }
    state = state.with_private_tokens(PrivateTokens::new(
        &env::var("FORUM_PROFILE_API_TOKEN").unwrap_or_default(),
        &env::var("MITTAB_PRIVATE_API_TOKENS").unwrap_or_default(),
    ));
    if let Some(address) = env::var("BLACK_ROD_MEMCACHED_ADDR")
        .ok()
        .or_else(|| production.then(|| "127.0.0.1:11211".to_owned()))
    {
        state = state.with_memcached(address.parse()?);
    }
    let client = env::var("APDAONLINE_CLIENT_ID").unwrap_or_else(|_| "default_client_id".into());
    let oauth_secret = env::var("APDAONLINE_SECRET_ID").unwrap_or_else(|_| "default_secret".into());
    state = state.with_oauth(OAuth::new(
        client,
        oauth_secret,
        env::var("BLACK_ROD_OAUTH_BASE").ok(),
    )?);
    let directory = env::var("LIVE_MITTAB_DIRECTORY_URL")
        .unwrap_or_else(|_| "https://nu-tab.com/api/tournaments".into());
    let sources = env::var("LIVE_MITTAB_SOURCES")
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect();
    state = state.with_live(Live::new(
        (!directory.is_empty()).then_some(directory),
        sources,
    )?);
    if command != "serve" {
        return state.run_command(&command, &args).await;
    }
    if !args.is_empty() {
        return Err("Usage: black-rod serve".into());
    }
    let listener = TcpListener::bind(address).await?;
    eprintln!("Black Rod listening on {}", listener.local_addr()?);
    axum::serve(
        listener,
        app(state).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown())
    .await?;
    Ok(())
}

async fn shutdown() {
    #[cfg(unix)]
    if let Ok(mut terminate) =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
    {
        tokio::select! {
            _ = terminate.recv() => (),
            _ = tokio::signal::ctrl_c() => (),
        }
        return;
    }
    let _ = tokio::signal::ctrl_c().await;
}

fn database_url(production: bool) -> Result<String, Box<dyn std::error::Error>> {
    if let Ok(url) = env::var("BLACK_ROD_DATABASE_URL") {
        return Ok(url);
    }
    if !production {
        return Err("Set BLACK_ROD_DATABASE_URL to an existing Black Rod database.".into());
    }
    let mut url = url::Url::parse("postgres://localhost:5433/standings")?;
    if let Ok(username) = env::var("DATABASE_USERNAME") {
        url.set_username(&username)
            .map_err(|_| "Invalid database username")?;
    }
    if let Ok(password) = env::var("DATABASE_PASSWORD") {
        url.set_password(Some(&password))
            .map_err(|_| "Invalid database password")?;
    }
    Ok(url.into())
}
