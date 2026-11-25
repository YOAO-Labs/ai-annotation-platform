"""Ordered sparse state, exact recovery scopes and visible saved-only diagnostics."""

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset
from app.db.models.dataset_source import DatasetSource
from app.db.models.storage_fault import StorageFaultState as State
from app.services import storage_fault_events as events, storage_fault_state as states
from app.services import storage_location as locations
from app.services import storage_observation_rates as rates
from tests.test_dataset_content import (
    change_content_location,
    content_world as content_world,
    headers,
)
from tests.test_directory_policies import object_store as object_store
from tests.test_storage_observation_rates_db import committed_world as committed_world
from tests.test_workspace_consumer_profile_native import _grants
from tests.test_workspace_project_authority_locking import _api_url


@pytest.fixture
async def world(content_world, test_engine):
    location_id = content_world[4].id
    yield content_world
    async with test_engine.begin() as connection:
        await connection.execute(delete(State).where(State.location_id == location_id))
        await connection.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
        await connection.execute(
            delete(AuditLog).where(
                AuditLog.target_id == str(location_id),
                AuditLog.action.in_([states.ACTION, states.RECOVERED_ACTION]),
            )
        )


async def token(db, world, **changes):
    _, _, actor, _, location, source, run, entry = world
    values = dict(
        location=location,
        source=source,
        operation="scan_content",
        actor_id=actor.id,
        node_id=locations.api_node_id(),
        anchor=location.config["registration_anchor"],
        maintenance_revision=run.snapshot.get("maintenance_revision", 1),
        path_digest=entry.path_digest,
        expected="a" * 64,
        target=["source", entry.relative_path],
        run_id=run.id,
        job_id=run.async_job_id,
    )
    values.update(changes)
    result = await events.prepare(db, **values)
    assert result is not None
    return result


async def saved(engine, location):
    async with engine.connect() as connection:
        return (
            (
                await connection.execute(
                    select(State.__table__).where(State.location_id == location.id)
                )
            )
            .mappings()
            .all()
        )


async def transitions(engine, location):
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(AuditLog.action, AuditLog.detail_json)
                .where(
                    AuditLog.target_id == str(location.id),
                    AuditLog.action.in_([states.ACTION, states.RECOVERED_ACTION]),
                )
                .order_by(AuditLog.id)
            )
        ).all()


async def test_sparse_ordered_dedup_recovery_and_reopened_watermark(
    world, db_session, test_engine
):
    one = await token(db_session, world)
    await events.recover(db_session, one)
    assert not await saved(test_engine, world[4])
    assert await states.record(
        db_session, one, ("root_unavailable", "root_not_configured")
    )
    assert await states.record(
        db_session, one, ("root_unavailable", "root_not_configured")
    )
    two = await token(db_session, world)
    assert await states.record(
        db_session, two, ("root_unavailable", "root_not_configured")
    )
    assert (await saved(test_engine, world[4]))[0]["failure_count"] == 2
    assert len(await transitions(test_engine, world[4])) == 1
    three = await token(db_session, world, operation="reference_read")
    await events.recover(db_session, three)
    recovered = (await saved(test_engine, world[4]))[0]
    assert (
        recovered["state"] == "recovered" and recovered["operation"] == "reference_read"
    )
    assert recovered["recovered_sequence"] == three.sequence
    await states.record(db_session, two, ("root_unavailable", "root_not_configured"))
    four = await token(db_session, world)
    await states.record(db_session, four, ("root_unavailable", "root_not_configured"))
    current = (await saved(test_engine, world[4]))[0]
    assert current["state"] == "active" and current["failure_count"] == 3
    assert (
        current["recovered_sequence"] == three.sequence
        and current["recovered_at"] == recovered["recovered_at"]
    )
    assert [
        row.detail_json["transition"]
        for row in await transitions(test_engine, world[4])
    ] == ["opened", "recovered", "reopened"]


async def test_late_first_failure_remains_conservative_until_next_success(
    world, db_session, test_engine
):
    failed = await token(db_session, world)
    healthy = await token(db_session, world)
    await events.recover(db_session, healthy)
    await states.record(db_session, failed, ("access_denied", "permission_denied"))
    assert (await saved(test_engine, world[4]))[0]["state"] == "active"
    await events.recover(db_session, await token(db_session, world))
    assert (await saved(test_engine, world[4]))[0]["state"] == "recovered"


