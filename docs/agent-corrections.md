# Agent round corrections API

This API is intended for an agent cleaning up imported rounds and Elo inputs.
Use `/api/agent/v1/` over HTTPS with `Authorization: Bearer br_agent_...`.
Browser sessions, existing mit-tab tokens, and ordinary user accounts do not grant
access. The public API and LLM proxy do not bypass this authentication.

## Administrator setup

Deploy the code and run `python manage.py migrate`. In Django admin, open
**Agent credentials**, add a named credential, set an expiration, and select
**Can write** only for agents authorized to edit. The token is shown once. Only
its SHA-256 digest is stored. Read-only is the default. Only active superusers
can issue/revoke credentials or inspect the complete journal in Django admin.
Revoke using the credential list's action. Expiration, revocation, or disabling
or removing superuser status from the issuing admin blocks further access.

From a trusted server console, the equivalent commands are:

```sh
python manage.py agent_credential --admin ADMIN_USERNAME --name round-cleanup --days 7 --write
python manage.py agent_credential --admin ADMIN_USERNAME --revoke CREDENTIAL_ID
```

Keep the token in a secret store/environment variable. Send it in the header;
never in a URL. Use a separate short-lived credential per agent. Agents cannot
mint credentials, grant permissions, or edit user accounts through this API.

## What a cleanup agent should read

Start with `GET /api/agent/v1/` for a machine-readable resource/field/choice
catalog, then `GET /api/agent/v1/guide/` for this guide. Reads are uncached and
include temporary and synthetic identities hidden by ordinary profile lists.

* `rounds`: tournament, gov/opp IDs, winner, round number, stage, division,
  elimination size, labels, import keys/origins, and metadata.
* `round_stats`: speaker identity, speaks, rank, role, score index, source status,
  stage, and metadata. Outround score/role values are sanitized as in the app.
* `round_metadata`: original outcome text/code, participant aliases, source IDs.
* `round_judges`: original judge names, resolved aliases, chair status.
* `imports` and `import_batches`: source filenames/hashes, import types, dates,
  tournament links, and batch grouping. This API exposes stored provenance;
  it does not retrieve external source files.
* `aliases` and `alias_groups`: source names, normalized names, and identity links.
* `debaters`, `teams`, `schools`, `school_lookups`: identity correction fields,
  synthetic/temporary flags, affiliation, team membership, season bounds, and
  manual Elo inclusion. Private contact emails, claimed-user links, and account
  permissions are excluded from agent reads/writes.
* `tournaments`, `team_results`, `speaker_results`: tournament date/season,
  settings and associated results used to cross-check imported data.
* `qualifications`, `reaffiliations`, `team_reaffiliations`: eligibility and
  identity continuity inputs that may also require correction.
* `synthetic_logs`: read-only prior resolution/rejection evidence.
* `qual_points`, `coty`, `noty`, `online_qual`, `soty`, `toty`: read-only derived
  standings for cross-checking merge effects.

List: `GET /api/agent/v1/records/rounds/?tournament_id=123&limit=100`.
Detail: `GET /api/agent/v1/records/rounds/456/`.

Lists support exposed scalar fields as exact filters, `field__in=1,2,3`,
`field=null`, `q=substring` for text search, `limit=1..500`, and `after=ID`.
Continue with `next_after` until null. IDs are stable database primary keys;
foreign keys use `_id`. Arrays `debaters` on teams and `sources` on round metadata
contain all IDs including synthetic identities. Resolve these IDs using the
corresponding resource. Unknown fields/filters are rejected.

For a tournament cleanup, fetch its rounds, stats by `round_id__in`, metadata
by `round_id__in`, judges by `round_metadata_id__in`, imports by `tournament_id`,
and referenced teams, identities, schools and aliases by `id__in`. This provides
both normalized records and the original source evidence before deciding a fix.

## Bulk corrections

`POST /api/agent/v1/batches/` takes a JSON object. A batch is atomic: all
operations commit together or none do. Maximum 200 operations and 20,000 changed
rows. Include a descriptive `reason` and a fresh UUID `request_id`. Repeating the
same committed payload with the same credential and UUID returns its original
receipt; reusing the UUID for another payload is a 409 conflict.

Each read includes an opaque `revision`. Pass it for update/delete. Synthetic
merges require revisions for both identities. Stale/missing revisions are 409.
Foreign keys and database/model constraints are validated. Dates use ISO 8601;
decimals use strings to preserve precision. Unknown/protected fields fail closed.

