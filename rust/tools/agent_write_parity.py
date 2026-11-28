"""Verify reversible Agent writes across runtimes on disposable fixture databases."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlencode, urlsplit
from uuid import uuid4

import psycopg2
from psycopg2 import sql

from compare import Case, ROOT, request, servers


SEED = """
from reference import application
from datetime import date, datetime, timedelta, timezone as datetime_timezone
import json
from uuid import uuid4
from django.utils import timezone
from core.models import (
    AgentCredential, COTY, TOTY, Debater, DebaterAlias, ImportBatch, ImportedRoundJudge,
    ImportedRoundMetadata, Round, RoundStats, School, Team, Tournament,
    SpeakerResult, TeamResult, TournamentImport, User,
)

admin = User.objects.create_superuser(username='parity-agent-writer-' + str(timezone.now().timestamp()))
_, token = AgentCredential.issue(admin=admin, name='Disposable parity writer', can_write=True,
    expires_at=timezone.now() + timedelta(hours=1))
suffix = uuid4().hex[:8]
school = School.objects.create(name=f'Agent fixture college {suffix}', short_name='AF')
synthetic_school = School.all_objects.create(name=f'Agent synthetic fixture school {suffix}', short_name='ASF', synthetic=True)
real = Debater.objects.create(first_name='Éva', last_name='Example', school=synthetic_school)
COTY.objects.create(school=school, season='2025', points=4)
COTY.objects.create(school=synthetic_school, season='2025', points=13)
second = Debater.objects.create(first_name='Second', last_name='Example', school=school)
third = Debater.objects.create(first_name='Opponent', last_name='Fixture', school=school)
synthetic = Debater.all_objects.create(first_name='Synthetic', last_name='Person', school=school, synthetic=True)
gov, opp = (Team.objects.create(name=name, short_name=name) for name in ('Agent Gov', 'Agent Opp'))
gov.debaters.add(real, second)
synthetic_team = Team.objects.create(name='Agent synthetic fixture team', synthetic=True)
synthetic_team.debaters.add(real)
TOTY.objects.create(team=gov, season='2025', points=2)
TOTY.objects.create(team=synthetic_team, season='2025', points=8)
opp.debaters.add(third)
tournament = Tournament.objects.create(host=school, season='2035', date=date(2035, 10, 1), num_teams=24)
TeamResult.objects.create(tournament=tournament, team=gov, place=1)
SpeakerResult.objects.create(tournament=tournament, debater=synthetic, place=1)
details = {"wide_id": 340282366920938463463374607431768211455,
    "float_values": [0.000001, 0.0001, -0.125, 1.25e-8], "marker": chr(127)}
round = Round.objects.create(tournament=tournament, gov=gov, opp=opp, round_number=1,
    victor=Round.GOV, metadata=details)
stat = RoundStats.objects.create(round=round, debater=real, speaks='27.5000', ranks='1.0000', debater_role='PM', metadata=details)
synthetic_stat = RoundStats.objects.create(round=round, debater=synthetic, speaks='28.5000', ranks='1.0000', debater_role='LO')
synthetic_alias = DebaterAlias.objects.create(debater=synthetic, source_name='Example', normalized_name='example')
instant = datetime(2025, 10, 1, 8, microsecond=1, tzinfo=datetime_timezone.utc)
DebaterAlias.objects.filter(pk=synthetic_alias.pk).update(created_at=instant, updated_at=instant)
alias = DebaterAlias.objects.create(debater=real, source_name='Example', normalized_name='example')
metadata = ImportedRoundMetadata.objects.create(round=round, gov_1_alias=alias, raw_result_code='G')
ImportedRoundJudge.objects.create(round_metadata=metadata, original_name='Fixture', debater_alias=alias, is_chair=True)
source = TournamentImport.objects.create(tournament=tournament, batch=ImportBatch.objects.create(),
    import_type='file_backup', original_file_name='fixture.json')
metadata.sources.add(source)
print(json.dumps({'token':token, 'rounds':round.pk, 'round_stats':stat.pk, 'round_metadata':metadata.pk,
    'teams':gov.pk, 'debaters':real.pk, 'school_ids':[synthetic_school.pk,school.pk],
    'team_ids':[synthetic_team.pk,gov.pk], 'synthetic':synthetic.pk, 'synthetic_stat':synthetic_stat.pk,
    'synthetic_alias':synthetic_alias.pk, 'tournament':tournament.pk}))