async def test_reservation_binds_order_scope_and_evidence(
    world, db_session, test_engine
):
    observation = await token(db_session, world)
    next_observation = await token(db_session, world)
    assert observation.sequence < next_observation.sequence
    failure = ("root_unavailable", "root_not_configured")
    for altered in (
        replace(observation, sequence=next_observation.sequence),
        replace(observation, source_id=uuid.uuid4()),
        replace(observation, scopes=next_observation.scopes | {"access_denied": {}}),
        replace(
            observation, evidence={**observation.evidence, "job_id": str(uuid.uuid4())}
        ),
    ):
        assert not await states.record(db_session, altered, failure)
    assert not await saved(test_engine, world[4])
    assert await states.record(db_session, observation, failure)
    assert (await saved(test_engine, world[4]))[0]["last_sequence"] == (
        observation.sequence
    )


@pytest.mark.parametrize(
    "category,changes",
    [
        ("access_denied", {"target": "neighbor"}),
        ("access_denied", {"operation": "reference_read"}),
        ("source_changed", {"path_digest": "b" * 64}),
        ("source_changed", {"expected": "b" * 64}),
        ("root_unavailable", {"node_id": "worker:other@test"}),
        ("root_unavailable", {"anchor": ["other-root"]}),
        ("root_unavailable", {"maintenance_revision": 22}),
        ("preview_failed", {"asset_id": uuid.UUID(int=333)}),
        ("preview_failed", {"expected": "b" * 64}),
        ("preview_failed", {"preview_spec": "other-spec"}),
    ],
)
async def test_neighbor_operation_content_node_and_dependency_cannot_recover(
    world, db_session, test_engine, category, changes
):
    base = (
        {"preview_spec": "webp-256-exif-q80", "asset_id": uuid.UUID(int=222)}
        if category == "preview_failed"
        else {}
    )
    first = await token(db_session, world, **base)
    await states.record(db_session, first, (category, category))
    other = await token(db_session, world, **{**base, **changes})
    await events.recover(db_session, other)
    assert (await saved(test_engine, world[4]))[0]["state"] == "active"
    await events.recover(db_session, await token(db_session, world, **base))
    assert (await saved(test_engine, world[4]))[0]["state"] == "recovered"


async def test_atomic_audit_failure_does_not_poison_or_commit_caller(
    world, db_session, test_engine, monkeypatch
):
    observation = await token(db_session, world)
    monkeypatch.setattr(
        states, "_audit", AsyncMock(side_effect=RuntimeError("private path"))
    )
    assert not await states.record(
        db_session, observation, ("root_unavailable", "root_not_configured")
    )
    assert not await saved(test_engine, world[4])
    assert await db_session.scalar(select(1)) == 1
    async with test_engine.connect() as other:
        assert (
            await other.scalar(select(Dataset.id).where(Dataset.id == world[3].id))
            is None
        )


async def test_scope_configuration_changes_are_historical_not_recovery(
    world, db_session, test_engine
):
    first = await token(db_session, world)
    await states.record(db_session, first, ("root_unavailable", "root_not_configured"))
    world[5].config_revision += 1
    await events.recover(db_session, await token(db_session, world))
    assert (await saved(test_engine, world[4]))[0]["state"] == "active"
    page = await states.visible_page(
        db_session, world[4].id, world[2], state="active", limit=20, offset=0
    )
    assert page["items"][0]["scope_status"] == "historical"


