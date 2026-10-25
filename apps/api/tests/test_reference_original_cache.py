"""Authorized consumers choose complete cached bytes without borrowing source access."""

import asyncio
from datetime import datetime, timezone
import hashlib
import threading
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    StorageLocation,
    StorageLocationProbe,
)
from app.db.models.dataset_source import (
    DatasetSource,
    DatasetScanRun,
    DatasetSourceRevision,
    DatasetRegistrationRun,
    DatasetSourceChange,
)
from app.db.models.original_cache import OriginalCacheEntry as Entry
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.services import dataset_references as refs, original_cache as cache
from app.services.content_snapshot import ContentError
from app.services.dataset_content import quality_identity, task_snapshot
from app.services.storage import storage_service
from app.services.training_dataset_sources import training_reference_snapshot
from tests.test_dataset_content import create_content_world
from tests.test_dataset_references import create_reference_world
from tests.test_original_cache import MemoryStorage, snapshot as input_snapshot

_REAL_STORAGE = cache.make_storage


@pytest.fixture
async def referenced_cache(test_engine, monkeypatch, tmp_path):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    users = [
        User(
            id=uuid.uuid4(),
            email=f"cache-ref-{uuid.uuid4()}@example.test",
            name="Cache reference test",
            password_hash="unused",
            role=role,
        )
        for role in ("super_admin", "employee", "employee")
    ]
    async with factory() as db:
        db.add_all(users)
        await db.commit()
        content = await create_content_world(db, users[0], tmp_path, monkeypatch)
        registration, project, task, _ = await create_reference_world(
            db, content, users[1]
        )
        location, source = content[4:6]
        location.config = {
            **location.config,
            "cache_policy": "on_demand",
            "original_cache_quota_bytes": len(content[1]) * 2,
        }
        source.cache_policy = "on_demand"
        await db.commit()
    world = SimpleNamespace(
        factory=factory,
        actor=users[1],
        manager=users[0],
        stranger=users[2],
        project=project,
        task=task,
        item_id=registration["item_id"],
        source_id=source.id,
        location_id=location.id,
        dataset_id=content[3].id,
        content=content,
        data=content[1],
        sha=hashlib.sha256(content[1]).hexdigest(),
        endpoint=storage_service.client.meta.endpoint_url,
        bucket=storage_service.media_cache_bucket,
        objects={},
        puts=[],
        deletions=[],
        clients=[],
        bodies=[],
        fail_put=False,
        corrupt_put=False,
        fail_delete=False,
        put_started=None,
        put_release=None,
        get_started=None,
        get_release=None,
    )
    monkeypatch.setattr(cache, "make_storage", lambda: MemoryStorage(world))
    try:
        yield world
    finally:
        for gate in (world.put_release, world.get_release):
            if gate:
                gate.set()
        async with factory() as db:
            await db.execute(
                delete(Entry).where(Entry.scope_location_id == world.location_id)
            )
            await db.execute(
                delete(Annotation).where(Annotation.project_id == project.id)
            )
            await db.execute(delete(Task).where(Task.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
            await db.execute(
                delete(DatasetSourceChange).where(
                    DatasetSourceChange.run_id.in_(
                        select(DatasetRegistrationRun.id).where(
                            DatasetRegistrationRun.source_id == source.id
                        )
                    )
                )
            )
            await db.execute(
                delete(DatasetSourceRevision).where(
                    DatasetSourceRevision.source_id == source.id
                )
            )
            await db.execute(
                delete(DatasetRegistrationRun).where(
                    DatasetRegistrationRun.source_id == source.id
                )
            )
            await db.execute(
                delete(DatasetScanRun).where(DatasetScanRun.source_id == source.id)
            )
            await db.execute(delete(DatasetSource).where(DatasetSource.id == source.id))
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location.id)
            )
            await db.execute(
                delete(AsyncJob).where(
                    AsyncJob.user_id.in_([user.id for user in users])
                )
            )
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id.in_([user.id for user in users])
                )
            )
            await db.execute(
                delete(User).where(User.id.in_([user.id for user in users]))
            )
            await db.commit()
        assert all(client.closed for client in world.clients)
        assert all(body.closed for body in world.bodies)


async def open_image(world, *, actor=None, node_id=None, key_id=None):
    async with world.factory() as db:
        return await refs.reference_snapshot(
            db,
            world.item_id,
            actor or world.actor,
            task_id=world.task.id,
            expected_sha256=world.sha,
            node_id=node_id,
            api_key_id=key_id,
        )


async def seed_cache(world):
    image = await open_image(world)
    image.close()
    async with world.factory() as db:
        entry = await db.scalar(
            select(Entry).where(
                Entry.source_id == world.source_id, Entry.state == "ready"
            )
        )
        assert entry is not None
        return entry