```json
{
  "request_id": "f23f746d-c7e2-4979-9b1a-6ef32c96db70",
  "reason": "Correct final ballot against original tab export",
  "dry_run": true,
  "operations": [
    {
      "op": "update",
      "resource": "rounds",
      "id": 456,
      "revision": "COPY_FROM_READ",
      "data": {"victor": 2}
    },
    {
      "op": "update",
      "resource": "round_stats",
      "id": 789,
      "revision": "COPY_FROM_READ",
      "data": {"speaks": "27.5000", "ranks": "2.0000", "debater_role": "LO"}
    }
  ]
}
```

Use `op: create` with `resource` and `data` to add missing records. A create can
include `ref: new_alias`; a later foreign key may use `"$new_alias"` in place of
its integer ID. Set M2M fields (`debaters`, `sources`) to an array of IDs to
replace the membership. Update/delete an existing row using its `id` and
`revision`. Get a new revision after changing a row, including changes earlier
in a batch. Prefer one update per row per batch.

Delete supports rounds, stats, round metadata/judges, imports/batches, team and
speaker results, and school lookups. Cascades and SET_NULL effects are journaled.
Qualifications and reaffiliations can also be deleted. Identity deletion is deliberately unavailable; use the explicit merge operation
or repair its fields.

```json
{
  "request_id": "36dccfaa-2afb-4261-85a9-56484aca2cb3",
  "reason": "Synthetic person confirmed as canonical debater using source roster",
  "operations": [{
    "op": "merge_synthetic",
    "entity_type": "debater",
    "source_id": 901,
    "target_id": 123,
    "source_revision": "COPY_FROM_SOURCE_READ",
    "target_revision": "COPY_FROM_TARGET_READ"
  }]
}
```

`entity_type` is `debater`, `team`, or `school`. Source must be synthetic and
target canonical. This uses the application's existing resolution logic,
including alias/score/team/result transfers, affected standings and resolution
logs. Conflicting identities/constraints cause the entire batch to roll back.

For duplicate canonical debaters, `op: merge_debaters` takes the same source/
target IDs and revisions without `entity_type`. It transfers the application's
full related data and is reversible through the same journal. Use individual
alias/stat/member updates when only some records belong to another identity.

Dry runs execute the full transaction, return results and a readable diff, then
roll it back; they create no change set. Inspect the diff before committing.
`expected_change_hash` optionally requires an exact preview match. Previews that
create records (including merges that create logs/standings) can receive new IDs
or timestamps on re-execution, so their hashes may differ; rerun and inspect
those previews and commit with the original row revisions. Never rely on IDs
returned by a dry run. A committed response includes its change-set ID, results,
a diff, and a change hash. Validation failures are 400, unauthorized tokens 401,
read-only write attempts 403, missing records 404, and stale/conflicting edits 409.

## Review and undo

* `GET /api/agent/v1/batches/?limit=50` lists recent changes across authorized
  agents; use `next_before` as `before` to continue.
* `GET /api/agent/v1/batches/ID/` returns the receipt, sanitized diff, original
  results, reason, credential, and reversal information.
* `POST /api/agent/v1/batches/ID/undo/` with a fresh `request_id` and `reason`
  reverses the entire batch. It also supports `dry_run`.

Every change set stores the complete before and after values of each affected
concrete row, including deleted objects, through-table membership, side effects
from stage changes and merges, original primary keys, and resolution logs.
Sensitive fields needed to restore a merge are stored in the protected journal;
agent diffs expose only allowlisted correction fields. Keep the journal in
normal database backups; do not prune it if those corrections need to remain
reversible. Audit rows and credentials cannot be deleted in Django admin.

Undo verifies the current contents of every changed row against its stored
postimage, and refuses if a later edit conflicts. Reverse dependent batches
in reverse order first. New references that would become invalid also prevent
undo. Undo never forcibly overwrites later work. An undo is another journaled
change set, so it can itself be reversed using the same endpoint. A retry with
the original UUID is idempotent.

Writes serialize with ordinary database writers while the journal is captured.
PostgreSQL briefly locks core tables (10-second lock timeout); SQLite acquires
its writer lock before reading. Keep batches focused, especially for large
identity merges. Only changed rows are retained; normal CRUD snapshots just the
row and its dependent side effects. Merges additionally inspect related rows
and standings so transferred/deleted/recomputed data is restorable. A batch
that exceeds the row limit rolls back and must be split.

Successful writes and undo invalidate Elo dashboard cache state after commit.
The runtime fingerprint includes the durable change-set version so other workers
also reload edited input rows. Search-index updates run after commit; dry runs
never update search, and undo synchronizes restored/deleted debaters.
Elo is derived from corrected rounds on the next calculation. This API does not
rewrite original import files, run external imports, or launch background jobs.