async def test_get_pages_and_counts_share_current_source_visibility_without_io(
    world,
    db_session,
    httpx_client,
    super_admin,
    project_admin,
    annotator,
    test_engine,
    monkeypatch,
):
    from app.services import reference_storage, content_snapshot

    world = await change_content_location(db_session, world, scope="global")
    location = world[4]
    await states.record(
        db_session,
        await token(db_session, world),
        ("root_unavailable", "root_not_configured"),
    )
    dataset = Dataset(
        display_id="fault-" + uuid.uuid4().hex[:10],
        name="Owned",
        created_by=project_admin[0].id,
    )
    db_session.add(dataset)
    await db_session.flush()
    source = DatasetSource(
        dataset_id=dataset.id,
        location_id=location.id,
        name="Owned source",
        config=deepcopy(world[5].config),
        config_revision=1,
    )
    db_session.add(source)
    await db_session.flush()
    await states.record(
        db_session,
        await token(db_session, world, source=source),
        ("root_unavailable", "root_not_configured"),
    )
    missing_source = DatasetSource(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        location_id=location.id,
        name="Uncommitted source snapshot",
        config=deepcopy(source.config),
        config_revision=1,
    )
    orphan = await token(db_session, world, source=missing_source)
    await states.record(db_session, orphan, ("root_unavailable", "root_not_configured"))
    monkeypatch.setattr(
        reference_storage,
        "configuration",
        AsyncMock(side_effect=AssertionError("GET must not touch source")),
    )
    monkeypatch.setattr(
        content_snapshot,
        "capture_from_location",
        AsyncMock(side_effect=AssertionError("GET must not capture")),
    )
    path = f"/api/v1/storage-locations/{location.id}/faults"
    all_rows = await httpx_client.get(
        path, params={"state": "all", "limit": 1}, headers=headers(super_admin)
    )
    assert all_rows.status_code == 200, all_rows.text
    assert (
        all_rows.json()["total"] == 3
        and all_rows.json()["counts"] == {"active": 3, "recovered": 0}
        and len(all_rows.json()["items"]) == 1
    )
    unpaged = await httpx_client.get(path, headers=headers(super_admin))
    assert unpaged.status_code == 200, unpaged.text
    assert str(world[6].async_job_id) in {
        item["job_id"] for item in unpaged.json()["items"]
    }
    scoped = await httpx_client.get(path, headers=headers(project_admin))
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["total"] == 1 and scoped.json()["counts"]["active"] == 1
    assert scoped.json()["items"][0]["source_id"] == str(source.id)
    assert scoped.json()["items"][0]["job_id"] is None
    assert "scope_facts" not in scoped.text and "scope_key" not in scoped.text
    assert (await httpx_client.get(path, headers=headers(annotator))).status_code == 403
    assert (
        await httpx_client.get(
            path, params={"limit": 101}, headers=headers(super_admin)
        )
    ).status_code == 422


async def test_actual_same_frozen_root_duplicate_failure_then_full_read_recovery(
    world, db_session, test_engine
):
    from pathlib import Path
    from app.config import settings
    from app.services import dataset_content
    from app.services.filesystem_storage import FilesystemStorageError

    snapshot = await dataset_content.scan_snapshot(
        db_session, world[6].id, world[7].path_digest, world[2], verify=True
    )
    snapshot.close()
    configured = Path(settings.storage_filesystem_roots["test"])
    missing = configured.with_name(configured.name + "-temporarily-unavailable")
    configured.rename(missing)
    try:
        for _ in range(2):
            with pytest.raises(FilesystemStorageError):
                await dataset_content.scan_snapshot(
                    db_session, world[6].id, world[7].path_digest, world[2], verify=True
                )
        active = (await saved(test_engine, world[4]))[0]
        assert active["state"] == "active" and active["failure_count"] == 2
    finally:
        missing.rename(configured)
    snapshot = await dataset_content.scan_snapshot(
        db_session, world[6].id, world[7].path_digest, world[2], verify=True
    )
    snapshot.close()
    current = (await saved(test_engine, world[4]))[0]
    assert current["state"] == "recovered" and current["failure_count"] == 2
    assert len(await transitions(test_engine, world[4])) == 2


async def test_unverified_refresh_does_not_recover_unchecked_old_observation(
    world, db_session, test_engine
):
    from app.services import dataset_content

    entry = world[7]
    old = await token(db_session, world, expected=deepcopy(entry.observed))
    await states.record(db_session, old, ("source_changed", "source_changed"))
    (world[0] / entry.relative_path).write_bytes(world[1] + b"new content")
    snapshot = await dataset_content.scan_snapshot(
        db_session,
        world[6].id,
        entry.path_digest,
        world[2],
        verify=True,
        refresh_unverified=True,
    )
    snapshot.close()
    assert (await saved(test_engine, world[4]))[0]["state"] == "active"


