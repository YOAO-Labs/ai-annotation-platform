"""Durable registered-access windows, exact admission and close-before-release."""

import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.core import security
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.storage_migration_copy import (
    StorageMigrationCopy as Copy,
    StorageMigrationCopyAttempt as Attempt,
)
from app.db.models.storage_migration_inventory import (
    StorageMigrationInventory as Inventory,
)
from app.db.models.storage_migration_window import StorageMigrationWindow as Window
from app.db.models.user import User
from app.schemas.storage_migration_window import (
    StorageMigrationWindowRequest as Open,
    StorageMigrationWindowTransition as Transition,
)
from app.services import (
    storage_access,
    storage_migration_copies as copies,
    storage_migration_inventories as inventories,
    storage_migration_windows as windows,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.prediction_inputs import settled_io
from tests import test_storage_migration_copy_worker as copy_worker
from tests.storage_migration_isolation import (
    annotator as annotator,
    db_session as db_session,
    httpx_client as httpx_client,
    migration_engine as migration_engine,
    project_admin as project_admin,
    super_admin as super_admin,
)
from tests.test_storage_migration_copies import seed

PATH = "/api/v1/storage/migration-windows"


@pytest.fixture
async def prepared(db_session, super_admin, monkeypatch):
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    _, _, _, body = await seed(db_session, super_admin[0])
    parent, job, _ = await copies.start(db_session, super_admin[0], body)
    parent.baseline, parent.baseline_sha256 = b"bounded fixture", "a" * 64
    attempt = await copies.last(db_session, parent)
    attempt.status = job.status = "completed"
    attempt.completed_at = job.completed_at = inventories.now()
    await db_session.flush()
    return parent


@pytest.mark.parametrize("role", ["super_admin", "project_admin", "annotator"])
async def test_window_routes_are_private_and_public_projection_is_closed(
    db_session, httpx_client, prepared, super_admin, project_admin, annotator, role
):
    actor, token = {
        "super_admin": super_admin,
        "project_admin": project_admin,
        "annotator": annotator,
    }[role]
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.post(
        PATH, json={"parent_copy_id": str(prepared.id)}, headers=headers
    )
    assert response.status_code == (201 if role == "super_admin" else 403), (
        response.text
    )
    if role == "super_admin":
        value = response.json()
        assert value["state"] == "draining" and value["revision"] == 1
        assert value["source_quiescence_proven"] is value["migration_ready"] is False
        assert value["registered_access_drained"] is False
        window_id = value["window_id"]
        for secret in (
            "endpoint",
            "bucket",
            "source_location_id",
            "source_report",
            "authority",
            "secret",
        ):
            assert secret not in value
    else:
        value = await windows.open_window(
            db_session, super_admin[0], Open(parent_copy_id=prepared.id)
        )
        window_id = value.window_id
    for suffix in ("?limit=1&offset=0", f"/{window_id}"):
        result = await httpx_client.get(PATH + suffix, headers=headers)
        assert result.status_code == (200 if role == "super_admin" else 403)
    result = await httpx_client.post(
        f"{PATH}/{window_id}/transitions",
        json={"action": "stop", "expected_revision": 1},
        headers=headers,
    )
    assert result.status_code == (200 if role == "super_admin" else 403)


@pytest.mark.parametrize("scopes", [[], ["datasets:read"], ["*"]])
async def test_every_window_route_requires_full_api_key_scope(
    db_session, httpx_client, prepared, super_admin, scopes
):
    value = await windows.open_window(
        db_session, super_admin[0], Open(parent_copy_id=prepared.id)
    )
    secret = "ak_" + uuid.uuid4().hex
    db_session.add(
        ApiKey(
            user_id=super_admin[0].id,
            name="Window",
            key_prefix=secret[:12],
            key_hash=security.hash_password(secret),
            scopes=scopes,
        )
    )
    await db_session.flush()
    for method, path, body in [
        ("GET", PATH, None),
        ("GET", f"{PATH}/{value.window_id}", None),
        ("POST", PATH, {"parent_copy_id": str(prepared.id)}),
        (
            "POST",
            f"{PATH}/{value.window_id}/transitions",
            {"action": "stop", "expected_revision": 1},
        ),
    ]:
        response = await httpx_client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert (
            response.status_code != 403
            if scopes == ["*"]
            else response.status_code == 403
        )


async def test_registered_drain_is_explicit_cas_and_close_restores_only_after_zero(
    db_session, prepared, super_admin
):
    actor = super_admin[0]
    value = await windows.open_window(
        db_session, actor, Open(parent_copy_id=prepared.id)
    )
    gate = await db_session.get(Gate, LEGACY_LOCATION_ID, populate_existing=True)
    assert gate.state == "draining" and gate.revision == value.source_gate_revision
    with pytest.raises(Exception, match="storage_maintenance"):
        await storage_access.check(db_session, LEGACY_LOCATION_ID)
    operation = StorageOperation(
        location_id=LEGACY_LOCATION_ID,
        revision=gate.revision,
        purpose="existing_io",
        node_id="window-test",
    )
    db_session.add(operation)
    await db_session.flush()
    with pytest.raises(HTTPException) as caught:
        await windows.transition(
            db_session,
            value.window_id,
            actor,
            Transition(action="stop", expected_revision=1),
        )
    assert caught.value.detail["code"] == "migration_window_unsettled"
    await db_session.delete(operation)
    await db_session.flush()
    stopped = await windows.transition(
        db_session,
        value.window_id,
        actor,
        Transition(action="stop", expected_revision=1),
    )
    assert (
        stopped.state == "stopped"
        and stopped.revision == 2
        and stopped.registered_access_drained
    )
    assert stopped.source_gate_revision == value.source_gate_revision + 1
    with pytest.raises(HTTPException) as caught:
        await windows.transition(
            db_session,
            value.window_id,
            actor,
            Transition(action="close", expected_revision=1),
        )
    assert caught.value.detail["code"] == "migration_window_changed"
    operation = StorageOperation(
        location_id=LEGACY_LOCATION_ID,
        revision=stopped.source_gate_revision,
        purpose="migration_read",
        node_id="window-test",
    )
    db_session.add(operation)
    await db_session.flush()
    closing = await windows.transition(
        db_session,
        value.window_id,
        actor,
        Transition(action="close", expected_revision=2),
    )
    assert closing.state == "closing" and closing.registered_operations == 1
    assert (await db_session.get(Gate, LEGACY_LOCATION_ID)).state == "stopped"
    with pytest.raises(HTTPException):
        await windows.source_revision(db_session, value.window_id, prepared.source)
    await db_session.delete(operation)
    await db_session.flush()
    closed = await windows.transition(
        db_session,
        value.window_id,
        actor,
        Transition(action="close", expected_revision=3),
    )
    assert closed.state == "closed" and not closed.registered_access_drained
    assert (
        await storage_access.check(db_session, LEGACY_LOCATION_ID)
        == closed.source_gate_revision
    )


async def test_protocol_disable_still_allows_authorized_close_and_history(
    db_session, prepared, super_admin, monkeypatch
):
    value = await windows.open_window(
        db_session, super_admin[0], Open(parent_copy_id=prepared.id)
    )
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", False)
    with pytest.raises(HTTPException) as caught:
        await windows.transition(
            db_session,
            value.window_id,
            super_admin[0],
            Transition(action="stop", expected_revision=1),
        )
    assert caught.value.detail["code"] == "storage_maintenance_protocol_unqualified"
    assert (
        await windows.view(db_session, value.window_id, super_admin[0])
    ).state == "draining"
    closed = await windows.transition(
        db_session,
        value.window_id,
        super_admin[0],
        Transition(action="close", expected_revision=1),
    )
    assert closed.state == "closed"
    with pytest.raises(HTTPException):
        await windows.open_window(
            db_session, super_admin[0], Open(parent_copy_id=prepared.id)
        )


@pytest.mark.parametrize(
    "problem",
    ["unprepared", "active", "permit", "successor", "source", "target_source"],
)
async def test_window_open_retains_original_parent_and_physical_identity_guards(
    db_session, prepared, super_admin, problem
):
    if problem == "unprepared":
        prepared.baseline_sha256 = None
    elif problem == "active":
        (await copies.last(db_session, prepared)).status = "running"
    elif problem == "permit":
        operation = StorageOperation(
            location_id=LEGACY_LOCATION_ID,
            revision=1,
            purpose=copies.KIND,
            node_id="not-a-ttl",
        )
        db_session.add(operation)
        await db_session.flush()
        (await copies.last(db_session, prepared)).source_operation_id = operation.id
    elif problem == "successor":
        db_session.add(
            Copy(
                id=uuid.uuid4(),
                parent_copy_id=prepared.id,
                generation=2,
                parent_evidence_sha256="a" * 64,
                input_fingerprint=uuid.uuid4().hex * 2,
                source_report=b"x",
                source_report_sha256="b" * 64,
                source_complete=False,
                source=prepared.source,
                target_location_id=prepared.target_location_id,
                target=prepared.target,
                handling=prepared.handling,
                limits=prepared.limits,
            )
        )
    elif problem == "source":
        prepared.source = {**prepared.source, "endpoint": "http://elsewhere.example"}
    else:
        from app.db.models.storage_connection import StorageConnection

        connection = await db_session.get(
            StorageConnection, uuid.UUID(prepared.target["connection_id"])
        )
        connection.config = {
            **connection.config,
            "endpoint": prepared.source["endpoint"],
            "bucket": next(iter(prepared.source["purpose_buckets"].values())),
        }
        prepared.target = {
            **prepared.target,
            "endpoint_url": prepared.source["endpoint"],
            "bucket": connection.config["bucket"],
        }
    await db_session.flush()
    with pytest.raises(HTTPException):
        await windows.open_window(
            db_session, super_admin[0], Open(parent_copy_id=prepared.id)
        )
    assert not await db_session.scalar(select(Window.id))


async def test_current_config_revision_is_frozen_without_requiring_old_parent_revision(
    db_session, prepared, super_admin
):
    from app.db.models.dataset_asset import StorageLocation

    location = await db_session.get(StorageLocation, LEGACY_LOCATION_ID)
    location.config_revision += 1
    await db_session.flush()
    value = await windows.open_window(
        db_session, super_admin[0], Open(parent_copy_id=prepared.id)
    )
    assert value.source_config_revision == prepared.source["location_revision"] + 1
    stopped = await windows.transition(
        db_session,
        value.window_id,
        super_admin[0],
        Transition(action="stop", expected_revision=1),
    )
    row = await windows.owner(db_session, value.window_id)
    assert (
        await windows.check_source(db_session, row.id, stopped.revision, row.source)
        == stopped.source_gate_revision
    )
    location.config_revision += 1
    await db_session.flush()
    with pytest.raises(HTTPException) as caught:
        await windows.check_source(db_session, row.id, stopped.revision, row.source)
    assert caught.value.detail["code"] == "migration_window_source_changed"
    assert (
        await windows.transition(
            db_session,
            row.id,
            super_admin[0],
            Transition(action="close", expected_revision=2),
        )
    ).state == "closed"


@pytest.fixture
async def reader_world(copy_worker_world, monkeypatch):
    world = copy_worker_world
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    assert await copy_worker.execute(world)
    world.operations = []
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        opened = await windows.open_window(
            db, actor, Open(parent_copy_id=world.copy_id)
        )
        value = await windows.transition(
            db, opened.window_id, actor, Transition(action="stop", expected_revision=1)
        )
        row = await windows.owner(db, value.window_id)
        world.window_id, world.window_revision, world.revision, world.source = (
            row.id,
            row.revision,
            row.source_gate_revision,
            deepcopy(row.source),
        )
        await db.commit()
    try:
        yield world
    finally:
        async with world.maker() as db:
            if world.operations:
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.id.in_(world.operations)
                    )
                )
            window_ids = list(
                await db.scalars(
                    select(Window.id).where(Window.parent_copy_id == world.copy_id)
                )
            )
            await db.execute(
                delete(Inventory).where(Inventory.window_id.in_(window_ids))
            )
            children = list(
                await db.scalars(
                    select(Copy.id)
                    .where(Copy.window_id.in_(window_ids))
                    .order_by(Copy.generation.desc())
                )
            )
            for child in children:
                await db.execute(delete(Copy).where(Copy.id == child))
            await db.execute(delete(Window).where(Window.id.in_(window_ids)))
            gate = await db.get(Gate, LEGACY_LOCATION_ID)
            gate.state, gate.revision = "active", gate.revision + 1
            await db.commit()


