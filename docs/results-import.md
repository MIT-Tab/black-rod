# Results imports from ephemeral mit-tab sites

Deploy the sister mit-tab change exposing `/api/import-results` before this change.
The lightweight `/api/live-results` endpoint remains bundle-free. Existing small
API endpoints remain available for older consumers.

Site superusers can use **Import results** in navigation or the live-results
notice. The page lists directory sources and retained snapshots. Cached-only
names are omitted from the public notice. Reviewing a source tries a fresh full
API snapshot first, then falls back to the retained snapshot on a network or
schema error. Embargoed snapshots cannot be imported. Host, season, tournament
type, and dates within seven days select the tournament automatically; a missing
object is created. Ambiguous matches require correcting the tournament records.

All result categories, participant counts, and the round bundle are imported
together. The deprecated manual bundle upload remains available on tournament
detail pages for older tournaments with downloaded bundles. The review uses an immutable, user/tournament-scoped snapshot for one
hour. Save does not contact mit-tab. Result and bundle changes share a database
transaction; failed bundle validation rolls everything back. Reviewed debater
and school links are carried into the bundle. Repeat identities within a bundle
are resolved once, and search indexing writes the affected debaters as one batch.

`manage.py cache_mittab_results` refreshes every current source and retries known
sources. The master deployment installs `deploy/mittab-results-cache.cron` in
`/etc/cron.d/black-rod-mittab-cache`, running at 00:00 and 12:00 server time with
`flock` preventing overlap. It uses the application's production `.env` and shared
memcached instance. The cron file uses the same `/home/rodda/black-rod` deployment
path as the existing workflow. Logs go to `/var/log/black-rod-mittab-cache.log`.

Successful refreshes replace snapshots. The first successful directory refresh
that no longer lists a site, together with a failed source fetch, starts its
seven-day retention period. Subsequent failures do not extend that deadline.
Directory outages and endpoint errors for sites still listed do not start it.
Live standings can continue using retained snapshots, without advertising cached
names in the public import notice.

Snapshots live only in shared cache memory, compressed and chunked below
memcached's per-item limit. Cache eviction or a memcached restart can remove them;
this is intentionally not a durable archive. Development's default LocMemCache
is per process, so scheduled commands and web workers must use shared memcached
when validating cross-process retention.

Saving results bulk-updates tied ranking placements, computes qualification
state once per debater across all partnerships, and bulk-writes season qual/COTY
state. It no longer renders the home page during ranking updates or runs a global
temporary-entity cleanup that could disrupt another administrator's review.