async def test_get_role_downgrade_rejects_previously_broader_page(
    world, db_session, httpx_client, super_admin, monkeypatch
):
    original = states.visible_page
    await change_content_location(db_session, world, scope="global")

    async def downgrade(*args, **kwargs):
        result = await original(*args, **kwargs)
        super_admin[0].role = "project_admin"
        await db_session.flush()
        return result

    monkeypatch.setattr(states, "visible_page", downgrade)
    response = await httpx_client.get(
        f"/api/v1/storage-locations/{world[4].id}/faults", headers=headers(super_admin)
    )
    assert (
        response.status_code == 403
        and response.json()["detail"]["code"] == "access_revoked"
    )


async def test_original_cache_hit_does_not_recover_source_fault(
    world, db_session, test_engine, monkeypatch
):
    from app.services import dataset_content, dataset_references, original_cache

    result = await dataset_references.register_entry(
        db_session, world[6].id, world[7].path_digest, world[2]
    )
    snapshot = await dataset_content.scan_snapshot(
        db_session, world[6].id, world[7].path_digest, world[2], verify=True
    )
    world[5].cache_policy = "on_demand"
    world = await change_content_location(
        db_session, world, cache_policy="on_demand", original_cache_quota_bytes=1000000
    )
    failure = await token(db_session, world, operation="reference_read")
    await states.record(
        db_session, failure, ("root_unavailable", "root_not_configured")
    )
    monkeypatch.setattr(dataset_references, "_cache_factory", lambda _db: object())
    monkeypatch.setattr(original_cache, "read", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        dataset_references,
        "reference_source_context",
        AsyncMock(side_effect=AssertionError("cache hit must not open source")),
    )
    returned = await dataset_references.reference_snapshot(
        db_session, result["item_id"], world[2]
    )
    returned.close()
    assert (await saved(test_engine, world[4]))[0]["state"] == "active"


async def test_preview_ready_before_refused_final_publish_cannot_recover(
    world, db_session, test_engine, monkeypatch, object_store
):
    from app.services import dataset_references, directory_previews
    from app.services.content_snapshot import ContentError

    source, run, entry, actor = world[5], world[6], world[7], world[2]
    source.cache_policy = "preview"
    original = directory_previews.encode
    monkeypatch.setattr(
        directory_previews,
        "encode",
        AsyncMock(side_effect=RuntimeError("private encoder path")),
    )
    await dataset_references.register_entry(
        db_session, run.id, entry.path_digest, actor
    )
    assert (await saved(test_engine, world[4]))[0]["category"] == "preview_failed"
    monkeypatch.setattr(directory_previews, "encode", original)

    async def refuse(_db, _result):
        assert (await saved(test_engine, world[4]))[0]["state"] == "active"
        raise ContentError("publication_refused")

    with pytest.raises(ContentError, match="publication_refused"):
        await dataset_references.register_entry(
            db_session, run.id, entry.path_digest, actor, on_result=refuse
        )
    assert (await saved(test_engine, world[4]))[0]["state"] == "active"


async def test_verified_cached_preview_token_recovers_only_after_final_commit(
    world, db_session, test_engine, monkeypatch, object_store
):
    from app.services import dataset_references, directory_previews

    source, run, entry, actor = world[5], world[6], world[7], world[2]
    source.cache_policy = "preview"
    generated = []
    original_generate = directory_previews.generate

    async def retain_row(*args, **kwargs):
        row = await original_generate(*args, **kwargs)
        generated.append(row)
        return row

    monkeypatch.setattr(directory_previews, "generate", retain_row)
    await dataset_references.register_entry(
        db_session, run.id, entry.path_digest, actor
    )
    row = generated[-1]
    captured = row._fault_observation
    dependencies = captured.scopes["preview_failed"]["facts"]["dependencies"]
    monkeypatch.setattr(states, "preview_facts", Mock(return_value=dependencies))
    await states.record(
        db_session, captured, ("preview_failed", "preview_generation_failed")
    )
    monkeypatch.setattr(
        directory_previews,
        "encode",
        AsyncMock(side_effect=AssertionError("verified cache must not encode")),
    )

    async def before_commit(_db, _result):
        assert (await saved(test_engine, world[4]))[0]["state"] == "active"

    await dataset_references.register_entry(
        db_session, run.id, entry.path_digest, actor, on_result=before_commit
    )
    assert (await saved(test_engine, world[4]))[0]["state"] == "recovered"
    directory_previews.encode.assert_not_awaited()