copy_worker_world = copy_worker.world


async def bound_reader(world, kind):
    async with world.maker() as db:
        task_id = str(uuid.uuid4())
        actor = await db.get(User, world.actor_id)
        _, proof = await inventories.authority(db, actor)
        source = deepcopy(world.source)
        job = await inventories.jobs.create_job(
            db,
            kind=inventories.KIND if kind == "inventory" else copies.KIND,
            user_id=actor.id,
            celery_task_id=task_id,
            payload={"scope": "selected-configured-source", "migration_ready": False},
        )
        job.status, job.started_at = "running", inventories.now()
        if kind == "inventory":
            from app.schemas.storage_migration_inventory import (
                StorageMigrationInventoryRequest,
            )

            if "window_id" in StorageMigrationInventoryRequest.model_fields:
                job.payload = {
                    **job.payload,
                    "window_id": str(world.window_id),
                    "window_revision": world.window_revision,
                }
            parent = await db.get(Copy, world.copy_id)
            record = Inventory(
                async_job_id=job.id,
                requested_by=actor.id,
                task_id=task_id,
                source_location_id=LEGACY_LOCATION_ID,
                source=source,
                limits={},
                handling={},
                authority={"api_key_id": None, "key_proof": proof},
                input_fingerprint=uuid.uuid4().hex * 2,
                maintenance_revision=world.revision,
                window_id=world.window_id,
                window_revision=world.window_revision,
                status="running",
                started_at=inventories.now(),
            )
            owner_id = job.id
        else:
            parent = await db.get(Copy, world.copy_id)
            child = Copy(
                id=uuid.uuid4(),
                parent_copy_id=parent.id,
                generation=2,
                parent_evidence_sha256="a" * 64,
                input_fingerprint=uuid.uuid4().hex * 2,
                source_report=b"x",
                source_report_sha256="b" * 64,
                source_complete=False,
                source=source,
                target_location_id=parent.target_location_id,
                target=parent.target,
                handling=parent.handling,
                limits=parent.limits,
                window_id=world.window_id,
                window_revision=world.window_revision,
            )
            db.add(child)
            await db.flush()
            record = Attempt(
                id=uuid.uuid4(),
                copy_id=child.id,
                async_job_id=job.id,
                task_id=task_id,
                requested_by=actor.id,
                authority={"api_key_id": None, "key_proof": proof},
                scope={
                    "maintenance": {str(LEGACY_LOCATION_ID): world.revision},
                    "window_id": str(world.window_id),
                    "window_revision": world.window_revision,
                },
                operation="run",
                status="running",
                started_at=inventories.now(),
            )
            child.last_attempt_id = record.id
            job.payload = {
                "copy_id": str(child.id),
                "operation": "run",
                "migration_ready": False,
            }
            owner_id = record.id
        db.add(record)
        await db.commit()
    return SimpleNamespace(kind=kind, owner_id=owner_id, job_id=job.id, task_id=task_id)