async def alter(world, model, identity, **changes):
    async with world.factory() as db:
        instance = await db.get(model, identity)
        for key, value in changes.items():
            setattr(instance, key, value)
        await db.commit()


async def test_first_read_retains_verified_original_and_source_offline_uses_same_asset(
    referenced_cache,
):
    w = referenced_cache
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    image = await open_image(w, node_id="worker:without-source-mount@test")
    try:
        assert image.file.read() == w.data and image.sha256 == w.sha
    finally:
        image.close()
    async with w.factory() as db:
        image = await task_snapshot(db, w.task.id, w.actor, w.sha)
        image.close()
    assert len(w.puts) == 1


async def test_path_overwrite_still_reads_pinned_old_asset_from_complete_cache(
    referenced_cache,
):
    w = referenced_cache
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).write_bytes(
        b"unaccepted new path content"
    )
    image = await open_image(w)
    try:
        assert image.file.read() == w.data and image.sha256 == w.sha
    finally:
        image.close()


async def test_real_cached_admission_has_initialized_source_context_and_no_read_count(
    referenced_cache, test_engine, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.db.models.storage_observation_rate import StorageObservationBucket
    from tests.test_storage_observation_readers import totals

    w = referenced_cache
    await seed_cache(w)
    before = await totals(test_engine, w.source_id)
    admitted = AsyncMock()
    monkeypatch.setattr(
        refs,
        "reference_source_context",
        AsyncMock(side_effect=AssertionError("complete cache must not read source")),
    )
    try:
        async with w.factory() as db:
            image = await refs.reference_snapshot(
                db,
                w.item_id,
                w.actor,
                task_id=w.task.id,
                expected_sha256=w.sha,
                on_acquired=admitted,
            )
            try:
                assert image.file.read() == w.data
            finally:
                image.close()
        admitted.assert_awaited_once()
        assert await totals(test_engine, w.source_id) == before
        refs.reference_source_context.assert_not_awaited()
    finally:
        async with test_engine.begin() as db:
            await db.execute(
                delete(StorageObservationBucket).where(
                    StorageObservationBucket.source_id == w.source_id
                )
            )


async def test_cached_original_does_not_grant_task_or_dataset_management(
    referenced_cache,
):
    w = referenced_cache
    await seed_cache(w)
    before = len(w.bodies)
    with pytest.raises(HTTPException):
        await open_image(w, actor=w.stranger)
    async with w.factory() as db:
        with pytest.raises(HTTPException):
            await refs.reference_snapshot(db, w.item_id, w.actor, expected_sha256=w.sha)
    assert len(w.bodies) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "source_disable",
        "location_disable",
        "member_remove",
        "account_disable",
        "key_revoke",
        "key_rotate",
        "key_scope",
    ],
)
async def test_current_authority_rechecked_after_cache_io(referenced_cache, mutation):
    w = referenced_cache
    await seed_cache(w)
    key_id = uuid.uuid4()
    async with w.factory() as db:
        db.add(
            ApiKey(
                id=key_id,
                user_id=w.actor.id,
                name="cache test",
                key_prefix="ak_cachetest",
                key_hash="test-proof",
                scopes=["tasks:read"],
            )
        )
        await db.commit()
    w.get_started, w.get_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(open_image(w, key_id=key_id))
    try:
        assert await asyncio.to_thread(w.get_started.wait, 3)
        if mutation == "source_disable":
            await alter(w, DatasetSource, w.source_id, enabled=False)
        elif mutation == "location_disable":
            await alter(w, StorageLocation, w.location_id, enabled=False)
        elif mutation == "account_disable":
            await alter(w, User, w.actor.id, is_active=False)
        elif mutation == "member_remove":
            async with w.factory() as db:
                await db.execute(
                    delete(ProjectMember).where(
                        ProjectMember.project_id == w.project.id,
                        ProjectMember.user_id == w.actor.id,
                    )
                )
                await db.commit()
        else:
            changes = (
                {"revoked_at": datetime.now(timezone.utc)}
                if mutation == "key_revoke"
                else {"key_hash": "rotated"}
                if mutation == "key_rotate"
                else {"scopes": []}
            )
            await alter(w, ApiKey, key_id, **changes)
        w.get_release.set()
        with pytest.raises((HTTPException, ContentError)):
            await pending
    finally:
        w.get_release.set()
        try:
            result = await pending
            result.close()
        except (HTTPException, ContentError):
            pass


