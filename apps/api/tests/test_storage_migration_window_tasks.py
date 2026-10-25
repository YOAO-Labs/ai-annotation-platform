"""Window-bound inventory and generations retain exact authority and actual drain."""

import json
from types import SimpleNamespace
import uuid

from botocore.hooks import HierarchicalEmitter
from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageLocation
from app.db.models.notification import Notification
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.storage_migration_copy import StorageMigrationCopy as Copy
from app.db.models.storage_migration_window import StorageMigrationWindow as Window
from app.db.models.user import User
from app.schemas.storage_migration_copy import StorageMigrationCopyGenerationRequest
from app.schemas.storage_migration_inventory import StorageMigrationInventoryRequest
from app.schemas.storage_migration_window import (
    StorageMigrationWindowRequest,
    StorageMigrationWindowTransition,
)
from app.services import storage_access, storage_migration_copies as copies
from app.services import (
    storage_migration_inventories as inventories,
    storage_migration_windows as windows,
)
from app.workers import (
    storage_migration_copy as copy_worker,
    storage_migration_inventory as inventory_worker,
)
from tests.factory import create_user
from tests.test_storage_migration_copies import seed
from tests.test_storage_migration_inventories import EmptySource


@pytest.fixture
async def world(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        source_location = await db.get(StorageLocation, inventories.LEGACY_LOCATION_ID)
        source_revision = (
            source_location.config_revision if source_location is not None else 1
        )
        actor = await create_user(
            db, "super_admin", f"window-tasks-{uuid.uuid4()}@test.local", "Window tasks"
        )
        source = await db.get(Gate, inventories.LEGACY_LOCATION_ID)
        if source is not None:
            source.state = "active"
        await db.commit()
        inventory, connection, location, body = await seed(db, actor)
        parent, job, _ = await copies.start(db, actor, body)
        parent_id, job_id, task_id, actor_id = (
            parent.id,
            job.id,
            job.celery_task_id,
            actor.id,
        )
        target_id, connection_id = location.id, connection.id
        await db.commit()
    async with maker() as db:
        assert await copy_worker.execute(db, job_id, task_id)
    try:
        yield SimpleNamespace(
            maker=maker,
            actor_id=actor_id,
            parent_id=parent_id,
            target_id=target_id,
            source=inventory.source,
        )
    finally:
        async with maker() as db:
            await db.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id.in_(
                        [inventories.LEGACY_LOCATION_ID, target_id]
                    )
                )
            )
            children = list(
                await db.scalars(
                    select(Copy.id)
                    .where(Copy.target_location_id == target_id, Copy.id != parent_id)
                    .order_by(Copy.generation.desc())
                )
            )
            for identifier in children:
                await db.execute(delete(Copy).where(Copy.id == identifier))
            await db.execute(delete(AsyncJob).where(AsyncJob.user_id == actor_id))
            await db.execute(delete(Window).where(Window.parent_copy_id == parent_id))
            await db.execute(delete(Copy).where(Copy.id == parent_id))
            await db.execute(
                delete(Notification).where(Notification.user_id == actor_id)
            )
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(delete(AuditLog).where(AuditLog.actor_id == actor_id))
            await db.execute(delete(User).where(User.id == actor_id))
            source_location = await db.get(
                StorageLocation, inventories.LEGACY_LOCATION_ID
            )
            source_location.config_revision = source_revision
            await db.execute(
                delete(Gate).where(
                    Gate.location_id.in_([inventories.LEGACY_LOCATION_ID, target_id])
                )
            )
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == target_id)
            )
            await db.execute(
                delete(StorageConnection).where(StorageConnection.id == connection_id)
            )
            await db.commit()


async def stopped(world):
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        row = await windows.open_window(
            db, actor, StorageMigrationWindowRequest(parent_copy_id=world.parent_id)
        )
        identifier = row.window_id
        await db.commit()
        await windows.transition(
            db,
            identifier,
            actor,
            StorageMigrationWindowTransition(action="stop", expected_revision=1),
        )
        await db.commit()
        return identifier


def install_source(monkeypatch, world):
    client = EmptySource()
    client.meta = SimpleNamespace(
        endpoint_url=world.source["endpoint"], events=HierarchicalEmitter()
    )
    client.closed = False

    def close():
        client.closed = True

    client.close = close
    monkeypatch.setattr(
        inventory_worker,
        "StorageService",
        lambda **kwargs: SimpleNamespace(
            client=client,
            **{
                attribute: world.source["purpose_buckets"][purpose]
                for purpose, attribute in inventories.PURPOSE_BUCKET_ATTRIBUTES.items()
            },
        ),
    )
    return client


async def report(world, window_id):
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        row, job, reused = await inventories.start(
            db, actor, StorageMigrationInventoryRequest(window_id=window_id)
        )
        assert not reused
        job_id, task_id = job.id, job.celery_task_id
        await db.commit()
    async with world.maker() as db:
        assert await inventory_worker.execute(db, job_id, task_id)
    return job_id


