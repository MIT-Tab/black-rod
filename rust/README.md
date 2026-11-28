# Black Rod Rust runtime

Run the Rust service from the repository root against an existing Black Rod database. For local development, `dev-up` creates and loads the isolated SQLite fixture and starts Rust with webpack watching the frontend assets. Restart the dev server after changing Rust code or server-rendered templates. The Rust server uses the existing table names and reads the schema directly. It has no startup migration.

```sh
scripts=.agents/skills/black-rod-task/scripts
"$scripts/dev-up"
"$scripts/dev-down"
"$scripts/dev-up" --django  # Start the reference server for manual comparison.
```

`BLACK_ROD_DATABASE_URL` accepts SQLite and PostgreSQL URLs. The default listener is `127.0.0.1:9090`. Set `BLACK_ROD_BIND` to override it or set `PORT` to bind on all interfaces. The service finds its release directory from the binary path and reads the default `.env` and assets there. Set `BLACK_ROD_RELEASE_ROOT` when installing the binary outside that directory. Use `DOT_ENV_FILE` to select a different configuration file. Shell variables take precedence. With `ENV=production` and no explicit database URL, it uses the existing Linode PostgreSQL database on `localhost:5433` with `DATABASE_USERNAME` and `DATABASE_PASSWORD` and the existing Memcached address on `127.0.0.1:11211`. Production defaults to the existing HTTPS origin and allowed hosts. Use `BLACK_ROD_PUBLIC_ORIGIN` and `BLACK_ROD_ALLOWED_HOSTS` to override them. Production uses `SENTRY_DSN` for panic and server-error reporting, with route patterns and status codes in HTTP reports. Browser pages use the existing webpack bundles and assets. Rust's vendored framework assets live in `rust/static/vendor`, alongside the shared frontend assets served under `/static/`. Set `ENV=development` to use the Django development signing key locally. Set the same `SECRET_KEY` in both services to test another key. Summernote defaults to the release directory for attachments. Set `BLACK_ROD_MEDIA_ROOT` to the existing file storage path when the service uses a separate media directory.

The current server covers standings, legacy standings directories, replay, live results and retained tournament snapshots, the schedule, statistics, results directories, debater search, alumni outreach, public profile APIs, the private forum and MIT-Tab lookups, and API discovery. The browser port includes tournament, school, team, and debater profiles, career tab card exports, the recent-results widget, video and motion libraries, resource authoring, motion spreadsheet imports, account code generation, Immaculate Grid, profile claims and self-service editing, the scheduling workspace, DOTY preview, partnership network and statistics, video authoring and image attachments, and school, debater, team, and tournament editing. It reads Django sessions and creates compatible sessions for password login and APDAOnline OAuth.

The statistics cache lasts one hour. Set `BLACK_ROD_MEMCACHED_ADDR` for shared directory and schedule caching across instances and to read existing MIT-Tab snapshots. Those entries use a separate key namespace and retain the five-minute lifetime. Debater search uses an in-process snapshot keyed by the generation in Memcached. Profile edits and reindex commands advance the generation so other workers reload the index. A worker reloads the snapshot after at most five minutes if a generation update fails. Without Memcached, Django and Rust use a database-specific generation file in the release's `tmp/` directory. Set `BLACK_ROD_SEARCH_GENERATION_FILE` to the same writable path in both runtimes when they use separate release directories. Partnership statistics use the existing 24-hour fragments, generation marker, and two-minute lock in shared Memcached. With no Memcached connection, Rust uses a bounded local equivalent.

Rust also serves staff speaker-score statistics, manual tournament data entry, legacy bundle uploads, reviewed MIT-Tab imports, tournament audits and round amendments, and the ELO dashboard. Reviewed imports and ELO dashboard results are readable by both servers through shared Memcached, including after a server switch. ELO retains the six-hour result cache and two bounded ten-minute runtime caches.

The Rust server also includes rankings recomputation, the browsable API renderer, signup and email maintenance, password reauthentication, OAuth connection management and pending signup, the Summernote iframe editor, and native Django-style model administration. Admin exchange supports CSV, TSV, JSON, YAML, HTML, XLS, XLSX, and ODS. Bulk import requires add and change permissions for both the admin model and the resource model. Export requires view or change permissions for both. The Rust correction API uses the same credential and change-set tables for authorized reads, atomic writes, previews, merges, and undo. The development launcher starts Rust. The Django implementation remains available as the parity reference and for provisioning new local fixture databases.