async def acquire(world, reader, **changes):
    async with world.maker() as db:
        permit = await windows.acquire_source_read(
            db,
            **{
                "window_id": world.window_id,
                "owner_kind": reader.kind,
                "owner_id": reader.owner_id,
                "task_id": reader.task_id,
                **changes,
            },
        )
        world.operations.append(permit.id)
        return permit


@pytest.mark.parametrize("kind", ["inventory", "copy"])
async def test_source_admission_binds_actual_running_owner_and_returns_real_permit(
    reader_world, kind
):
    world = reader_world
    reader = await bound_reader(world, kind)
    permit = await acquire(world, reader)
    async with world.maker() as db:
        operation = await db.get(StorageOperation, permit.id)
        assert (
            operation.location_id == LEGACY_LOCATION_ID
            and operation.revision == world.revision
        )
        with pytest.raises(Exception, match="storage_maintenance"):
            await storage_access.acquire(db, LEGACY_LOCATION_ID, "ordinary_read")
    async with permit:
        pass
    assert permit._settled
    async with world.maker() as db:
        assert await db.get(StorageOperation, permit.id) is None


@pytest.mark.parametrize("kind", ["inventory", "copy"])
@pytest.mark.parametrize(
    "problem",
    ["task", "cancel", "job", "window_revision", "scope", "gate", "authority"],
)
async def test_forged_or_changed_reader_never_gets_special_admission(
    reader_world, kind, problem
):
    world = reader_world
    reader = await bound_reader(world, kind)
    async with world.maker() as db:
        record = await db.get(
            Inventory if kind == "inventory" else Attempt, reader.owner_id
        )
        if problem == "cancel":
            record.cancel_requested_at = inventories.now()
        elif problem == "job":
            (await db.get(AsyncJob, reader.job_id)).status = "completed"
        elif problem == "scope":
            if kind == "inventory":
                record.source = {**record.source, "endpoint": "http://other.example"}
            else:
                record.scope = {**record.scope, "window_revision": 999}
        elif problem == "gate":
            (await db.get(Gate, LEGACY_LOCATION_ID)).revision += 1
        elif problem == "authority":
            record.authority = {"api_key_id": None, "key_proof": "revoked"}
        elif problem == "window_revision":
            row = await db.get(Window, world.window_id)
            row.revision += 1
        await db.commit()
    with pytest.raises(HTTPException):
        await acquire(
            world, reader, **({"task_id": "other-task"} if problem == "task" else {})
        )


