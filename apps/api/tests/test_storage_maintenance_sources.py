"""Source delegates, connector handles and physical I/O share maintenance gates."""

import asyncio
from contextlib import suppress
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageLocationProbe
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.schemas.dataset_source import SourceSyncScheduleUpdate
from app.services import (
    dataset_scans as scans,
    dataset_registration as registrations,
    dataset_source_sync as sync,
    filesystem_storage as fs,
    storage_access,
    storage_location as locations,
)
from app.services.content_snapshot import ContentError
from app.workers import dataset_source_sync as sync_worker
from tests.test_dataset_content import content_world as content_world, headers

OLD = {"access_key": "old-access", "secret_key": "old-secret"}
CONFIG = {"endpoint": "http://8.8.8.8:9000", "bucket": "data"}


@pytest.fixture
async def connection(httpx_client, super_admin, monkeypatch):
    """Create the owner connection through the published API only."""
    from cryptography.fernet import Fernet

    from app.config import settings

    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "connector_host_allowlist", ["8.8.8.8/32"])
    response = await httpx_client.post(
        "/api/v1/storage-connections",
        headers={"Authorization": f"Bearer {super_admin[1]}"},
        json={
            "name": "original",
            "kind": "s3",
            "config": CONFIG,
            "secret": OLD,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], {"Authorization": f"Bearer {super_admin[1]}"}


async def gate(db, location_id, state, revision):
    # Owner tests use the caller's isolated transaction. Control concurrency and
    # committed receipt behavior are exercised separately below and by core tests.
    await storage_access.check(db, location_id, lock=True)
    await db.execute(
        update(Gate)
        .where(Gate.location_id == location_id)
        .values(state=state, revision=revision)
    )


@pytest.mark.parametrize("operation", ["scan", "resume", "preflight", "schedule"])
async def test_draining_rejects_source_delegates_before_io(
    db_session, content_world, monkeypatch, operation
):
    _, _, actor, _, location, source, scan, _ = content_world

    async def forbidden(*args, **kwargs):
        pytest.fail("Maintenance must reject before probing the source")

    monkeypatch.setattr(locations, "perform_probe", forbidden)
    await gate(db_session, location.id, "draining", 2)
    with pytest.raises(ContentError) as error:
        if operation == "scan":
            await scans.start_scan(db_session, source.id, actor)
        elif operation == "resume":
            await scans.resume_scan(db_session, scan.id, actor)
        elif operation == "preflight":
            await registrations.start_preflight(db_session, scan.id, actor)
        else:
            await sync.configure(
                db_session,
                source.id,
                SourceSyncScheduleUpdate(expected_revision=0, enabled=True),
                actor,
            )
    assert error.value.code in {"storage_maintenance", "storage_maintenance_changed"}


async def test_resume_does_not_replay_old_scan_or_sync_delegation(
    db_session, content_world
):
    _, _, actor, _, location, source, scan, _ = content_world
    row = await sync.configure(
        db_session,
        source.id,
        SourceSyncScheduleUpdate(expected_revision=0, enabled=True),
        actor,
    )
    assert (
        scan.snapshot["maintenance_revision"]
        == row.snapshot["maintenance_revision"]
        == 1
    )
    row.scan_id = scan.id
    row.state = "scanning"
    await gate(db_session, location.id, "active", 4)
    with pytest.raises(ContentError, match="storage_maintenance_changed"):
        await registrations.start_preflight(db_session, scan.id, actor)
    with pytest.raises(ContentError, match="storage_maintenance_changed"):
        await sync.authority(db_session, location, source, row)
    renewed = await sync.configure(
        db_session,
        source.id,
        SourceSyncScheduleUpdate(expected_revision=row.revision, enabled=True),
        actor,
    )
    assert renewed.snapshot["maintenance_revision"] == 4
    assert renewed.scan_id is None and renewed.registration_id is None
    assert renewed.state == "idle"
    # A new request must explicitly verify the worker again in the new generation.
    probe = await db_session.get(
        StorageLocationProbe, (location.id, "worker:content@test")
    )
    probe.result = {**probe.result, "maintenance_revision": 4}
    fresh, created = await scans.start_scan(db_session, source.id, actor)
    assert created and fresh.snapshot["maintenance_revision"] == 4


async def test_directory_checkpoint_rejects_after_maintenance(
    db_session, content_world
):
    _, _, actor, _, location, source, _, _ = content_world
    run, _ = await scans.start_scan(db_session, source.id, actor)
    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db_session, run.id, job.celery_task_id, "content@test")
    for _ in range(3):
        work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
        assert work is not None, "A fresh scan must enumerate its source directory"
        if not work.get("transition"):
            break
    else:
        pytest.fail("Fresh scan did not advance from lease validation to enumeration")
    assert work["phase"] == "enumerating"
    assert work["location_id"] == location.id and work["maintenance_revision"] == 1
    manifest = fs.scan_directory(
        work["config"],
        work["path"],
        expected_anchor=work["anchor"],
        expected_identity=work["identity"],
    )
    before = dict(run.counts)
    await gate(db_session, location.id, "draining", 2)
    with pytest.raises(ContentError, match="storage_maintenance_changed"):
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest
        )
    assert run.counts == before


