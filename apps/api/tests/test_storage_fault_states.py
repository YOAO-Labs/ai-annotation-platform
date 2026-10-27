"""Ordered sparse state, exact recovery scopes and visible saved-only diagnostics."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, select, text

from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset
from app.db.models.dataset_source import DatasetSource
from app.db.models.storage_fault import StorageFaultState as State
from app.services import storage_fault_events as events, storage_fault_state as states
from app.services import storage_location as locations
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_directory_policies import object_store as object_store


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
    row = await db_session.get(State, (await saved(test_engine, world[4]))[0]["id"])
    assert (
        await states.scope_status(db_session, row, world[4], world[5]) == "historical"
    )


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

    location = world[4]
    location.config = {**location.config, "scope": "global"}
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
    orphan = replace(
        await token(db_session, world),
        source_id=uuid.uuid4(),
        scopes=tuple(
            replace(scope, key=states.digest([scope.key, "orphan"]))
            for scope in (await token(db_session, world)).scopes
        ),
    )
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
    scoped = await httpx_client.get(path, headers=headers(project_admin))
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["total"] == 1 and scoped.json()["counts"]["active"] == 1
    assert scoped.json()["items"][0]["source_id"] == str(source.id)
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
    world[4].config = {**world[4].config, "scope": "global"}

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
    world[4].config = {
        **world[4].config,
        "cache_policy": "on_demand",
        "original_cache_quota_bytes": 1000000,
    }
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
    # The outer SAVEPOINT's new binding is not visible independently. Supply the
    # actual captured binding facts for this exact cached-preview seam test.
    dependencies = captured.scopes[0].facts["dependencies"]
    monkeypatch.setattr(states, "preview_facts", AsyncMock(return_value=dependencies))
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

    monkeypatch.setattr(states, "common_facts", broken)
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
    assert [scope.key for scope in first.scopes] == [
        scope.key for scope in invalid.scopes
    ]
    assert invalid.request_id is None and "request_id" not in invalid.evidence
    await states.record(db_session, first, ("root_unavailable", "root_not_configured"))
    assert (await saved(test_engine, world[4]))[0]["evidence"][
        "request_id"
    ] == "request-1"