async def test_closing_keeps_native_cancel_responsibility_until_actual_drain(
    reader_world,
):
    world = reader_world
    reader = await bound_reader(world, "inventory")
    started, release = threading.Event(), threading.Event()

    def native():
        started.set()
        assert release.wait(5)

    async def perform():
        permit = await acquire(world, reader)
        async with permit:
            await settled_io(native)

    pending = asyncio.create_task(perform())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with world.maker() as db:
            actor = await db.get(User, world.actor_id)
            closing = await windows.transition(
                db,
                world.window_id,
                actor,
                Transition(action="close", expected_revision=world.window_revision),
            )
            assert closing.state == "closing" and closing.registered_operations == 1
            await db.commit()
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        with pytest.raises(HTTPException):
            await acquire(world, reader)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        closed = await windows.transition(
            db,
            world.window_id,
            actor,
            Transition(action="close", expected_revision=closing.revision),
        )
        assert closed.state == "closed" and closed.registered_operations == 0
        await db.commit()


async def test_unknown_physical_close_and_job_cleanup_do_not_release_window(
    reader_world,
):
    world = reader_world
    reader = await bound_reader(world, "inventory")
    permit = await acquire(world, reader)
    with pytest.raises(OSError):
        async with permit:
            raise storage_access.mark_unsettled(OSError("unknown native close"))
    assert not permit._settled
    async with world.maker() as db:
        await db.execute(delete(AsyncJob).where(AsyncJob.id == reader.job_id))
        actor = await db.get(User, world.actor_id)
        closing = await windows.transition(
            db,
            world.window_id,
            actor,
            Transition(action="close", expected_revision=world.window_revision),
        )
        assert closing.state == "closing" and closing.registered_operations == 1
        assert await db.get(StorageOperation, permit.id) is not None
        assert await windows.owner(db, world.window_id)
        await db.commit()


async def test_concurrent_open_reserves_one_active_window_for_source(reader_world):
    world = reader_world
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        await windows.transition(
            db,
            world.window_id,
            actor,
            Transition(action="close", expected_revision=world.window_revision),
        )
        await db.commit()

    async def open_one():
        async with world.maker() as db:
            actor = await db.get(User, world.actor_id)
            try:
                value = await windows.open_window(
                    db, actor, Open(parent_copy_id=world.copy_id)
                )
                await db.commit()
                return value.window_id
            except HTTPException as exc:
                return exc.detail["code"]

    results = await asyncio.gather(open_one(), open_one())
    assert sum(isinstance(value, uuid.UUID) for value in results) == 1
    assert "migration_window_exists" in results
