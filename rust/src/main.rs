use std::{env, net::SocketAddr, path::Path, time::Duration};

use black_rod::{AppState, Live, OAuth, PrivateTokens, app};
use sqlx::any::{AnyPoolOptions, install_default_drivers};
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    install_default_drivers();
    let database = env::var("BLACK_ROD_DATABASE_URL")?;
    let pool = AnyPoolOptions::new()
        .max_connections(8)
        .connect(&database)
        .await?;
    let season = env::var("BLACK_ROD_CURRENT_SEASON").unwrap_or_else(|_| "2026".into());
    let enabled = |name| {
        env::var(name)
            .is_ok_and(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
    };
    let address: SocketAddr = env::var("BLACK_ROD_BIND")
        .unwrap_or_else(|_| "127.0.0.1:9090".into())
        .parse()?;
    let cache_lifetime = env::var("BLACK_ROD_CACHE_TTL_SECONDS")
        .map(|value| value.parse())
        .unwrap_or(Ok(300))?;
    let mut state =
        AppState::with_cache_lifetime(pool, season.parse()?, Duration::from_secs(cache_lifetime));
    state = state.with_debater_profile_features(
        enabled("ENABLE_DEBATER_PROFILE_TAB_CARDS"),
        enabled("ENABLE_DEBATER_PARTNER_PIE_CHART"),
    );
    if let Ok(origin) = env::var("BLACK_ROD_PUBLIC_ORIGIN") {
        state = state.with_public_origin(&origin)?;
    }
    if let Ok(secret) = env::var("SECRET_KEY") {
        state = state.with_session_secret(secret);
    }
    let stats = env::var("WEBPACK_STATS_FILE").unwrap_or_else(|_| "webpack-stats.json".into());
    if Path::new(&stats).is_file() {
        state = state.with_webpack_stats(Path::new(&stats))?;
    }
    state = state.with_private_tokens(PrivateTokens::new(
        &env::var("FORUM_PROFILE_API_TOKEN").unwrap_or_default(),
        &env::var("MITTAB_PRIVATE_API_TOKENS").unwrap_or_default(),
    ));
    if let Ok(address) = env::var("BLACK_ROD_MEMCACHED_ADDR") {
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
    if env::args().nth(1).as_deref() == Some("refresh_import_sources") {
        println!(
            "Refreshed {} tournament snapshots.",
            state.refresh_tournament_sources().await
        );
        return Ok(());
    }
    let listener = TcpListener::bind(address).await?;
    eprintln!("Black Rod listening on {}", listener.local_addr()?);
    axum::serve(
        listener,
        app(state).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(async {
        let _ = tokio::signal::ctrl_c().await;
    })
    .await?;
    Ok(())
}
