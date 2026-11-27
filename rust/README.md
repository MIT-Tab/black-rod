# Black Rod Rust port

Run the Rust service from the repository root against an existing Black Rod database. For local development, `dev-up` creates and loads the isolated SQLite fixture. The Rust server uses the existing table names and reads the schema directly. It has no startup migration.

```sh
scripts=.agents/skills/black-rod-task/scripts
"$scripts/dev-up"
cargo build --release --locked --manifest-path rust/Cargo.toml
"$scripts/br" bash -c '
  export BLACK_ROD_DATABASE_URL="sqlite://$BR_STATE/dev.sqlite3?mode=ro"
  exec rust/target/release/black-rod
'
```

`BLACK_ROD_DATABASE_URL` accepts SQLite and PostgreSQL URLs. The default listener is `127.0.0.1:9090`. Set `BLACK_ROD_PUBLIC_ORIGIN` to the HTTPS origin when running behind a proxy. Browser pages use the existing webpack bundles and assets. Set the existing `SECRET_KEY` in both services to test valid Django database sessions.

The current server covers standings, replay, live results from active sources, the schedule, statistics, results directories, debater search, alumni outreach, public profile APIs, the private forum and MIT-Tab lookups, and API discovery. The browser port includes tournament, school, team, and debater profiles, career tab card exports, the recent-results widget, video and motion libraries, the resource directory, Immaculate Grid, profile claims and self-service editing, and the scheduling workspace. It reads Django sessions and creates compatible sessions for password login and APDAOnline OAuth.

The statistics cache lasts one hour. Set `BLACK_ROD_MEMCACHED_ADDR` for shared directory and schedule caching across instances. Those entries use a separate key namespace and retain the five-minute lifetime. The debater search index uses a bounded in-process cache and refreshes from the shared database, with immediate invalidation after a native profile edit.

The Rust runtime is a partial port. Remaining work includes the live import snapshot fallback, unported authoring and bulk import flows, ELO administration, tournament data entry and imports, Django admin equivalents, remaining account flows, management commands, and the browsable API. Django is the application entry point.

## Parity tests

```sh
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --locked
"$scripts/br" venv/bin/python rust/tools/compare.py
```

The HTTP harness starts Django and Rust against the same fixture. It compares status, relevant headers, JSON fields, array order, and plain text. Equal-score COTY ties, equal-rank TOTY ties, school members with equal tenure, video lists, and tags are compared without requiring an order where the reference query has no final sort. The SQLite fixture is hashed before and after the run. Set `BLACK_ROD_PARITY_POSTGRES_URL` to a locally seeded PostgreSQL database for the PostgreSQL run. The Rust parity workflow loads the tracked fixture into both database engines.

The Chrome harness compares the standings, directories, search, profiles, recent-results widget, live results, media permissions, motion progress and authoring, career CSV exports, profile claims, outreach, scheduling, and the daily and infinite Immaculate Grid flows. It also checks password and OAuth session compatibility, theme persistence, and desktop and mobile replay screenshots. The harness makes writes only against a temporary copy of the SQLite fixture.

```sh
venv/screenshots/bin/python -m pip install Pillow==12.3.0
"$scripts/br" venv/screenshots/bin/python rust/tools/browser.py /tmp/black-rod-replay
```

## PostgreSQL benchmark

Run the same HTTP comparison and benchmarks together. The harness checks that each measured endpoint returns equivalent data before timing it.

```sh
BLACK_ROD_PARITY_POSTGRES_URL=postgres://black_rod@127.0.0.1:5432/black_rod_parity \
  "$scripts/br" venv/bin/python rust/tools/compare.py --benchmark
```

This run used PostgreSQL 17.12 and the 60,693-object checked-in fixture on an arm64 Mac with 10 CPU cores and 32 GiB RAM. Django ran with one Gunicorn worker and eight threads. Rust ran in release mode with eight Tokio worker threads. Warm tests sent 1,200 requests with 12 concurrent clients and both application caches enabled. Cache-disabled tests sent 30 requests with one client and both application caches bypassed. The database stayed running across cases. The RSS figures are the sampled sum of the application process tree and exclude the database and load client. Peak RSS can include shared pages in both Django processes.

| Endpoint | Warm p95 ms Django → Rust | Warm req/s Django → Rust | Warm peak RSS MiB Django → Rust | Cache-disabled p95 ms Django → Rust | Cache-disabled peak RSS MiB Django → Rust |
| --- | ---: | ---: | ---: | ---: | ---: |
| Active schools | 8.38 → 1.93 | 1,844 → 13,105 | 122.25 → 10.86 | 10.05 → 4.35 | 120.55 → 10.47 |
| All schools | 15.17 → 10.79 | 1,102 → 3,763 | 132.61 → 11.12 | 51.08 → 13.72 | 127.20 → 11.52 |
| Harvard roster | 25.87 → 3.55 | 677 → 7,174 | 158.12 → 11.53 | 51.48 → 2.72 | 131.88 → 13.62 |
| 2025–26 schedule | 20.51 → 3.49 | 831 → 6,724 | 160.84 → 11.98 | 17.92 → 0.85 | 132.83 → 14.09 |
| 2025–26 standings | 106.65 → 3.43 | 220 → 6,177 | 181.45 → 17.73 | 1,008.83 → 143.63 | 139.14 → 32.09 |
| 2025–26 replay | 185.16 → 21.95 | 115 → 869 | 215.34 → 54.44 | 1,608.70 → 48.46 | 152.12 → 47.73 |

The warm benchmark uses Django’s local-memory cache and Rust’s bounded five-minute cache. PostgreSQL data volume, traffic, production Memcached, and worker configuration can change these results. The measurements apply to the endpoints in the table.