async def test_unknown_source_reader_requires_probe_only_when_cache_misses(
    referenced_cache,
):
    w = referenced_cache
    async with w.factory() as db:
        identity = await refs.reference_context(
            db, w.item_id, w.actor, task_id=w.task.id, node_id="worker:unknown"
        )
        assert "config" not in identity and identity["sha256"] == w.sha
    with pytest.raises(ContentError, match="worker_read_not_verified"):
        await open_image(w, node_id="worker:unknown")
    await seed_cache(w)
    async with w.factory() as db:
        await db.execute(
            delete(StorageLocationProbe).where(
                StorageLocationProbe.location_id == w.location_id
            )
        )
        await db.commit()
    image = await open_image(w, node_id="worker:unknown")
    image.close()


@pytest.mark.parametrize("failed", ["quota", "put", "delete"])
async def test_optional_retention_failure_keeps_verified_request_readable(
    referenced_cache, failed
):
    w = referenced_cache
    if failed == "quota":
        async with w.factory() as db:
            location = await db.get(StorageLocation, w.location_id)
            location.config = {**location.config, "original_cache_quota_bytes": 1}
            await db.commit()
    else:
        w.fail_put = True
        w.fail_delete = failed == "delete"
    image = await open_image(w)
    try:
        assert image.file.read() == w.data
    finally:
        image.close()


async def test_quality_verification_still_reads_source_not_cached_history(
    referenced_cache,
):
    w = referenced_cache
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    from app.services.dataset_content import quality_snapshot

    async with w.factory() as db:
        identity = await quality_identity(db, w.item_id, w.manager)
        assert "config" in identity[3]
        with pytest.raises((ContentError, ValueError, OSError)):
            await quality_snapshot(db, w.item_id, w.manager, identity)


async def test_training_snapshot_freezes_same_complete_cached_bytes(referenced_cache):
    w = referenced_cache
    entry = await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    sample = {
        "source_key": w.task.file_path,
        "dataset_item_id": str(w.item_id),
        "task_id": str(w.task.id),
        "dataset_id": str(w.dataset_id),
    }
    async with w.factory() as db:
        image, identity, _ = await training_reference_snapshot(
            db, sample, w.actor, node_id="worker:training-without-source"
        )
        try:
            assert image.file.read() == w.data and identity["asset_id"] == str(
                entry.asset_id
            )
        finally:
            image.close()


@pytest.mark.parametrize("owner", [DatasetSource, StorageLocation])
@pytest.mark.parametrize("policy", ["none", "preview"])
async def test_policy_downgrade_cannot_read_an_existing_cached_copy(
    referenced_cache, owner, policy
):
    w = referenced_cache
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    if owner is DatasetSource:
        await alter(w, owner, w.source_id, cache_policy=policy)
    else:
        await alter(
            w,
            owner,
            w.location_id,
            config={**w.content[4].config, "cache_policy": policy},
        )
    before = len(w.bodies)
    with pytest.raises((ContentError, ValueError, OSError)):
        await open_image(w)
    assert len(w.bodies) == before


async def test_inference_qualification_can_capture_cache_without_source_mount(
    referenced_cache,
):
    from starlette.requests import Request
    from app.services.reference_inference import ReferenceImageOperation

    w = referenced_cache
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    request = Request({"type": "http"})
    async with w.factory() as db:
        owner = ReferenceImageOperation(db, w.task, w.manager, request, "predict_test")
        try:
            await owner.capture()
            assert owner.snapshot.sha256 == w.sha
            await owner.validate()
        finally:
            await owner.close()


@pytest.mark.parametrize("real_storage", [False, True])
async def test_actual_http_task_read_and_manual_annotation_use_verified_cache(
    referenced_cache, app_module, monkeypatch, real_storage
):
    import httpx
    from app.core.security import create_access_token
    from app.deps import get_db

    w = referenced_cache
    if real_storage:
        monkeypatch.setattr(cache, "make_storage", _REAL_STORAGE)
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    token = create_access_token(subject=str(w.actor.id), role="employee")
    headers = {"Authorization": f"Bearer {token}"}

    async def database():
        async with w.factory() as db:
            yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            path = f"/api/v1/files/tasks/{w.task.id}/content"
            result = await client.get(path, params={"sha256": w.sha}, headers=headers)
            assert result.status_code == 200 and result.content == w.data
            assert result.headers["x-content-sha256"] == w.sha
            partial = await client.get(
                path,
                params={"sha256": w.sha},
                headers={**headers, "Range": "bytes=0-1"},
            )
            assert partial.status_code == 416
            response = await client.post(
                f"/api/v1/tasks/{w.task.id}/annotations",
                headers=headers,
                json={
                    "class_name": "car",
                    "geometry": {
                        "type": "bbox",
                        "x": 0.1,
                        "y": 0.1,
                        "w": 0.2,
                        "h": 0.2,
                    },
                },
            )
            assert response.status_code == 201, response.text
            await alter(w, DatasetSource, w.source_id, enabled=False)
            denied = await client.get(path, params={"sha256": w.sha}, headers=headers)
            assert denied.status_code == 409
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous
        if real_storage:
            async with w.factory() as db:
                entries = list(
                    await db.scalars(
                        select(Entry).where(Entry.source_id == w.source_id)
                    )
                )
            for entry in entries:
                await cache.retire(w.factory, entry.id)
                await cache.purge(w.factory, entry.id)