Run `black-rod --help` to list the native maintenance commands. Use `black-rod rerank_current_awards` for the old standalone `bin/redo_rankings.py` utility and `black-rod redo_rankings` for the distinct Django management command. `black-rod update_otys` refreshes current-season awards. `black-rod generate_speaker_position_amendments` accepts the existing raw backup directory and reads both MIT-Tab JSON fixtures and historical MySQL dump files. The fixture exporter replaces credentials, registration emails, and access codes with sample values and redacts the contents of correction audit records. `black-rod cache_mittab_results` uses the same retained snapshot data and can run on the existing 12-hour schedule. `black-rod export_generated_codes --output PATH` writes the existing CSV format.

## Deployment and rollback

Build the release binary with Rust 1.94 and build the existing frontend assets with `npm ci` and `npm run build`. Keep the webpack manifest, bundles, and static files with that release. Start `rust/target/release/black-rod serve` from the release root with `ENV=production` and the existing environment. Production web startup requires a nonempty `SECRET_KEY` and a webpack manifest with main JavaScript assets. Set `BLACK_ROD_CURRENT_SEASON` to the start year of the active season (for example, `2026` for 2026–27) in the shared environment file for the web service and maintenance commands. Django reads the same setting for rollback and staged comparisons. Update it at each season rollover. Use the same PostgreSQL database, application secret, Memcached, OAuth, and file-storage configuration. A staged service can bind to an unused loopback port while the current service handles traffic.

Verify the staged service through the configured HTTPS origin before redirecting traffic. Check an existing account session, public standings, admin, uploaded media, and a representative existing import review. For a proxied deployment, verify that the immediate reverse proxy appends each client's address to `X-Forwarded-For` and set `BLACK_ROD_TRUSTED_PROXY_IP` to that proxy's exact address before starting the Rust server. A loopback production listener requires this setting. For an intentional direct loopback listener without a proxy, set `BLACK_ROD_DIRECT_CONNECTIONS=1`. Use the canonical HTTPS origin for requests with session cookies.

Select the snapshot refresh runtime by putting `rust` in the root-managed file `/etc/default/black-rod-mittab-runtime` at cutover. The tracked cron keeps the existing 12-hour interval and reads the selection after every deployment. The existing Linode deploy workflow invokes the host-managed deploy script. Verify that the host builds and installs the release binary after each update before routing requests or the Rust cron job to that release.

For rollback, select `django` and restore the previous service and release assets. Django can reuse the existing database and files. Rebuild Django's Whoosh search index from the previous release root with `venv/bin/python manage.py rebuild_index --noinput`, using Django's production environment. Do this after Rust stops accepting writes, then verify that search finds a recently changed debater. Each runtime maintains its own search index. The Rust binary's `reindex_haystack` command refreshes Rust search.

## Runtime boundaries

Cargo forbids unsafe code in this package. Axum and Tower own routing and HTTP transport, SQLx owns database connections and transactions, and reqwest uses Rustls for outbound TLS. The cookie crate serializes response cookies. The oauth2 client exchanges authorization codes with APDAOnline, while the provider adapter preserves allauth's callback, token envelope, and account-linking data.

The `adotenv` Python dialect reads the existing `.env` file. The startup loader uses Django's `${NAME}` interpolation and preserves literal dollar signs and backslashes in unquoted credentials. Process variables take precedence over file values.

The SQLx pool defaults to eight connections and a 30-second checkout timeout. Set `BLACK_ROD_DATABASE_POOL_SIZE` and `BLACK_ROD_DATABASE_ACQUIRE_TIMEOUT_SECONDS` to change those values.

The session module loads existing Django sessions and applies permissions. Its signing adapter uses Ring for HMAC, flate2 for Django's compressed payloads, and Serde for decoding. The password verifier uses Ring, Argon2, bcrypt, and scrypt for the formats stored by Django. Successful verification upgrades legacy password hashes to Django-compatible PBKDF2. Route registration, request middleware, and Django template translation live in separate modules.

## Parity tests

```sh
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --locked
"$scripts/br" venv/bin/python rust/tools/compare.py
"$scripts/br" venv/bin/python rust/tools/agent_write_parity.py
"$scripts/br" venv/bin/python rust/tools/command_parity.py
```

The HTTP harness starts Django and Rust against the same fixture. It compares status, relevant headers, JSON fields, array order, and plain text. The harness compares video lists and tags without requiring an order where the reference query has no final sort. The SQLite fixture is hashed before and after the run. Set `BLACK_ROD_PARITY_POSTGRES_URL` to a locally seeded PostgreSQL database for the PostgreSQL run. The Rust parity workflow loads the tracked fixture into both database engines.

