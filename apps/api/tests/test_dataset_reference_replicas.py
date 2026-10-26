"""Directed replica grants preserve current authority and immutable business identity."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import shutil
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.api_key import ApiKey
from app.db.models.dataset_asset import DatasetAsset
from app.db.models.dataset_reference_replica import DatasetReferenceReplica as Replica
from app.db.models.dataset_source import DatasetScanEntry
from app.db.models.site_agent import AgentCredential
from app.schemas.dataset_reference_replica import ReferenceReplicaCreate
from app.schemas.dataset_source import DatasetSourceCreate
from app.schemas.storage_location import FilesystemLocationCreate
from app.services import agent_storage, content_snapshot as snapshots
from app.services import (
    dataset_reference_replicas as replicas,
    dataset_references as references,
)
from app.services import dataset_scans as scans, filesystem_storage as fs
from app.services import original_cache, storage_location as locations
from app.services.training_dataset_sources import training_reference_snapshot
from tests.test_agent_offline_availability import attach_agent
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_source_changes import picture, rescan


async def another_copy(db, world, name, data=None):
    root = world[0].parent / name
    root.mkdir()
    data = world[1] if data is None else data
    (root / "copy.png").write_bytes(data)
    actor, dataset = world[2:4]
    location = await locations.create_filesystem(
        db,
        FilesystemLocationCreate(
            name=f"replica-{uuid.uuid4().hex}", root_handle="test"
        ),
    )
    probe = fs.probe_directory(location.config, f"{name}/copy.png")
    for node in ["worker:content@test", locations.api_node_id()]:
        await locations.store_probe(
            db, location, node, probe, datetime.now(timezone.utc)
        )
    source = await scans.create_source(
        db,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name=name,
            relative_path=name,
            node_id="content@test",
            cache_policy="none",
        ),
        actor,
    )
    other = await rescan(db, (root, data, actor, dataset, location, source, None, None))
    entry = await db.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == other[6].id,
            DatasetScanEntry.kind == "file",
        )
    )
    registered = await references.register_entry(
        db, other[6].id, entry.path_digest, actor, node_id="worker:content@test"
    )
    return (*other[:7], entry), registered


@pytest.fixture
async def replica_world(db_session, reference_world):
    registered, project, task, world = reference_world
    other, replica = await another_copy(db_session, world, "replica")
    try:
        yield SimpleNamespace(
            original=world,
            other=other,
            item_id=registered["item_id"],
            replica_item_id=replica["item_id"],
            task=task,
            project=project,
            actor=world[2],
            data=world[1],
        )
    finally:
        for path in world[0].parent.glob("replica*"):
            if path.is_dir():
                shutil.rmtree(path)


async def publish(db, world, *, priority=0):
    result = await replicas.create(
        db,
        world.item_id,
        ReferenceReplicaCreate(
            replica_item_id=world.replica_item_id, priority=priority
        ),
        world.actor,
    )
    await db.commit()
    return result


async def capture(db, world, actor=None, **kwargs):
    return await references.reference_snapshot(
        db, world.item_id, actor or world.actor, **kwargs
    )


async def test_same_sha_is_not_authority_and_publication_preserves_both_identities(
    db_session, replica_world, httpx_client, super_admin, annotator
):
    w = replica_world
    before = {
        item.id: item.file_path
        for item in await db_session.scalars(select(DatasetItem))
    }
    (w.original[0] / "图片 空格.png").unlink()
    with pytest.raises((snapshots.ContentError, fs.FilesystemStorageError)):
        await capture(db_session, w)
    base = f"/api/v1/dataset-items/{w.item_id}/reference-replicas"
    denied = await httpx_client.post(
        base,
        headers=headers(annotator),
        json={"replica_item_id": str(w.replica_item_id)},
    )
    assert denied.status_code == 403
    created = await httpx_client.post(
        base,
        headers=headers(super_admin),
        json={"replica_item_id": str(w.replica_item_id), "priority": 0},
    )
    assert created.status_code == 201, created.text
    assert created.json()["item_id"] == str(w.item_id)
    assert created.json()["replica_item_id"] == str(w.replica_item_id)
    assert "relative_path" not in created.text and str(w.other[0]) not in created.text
    reverse = await replicas.list_replicas(db_session, w.replica_item_id, w.actor)
    assert reverse == {"items": [], "total": 0}
    assert before == {
        item.id: item.file_path
        for item in await db_session.scalars(select(DatasetItem))
    }
    snapshot = await capture(db_session, w, annotator[0], task_id=w.task.id)
    try:
        assert snapshot.file.read() == w.data
    finally:
        snapshot.close()
    assert (await httpx_client.get(base, headers=headers(annotator))).status_code == 403
    # The original task cannot be reused to authorize B as a separate business item.
    with pytest.raises(HTTPException) as error:
        await references.reference_snapshot(
            db_session, w.replica_item_id, annotator[0], task_id=w.task.id
        )
    assert error.value.status_code == 404
    replica_id = created.json()["id"]
    duplicate = await httpx_client.post(
        base,
        headers=headers(super_admin),
        json={"replica_item_id": str(w.replica_item_id)},
    )
    assert (
        duplicate.status_code == 409
        and duplicate.json()["detail"]["code"] == "reference_replica_exists"
    )
    changed = await httpx_client.patch(
        base + "/" + replica_id, headers=headers(super_admin), json={"priority": 1}
    )
    assert changed.status_code == 200 and changed.json()["priority"] == 1
    assert (
        await httpx_client.delete(base + "/" + replica_id, headers=headers(super_admin))
    ).status_code == 204
    with pytest.raises((snapshots.ContentError, fs.FilesystemStorageError)):
        await capture(db_session, w)


async def test_candidates_and_links_have_current_scope_and_total_before_pagination(
    db_session, replica_world, project_admin
):
    w = replica_world
    third, registered = await another_copy(db_session, w.original, "replica-third")
    w.original[3].created_by = project_admin[0].id
    for world in [w.original, third]:
        world[4].config = {
            **world[4].config,
            "scope": "restricted",
            "use_user_ids": [str(project_admin[0].id)],
        }
    w.other[4].config = {**w.other[4].config, "scope": "restricted", "use_user_ids": []}
    await db_session.commit()
    page = await replicas.candidates(db_session, w.item_id, w.actor, limit=1, offset=1)
    assert page["total"] == 2 and len(page["items"]) == 1
    scoped = await replicas.candidates(db_session, w.item_id, project_admin[0])
    assert (
        scoped["total"] == 1 and scoped["items"][0]["item_id"] == registered["item_id"]
    )
    with pytest.raises(HTTPException) as denied:
        await replicas.create(
            db_session,
            w.item_id,
            ReferenceReplicaCreate(replica_item_id=w.replica_item_id),
            project_admin[0],
        )
    assert denied.value.status_code == 404
    await publish(db_session, w)
    remaining = await replicas.candidates(
        db_session, w.item_id, w.actor, limit=1, offset=1
    )
    assert remaining == {"items": [], "total": 1}
    await replicas.create(
        db_session,
        w.item_id,
        ReferenceReplicaCreate(replica_item_id=registered["item_id"]),
        w.actor,
    )
    await db_session.commit()
    page = await replicas.list_replicas(
        db_session, w.item_id, w.actor, limit=1, offset=1
    )
    assert page["total"] == 2 and len(page["items"]) == 1
    expression = replicas.active_replica_exists(DatasetAsset.id, actor=project_admin[0])
    assert await db_session.scalar(
        select(expression).where(DatasetAsset.dataset_item_id == w.item_id)
    )
    third[5].enabled = False
    await db_session.commit()
    assert not await db_session.scalar(
        select(expression).where(DatasetAsset.dataset_item_id == w.item_id)
    )


async def test_other_content_and_database_identity_rebinding_are_rejected(
    db_session, replica_world
):
    w = replica_world
    _, changed = await another_copy(
        db_session, w.original, "replica-blue", picture("blue")
    )
    with pytest.raises(
        snapshots.ContentError, match="reference_replica_content_mismatch"
    ):
        await replicas.create(
            db_session,
            w.item_id,
            ReferenceReplicaCreate(replica_item_id=changed["item_id"]),
            w.actor,
        )
    foreign = Dataset(
        display_id=f"ds-{uuid.uuid4().hex[:10]}",
        name="Other dataset",
        created_by=w.actor.id,
    )
    db_session.add(foreign)
    await db_session.flush()
    _, foreign_copy = await another_copy(
        db_session, (*w.original[:3], foreign, *w.original[4:]), "replica-foreign"
    )
    with pytest.raises(
        snapshots.ContentError, match="reference_replica_content_mismatch"
    ):
        await replicas.create(
            db_session,
            w.item_id,
            ReferenceReplicaCreate(replica_item_id=foreign_copy["item_id"]),
            w.actor,
        )
    linked = await publish(db_session, w)
    blue = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == changed["item_id"])
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Replica)
                .where(Replica.id == linked["id"])
                .values(replica_asset_id=blue.id)
            )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Replica(
                    primary_asset_id=linked["replica_asset_id"],
                    replica_asset_id=blue.id,
                    created_by=w.actor.id,
                )
            )
            await db_session.flush()


async def test_corrupt_preferred_copy_is_excluded_and_falls_back_without_mixed_bytes(
    db_session, replica_world
):
    w = replica_world
    await publish(db_session, w)
    (w.other[0] / "copy.png").write_bytes(picture("blue"))
    snapshot = await capture(db_session, w)
    try:
        assert snapshot.file.read() == w.data
    finally:
        snapshot.close()
    (w.original[0] / "图片 空格.png").unlink()
    with pytest.raises((snapshots.ContentError, fs.FilesystemStorageError)):
        await capture(db_session, w)


@pytest.mark.parametrize("change", ["link", "source", "task", "key"])
async def test_current_grants_are_rechecked_after_complete_capture(
    db_session, replica_world, monkeypatch, annotator, change
):
    w = replica_world
    linked = await publish(db_session, w)
    actual = snapshots.capture_from_location
    captured = []
    key = None
    if change == "key":
        key = ApiKey(
            user_id=annotator[0].id,
            name="Replica read",
            key_prefix="ak_replica",
            key_hash="replica-test-proof",
            scopes=["tasks:read"],
        )
        db_session.add(key)
        await db_session.commit()

    async def changed(*args, **kwargs):
        snapshot = await actual(*args, **kwargs)
        captured.append(snapshot)
        if change == "link":
            await replicas.revoke(db_session, w.item_id, linked["id"], w.actor)
        elif change == "source":
            w.original[5].enabled = False
        elif change == "task":
            w.task.assignee_id = None
        else:
            key.revoked_at = datetime.now(timezone.utc)
        await db_session.commit()
        return snapshot

    monkeypatch.setattr(snapshots, "capture_from_location", changed)
    with pytest.raises((snapshots.ContentError, HTTPException)):
        await capture(
            db_session,
            w,
            annotator[0],
            task_id=w.task.id,
            api_key_id=key.id if key else None,
        )
    assert len(captured) == 1 and captured[0].file.closed


def opened(data):
    @contextmanager
    def reader():
        with io.BytesIO(data) as body:
            yield body, {"size": len(data)}

    return reader


async def make_agent(db, world):
    agent, credential, site = await attach_agent(db, world[4], world[2])
    world[4].config = {**world[4].config, "agent_anchor": world[6].snapshot["anchor"]}
    await db.commit()
    return agent, credential, site


@pytest.mark.parametrize("change", ["root", "credential", "rotate"])
async def test_agent_authority_changes_reject_the_complete_candidate_before_delivery(
    db_session, replica_world, monkeypatch, change
):
    w = replica_world
    await publish(db_session, w)
    agent, credential, _ = await make_agent(db_session, w.other)
    captured = []

    async def remote(*args, **kwargs):
        snapshot = await snapshots.capture(opened(w.data), kwargs["expected_sha256"])
        captured.append(snapshot)
        if change == "root":
            agent.roots = [{"root_handle": "images", "root_revision": 4}]
        elif change == "credential":
            credential.revoked_at = datetime.now(timezone.utc)
        else:
            agent.credential_revision += 1
            db_session.add(
                AgentCredential(
                    agent_id=agent.id,
                    revision=agent.credential_revision,
                    token_digest=uuid.uuid4().hex * 2,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
        await db_session.commit()
        return snapshot

    monkeypatch.setattr(agent_storage, "capture", remote)
    with pytest.raises((snapshots.ContentError, fs.FilesystemStorageError)):
        await capture(db_session, w)
    assert len(captured) == 1 and captured[0].file.closed


async def test_offline_original_uses_online_replica_and_range_remains_full_sha_verified(
    db_session, replica_world, monkeypatch, httpx_client, super_admin
):
    w = replica_world
    await publish(db_session, w, priority=100)
    original_agent, _, _ = await make_agent(db_session, w.original)
    original_agent.last_seen = None
    await make_agent(db_session, w.other)
    attempts = []
    data = w.data

    async def remote(db, config, *args, **kwargs):
        attempts.append(config.location_id)
        return await snapshots.capture(opened(data), kwargs["expected_sha256"])

    monkeypatch.setattr(agent_storage, "capture", remote)
    url = f"/api/v1/dataset-items/{w.item_id}/content"
    sha256 = (await references.reference_context(db_session, w.item_id, w.actor))[
        "sha256"
    ]
    response = await httpx_client.get(
        url,
        params={"sha256": sha256},
        headers={**headers(super_admin), "Range": "bytes=0-1"},
    )
    assert response.status_code == 206 and response.content == w.data[:2], response.text
    assert attempts == [w.other[4].id]
    data = data[:-1] + bytes([data[-1] ^ 1])
    rejected = await httpx_client.get(
        url,
        params={"sha256": sha256},
        headers={**headers(super_admin), "Range": "bytes=0-1"},
    )
    assert rejected.status_code in {409, 503}
    assert "content-range" not in rejected.headers


@pytest.mark.parametrize(
    "candidate_policy,location_policy,retained",
    [
        ("none", "on_demand", False),
        ("on_demand", "preview", False),
        ("on_demand", "on_demand", True),
    ],
)
async def test_retention_obeys_both_original_and_candidate_policies(
    db_session, replica_world, monkeypatch, candidate_policy, location_policy, retained
):
    w = replica_world
    await publish(db_session, w)
    for world in [w.original, w.other]:
        world[4].config = {**world[4].config, "cache_policy": "on_demand"}
        world[5].cache_policy = "on_demand"
    w.other[4].config = {**w.other[4].config, "cache_policy": location_policy}
    w.other[5].cache_policy = candidate_policy
    await db_session.commit()
    retained_contexts = []

    async def miss(*args, **kwargs):
        return None

    async def store(cache, factory, context, snapshot):
        retained_contexts.append(context)

    monkeypatch.setattr(references, "_cache_factory", lambda db: object())
    monkeypatch.setattr(original_cache, "read", miss)
    monkeypatch.setattr(references, "_retain_snapshot", store)
    snapshot = await capture(db_session, w)
    snapshot.close()
    assert bool(retained_contexts) is retained
    if retained:
        assert retained_contexts[0]["source_id"] == w.original[5].id


async def test_freezing_via_replica_keeps_primary_identity_and_lineage(
    db_session, replica_world
):
    w = replica_world
    await publish(db_session, w)
    (w.original[0] / "图片 空格.png").unlink()
    sample = {
        "task_id": str(w.task.id),
        "dataset_id": str(w.original[3].id),
        "dataset_item_id": str(w.item_id),
        "source_key": w.task.file_path,
    }
    snapshot, identity, lineage = await training_reference_snapshot(
        db_session, sample, w.actor, node_id="worker:content@test", freeze_lineage=True
    )
    try:
        assert snapshot.file.read() == w.data
        assert identity["dataset_item_id"] == str(w.item_id)
        assert identity["asset_id"] != str(
            (
                await db_session.scalar(
                    select(DatasetAsset).where(
                        DatasetAsset.dataset_item_id == w.replica_item_id
                    )
                )
            ).id
        )
        assert lineage["source_id"] == str(w.original[5].id)
        assert lineage["scan_run_id"] == str(w.original[6].id)
        assert lineage["source_id"] != str(w.other[5].id)
    finally:
        snapshot.close()


async def test_disabled_primary_cannot_be_repaired_by_an_available_replica(
    db_session, replica_world
):
    w = replica_world
    await publish(db_session, w)
    w.original[5].enabled = False
    await db_session.commit()
    with pytest.raises(snapshots.ContentError, match="source_unavailable"):
        await capture(db_session, w)


async def test_replica_authority_is_not_transitive(db_session, replica_world):
    w = replica_world
    third, registered = await another_copy(db_session, w.original, "replica-third")
    await publish(db_session, w)
    await replicas.create(
        db_session,
        w.replica_item_id,
        ReferenceReplicaCreate(replica_item_id=registered["item_id"], priority=0),
        w.actor,
    )
    await db_session.commit()
    (w.original[0] / "图片 空格.png").unlink()
    (w.other[0] / "copy.png").unlink()
    with pytest.raises((snapshots.ContentError, fs.FilesystemStorageError)):
        await capture(db_session, w)
    snapshot = await references.reference_snapshot(
        db_session, w.replica_item_id, w.actor
    )
    try:
        assert snapshot.file.read() == third[1]
    finally:
        snapshot.close()


async def test_direct_manager_losing_candidate_scope_cannot_publish_captured_bytes(
    db_session, replica_world, project_admin, monkeypatch
):
    w = replica_world
    await publish(db_session, w)
    actor = project_admin[0]
    w.original[3].created_by = actor.id
    for world in [w.original, w.other]:
        world[4].config = {
            **world[4].config,
            "scope": "restricted",
            "use_user_ids": [str(actor.id)],
        }
    await db_session.commit()
    actual, captured = snapshots.capture_from_location, []

    async def revoked(*args, **kwargs):
        snapshot = await actual(*args, **kwargs)
        captured.append(snapshot)
        w.other[4].config = {**w.other[4].config, "use_user_ids": []}
        await db_session.commit()
        return snapshot

    monkeypatch.setattr(snapshots, "capture_from_location", revoked)
    with pytest.raises(HTTPException) as error:
        await capture(db_session, w, actor)
    assert error.value.status_code == 404
    assert len(captured) == 1 and captured[0].file.closed


async def test_original_cache_precedes_unreachable_or_revoked_physical_candidates(
    db_session, replica_world, monkeypatch
):
    w = replica_world
    await publish(db_session, w)
    original_agent, _, _ = await make_agent(db_session, w.original)
    _, credential, _ = await make_agent(db_session, w.other)
    original_agent.last_seen = None
    credential.revoked_at = datetime.now(timezone.utc)
    w.original[4].config = {**w.original[4].config, "cache_policy": "on_demand"}
    w.original[5].cache_policy = "on_demand"
    await db_session.commit()
    reads = []

    async def cached(factory, source_id, asset_id, **kwargs):
        reads.append((source_id, asset_id))
        return await snapshots.capture(opened(w.data))

    async def no_physical_candidates(*args, **kwargs):
        pytest.fail(
            "An authorized original-cache hit must not qualify physical candidates"
        )

    monkeypatch.setattr(references, "_cache_factory", lambda db: object())
    monkeypatch.setattr(original_cache, "read", cached)
    monkeypatch.setattr(replicas, "route_order", no_physical_candidates)
    snapshot = await capture(db_session, w, require_range_support=True)
    try:
        assert snapshot.supports_ranges and snapshot.file.read() == w.data
        assert len(reads) == 1 and reads[0][0] == w.original[5].id
    finally:
        snapshot.close()