@pytest.mark.parametrize("cosmetic_revision", [False, True])
async def test_stopped_inventory_full_native_scan_and_generation_prepare(
    world, monkeypatch, cosmetic_revision
):
    if cosmetic_revision:
        async with world.maker() as db:
            (
                await db.get(StorageLocation, inventories.LEGACY_LOCATION_ID)
            ).config_revision += 1
            await db.commit()
    window_id = await stopped(world)
    client = install_source(monkeypatch, world)
    job_id = await report(world, window_id)
    assert client.closed
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        row = await inventories.by_job(db, job_id)
        assert (
            row.status == "completed"
            and row.window_id == window_id
            and row.window_revision == 2
        )
        assert inventories.item(row).window_revision == 2
        job = await db.get(AsyncJob, job_id)
        assert (
            job.payload["window_id"] == str(window_id)
            and job.payload["window_revision"] == 2
        )
        raw, _ = await inventories.download(db, job_id, actor)
        header = json.loads(raw.splitlines()[0])
        assert header["observations"]["migration_window"] == {
            "window_id": str(window_id),
            "window_revision": 2,
            "source_quiescence_proven": False,
        }
        child, job, reused = await copies.generation(
            db,
            world.parent_id,
            actor,
            StorageMigrationCopyGenerationRequest(inventory_job_id=job_id),
        )
        assert (
            not reused and child.window_id == window_id and child.window_revision == 2
        )
        assert child.source == (await db.get(Window, window_id)).source
        assert (await copies.item(db, child)).migration_ready is False
        child_id, copy_job_id, task_id = child.id, job.id, job.celery_task_id
        await db.commit()
    async with world.maker() as db:
        assert await copy_worker.execute(db, copy_job_id, task_id)
        child = await copies.owner(db, child_id)
        attempt = await copies.last(db, child)
        assert attempt.status == "completed" and child.baseline_sha256
        assert (
            attempt.scope["window_id"] == str(window_id)
            and attempt.scope["window_revision"] == 2
        )
        assert not await db.scalar(
            select(StorageOperation.id).where(
                StorageOperation.location_id == inventories.LEGACY_LOCATION_ID
            )
        )


async def test_old_admission_and_root_copy_never_inherit_source_exception(
    world, monkeypatch
):
    window_id = await stopped(world)
    install_source(monkeypatch, world)
    job_id = await report(world, window_id)
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        with pytest.raises(Exception) as ordinary:
            await storage_access.acquire(
                db, inventories.LEGACY_LOCATION_ID, "import_upload_sign"
            )
        assert getattr(ordinary.value, "code", None) == "storage_maintenance"
        await db.rollback()
        actor = await db.get(User, world.actor_id)
        with pytest.raises(HTTPException) as caught:
            await inventories.start(db, actor)
        assert caught.value.detail == {"code": "storage_maintenance"}
        await db.rollback()
        actor = await db.get(User, world.actor_id)
        from app.schemas.storage_migration_copy import StorageMigrationCopyRequest

        with pytest.raises(HTTPException) as caught:
            await copies.start(
                db,
                actor,
                StorageMigrationCopyRequest(
                    inventory_job_id=job_id,
                    target_location_id=world.target_id,
                    handling=dict.fromkeys(inventories.PURPOSES, "migrate"),
                ),
            )
        assert caught.value.detail == {"code": "copy_window_requires_generation"}


@pytest.mark.parametrize(
    "drift", ["window_revision", "source_revision", "target_stopped", "window_target"]
)
async def test_generation_rejects_window_source_or_target_drift(
    world, monkeypatch, drift
):
    window_id = await stopped(world)
    install_source(monkeypatch, world)
    job_id = await report(world, window_id)
    async with world.maker() as db:
        if drift == "window_revision":
            (await db.get(Window, window_id)).revision += 1
        elif drift == "source_revision":
            (
                await db.get(StorageLocation, inventories.LEGACY_LOCATION_ID)
            ).config_revision += 1
        elif drift == "target_stopped":
            await storage_access.ensure(db, world.target_id)
            (await db.get(Gate, world.target_id)).state = "stopped"
        else:
            # The immutable window cannot be tampered with: reject a caller
            # trying to borrow its exception for a different parent instead.
            parent = await db.get(Copy, world.parent_id)
            with pytest.raises(HTTPException) as caught:
                await copies.scope(
                    db,
                    SimpleNamespace(
                        target_location_id=parent.target_location_id,
                        target=parent.target,
                        source=(await db.get(Window, window_id)).source,
                        parent_copy_id=uuid.uuid4(),
                        window_id=window_id,
                        window_revision=2,
                    ),
                )
            assert caught.value.detail == {"code": "copy_window_changed"}
            return
        await db.commit()
        actor = await db.get(User, world.actor_id)
        with pytest.raises(Exception):
            await copies.generation(
                db,
                world.parent_id,
                actor,
                StorageMigrationCopyGenerationRequest(inventory_job_id=job_id),
            )


async def test_stopped_source_and_target_return_controlled_http_conflicts(
    world, monkeypatch
):
    from app.core.security import create_access_token
    from app.deps import get_db
    from app.main import app

    window_id = await stopped(world)
    install_source(monkeypatch, world)
    job_id = await report(world, window_id)
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        child, job, _ = await copies.generation(
            db,
            world.parent_id,
            actor,
            StorageMigrationCopyGenerationRequest(inventory_job_id=job_id),
        )
        child_id, job_id, task_id = child.id, job.id, job.celery_task_id
        await db.commit()
    async with world.maker() as db:
        assert await copy_worker.execute(db, job_id, task_id)
        await storage_access.ensure(db, world.target_id)
        (await db.get(Gate, world.target_id)).state = "stopped"
        await db.commit()

    async def fresh_request():
        async with world.maker() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = fresh_request
    token = create_access_token(subject=str(world.actor_id), role="super_admin")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            ordinary = await client.post(
                "/api/v1/storage/migration-inventories", json={}
            )
            assert ordinary.status_code == 409
            assert ordinary.json() == {"detail": {"code": "storage_maintenance"}}
            target = await client.post(
                f"/api/v1/storage/migration-copies/{child_id}/attempts",
                json={"operation": "run"},
            )
            assert target.status_code == 409
            assert target.json() == {"detail": {"code": "storage_maintenance"}}
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