The Agent harness tests both runtimes against the same local database. It covers atomic and reversible correction batches, stale revisions, preview hashes, concurrent idempotent requests, cascading deletes, and identity merges. It copies SQLite to a temporary file and clones the supplied loopback PostgreSQL fixture before testing writes. The PostgreSQL fixture name must start with `black_rod_parity`.

The Chrome harness compares the standings, directories, search, profiles, recent-results widget, live results, media permissions, motion progress, authoring, and spreadsheet imports, career CSV exports, profile claims, outreach, scheduling, legacy standings, DOTY, resource authoring, account code generation, and the daily and infinite Immaculate Grid flows. It also checks password and OAuth session compatibility, theme persistence, desktop and mobile replay screenshots, manual and imported results, audit write rollback, and ELO computation, filters, sorting, and pagination. With local Memcached enabled, it verifies that both servers can open each other's import reviews and cached ELO results and can invalidate the shared ELO cache. The harness makes writes only against a temporary copy of the SQLite fixture.

```sh
venv/screenshots/bin/python -m pip install Pillow==12.3.0
"$scripts/br" venv/screenshots/bin/python rust/tools/browser.py /tmp/black-rod-replay
"$scripts/br" venv/screenshots/bin/python rust/tools/html_parity.py /tmp/black-rod-html --group public
"$scripts/br" venv/screenshots/bin/python rust/tools/html_parity.py /tmp/black-rod-admin --group admin-models
```

The HTML harness compares the normalized live DOM, including controls and client-side widgets, and checks screenshots at desktop and mobile widths. The workflow runs the public, staff, and full admin model groups. A fresh Chrome session checks any initial difference, and the harness retains the first artifacts. Screenshots use an RGB channel tolerance of 8 and allow up to two differing pixels per capture. The normalized DOM must match. For Create Video screenshots, the harness checks the original sidebar geometry and disables sticky painting only when every affected element keeps its original bounds. CI retains screenshots from failed comparisons.

## PostgreSQL benchmark

Run the same HTTP comparison and benchmarks together. The harness checks that each measured endpoint returns equivalent data before timing it.

```sh
BLACK_ROD_PARITY_POSTGRES_URL=postgres://black_rod@127.0.0.1:5432/black_rod_parity \
  "$scripts/br" venv/bin/python rust/tools/compare.py --benchmark
```

The measurements below were captured before batching team rosters and tournament data on the Rust debater profile. This run used PostgreSQL 17.12 and the 60,693-object checked-in fixture on an arm64 Mac with 10 CPU cores and 32 GiB RAM. Django ran with one Gunicorn worker and eight threads. Rust ran in release mode with eight Tokio worker threads. Warm tests sent 1,200 requests with 12 concurrent clients and both application caches enabled. Cache-disabled tests sent 30 requests with one client and both application caches bypassed. The database stayed running across cases. The RSS figures are the sampled sum of the application process tree and exclude the database and load client. Peak RSS can include shared pages in both Django processes.

A PostgreSQL results-save benchmark rebuilt a synthetic current season with 620 participants. Median save time over five measured requests was 305.59 ms in Django, 208.45 ms in Rust before batching, and 135.44 ms in Rust with batching. All three produced equivalent qualification, COTY, and participation records. Run `"$scripts/br" venv/bin/python rust/tools/qualification_rebuild_parity.py --samples 5` with a disposable local PostgreSQL fixture to reproduce it. Supply an earlier executable with `--prior-rust` to include that version.

The same harness checks replacement of 240 existing team results. One local run measured 2.15 seconds for Django and 0.232 seconds for Rust. Both kept the retained result's hidden-profile setting and removed the other results and their visibility links.

The tournament directory benchmark checks the rendered table before timing 260 tournaments per request. It uses the same server harness with application caches disabled. One local PostgreSQL 16.16 run produced the following results. RSS is the sampled peak for the application process tree.

| Clients | Django p50 / p95 | Rust p50 / p95 | Django → Rust RSS |
| ---: | ---: | ---: | ---: |
| 1 | 227.44 / 328.10 ms | 22.40 / 25.11 ms | 156.67 → 54.00 MiB |
| 8 | 1,626.95 / 3,070.75 ms | 27.55 / 34.63 ms | 196.17 → 69.25 MiB |

Run `"$scripts/br" venv/bin/python rust/tools/tournament_directory_benchmark.py` with the disposable PostgreSQL fixture and environment above. The earlier Rust build measured 35.88 ms median at one client and 67.63 ms p95 at eight clients. Its roster order differed from Django for some tournaments. The batched build matched all 260 rows on SQLite and PostgreSQL.

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