async def test_observer_metadata_sql_failure_keeps_business_read_and_transaction(
    world, db_session, test_engine, monkeypatch
):
    from app.services import dataset_content, content_snapshot

    async def broken(independent, *_args):
        await independent.execute(text("SELECT observation_missing_column"))

    monkeypatch.setattr(states, "_read_facts", broken)
    capture = AsyncMock(wraps=content_snapshot.capture_from_location)
    monkeypatch.setattr(content_snapshot, "capture_from_location", capture)
    snapshot = await dataset_content.scan_snapshot(
        db_session, world[6].id, world[7].path_digest, world[2], verify=True
    )
    snapshot.close()
    assert capture.await_count == 1 and await db_session.scalar(select(1)) == 1
    async with test_engine.connect() as connection:
        assert (
            await connection.scalar(select(Dataset.id).where(Dataset.id == world[3].id))
            is None
        )


@pytest.mark.parametrize(
    "scopes,revoke,expected",
    [
        (["datasets:read"], False, 200),
        (["datasets:write"], False, 403),
        (["datasets:read"], True, 403),
    ],
)
async def test_get_key_scope_and_final_revocation(
    world, db_session, httpx_client, super_admin, monkeypatch, scopes, revoke, expected
):
    from datetime import datetime, timezone
    from app.core.security import hash_password
    from app.db.models.api_key import ApiKey

    raw = "ak_" + uuid.uuid4().hex
    key = ApiKey(
        user_id=super_admin[0].id,
        name="fault-reader",
        key_prefix=raw[:12],
        key_hash=hash_password(raw),
        scopes=scopes,
    )
    db_session.add(key)
    await db_session.flush()
    if revoke:
        original = states.visible_page

        async def revoked(*args, **kwargs):
            result = await original(*args, **kwargs)
            key.revoked_at = datetime.now(timezone.utc)
            await db_session.flush()
            return result

        monkeypatch.setattr(states, "visible_page", revoked)
    response = await httpx_client.get(
        f"/api/v1/storage-locations/{world[4].id}/faults",
        headers={"Authorization": "Bearer " + raw},
    )
    assert response.status_code == expected, response.text


async def test_request_ids_are_safe_evidence_and_never_dedup_keys(
    world, db_session, test_engine
):
    from app.middleware.request_id import request_id_var

    marker = request_id_var.set("request-1")
    try:
        first = await token(db_session, world)
    finally:
        request_id_var.reset(marker)
    marker = request_id_var.set("/private/invalid-request")
    try:
        invalid = await token(db_session, world)
    finally:
        request_id_var.reset(marker)
    assert {key: scope["key"] for key, scope in first.scopes.items()} == {
        key: scope["key"] for key, scope in invalid.scopes.items()
    }
    assert invalid.request_id is None and "request_id" not in invalid.evidence
    await states.record(db_session, first, ("root_unavailable", "root_not_configured"))
    assert (await saved(test_engine, world[4]))[0]["evidence"][
        "request_id"
    ] == "request-1"


