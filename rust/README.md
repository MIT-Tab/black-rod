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

The current server covers standings, legacy standings directories, replay, live results and retained tournament snapshots, the schedule, statistics, results directories, debater search, alumni outreach, public profile APIs, the private forum and MIT-Tab lookups, and API discovery. The browser port includes tournament, school, team, and debater profiles, career tab card exports, the recent-results widget, video and motion libraries, resource authoring, motion spreadsheet imports, account code generation, Immaculate Grid, profile claims and self-service editing, the scheduling workspace, DOTY preview, and the school short-name audit. It reads Django sessions and creates compatible sessions for password login and APDAOnline OAuth.

The statistics cache lasts one hour. Set `BLACK_ROD_MEMCACHED_ADDR` for shared directory and schedule caching across instances and to read existing MIT-Tab snapshots. Those entries use a separate key namespace and retain the five-minute lifetime. The debater search index uses a bounded in-process cache and refreshes from the shared database, with immediate invalidation after a native profile edit.

The Rust runtime is a partial port. Remaining work includes the partnership network and statistics, video authoring, ELO administration, tournament data entry and imports, Django admin equivalents, remaining account flows, management commands, the agent API, and the browsable API. Django is the application entry point.

## Parity tests

```sh
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --locked
"$scripts/br" venv/bin/python rust/tools/compare.py
```

The HTTP harness starts Django and Rust against the same fixture. It compares status, relevant headers, JSON fields, array order, and plain text. Equal-score COTY ties, equal-rank TOTY ties, school members with equal tenure, video lists, and tags are compared without requiring an order where the reference query has no final sort. The SQLite fixture is hashed before and after the run. Set `BLACK_ROD_PARITY_POSTGRES_URL` to a locally seeded PostgreSQL database for the PostgreSQL run. The Rust parity workflow loads the tracked fixture into both database engines.

The Chrome harness compares the standings, directories, search, profiles, recent-results widget, live results, media permissions, motion progress, authoring, and spreadsheet imports, career CSV exports, profile claims, outreach, scheduling, legacy standings, DOTY, resource authoring, account code generation, and the daily and infinite Immaculate Grid flows. It also checks password and OAuth session compatibility, theme persistence, and desktop and mobile replay screenshots. The harness makes writes only against a temporary copy of the SQLite fixture.

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

The measurements below were captured before batching team rosters and tournament data on the Rust debater profile. This run used PostgreSQL 17.12 and the 60,693-object checked-in fixture on an arm64 Mac with 10 CPU cores and 32 GiB RAM. Django ran with one Gunicorn worker and eight threads. Rust ran in release mode with eight Tokio worker threads. Warm tests sent 1,200 requests with 12 concurrent clients and both application caches enabled. Cache-disabled tests sent 30 requests with one client and both application caches bypassed. The database stayed running across cases. The RSS figures are the sampled sum of the application process tree and exclude the database and load client. Peak RSS can include shared pages in both Django processes.

| Endpoint | Warm p95 ms Django → Rust | Warm req/s Django → Rust | Warm peak RSS MiB Django → Rust | Cache-disabled p95 ms Django → Rust | Cache-disabled peak RSS MiB Django → Rust |
| --- | ---: | ---: | ---: | ---: | ---: |
| Active schools | 21.70 → 5.12 | 872 → 5,380 | 122.08 → 15.23 | 151.22 → 35.09 | 120.19 → 13.48 |
| All schools | 29.92 → 5.16 | 634 → 5,210 | 131.56 → 15.36 | 163.48 → 37.07 | 125.34 → 13.98 |
| Harvard roster | 41.31 → 4.02 | 464 → 6,030 | 143.97 → 15.64 | 112.78 → 15.29 | 127.58 → 15.56 |
| 2025–26 schedule | 35.13 → 6.10 | 552 → 4,696 | 133.86 → 15.80 | 74.25 → 16.11 | 126.39 → 14.89 |
| 2025–26 standings | 132.53 → 5.32 | 169 → 4,050 | 162.95 → 20.62 | 3,225.52 → 52.20 | 135.03 → 30.22 |
| 2025–26 replay | 217.14 → 8.05 | 90 → 2,679 | 193.34 → 29.52 | 858.84 → 63.18 | 148.28 → 41.83 |
| Team detail 6719 | 815.67 → 31.00 | 18 → 848 | 127.62 → 17.64 | 170.68 → 10.93 | 124.98 → 13.50 |
| Tournament detail 1203 | 1,759.63 → 74.48 | 8 → 285 | 129.02 → 19.12 | 346.57 → 102.88 | 128.41 → 13.84 |
| Debater detail 5270 | 12,557.30 → 839.64 | 1 → 26 | 139.34 → 28.12 | 2,942.12 → 99.65 | 131.84 → 15.95 |

The warm benchmark enables Django’s local-memory cache and Rust’s endpoint-specific caches where implemented. The profile endpoints run their normal database queries. This was one local run while other development processes were active. PostgreSQL data volume, traffic, production Memcached, and worker configuration can change these results. The measurements apply to the endpoints in the table.