A separate follow-up run after batching debater profile reads used the same database fixture and host. It sent 120 warm requests with 8 concurrent clients and 12 cache-disabled requests with 1 client. The harness verified both endpoints against Django before sending load.

| Endpoint | Mode | p95 ms Django → Rust | Req/s Django → Rust | Peak RSS MiB Django → Rust |
| --- | --- | ---: | ---: | ---: |
| Debater detail (5270) | Warm | 1,818.68 → 33.30 | 4.83 → 438.61 | 136.95 → 32.56 |
| Debater detail (5270) | Cache disabled | 300.46 → 21.37 | 3.58 → 86.93 | 131.02 → 17.12 |
| Partnership network (since 2025) | Warm | 2,690.38 → 53.84 | 3.78 → 204.22 | 374.30 → 111.11 |
| Partnership network (since 2025) | Cache disabled | 277.21 → 16.86 | 3.77 → 65.06 | 164.14 → 30.20 |

A later release build was measured with the same PostgreSQL version, client counts, and request counts. The database included a small number of additional disposable test records. Other local development processes were running, so compare Django and Rust within each run. The harness verified all 249 HTTP cases and the four benchmark response pairs before load.

| Endpoint | Warm p95 ms Django → Rust | Warm req/s Django → Rust | Warm peak RSS MiB Django → Rust | Cache-disabled p95 ms Django → Rust | Cache-disabled peak RSS MiB Django → Rust |
| --- | ---: | ---: | ---: | ---: | ---: |
| Active schools | 12.47 → 2.79 | 985 → 6,365 | 121.25 → 18.19 | 10.52 → 1.75 | 120.28 → 18.30 |
| 2025–26 replay | 153.61 → 3.72 | 97 → 3,013 | 186.75 → 31.45 | 311.69 → 30.94 | 149.83 → 57.61 |
| Debater detail 5270 | 4,130.41 → 54.76 | 2.53 → 254.06 | 138.06 → 36.61 | 355.22 → 16.77 | 131.02 → 20.81 |
| Partnership network since 2025 | 7,192.45 → 104.01 | 1.54 → 106.82 | 384.16 → 106.23 | 369.74 → 21.63 | 165.91 → 34.34 |


The latest four-endpoint release run used the same local PostgreSQL fixture, 120 warm requests at concurrency 8, and 12 cache-disabled requests at concurrency 1. It verified all 258 HTTP cases and the benchmark response pairs before load.

| Endpoint | Warm p95 ms Django → Rust | Warm req/s Django → Rust | Warm peak RSS MiB Django → Rust | Cache-disabled p95 ms Django → Rust | Cache-disabled peak RSS MiB Django → Rust |
| --- | ---: | ---: | ---: | ---: | ---: |
| Active schools | 4.90 → 1.04 | 2,198 → 15,556 | 120.69 → 20.69 | 8.44 → 1.50 | 119.67 → 20.42 |
| 2025–26 replay | 60.79 → 2.21 | 221 → 6,116 | 185.39 → 34.89 | 189.60 → 23.52 | 147.70 → 61.00 |
| Debater detail 5270 | 2,169.71 → 55.17 | 4.34 → 288.81 | 139.22 → 37.23 | 317.99 → 14.70 | 130.91 → 23.22 |
| Partnership network since 2025 | 5,084.50 → 71.58 | 2.45 → 151.25 | 391.64 → 108.03 | 739.58 → 27.06 | 168.56 → 37.19 |

A release run with local PostgreSQL and shared Memcached verified 302 HTTP cases. Each warm endpoint received 1,200 requests from 12 concurrent clients. RSS is the sampled peak sum for the application process tree.

| Endpoint | Warm p95 ms Django → Rust | Warm req/s Django → Rust | Warm peak RSS MiB Django → Rust |
| --- | ---: | ---: | ---: |
| 2025–26 standings | 75.21 → 3.45 | 287 → 5,847 | 167.62 → 28.72 |
| 2025–26 replay | 107.75 → 3.89 | 159 → 4,793 | 174.84 → 40.34 |
| All schools | 24.28 → 3.12 | 853 → 7,607 | 140.00 → 21.50 |

Set `BLACK_ROD_PARITY_MEMCACHED_ADDR` to a disposable local Memcached instance and run the HTTP benchmark with `--benchmark-path` for each endpoint. The cache-disabled cases bypass Memcached regardless of that setting.