async def test_storage_fault_capabilities_use_existing_safe_owners(db_session):
    expected = {
        "yoao_assets_v1": {
            "reserve_storage_fault_observation",
            "record_storage_fault_observation",
            "read_storage_fault_facts",
            "storage_fault_location_visible",
            "storage_fault_page",
            "record_storage_observation_rate",
            "read_storage_observation_rates",
        },
        "yoao_jobs_v1": {"read_storage_fault_job"},
    }
    rows = (
        await db_session.execute(
            text(
                """
                SELECT ns.nspname AS namespace, p.proname AS name, p.prosecdef,
                       p.proconfig,
                       p.proowner = anchor.proowner AS expected_owner,
                       NOT EXISTS (
                           SELECT FROM pg_catalog.aclexplode(
                               COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
                           ) AS acl WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                       ) AS no_public_execute
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS ns ON ns.oid = p.pronamespace
                JOIN pg_catalog.pg_proc AS anchor ON anchor.oid =
                    CASE WHEN ns.nspname = 'yoao_assets_v1'
                         THEN 'yoao_assets_v1.read_dataset_source_authority(uuid,boolean)'::regprocedure
                         ELSE 'yoao_jobs_v1.read_job(uuid)'::regprocedure END
                WHERE ns.nspname IN ('yoao_assets_v1', 'yoao_jobs_v1')
                  AND p.proname = ANY(CAST(:names AS text[]))
                """
            ),
            {"names": sorted(set.union(*expected.values()))},
        )
    ).all()
    assert {(row.namespace, row.name) for row in rows} == {
        (namespace, name) for namespace, names in expected.items() for name in names
    }
    for row in rows:
        assert row.prosecdef and row.expected_owner and row.no_public_execute
        assert row.proconfig == ["search_path=pg_catalog, pg_temp"]


