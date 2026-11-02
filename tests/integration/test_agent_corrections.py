"""Agent authorization, atomic corrections, and full graph round-trip tests."""

import hashlib
import json
import uuid
from datetime import date, timedelta

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib import admin as django_admin
from django.core.management import call_command
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AgentCredential,
    AgentChangeSet,
    Debater,
    DebaterAlias,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    ImportBatch,
    Round,
    RoundStats,
    School,
    SchoolLookup,
    Team,
    Tournament,
    TournamentImport,
    User,
    ClaimDebaterRequest,
    TeamResult,
    COTY,
    TOTY,
    QualPoints,
)
from core.utils.agent_data import public_row, row_data

BASE = "/api/agent/v1/"
pytestmark = pytest.mark.django_db


@pytest.fixture
def access():
    admin = User.objects.create_superuser(
        username="agent-admin", password="pass", email="admin@example.com"
    )
    credential, token = AgentCredential.issue(
        admin=admin,
        name="cleanup",
        can_write=True,
        expires_at=timezone.now() + timedelta(days=1),
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer " + token)
    return client, credential, token


@pytest.fixture
def graph():
    school = School.objects.create(name="Canonical College", short_name="CC")
    synthetic_school = School.all_objects.create(name="Fake School", synthetic=True)
    real = Debater.all_objects.create(
        first_name="Real",
        last_name="Person",
        school=school,
        email="private@example.com",
    )
    fake = Debater.all_objects.create(
        first_name="Fake", last_name="Person", school=synthetic_school, synthetic=True
    )
    opponent = Debater.all_objects.create(first_name="Opponent", school=school)
    gov = Team.objects.create(name="Fake team", synthetic=True)
    gov.debaters.add(fake)
    opp = Team.objects.create(name="Real team")
    opp.debaters.add(real, opponent)
    tournament = Tournament.objects.create(
        name="Test",
        manual_name="Test",
        host=school,
        date=date(2025, 10, 1),
        season="2025",
    )
    round = Round.objects.create(
        tournament=tournament, gov=gov, opp=opp, round_number=1, victor=Round.GOV
    )
    stat = RoundStats.objects.create(
        round=round, debater=fake, speaks="27.5000", ranks="1.0000", debater_role="PM"
    )
    alias = DebaterAlias.objects.create(
        debater=fake, source_name="F Person", normalized_name="f person"
    )
    meta = ImportedRoundMetadata.objects.create(
        round=round, gov_1_alias=alias, raw_result_code="G"
    )
    batch = ImportBatch.objects.create()
    source = TournamentImport.objects.create(
        tournament=tournament,
        batch=batch,
        import_type="file_backup",
        original_file_name="source.json",
    )
    meta.sources.add(source)
    judge = ImportedRoundJudge.objects.create(
        round_metadata=meta,
        original_name="F Person",
        debater_alias=alias,
        is_chair=True,
    )
    return dict(
        school=school,
        synthetic_school=synthetic_school,
        real=real,
        fake=fake,
        opponent=opponent,
        gov=gov,
        opp=opp,
        tournament=tournament,
        round=round,
        stat=stat,
        alias=alias,
        meta=meta,
        source=source,
        judge=judge,
        batch=batch,
    )


def post(client, operations=None, **kwargs):
    payload = dict(
        request_id=str(uuid.uuid4()), reason="Verified against source", **kwargs
    )
    if operations is not None:
        payload["operations"] = operations
    return client.post(BASE + "batches/", payload, format="json")


def update(resource, obj, **data):
    return dict(
        op="update",
        resource=resource,
        id=obj.pk,
        revision=public_row(resource, obj)["revision"],
        data=data,
    )


def undo(client, batch_id, **kwargs):
    return client.post(
        BASE + f"batches/{batch_id}/undo/",
        dict(request_id=str(uuid.uuid4()), reason="Undo correction", **kwargs),
        format="json",
    )


def graph_snapshot():
    excluded = {"AgentCredential", "AgentChangeSet"}
    return {
        m._meta.label: [row_data(obj) for obj in m._base_manager.order_by("pk")]
        for m in apps.get_app_config("core").get_models(include_auto_created=True)
        if m.__name__ not in excluded
    }


def test_auth_is_bearer_only_and_tokens_hashed(access):
    client, credential, token = access
    assert credential.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(credential.__dict__)
    assert client.get(BASE).status_code == 200
    anonymous = APIClient()
    assert anonymous.get(BASE).status_code == 401
    anonymous.force_login(credential.created_by)
    assert anonymous.get(BASE).status_code == 401
    anonymous.credentials(HTTP_AUTHORIZATION="Bearer wrong")
    assert anonymous.get(BASE).status_code == 401
    for field, value in [
        ("revoked_at", timezone.now()),
        ("expires_at", timezone.now() - timedelta(seconds=1)),
    ]:
        old = getattr(credential, field)
        setattr(credential, field, value)
        credential.save()
        assert client.get(BASE).status_code == 401
        setattr(credential, field, old)
        credential.save()
    credential.created_by.is_superuser = False
    credential.created_by.save()
    assert client.get(BASE).status_code == 401


def test_read_only_and_admin_permissions(access):
    client, credential, _ = access
    credential.can_write = False
    credential.save()
    assert client.get(BASE).status_code == 200
    assert post(client, []).status_code == 403
    assert undo(client, 1).status_code == 403
    normal = User.objects.create_user(username="ordinary", is_staff=True)
    with pytest.raises(ValueError):
        AgentCredential.issue(
            admin=normal,
            name="forbidden",
            expires_at=timezone.now() + timedelta(days=1),
        )
    model_admin = django_admin.site._registry[AgentCredential]
    request = RequestFactory().get("/")
    request.user = normal
    assert not model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    request.user = credential.created_by
    assert model_admin.has_add_permission(request)
    assert not model_admin.has_delete_permission(request)
    journal_admin = django_admin.site._registry[AgentChangeSet]
    assert not journal_admin.has_change_permission(request)
    assert not journal_admin.has_delete_permission(request)


def test_reads_include_synthetic_members_provenance_and_pagination(access, graph):
    client, _, _ = access
    g = graph
    record = client.get(BASE + f"records/teams/{g['gov'].pk}/").json()
    assert record["debaters"] == [g["fake"].pk]
    result = client.get(BASE + "records/debaters/", {"synthetic": "true"})
    assert result.status_code == 200, result.content
    assert [r["id"] for r in result.json()["results"]] == [g["fake"].pk]
    record = client.get(BASE + f"records/debaters/{g['real'].pk}/").json()
    assert "email" not in record and "user_id" not in record
    meta = client.get(
        BASE + "records/round_metadata/", {"round_id": g["round"].pk}
    ).json()["results"][0]
    assert meta["sources"] == [g["source"].pk]
    assert meta["raw_result_code"] == "G"
    first = client.get(BASE + "records/debaters/?limit=1").json()
    second = client.get(
        BASE + f"records/debaters/?limit=1&after={first['next_after']}"
    ).json()
    assert first["results"][0]["id"] != second["results"][0]["id"]
    assert (
        client.get(BASE + "records/debaters/?email=private@example.com").status_code
        == 400
    )
    assert client.get(BASE + "records/users/").status_code == 400
    assert client.get(BASE + "guide/").status_code == 200


def test_atomic_bulk_idempotency_and_reversible_stage_side_effects(access, graph):
    client, _, _ = access
    g = graph
    before = graph_snapshot()
    operations = [
        update("rounds", g["round"], stage="outround", elim_size=2),
        update("round_metadata", g["meta"], raw_result_code="O"),
    ]
    preview = post(client, operations, dry_run=True)
    assert preview.status_code == 200, preview.content
    assert graph_snapshot() == before
    assert not AgentChangeSet.objects.exists()
    payload = dict(
        request_id=str(uuid.uuid4()),
        reason="Correct bracket",
        operations=operations,
        expected_change_hash=preview.json()["change_hash"],
    )
    with pytest.MonkeyPatch.context() as patch:
        from core.utils import agent_data

        called = []
        patch.setattr(
            agent_data, "invalidate_cached_elo_state", lambda: called.append(True)
        )
        from django.test import TestCase

        with TestCase.captureOnCommitCallbacks(execute=True):
            response = client.post(BASE + "batches/", payload, format="json")
        assert called == [True]
    assert response.status_code == 200, response.content
    g["stat"].refresh_from_db()
    assert g["stat"].speaks is None and g["stat"].debater_role is None
    retry = client.post(BASE + "batches/", payload, format="json")
    assert retry.json()["id"] == response.json()["id"]
    assert AgentChangeSet.objects.count() == 1
    payload["reason"] = "Different request"
    assert client.post(BASE + "batches/", payload, format="json").status_code == 409
    assert undo(client, response.json()["id"]).status_code == 200
    assert graph_snapshot() == before


def test_failure_and_stale_revisions_roll_back_whole_batch(access, graph):
    client, _, _ = access
    g = graph
    operations = [
        update("rounds", g["round"], victor=Round.OPP),
        update("round_stats", g["stat"], score_index=0, speaks="9000"),
    ]
    response = post(client, operations)
    assert response.status_code == 400, response.content
    g["round"].refresh_from_db()
    assert g["round"].victor == Round.GOV
    assert not AgentChangeSet.objects.exists()
    op = update("rounds", g["round"], victor=Round.OPP)
    Round.objects.filter(pk=g["round"].pk).update(victor=Round.BYE)
    assert post(client, [op]).status_code == 409
    assert (
        post(
            client,
            [
                dict(
                    op="update",
                    resource="debaters",
                    id=g["real"].pk,
                    revision=public_row("debaters", g["real"])["revision"],
                    data={"user_id": 1},
                )
            ],
        ).status_code
        == 400
    )


def test_create_refs_membership_and_round_delete_restore_every_row(access, graph):
    client, _, _ = access
    g = graph
    before = graph_snapshot()
    response = post(
        client,
        [
            dict(
                op="create",
                resource="aliases",
                ref="alias",
                data=dict(
                    debater_id=g["real"].pk,
                    source_name="Real P",
                    normalized_name="real p",
                ),
            ),
            update("round_metadata", g["meta"], opp_1_alias_id="$alias", sources=[]),
            update("teams", g["opp"], debaters=[g["real"].pk]),
        ],
    )
    assert response.status_code == 200, response.content
    created = graph_snapshot()
    reversed = undo(client, response.json()["id"])
    assert reversed.status_code == 200, reversed.content
    assert graph_snapshot() == before
    assert undo(client, reversed.json()["id"]).status_code == 200
    assert graph_snapshot() == created
    g["round"].refresh_from_db()
    response = post(
        client,
        [
            dict(
                op="delete",
                resource="rounds",
                id=g["round"].pk,
                revision=public_row("rounds", g["round"])["revision"],
            )
        ],
    )
    assert response.status_code == 200, response.content
    assert not RoundStats.objects.exists()
    assert not ImportedRoundJudge.objects.exists()
    assert undo(client, response.json()["id"]).status_code == 200
    assert graph_snapshot() == created


@pytest.mark.parametrize(
    "entity,source_key,target_key",
    [
        ("debater", "fake", "real"),
        ("school", "synthetic_school", "school"),
        ("team", "gov", "opp"),
    ],
)
def test_synthetic_merge_undo_restores_complete_database_graph(
    access, graph, entity, source_key, target_key
):
    client, _, _ = access
    g = graph
    ClaimDebaterRequest.objects.create(
        requested_by=access[1].created_by, debater=g["fake"]
    )
    SchoolLookup.objects.create(
        server_name="original-school", school=g["synthetic_school"]
    )
    COTY.objects.create(school=g["school"], season="2025", points=1)
    COTY.objects.create(school=g["synthetic_school"], season="2025", points=2)
    TOTY.objects.create(team=g["gov"], season="2025", points=1)
    TOTY.objects.create(team=g["opp"], season="2025", points=2)
    before = graph_snapshot()
    source, target = g[source_key], g[target_key]
    resource = {"debater": "debaters", "school": "schools", "team": "teams"}[entity]
    response = post(
        client,
        [
            dict(
                op="merge_synthetic",
                entity_type=entity,
                source_id=source.pk,
                target_id=target.pk,
                source_revision=public_row(resource, source)["revision"],
                target_revision=public_row(resource, target)["revision"],
            )
        ],
    )
    assert response.status_code == 200, response.content
    assert not type(source)._base_manager.filter(pk=source.pk).exists()
    after = graph_snapshot()
    result = undo(client, response.json()["id"])
    assert result.status_code == 200, result.content
    assert graph_snapshot() == before
    result = undo(client, result.json()["id"])
    assert result.status_code == 200, result.content
    assert graph_snapshot() == after
    assert "private@example.com" not in json.dumps(response.json())


def test_merge_journal_keeps_whole_table_scope_after_narrow_watch(access, graph):
    """A narrow FK watch must not shrink the later whole-standings-table watch.

    Otherwise unrelated standings rows are journaled as deleted and undo fails.
    """
    client, _, _ = access
    g = graph
    QualPoints.objects.create(debater=g["fake"], season="2025", points=3)
    unrelated = [
        QualPoints.objects.create(debater=g["opponent"], season=season, points=4)
        for season in ("2024", "2025")
    ]
    before = graph_snapshot()
    response = post(
        client,
        [
            dict(
                op="merge_synthetic",
                entity_type="debater",
                source_id=g["fake"].pk,
                target_id=g["real"].pk,
                source_revision=public_row("debaters", g["fake"])["revision"],
                target_revision=public_row("debaters", g["real"])["revision"],
            )
        ],
    )
    assert response.status_code == 200, response.content
    changed = {(c["model"], c["id"]) for c in response.json()["changes"]}
    for row in unrelated:
        assert ("core.QualPoints", row.pk) not in changed
    result = undo(client, response.json()["id"])
    assert result.status_code == 200, result.content
    assert graph_snapshot() == before


def test_undo_refuses_later_edits_and_dependent_references(access, graph):
    client, _, _ = access
    g = graph
    result = post(client, [update("rounds", g["round"], victor=Round.OPP)])
    assert result.status_code == 200, result.content
    Round.objects.filter(pk=g["round"].pk).update(victor=Round.ALL_WIN)
    assert undo(client, result.json()["id"]).status_code == 409
    g["round"].refresh_from_db()
    assert g["round"].victor == Round.ALL_WIN
    created = post(
        client, [dict(op="create", resource="teams", data={"name": "New team"})]
    )
    assert created.status_code == 200, created.content
    TeamResult.objects.create(
        team_id=created.json()["results"][0]["id"], tournament=g["tournament"], place=1
    )
    assert undo(client, created.json()["id"]).status_code in {400, 409}
    assert Team.objects.filter(pk=created.json()["results"][0]["id"]).exists()


def test_expiry_validation_and_console_admin_requirement(access):
    from django.core.management.base import CommandError
    from io import StringIO

    with pytest.raises(CommandError):
        call_command("agent_credential", admin="missing", name="bad", stdout=StringIO())
    with pytest.raises(ValueError):
        AgentCredential.issue(
            admin=access[1].created_by,
            name="expired",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
    out = StringIO()
    call_command("agent_credential", admin="agent-admin", name="reader", stdout=out)
    issued = AgentCredential.objects.get(name="reader")
    assert not issued.can_write
    assert "br_agent_" in out.getvalue()
    call_command(
        "agent_credential", admin="agent-admin", revoke=issued.pk, stdout=StringIO()
    )
    issued.refresh_from_db()
    assert issued.revoked_at


def test_fingerprint_changes_on_in_place_correction_and_undo(access, graph):
    from core.utils.elo_runtime_engine.cache import data_fingerprint

    client, _, _ = access
    before = data_fingerprint()
    operation = update("rounds", graph["round"], victor=Round.OPP)
    assert post(client, [operation], dry_run=True).status_code == 200
    assert data_fingerprint() == before
    response = post(client, [operation])
    assert response.status_code == 200, response.content
    changed = data_fingerprint()
    assert changed != before
    assert undo(client, response.json()["id"]).status_code == 200
    assert data_fingerprint() != changed


def test_search_is_unchanged_by_preview_and_updated_after_undo(
    access, graph, monkeypatch
):
    from django.apps import apps as django_apps
    from django.test import TestCase
    from core.search_signals import CommitSignalProcessor

    processor = django_apps.get_app_config("haystack").signal_processor
    assert isinstance(processor, CommitSignalProcessor)
    called = []
    monkeypatch.setattr(
        processor,
        "synchronize",
        lambda sender, pk, using="default": called.append((sender, pk)),
    )
    client, _, _ = access
    operation = update("debaters", graph["real"], last_name="Corrected")
    with TestCase.captureOnCommitCallbacks(execute=True):
        preview = post(client, [operation], dry_run=True)
    assert preview.status_code == 200, preview.content
    assert called == []
    with TestCase.captureOnCommitCallbacks(execute=True):
        committed = post(client, [operation])
    assert committed.status_code == 200, committed.content
    assert (Debater, graph["real"].pk) in called
    called.clear()
    with TestCase.captureOnCommitCallbacks(execute=True):
        reversal = undo(client, committed.json()["id"])
    assert reversal.status_code == 200, reversal.content
    assert (Debater, graph["real"].pk) in called


def test_canonical_merge_and_conflicting_scores_aliases_are_reversible(access, graph):
    client, _, _ = access
    g = graph
    # Shared source names and score indexes exercise deduplication and reindexing.
    DebaterAlias.objects.create(
        debater=g["real"], source_name="F Person", normalized_name="f person"
    )
    RoundStats.objects.create(
        round=g["round"], debater=g["real"], speaks="28.0000", score_index=1
    )
    Debater.all_objects.filter(pk=g["fake"].pk).update(synthetic=False)
    g["fake"].refresh_from_db()
    before = graph_snapshot()
    response = post(
        client,
        [
            dict(
                op="merge_debaters",
                source_id=g["fake"].pk,
                target_id=g["real"].pk,
                source_revision=public_row("debaters", g["fake"])["revision"],
                target_revision=public_row("debaters", g["real"])["revision"],
            )
        ],
    )
    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["source_id"] == g["fake"].pk
    assert list(
        RoundStats.objects.filter(debater=g["real"])
        .order_by("score_index")
        .values_list("score_index", flat=True)
    ) == [1, 2]
    result = undo(client, response.json()["id"])
    assert result.status_code == 200, result.content
    assert graph_snapshot() == before


def test_malformed_payloads_and_public_proxy_do_not_bypass_controls(access, graph):
    client, credential, _ = access
    assert client.get(BASE).get("Cache-Control") == "no-store, private"
    for payload in [
        [],
        {},
        {
            "request_id": str(uuid.uuid4()),
            "reason": "x",
            "dryRun": True,
            "operations": [],
        },
    ]:
        assert client.post(BASE + "batches/", payload, format="json").status_code == 400
    assert post(client, [], dry_run="true").status_code == 400
    assert (
        post(client, [update("rounds", graph["round"], victor=999)]).status_code == 400
    )
    public = APIClient()
    public.force_login(credential.created_by)
    assert (
        public.get("/llm/", {"endpoint": BASE + "records/rounds/"}).status_code == 401
    )
    assert client.get(BASE + "records/rounds/?limit=0").status_code == 400
    assert client.get(BASE + "records/rounds/?gov_id__regex=.*").status_code == 400


def test_revocation_is_rechecked_inside_write_transaction(access, graph):
    from core.utils.agent_data import execute_batch
    from rest_framework.exceptions import PermissionDenied

    _, credential, _ = access
    AgentCredential.objects.filter(pk=credential.pk).update(revoked_at=timezone.now())
    with pytest.raises(PermissionDenied):
        execute_batch(
            credential,
            dict(
                request_id=str(uuid.uuid4()),
                reason="stale access",
                operations=[update("rounds", graph["round"], victor=Round.OPP)],
            ),
        )
    graph["round"].refresh_from_db()
    assert graph["round"].victor == Round.GOV


def test_replacing_synthetic_team_members_is_complete_and_reversible(access, graph):
    client, _, _ = access
    before = graph_snapshot()
    result = post(client, [update("teams", graph["gov"], debaters=[graph["real"].pk])])
    assert result.status_code == 200, result.content
    through = Team.debaters.through
    assert list(
        through.objects.filter(team_id=graph["gov"].pk).values_list(
            "debater_id", flat=True
        )
    ) == [graph["real"].pk]
    assert undo(client, result.json()["id"]).status_code == 200
    assert graph_snapshot() == before


def test_empty_create_and_bulk_references_for_missing_provenance(access, graph):
    client, _, _ = access
    before = graph_snapshot()
    response = post(
        client,
        [
            dict(op="create", resource="import_batches", data={}, ref="batch"),
            dict(
                op="create",
                resource="imports",
                data=dict(
                    batch_id="$batch",
                    tournament_id=graph["tournament"].pk,
                    import_type="other",
                    original_file_name="verified.json",
                ),
            ),
        ],
    )
    assert response.status_code == 200, response.content
    assert (
        response.json()["results"][1]["batch_id"] == response.json()["results"][0]["id"]
    )
    assert undo(client, response.json()["id"]).status_code == 200
    assert graph_snapshot() == before


def test_admin_can_issue_token_once_and_revoke_through_browser(access):
    client, credential, _ = access
    client.force_login(credential.created_by)
    result = client.post(
        "/admin/core/agentcredential/add/",
        dict(
            name="browser-agent",
            can_write="on",
            expires_at_0=(timezone.now() + timedelta(days=2)).date().isoformat(),
            expires_at_1="12:00:00",
            _save="Save",
        ),
    )
    assert result.status_code == 200, result.content
    issued = AgentCredential.objects.get(name="browser-agent")
    secret = result.context_data["token"]
    assert secret.startswith("br_agent_")
    assert issued.can_write
    assert issued.created_by_id == credential.created_by_id
    assert "no-store" in result.get("Cache-Control") and "private" in result.get(
        "Cache-Control"
    )
    detail = client.get(f"/admin/core/agentcredential/{issued.pk}/change/")
    assert secret.encode() not in detail.content
    result = client.post(
        "/admin/core/agentcredential/",
        dict(action="revoke_credentials", _selected_action=[issued.pk], index="0"),
    )
    assert result.status_code == 302, result.content
    issued.refresh_from_db()
    assert issued.revoked_at


@pytest.mark.skipif(
    "postgresql" not in settings.DATABASES["default"]["ENGINE"],
    reason="PostgreSQL table-lock integration test",
)
@pytest.mark.django_db(transaction=True)
def test_postgres_serializes_ordinary_writers_with_agent_batch(
    access, graph, monkeypatch
):
    from django.db import connections
    from threading import Event, Thread
    from core.utils import agent_data

    client, _, _ = access
    started, finished = Event(), Event()
    errors = []

    def ordinary_writer():
        try:
            started.set()
            Round.objects.filter(pk=graph["round"].pk).update(round_number=99)
            finished.set()
        except Exception as exc:  # capture thread failures for the assertion below
            errors.append(exc)
        finally:
            connections.close_all()

    worker = Thread(target=ordinary_writer, daemon=True)
    original = agent_data.mutate

    def inspect_lock(*args, **kwargs):
        worker.start()
        assert started.wait(2)
        assert not finished.wait(0.2)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_data, "mutate", inspect_lock)
    result = post(client, [update("rounds", graph["round"], victor=Round.OPP)])
    worker.join(timeout=5)
    assert result.status_code == 200, result.content
    assert not errors
    assert finished.is_set()
    graph["round"].refresh_from_db()
    assert graph["round"].victor == Round.OPP
    assert graph["round"].round_number == 99
    assert undo(client, result.json()["id"]).status_code == 409


def test_tournament_without_host_can_be_corrected(access, graph):
    client, _, _ = access
    Tournament.objects.filter(pk=graph["tournament"].pk).update(host=None)
    graph["tournament"].refresh_from_db()
    before = graph_snapshot()
    result = post(
        client, [update("tournaments", graph["tournament"], date="2025-10-02")]
    )
    assert result.status_code == 200, result.content
    graph["tournament"].refresh_from_db()
    assert graph["tournament"].date == date(2025, 10, 2)
    assert undo(client, result.json()["id"]).status_code == 200
    assert graph_snapshot() == before