async def test_retained_reference_context_skips_only_historical_epoch(
    db_session, content_world
):
    _, _, _, _, location, source, scan, _ = content_world
    historic = {
        key: value
        for key, value in scan.snapshot.items()
        if key != "maintenance_revision"
    }
    await gate(db_session, location.id, "active", 4)
    with pytest.raises(ContentError, match="storage_maintenance_changed"):
        await scans.validate_context(db_session, location, source, snapshot=historic)
    await scans.validate_context(
        db_session,
        location,
        source,
        snapshot=historic,
        check_maintenance_snapshot=False,
    )
    await gate(db_session, location.id, "stopped", 5)
    with pytest.raises(ContentError, match="storage_maintenance"):
        await scans.validate_context(
            db_session,
            location,
            source,
            snapshot=historic,
            check_maintenance_snapshot=False,
        )


@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        (kind, operation)
        for kind in ("filesystem", "s3", "sftp")
        for operation in ("probe", "browse")
    ],
)
async def test_cancelled_location_io_retains_receipt_until_thread_closes(
    test_engine, monkeypatch, kind, operation
):
    location_id = uuid.uuid4()
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    location = SimpleNamespace(
        id=location_id,
        kind=kind,
        enabled=True,
        connection_id=uuid.uuid4(),
        config_revision=1,
        config={
            "registration_node": locations.api_node_id(),
            "registration_anchor": {"root": 1},
        },
    )

    def blocking(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5), "Test did not release the source thread"
            if operation == "probe":
                return {"ok": True, "code": "ok", "capabilities": {}}
            if kind == "s3":
                return {"directories": [], "next_cursor": None}
            return [], False, "snapshot"
        finally:
            closed.set()

    if kind == "filesystem":
        monkeypatch.setattr(
            fs,
            "probe_directory" if operation == "probe" else "browse_directory",
            blocking,
        )
    else:

        async def configuration(*_args):
            return SimpleNamespace(
                probe=blocking, browse_directory=blocking, list_page=blocking
            )

        monkeypatch.setattr(
            "app.services.reference_storage.configuration", configuration
        )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    task = None
    try:
        async with maker() as db:
            if operation == "probe":
                call = locations.perform_probe(
                    db, location, locations.api_node_id(), None
                )
            else:
                call = locations.browse(db, location, path="", limit=20, cursor=None)
            task = asyncio.create_task(call)
            assert await asyncio.to_thread(entered.wait, 2)
            async with maker() as observer:
                statement = (
                    select(func.count())
                    .select_from(StorageOperation)
                    .where(StorageOperation.location_id == location_id)
                )
                assert await observer.scalar(statement) == 1
                task.cancel()
                await asyncio.sleep(0.02)
                assert not task.done() and not closed.is_set()
                assert await observer.scalar(statement) == 1
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert closed.is_set()
                assert await observer.scalar(statement) == 0
    finally:
        release.set()
        if task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await task
        async with test_engine.begin() as connection:
            # No business location exists; this test owns only its independent gate.
            await connection.execute(
                delete(Gate).where(Gate.location_id == location_id)
            )


async def test_persisted_connection_test_and_copy_enqueue_reject_maintenance(
    db_session, httpx_client, super_admin, content_world, connection
):
    connection_id, _ = connection
    location_id = locations.connector_location_id(uuid.UUID(connection_id))
    await gate(db_session, location_id, "stopped", 3)
    paths = [
        (f"/api/v1/storage-connections/{connection_id}/test", None),
        (
            f"/api/v1/datasets/{content_world[3].id}/import-from-connection",
            {"connection_id": connection_id},
        ),
    ]
    for path, body in paths:
        response = await httpx_client.post(
            path, headers=headers(super_admin), json=body
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert (
            detail["code"] == "storage_maintenance"
            or detail["reason"] == "storage_maintenance"
        )


def test_sync_and_scan_errors_preserve_maintenance_codes():
    for code in (
        "storage_maintenance",
        "storage_maintenance_changed",
        "storage_maintenance_busy",
    ):
        error = ContentError(code)
        assert scans.failure_code(error, "scan_interrupted") == code
        assert sync_worker.error_code(error) == code
        assert code in sync.BLOCKING