async def _replay_fault_grants(test_engine, role):
    directory = Path(__file__).resolve().parents[3] / "infra/docker"
    async with test_engine.begin() as connection:
        target = connection.dialect.identifier_preparer.quote_identifier(role)
        for privileges in (
            "SELECT, UPDATE ON public.storage_fault_states",
            "SELECT (last_sequence) ON public.storage_fault_states",
            "UPDATE (last_observed_at) ON public.storage_observation_buckets",
            "SELECT (secret) ON yoao_assets_v1.storage_fault_observation_key",
            "SELECT, UPDATE, USAGE ON SEQUENCE public.storage_fault_observation_seq",
            "EXECUTE ON FUNCTION yoao_jobs_v1.read_storage_fault_job(uuid)",
        ):
            await connection.exec_driver_sql(f"GRANT {privileges} TO {target}")
    remote = role == "identity_api_consumer"
    script = "identity-consumer-grants.sql" if remote else "lan-production-grants.sql"
    variables = {"identity_avatar_runtime": "1"} if remote else {}
    applied = await _grants(test_engine, directory / script, role, **variables)
    assert applied.returncode == 0, applied.stderr


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable verifier's real runtime LOGINs",
)
@pytest.mark.parametrize(
    ("selector", "role"),
    [
        (
            "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
            "identity_embedded_api_consumer",
        ),
        ("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer"),
        ("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime"),
    ],
    ids=("lan-embedded", "lan-isolated-issuer", "worktree"),
)
async def test_real_login_observes_and_reads_without_private_storage_access(
    test_engine, app_module, committed_world, role, selector, tmp_path, monkeypatch
):
    from contextlib import AsyncExitStack
    import secrets
    import sys

    from app.config import settings
    from app.core.security import create_access_token
    from app.deps import get_db
    from tests.factory import DEFAULT_PASSWORD
    from tests.test_identity_admission_live import _start_identity
    from tests.test_identity_sessions_live import _login_redis

    actors, location, sources = committed_world
    actor, source = actors[1], sources[0]
    expected = _api_url(test_engine)
    url = make_url(os.environ[selector])
    assert (url.host, url.port, url.database, url.username) == (
        expected.host,
        expected.port,
        expected.database,
        role,
    )
    await _replay_fault_grants(test_engine, role)
    engine = create_async_engine(url, hide_parameters=True)

    async def runtime_db():
        async with AsyncSession(engine) as db:
            yield db

    remote = role == "identity_api_consumer"
    try:
        async with AsyncSession(engine) as db:
            assert await db.scalar(text("SELECT current_user")) == role
            for statement in (
                "SELECT last_sequence FROM public.storage_fault_states",
                "UPDATE public.storage_fault_states SET operation = 'scan' WHERE false",
                "SELECT last_observed_at FROM public.storage_observation_buckets",
                "DELETE FROM public.storage_observation_buckets WHERE false",
                "SELECT nextval('public.storage_fault_observation_seq')",
                "SELECT secret FROM yoao_assets_v1.storage_fault_observation_key",
                "SELECT * FROM yoao_jobs_v1.read_storage_fault_job("
                "CAST('00000000-0000-0000-0000-000000000001' AS uuid))",
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(text(statement))
                assert denied.value.orig.sqlstate == "42501"

            await db.execute(
                text(
                    "SELECT yoao_assets_v1.record_storage_observation_rate("
                    ":location_id, :source_id, 'preview_generate', false)"
                ),
                {"location_id": location.id, "source_id": source.id},
            )
            values = {
                "location_id": location.id,
                "source_id": source.id,
                "operation": "reference_read",
                "actor_id": actor.id,
                "node_id": "worker:fault-acl",
                "maintenance_revision": 1,
                "expected_context": {"source_revision": 1, "location_revision": 1},
            }
            early = await events.prepare(db, **values)
            late = await events.prepare(db, **values)
            assert early is not None and late is not None
            assert early.sequence < late.sequence
            failure = ("root_unavailable", "root_not_configured")
            assert await states.record(db, late, failure)
            assert await states.record(db, early, failure)
            assert await rates.finish(
                db, rates.begin(location.id, source.id, "original_read"), "success"
            )
            assert [
                row.detail_json["transition"]
                for row in await transitions(test_engine, location)
            ] == ["opened"]
            await db.rollback()

        app_module.dependency_overrides[get_db] = runtime_db
        async with (
            AsyncExitStack() as stack,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=app_module, client=("127.0.0.42", 12345)
                ),
                base_url="http://test",
            ) as client,
        ):
            if remote:
                patch = stack.enter_context(monkeypatch.context())
                patch.setenv("AAP_IDENTITY_RUNTIME_PYTHON", sys.executable)
                redis_url, _ = await stack.enter_async_context(_login_redis())
                control = secrets.token_urlsafe(32)
                issuer = _start_identity(
                    test_engine,
                    tmp_path,
                    secrets.token_urlsafe(32),
                    control,
                    enable_keys=False,
                    enable_accounts=False,
                    enable_presence=False,
                    login_redis_url=redis_url,
                )
                stack.callback(issuer.stop)
                patch.setattr(settings, "identity_url", issuer.origin)
                patch.setattr(settings, "identity_token", control)
            path = f"/api/v1/storage-locations/{location.id}/faults"
            for requested_actor, total in ((actor, 1), (actors[2], 0), (actors[0], 1)):
                if remote:
                    login = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "email": requested_actor.email,
                            "password": DEFAULT_PASSWORD,
                        },
                    )
                    assert login.status_code == 200
                    jwt = login.json()["access_token"]
                else:
                    jwt = create_access_token(
                        str(requested_actor.id), requested_actor.role
                    )
                response = await client.get(
                    path, headers={"Authorization": f"Bearer {jwt}"}
                )
                assert response.status_code == 200, response.text
                page = response.json()
                assert page["total"] == total
                assert page["rates"]["original_read"]["total_count"] == total
                assert page["rates"]["preview_generate"]["total_count"] == 0
                if total:
                    assert page["items"][0]["scope_status"] == "current"
                    assert page["items"][0]["failure_count"] == 1
                assert "scope_facts" not in response.text
                assert "scope_key" not in response.text

        async with AsyncSession(engine) as db:
            recovered = await events.prepare(db, **values)
            assert recovered is not None and recovered.sequence > late.sequence
            await events.recover(db, recovered)
        assert (await saved(test_engine, location))[0]["state"] == "recovered"
        assert [
            row.detail_json["transition"]
            for row in await transitions(test_engine, location)
        ] == ["opened", "recovered"]
    finally:
        app_module.dependency_overrides.pop(get_db, None)
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await connection.execute(
                delete(AuditLog).where(
                    AuditLog.target_id == str(location.id),
                    AuditLog.action.in_([states.ACTION, states.RECOVERED_ACTION]),
                )
            )
            if remote:
                await connection.execute(
                    delete(AuditLog).where(
                        AuditLog.actor_id.in_([user.id for user in actors]),
                        AuditLog.action == "auth.login",
                    )
                )