@pytest.mark.parametrize("deletion_failed", [False, True])
async def test_full_location_budget_evicts_only_rebuildable_lru_or_reads_without_retention(
    referenced_cache, deletion_failed
):
    w = referenced_cache
    old_asset, old_item = uuid.uuid4(), uuid.uuid4()
    async with w.factory() as db:
        location = await db.get(StorageLocation, w.location_id)
        location.config = {**location.config, "original_cache_quota_bytes": len(w.data)}
        db.add(
            DatasetItem(
                id=old_item,
                dataset_id=w.dataset_id,
                file_name="older.png",
                file_path="cache-test/older.png",
            )
        )
        await db.flush()
        db.add(
            DatasetAsset(
                id=old_asset,
                dataset_item_id=old_item,
                media_type="image",
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=w.sha,
                size_bytes=len(w.data),
            )
        )
        await db.commit()
    image = await input_snapshot(w)
    try:
        assert await cache.store(w.factory, w.source_id, old_asset, image) == "stored"
    finally:
        image.close()
    w.fail_delete = deletion_failed
    image = await open_image(w)
    try:
        assert image.file.read() == w.data
    finally:
        image.close()
    async with w.factory() as db:
        assert await db.get(DatasetItem, old_item) is not None
        current = list(
            await db.scalars(select(Entry).where(Entry.source_id == w.source_id))
        )
        assert await cache.charged_bytes(db, w.location_id) == len(w.data)
        assert [entry.state for entry in current] == (
            ["deleting"] if deletion_failed else ["ready"]
        )
        assert (current[0].asset_id == old_asset) == deletion_failed


async def test_cache_capacity_pressure_returns_retry_without_retiring_valid_copy(
    referenced_cache,
):
    w = referenced_cache
    await seed_cache(w)
    (w.content[0] / w.content[7].relative_path).unlink()
    held = []
    try:
        for _ in range(4):
            held.append(await input_snapshot(w))
        with pytest.raises(ContentError, match="snapshot_capacity_exceeded") as error:
            await open_image(w)
        assert error.value.status == 429
        async with w.factory() as db:
            assert (
                await db.scalar(
                    select(Entry.state).where(Entry.source_id == w.source_id)
                )
                == "ready"
            )
    finally:
        for image in held:
            image.close()


async def test_oversized_original_does_not_evict_smaller_usable_copies(
    referenced_cache,
):
    import io
    from PIL import Image

    w = referenced_cache
    output = io.BytesIO()
    with Image.new("RGB", (1, 1)) as image:
        image.save(output, "PNG")
    smaller = output.getvalue()
    output.close()
    assert len(smaller) < len(w.data)
    old_item, old_asset = uuid.uuid4(), uuid.uuid4()
    async with w.factory() as db:
        location = await db.get(StorageLocation, w.location_id)
        location.config = {
            **location.config,
            "original_cache_quota_bytes": len(smaller),
        }
        db.add(
            DatasetItem(
                id=old_item,
                dataset_id=w.dataset_id,
                file_name="smaller.png",
                file_path="cache-test/smaller.png",
            )
        )
        await db.flush()
        db.add(
            DatasetAsset(
                id=old_asset,
                dataset_item_id=old_item,
                media_type="image",
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=hashlib.sha256(smaller).hexdigest(),
                size_bytes=len(smaller),
            )
        )
        await db.commit()
    image = await input_snapshot(w, data=smaller)
    try:
        assert await cache.store(w.factory, w.source_id, old_asset, image) == "stored"
    finally:
        image.close()
    image = await open_image(w)
    image.close()
    assert not w.deletions
    async with w.factory() as db:
        current = list(
            await db.scalars(select(Entry).where(Entry.source_id == w.source_id))
        )
        assert (
            len(current) == 1
            and current[0].asset_id == old_asset
            and current[0].state == "ready"
        )


async def test_cache_read_and_fill_never_commit_unrelated_caller_state(
    referenced_cache,
):
    w = referenced_cache
    async with w.factory() as db:
        dataset = await db.get(Dataset, w.dataset_id)
        previous = dataset.name
        dataset.name = "uncommitted caller edit"
        await db.flush()
        image = await refs.reference_snapshot(
            db, w.item_id, w.actor, task_id=w.task.id, expected_sha256=w.sha
        )
        image.close()
        async with w.factory() as observer:
            assert (await observer.get(Dataset, w.dataset_id)).name == previous
        await db.rollback()