"""


def exercise(django, rust, fixture):
    base = "/api/agent/v1/"
    authorization = f"Bearer {fixture['token']}"

    def run(server, path, *, payload=None, expected=200):
        code, _, body = request(server, Case(
            base + path, method="GET" if payload is None else "POST",
            body=payload if isinstance(payload, str) else (None if payload is None else json.dumps(payload)),
            authorization=authorization,
        ))
        data = json.loads(body)
        assert code == expected, (server.name, path, code, expected, data)
        return data

    def get(server, resource):
        return run(server, f"records/{resource}/{fixture[resource]}/")

    def update(resource, **data):
        return {"op": "update", "resource": resource, "id": fixture[resource],
                "revision": get(django, resource)["revision"], "data": data}

    def prepare(operations=None):
        payload = {"request_id": str(uuid4()), "reason": "Disposable correction fixture"}
        if operations is not None:
            payload["operations"] = operations
        return payload

    for resource, filters, expected_id in (
        ("tournaments", {"date": "2035-10-1"}, fixture["tournament"]),
        ("tournaments", {"date__in": "2035-10-1,2034-10-01"}, fixture["tournament"]),
        ("aliases", {"created_at": "2025-10-01T08:00:00.000001Z"}, fixture["synthetic_alias"]),
        ("aliases", {"created_at__in": "2025-10-01 01:00:00.000001-07:00,2026-09-01"},
         fixture["synthetic_alias"]),
        ("tournaments", {"date": "bad-date"}, None),
        ("tournaments", {"date__in": "2035-10-01,2035-02-30"}, None),
        ("aliases", {"created_at": "bad-time"}, None),
        ("aliases", {"created_at__in": "bad-time,2026-09-01"}, None),
        ("aliases", {"created_at": "2025-10-01T27:00:00Z"}, None),
    ):
        path = f"records/{resource}/?{urlencode(filters)}"
        status = 400 if expected_id is None else 200
        reference = run(django, path, expected=status)
        assert reference == run(rust, path, expected=status), path
        if expected_id is not None:
            assert expected_id in {row["id"] for row in reference["results"]}, path

    # Preserve the exact stored creation timestamp during an unrelated edit and its undo.
    alias_id = fixture["synthetic_alias"]
    alias_path = f"records/aliases/{alias_id}/"
    alias = run(django, alias_path)
    precise = "records/aliases/?" + urlencode({"created_at": "2025-10-01T08:00:00.000001Z"})
    correction = prepare([{"op": "update", "resource": "aliases", "id": alias_id,
                           "revision": alias["revision"], "data": {"source_name": "Corrected alias"}}])
    applied = run(rust, "batches/", payload=correction)
    assert run(django, alias_path)["source_name"] == "Corrected alias"
    assert alias_id in {row["id"] for row in run(django, precise)["results"]}
    run(rust, f"batches/{applied['id']}/undo/", payload=prepare())
    assert run(django, alias_path)["source_name"] == alias["source_name"]
    for server in (django, rust):
        assert alias_id in {row["id"] for row in run(server, precise)["results"]}

    scientific = {**prepare([update("round_stats", speaks="2.345e1")]), "dry_run": True}
    expected_scientific = run(django, "batches/", payload=scientific)
    actual_scientific = run(rust, "batches/", payload=scientific)
    assert expected_scientific == actual_scientific, (expected_scientific, actual_scientific)
    overprecise = {**prepare([update("round_stats", speaks="1e-5")]), "dry_run": True}
    assert run(rust, "batches/", payload=overprecise, expected=400) == run(
        django, "batches/", payload=overprecise, expected=400,
    )

    event = run(django, f"records/tournaments/{fixture['tournament']}/")
    numeric = {**prepare([{"op": "update", "resource": "tournaments", "id": float(event["id"]),
                           "revision": event["revision"], "data": {
                               "num_teams": 32.0, "host_id": float(event["host_id"]), "qual": 1.0,
                           }}]), "dry_run": True}
    normalized = run(django, "batches/", payload=numeric)
    for representation in ("32.0", "3.2e1", "32.75"):
        payload = json.dumps(numeric).replace('"num_teams": 32.0', f'"num_teams": {representation}')
        assert run(django, "batches/", payload=payload) == normalized
        assert run(rust, "batches/", payload=payload) == normalized

    resources = ("rounds", "round_stats", "round_metadata", "teams")
    before = {resource: get(django, resource) for resource in resources}
    assert before == {resource: get(rust, resource) for resource in resources}
    preview_payload = prepare([
        update("rounds", stage="outround", elim_size=2),
        update("round_metadata", raw_result_code="O"),
    ])
    reference_preview = run(django, "batches/", payload={**preview_payload, "dry_run": True})
    candidate_preview = run(rust, "batches/", payload={**preview_payload, "dry_run": True})
    assert reference_preview == candidate_preview
    assert before == {resource: get(rust, resource) for resource in resources}
    run(rust, "batches/", payload={**preview_payload, "expected_change_hash": "stale"}, expected=409)
    committed = run(rust, "batches/", payload={**preview_payload,
                    "expected_change_hash": reference_preview["change_hash"]})
    assert committed == run(django, "batches/", payload={**preview_payload,
                            "expected_change_hash": reference_preview["change_hash"]})
    assert committed == run(rust, f"batches/{committed['id']}/")
    stat = get(django, "round_stats")
    assert stat["stage"] == "outround" and stat["speaks"] is None and stat["debater_role"] is None
    run(rust, "batches/", payload={**preview_payload, "reason": "Changed reason"}, expected=409)
    stale = run(rust, "batches/", payload=prepare(preview_payload["operations"]), expected=409)
    assert stale["detail"] == "Missing or stale revision."
    undo_path = f"batches/{committed['id']}/undo/"
    reversed_batch = run(django, undo_path, payload=prepare())
    assert reversed_batch == run(rust, f"batches/{reversed_batch['id']}/")
    assert before == {resource: get(django, resource) for resource in resources}
    run(rust, undo_path, payload=prepare(), expected=409)

    invalid = prepare([update("rounds", victor=2), update("round_stats", speaks="9000")])
    run(rust, "batches/", payload=invalid, expected=400)
    assert before == {resource: get(django, resource) for resource in resources}
    created = run(rust, "batches/", payload=prepare([
        {"op": "create", "resource": "aliases", "ref": "alias", "data": {
            "debater_id": fixture["debaters"], "source_name": "Fixture É", "normalized_name": "fixture é",
        }},
        update("round_metadata", opp_1_alias_id="$alias", sources=[]),
        update("teams", debaters=[fixture["debaters"]]),
    ]))
    assert get(django, "round_metadata")["opp_1_alias_id"] == created["results"][0]["id"]
    assert get(django, "teams")["debaters"] == [fixture["debaters"]]
    after = {resource: get(django, resource) for resource in resources}
    reversal = run(rust, f"batches/{created['id']}/undo/", payload=prepare())
    assert before == {resource: get(django, resource) for resource in resources}
    run(rust, f"batches/{reversal['id']}/undo/", payload=prepare())
    assert after == {resource: get(django, resource) for resource in resources}

    record = get(django, "rounds")
    deletion = prepare([{"op": "delete", "resource": "rounds", "id": record["id"], "revision": record["revision"]}])
    reference_preview = run(django, "batches/", payload={**deletion, "dry_run": True})
    candidate_preview = run(rust, "batches/", payload={**deletion, "dry_run": True})
    assert reference_preview == candidate_preview
    deleted = run(django, "batches/", payload=deletion)
    run(django, f"records/rounds/{record['id']}/", expected=404)
    run(rust, f"records/round_stats/{fixture['round_stats']}/", expected=404)
    run(rust, f"batches/{deleted['id']}/undo/", payload=prepare())
    assert after == {resource: get(django, resource) for resource in resources}
    assert after == {resource: get(rust, resource) for resource in resources}
    concurrent = prepare([update("rounds", victor=2)])
    with ThreadPoolExecutor(max_workers=2) as workers:
        left = workers.submit(run, django, "batches/", payload=concurrent)
        right = workers.submit(run, rust, "batches/", payload=concurrent)
        assert left.result() == right.result()
    first = left.result()
    run(django, "batches/", payload=prepare([update("rounds", victor=1)]))
    blocked = run(rust, f"batches/{first['id']}/undo/", payload=prepare(), expected=409)
    assert blocked["detail"] == "Later edits prevent undo."
    assert get(django, "rounds")["victor"] == 1
    for entity in ("school", "team"):
        source_id, target_id = fixture[f"{entity}_ids"]
        source_path, target_path = (f"records/{entity}s/{pk}/" for pk in (source_id, target_id))
        source, target = run(django, source_path), run(django, target_path)
        merged = run(rust, "batches/", payload=prepare([{
            "op": "merge_synthetic", "entity_type": entity, "source_id": source_id, "target_id": target_id,
            "source_revision": source["revision"], "target_revision": target["revision"],
        }]))
        assert merged["results"] == [{"entity_type": entity, "source_id": source_id, "target_id": target_id}]
        assert any(change["model"] == {"school": "core.COTY", "team": "core.TOTY"}[entity]
                   for change in merged["changes"])
        assert merged == run(django, f"batches/{merged['id']}/")
        run(django, source_path, expected=404)
        run(django, f"batches/{merged['id']}/undo/", payload=prepare())
        assert run(rust, source_path) == source and run(rust, target_path) == target
    assert get(rust, "debaters")["school_id"] == fixture["school_ids"][0]
    source_id, target_id = fixture["synthetic"], fixture["debaters"]
    source_path, target_path = (f"records/debaters/{pk}/" for pk in (source_id, target_id))
    source, target = run(django, source_path), run(django, target_path)
    stat_path = f"records/round_stats/{fixture['synthetic_stat']}/"
    alias_path = f"records/aliases/{fixture['synthetic_alias']}/"
    synthetic_stat = run(django, stat_path)
    synthetic_alias = run(django, alias_path)
    assert synthetic_alias == run(rust, alias_path)
    assert synthetic_alias["created_at"].endswith(".000Z")
    for action in ("merge_synthetic", "merge_debaters"):
        operation = {
            "op": action, "source_id": source_id, "target_id": target_id,
            "source_revision": source["revision"], "target_revision": target["revision"],
        }
        if action == "merge_synthetic":
            operation["entity_type"] = "debater"
        reference = run(django, "batches/", payload={**prepare([operation]), "dry_run": True})
        candidate = run(rust, "batches/", payload={**prepare([operation]), "dry_run": True})
        def effects(batch):
            # Log timestamps and IDs of newly sequenced PostgreSQL rows vary across previews.
            return Counter((entry["model"], entry["action"], entry["after"].get("points"))
                           for entry in batch["changes"] if entry["model"] != "core.SyntheticResolutionLog")
        assert effects(reference) == effects(candidate), (effects(reference), effects(candidate))
        merged = run(rust, "batches/", payload=prepare([operation]))
        assert merged["results"] == [{"entity_type": "debater", "source_id": source_id, "target_id": target_id}]
        assert merged == run(django, f"batches/{merged['id']}/")
        run(django, source_path, expected=404)
        run(django, alias_path, expected=404)
        stat = run(django, stat_path)
        assert stat["debater_id"] == target_id and stat["score_index"] == 2
        undo_server = rust if action == "merge_synthetic" else django
        reversal = run(undo_server, f"batches/{merged['id']}/undo/", payload=prepare())
        assert run(rust, source_path) == source and run(rust, target_path) == target
        assert run(rust, stat_path) == synthetic_stat
        # Both runtimes restore the millisecond value stored in the journal.
        restored_alias = run(rust, alias_path)
        assert restored_alias == run(django, alias_path)
        assert restored_alias["created_at"] == synthetic_alias["created_at"].replace(".000Z", "Z")
        assert restored_alias["updated_at"] == synthetic_alias["updated_at"].replace(".000Z", "Z")
        for field in synthetic_alias.keys() - {"revision", "created_at", "updated_at"}:
            assert restored_alias[field] == synthetic_alias[field]
        if undo_server is rust:
            # A reversed journal written by Rust can itself be reversed by legacy Django.
            redo = run(django, f"batches/{reversal['id']}/undo/", payload=prepare())
            run(rust, f"batches/{redo['id']}/undo/", payload=prepare())
            assert run(rust, stat_path) == synthetic_stat
            assert run(rust, alias_path) == restored_alias
    print("Agent parity passed: date filters, preview hashes, concurrent cross-runtime retries, stale revisions, "
          "atomic rollback, references, teams, cascade restoration, conflicting later edits, and school, team, and debater merges")



@contextmanager
def fixture_database(seed, local, postgres):
    if not postgres:
        with closing(sqlite3.connect(f"{seed.as_uri()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(local)) as copy:
            source.backup(copy)
        yield
        return
    parsed = urlsplit(postgres)
    source = unquote(parsed.path.removeprefix("/"))
    if parsed.hostname != "127.0.0.1" or not source.startswith("black_rod_parity"):
        raise ValueError("Agent writes require a local black_rod_parity PostgreSQL fixture")
    clone = f"black_rod_parity_{uuid4().hex}"
    with closing(psycopg2.connect(
        dbname="postgres", host=parsed.hostname, port=parsed.port,
        user=unquote(parsed.username or ""), password=unquote(parsed.password or ""),
    )) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(clone), sql.Identifier(source)))
            try:
                os.environ["BLACK_ROD_PARITY_POSTGRES_URL"] = parsed._replace(path=f"/{clone}").geturl()
                yield
            finally:
                os.environ["BLACK_ROD_PARITY_POSTGRES_URL"] = postgres
                cursor.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(clone)))


def main():
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    postgres = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL")
    with TemporaryDirectory(prefix="black-rod-agent-fixture-") as folder:
        db = Path(folder) / "dev.sqlite3"
        with fixture_database(seed, db, postgres):
            os.environ.update(BR_STATE=folder, BLACK_ROD_PARITY_WRITABLE="1")
            env = {**os.environ, "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}",
                   "SECRET_KEY": "disposable-local-parity-key"}
            result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", SEED], env=env, text=True)
            fixture = json.loads(result.splitlines()[-1])
            with servers(db) as (django, rust):
                exercise(django, rust, fixture)


if __name__ == "__main__":
    main()
