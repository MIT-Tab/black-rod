"""Verify reversible Agent writes across runtimes on disposable fixture databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from compare import Case, ROOT, request, servers


SEED = """
from reference import application
from datetime import date, timedelta
import json
from django.utils import timezone
from core.models import (
    AgentCredential, COTY, TOTY, Debater, DebaterAlias, ImportBatch, ImportedRoundJudge,
    ImportedRoundMetadata, Round, RoundStats, School, Team, Tournament,
    TournamentImport, User,
)

admin = User.objects.create_superuser(username='parity-agent-writer-' + str(timezone.now().timestamp()))
_, token = AgentCredential.issue(admin=admin, name='Disposable parity writer', can_write=True,
    expires_at=timezone.now() + timedelta(hours=1))
school = School.objects.create(name='Agent fixture college', short_name='AF')
synthetic_school = School.all_objects.create(name='Agent synthetic fixture school', short_name='ASF', synthetic=True)
real = Debater.objects.create(first_name='Éva', last_name='Example', school=synthetic_school)
COTY.objects.create(school=school, season='2025', points=4)
COTY.objects.create(school=synthetic_school, season='2025', points=13)
second = Debater.objects.create(first_name='Second', last_name='Example', school=school)
third = Debater.objects.create(first_name='Opponent', last_name='Fixture', school=school)
gov, opp = (Team.objects.create(name=name, short_name=name) for name in ('Agent Gov', 'Agent Opp'))
gov.debaters.add(real, second)
synthetic_team = Team.objects.create(name='Agent synthetic fixture team', synthetic=True)
synthetic_team.debaters.add(real)
TOTY.objects.create(team=gov, season='2025', points=2)
TOTY.objects.create(team=synthetic_team, season='2025', points=8)
opp.debaters.add(third)
tournament = Tournament.objects.create(host=school, season='2025', date=date(2025, 10, 1))
round = Round.objects.create(tournament=tournament, gov=gov, opp=opp, round_number=1, victor=Round.GOV)
stat = RoundStats.objects.create(round=round, debater=real, speaks='27.5000', ranks='1.0000', debater_role='PM')
alias = DebaterAlias.objects.create(debater=real, source_name='Example', normalized_name='example')
metadata = ImportedRoundMetadata.objects.create(round=round, gov_1_alias=alias, raw_result_code='G')
ImportedRoundJudge.objects.create(round_metadata=metadata, original_name='Fixture', debater_alias=alias, is_chair=True)
source = TournamentImport.objects.create(tournament=tournament, batch=ImportBatch.objects.create(),
    import_type='file_backup', original_file_name='fixture.json')
metadata.sources.add(source)
print(json.dumps({'token':token, 'rounds':round.pk, 'round_stats':stat.pk, 'round_metadata':metadata.pk,
    'teams':gov.pk, 'debaters':real.pk, 'school_ids':[synthetic_school.pk,school.pk],
    'team_ids':[synthetic_team.pk,gov.pk]}))
"""


def exercise(django, rust, fixture):
    base = "/api/agent/v1/"
    authorization = f"Bearer {fixture['token']}"

    def run(server, path, *, payload=None, expected=200):
        code, _, body = request(server, Case(
            base + path, method="GET" if payload is None else "POST",
            body=None if payload is None else json.dumps(payload), authorization=authorization,
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
    print("Agent write parity passed: preview hashes, concurrent cross-runtime retries, stale revisions, "
          "atomic rollback, references, teams, cascade restoration, conflicting later edits, and synthetic school/team merges")


def main():
    seed = Path(os.environ["BR_STATE"]) / "dev.sqlite3"
    postgres = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL")
    if postgres:
        parsed = urlsplit(postgres)
        if parsed.hostname != "127.0.0.1" or not parsed.path.startswith("/black_rod_parity"):
            raise ValueError("Agent writes require a disposable local black_rod_parity PostgreSQL database")
    with TemporaryDirectory(prefix="black-rod-agent-fixture-") as folder:
        db = Path(folder) / "dev.sqlite3"
        if not postgres:
            with closing(sqlite3.connect(f"{seed.as_uri()}?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(db)) as copy:
                    source.backup(copy)
        os.environ.update(BR_STATE=folder, BLACK_ROD_PARITY_WRITABLE="1")
        env = {**os.environ, "PYTHONPATH": f"{Path(__file__).resolve().parent}{os.pathsep}{os.environ['PYTHONPATH']}",
               "SECRET_KEY": "disposable-local-parity-key"}
        result = subprocess.check_output([str(ROOT / "venv/bin/python"), "-c", SEED], env=env, text=True)
        fixture = json.loads(result.splitlines()[-1])
        with servers(db) as (django, rust):
            exercise(django, rust, fixture)


if __name__ == "__main__":
    main()
