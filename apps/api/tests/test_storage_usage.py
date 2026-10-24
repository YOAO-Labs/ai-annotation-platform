"""Usage reads are cheap; incomplete inventories never publish false zeroes."""

from datetime import timedelta
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, select, inspect

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_usage import (
    StorageUsageInventory as Inventory,
    StorageUsageSnapshot as Snapshot,
)
from app.services import storage_usage as svc
from app.services.dataset_assets import ensure_legacy_bindings
from app.workers import storage_usage as worker


@pytest.fixture
async def ready(db_session, super_admin, monkeypatch):
    await ensure_legacy_bindings(db_session)

    async def dispatch(db, job):
        await db.commit()

    monkeypatch.setattr(worker, "dispatch", dispatch)
    monkeypatch.setattr(
        worker, "list_page", lambda name, token: {"count": 0, "bytes": 0, "token": None}
    )
    return db_session, super_admin[0]


async def run_all(db, job):
    for _ in range(20):
        await db.refresh(job)
        if job.status not in svc.ACTIVE:
            return
        await worker.execute(db, job.id, job.celery_task_id)
    raise AssertionError("inventory never completed")


async def test_snapshot_reads_and_overview_never_touch_s3(
    ready, httpx_client, auth_headers, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Reads must not access S3")

    monkeypatch.setattr(svc.storage_service.client, "list_objects_v2", forbidden)
    monkeypatch.setattr(svc.storage_service.client, "head_bucket", forbidden)
    monkeypatch.setattr(svc.storage_service, "summarize_bucket", forbidden)
    for path in [
        "/api/v1/storage/buckets",
        "/api/v1/admin/ml-integrations/overview",
    ] * 2:
        response = await httpx_client.get(path, headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        data = data.get("storage", data)
        assert data["bucket_count"] == 9
        assert data["measured_bucket_count"] == 0
        assert data["total_size_bytes"] is None
        assert data["physical_total_bytes"] is None
        assert {role for item in data["items"] for role in item["roles"]} == set(
            svc.PURPOSE_BUCKET_ATTRIBUTES
        )
        assert all(
            item["object_count"] is None and item["status"] == "unknown"
            for item in data["items"]
        )
        assert data["uncovered_binding_purposes"] == []


@pytest.mark.parametrize(
    "target_endpoint,target_bucket,uncovered",
    [
        (None, None, False),
        (None, "new-avatar-bucket", True),
        ("http://different.example:9000", None, True),
    ],
)
async def test_current_binding_coverage_uses_endpoint_and_bucket_without_io(
    ready, monkeypatch, target_endpoint, target_bucket, uncovered
):
    db, _ = ready

    def forbidden(*args, **kwargs):
        raise AssertionError("Coverage reads must not access S3")

    monkeypatch.setattr(svc.storage_service.client, "list_objects_v2", forbidden)
    monkeypatch.setattr(svc.storage_service.client, "head_bucket", forbidden)
    connection = StorageConnection(
        name="Avatar coverage target",
        kind="s3",
        scope="global",
        config={
            "endpoint": target_endpoint or svc.settings.minio_endpoint,
            "use_ssl": svc.settings.minio_use_ssl,
            "bucket": target_bucket or svc.storage_service.avatars_bucket,
            "base_prefix": "new-managed-prefix",
        },
        secret_enc=b"not-read-by-coverage",
    )
    db.add(connection)
    await db.flush()
    location = StorageLocation(
        name="Avatar coverage location",
        kind="s3",
        config={},
        connection_id=connection.id,
    )
    db.add(location)
    await db.flush()
    binding = await db.get(StorageBinding, "avatars")
    binding.location_id = location.id
    binding.bucket = connection.config["bucket"]
    binding.generation = 2
    await db.flush()
    result = await svc.read_usage(db)
    assert result.uncovered_binding_purposes == (["avatars"] if uncovered else [])
    assert result.bucket_count == 9
    assert svc.storage_service.avatars_bucket in {item.name for item in result.items}
    assert result.total_size_bytes is None


async def test_shared_bucket_multi_page_and_true_zero(ready, monkeypatch):
    db, actor = ready
    monkeypatch.setattr(
        svc.storage_service, "datasets_bucket", svc.storage_service.bucket
    )
    binding = await db.scalar(
        select(StorageBinding).where(StorageBinding.purpose == "datasets")
    )
    binding.bucket = svc.storage_service.bucket
    await db.commit()
    calls = []

    def page(name, token):
        calls.append((name, token))
        if name == svc.storage_service.bucket:
            return {
                "count": 2 if not token else 1,
                "bytes": 5 if not token else 7,
                "token": "opaque" if not token else None,
            }
        return {"count": 0, "bytes": 0, "token": None}

    monkeypatch.setattr(worker, "list_page", page)
    job, reused = await svc.start(db, actor)
    again, reused_again = await svc.start(db, actor)
    assert not reused and reused_again and again.id == job.id
    await db.commit()
    await run_all(db, job)
    result = await svc.read_usage(db)
    assert result.bucket_count == result.measured_bucket_count == 8
    assert result.total_size_bytes == 12 and result.total_object_count == 3
    shared = next(i for i in result.items if i.name == svc.storage_service.bucket)
    assert shared.roles == ["annotations", "datasets"]
    assert len(calls) == 9
    assert all(
        i.status == "ok" and i.coverage == "complete" and i.is_estimate
        for i in result.items
    )
    assert all(i.measurement_started_at <= i.measured_at for i in result.items)
    assert job.status == "completed"
    # Job retention cannot delete observation evidence.
    await db.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
    await db.commit()
    assert (await svc.read_usage(db)).total_size_bytes == 12
    assert (await svc.read_usage(db)).inventory_job_id is None


def test_provider_page_counts_directory_markers_and_rejects_loop(monkeypatch):
    client = MagicMock()
    client.list_objects_v2.return_value = {
        "Contents": [{"Key": "folder/", "Size": 9}, {"Key": "empty/", "Size": 0}],
        "IsTruncated": False,
    }
    monkeypatch.setattr(worker, "_client", lambda: client)
    assert worker.list_page("bucket", None) == {
        "count": 2,
        "bytes": 9,
        "token": None,
        "objects": [{"key": "folder/", "size": 9}, {"key": "empty/", "size": 0}],
    }
    client.list_objects_v2.assert_called_once_with(Bucket="bucket", MaxKeys=1000)
    client.list_objects_v2.return_value = {
        "IsTruncated": True,
        "NextContinuationToken": "same",
    }
    with pytest.raises(ValueError):
        worker.list_page("bucket", "same")


async def seed_success(db):
    target = svc.configured_scope()[0]
    row = Snapshot(
        scope_key=target["key"],
        object_count=7,
        total_size_bytes=123,
        measured_at=svc.now() - timedelta(hours=1),
        measurement_started_at=svc.now() - timedelta(hours=2),
        attempt_status="completed",
    )
    db.add(row)
    await db.commit()
    return row


async def test_second_page_failure_retains_success_and_continues_other_buckets(
    ready, monkeypatch
):
    db, actor = ready
    row = await seed_success(db)
    measured = row.measured_at

    def page(name, token):
        if name == svc.configured_scope()[0]["name"]:
            if token:
                raise OSError("secret provider URL and token must not escape")
            return {"count": 5, "bytes": 900, "token": "next"}
        return {"count": 0, "bytes": 0, "token": None}

    monkeypatch.setattr(worker, "list_page", page)
    job, _ = await svc.start(db, actor)
    await db.commit()
    await run_all(db, job)
    result = await svc.read_usage(db)
    assert job.status == "failed"
    assert job.result["failed_count"] == 1 and job.result["success_count"] == 8
    assert result.items[0].total_size_bytes == 123
    assert result.items[0].measured_at == measured
    assert result.items[0].status == "stale"
    assert result.items[0].error == "object_listing_failed"
    assert "secret" not in job.error_message


@pytest.mark.parametrize("reason", ["cancel", "config", "revoke", "key", "budget"])
async def test_incomplete_inventory_preserves_success(ready, monkeypatch, reason):
    db, actor = ready
    original = await seed_success(db)
    measured = original.measured_at
    key = None
    if reason == "key":
        key = ApiKey(
            user_id=actor.id,
            name="inventory",
            key_prefix="ak_inventory",
            key_hash="proof",
            scopes=["*"],
        )
        db.add(key)
        await db.flush()
    job, _ = await svc.start(db, actor, key.id if key else None)
    await db.commit()
    inventory = await db.get(Inventory, "platform")
    if reason == "cancel":
        await svc.cancel(db, job.id)
        await db.commit()
    elif reason == "config":
        monkeypatch.setattr(svc.storage_service, "bucket", "new-bucket")
    elif reason == "revoke":
        actor.role = "project_admin"
        await db.commit()
    elif reason == "key":
        key.revoked_at = svc.now()
        await db.commit()
    else:
        inventory.started_at = svc.now() - timedelta(days=2)
        await db.commit()
    await run_all(db, job)
    await db.refresh(original)
    assert original.total_size_bytes == 123 and original.measured_at == measured
    assert job.status == ("cancelled" if reason == "cancel" else "failed")


async def test_recovery_uses_checkpoint_and_fences_late_page(ready, monkeypatch):
    db, actor = ready
    job, _ = await svc.start(db, actor)
    await db.commit()
    inventory = await db.get(Inventory, "platform")
    old_task, generation = job.celery_task_id, inventory.generation
    inventory.status = "running"
    inventory.checkpoint = {
        "count": 2,
        "bytes": 5,
        "pages": 1,
        "token": "resume",
        "started_at": svc.now().isoformat(),
    }
    inventory.lease_until = svc.now() - timedelta(seconds=1)
    await db.commit()
    await worker.recover(db)
    await db.refresh(job)
    assert job.celery_task_id != old_task
    assert await svc.fenced(db, job.id, old_task, generation) is None
    await db.rollback()
    seen = []

    def page(name, token):
        seen.append(token)
        return {"count": 1, "bytes": 8, "token": None}

    monkeypatch.setattr(worker, "list_page", page)
    await run_all(db, job)
    assert seen[0] == "resume"
    assert (await svc.read_usage(db)).items[0].total_size_bytes == 13
    # A late old failure signal cannot overwrite the completed new attempt.
    await svc.signal_interrupted(db, job.id, old_task, cancelled=False)
    assert job.status == "completed"


async def test_in_flight_cancel_discards_page(ready, monkeypatch):
    db, actor = ready
    await seed_success(db)
    job, _ = await svc.start(db, actor)
    await db.commit()
    inventory = await svc.locked(db)
    inventory.status = "running"
    generation = inventory.generation
    await db.commit()
    original_to_thread = worker.asyncio.to_thread

    async def cancelled_during_io(fn, *args):
        if fn is worker.list_page:
            await svc.cancel(db, job.id)
            await db.commit()
            return {"count": 90, "bytes": 999, "token": None}
        return await original_to_thread(fn, *args)

    monkeypatch.setattr(worker.asyncio, "to_thread", cancelled_during_io)
    assert not await worker.process_page(db, job.id, job.celery_task_id, generation)
    assert (await svc.read_usage(db)).items[0].total_size_bytes == 123


async def test_dispatch_failure_leaves_recoverable_lease(ready, monkeypatch):
    db, actor = ready
    job, _ = await svc.start(db, actor)

    def fail(*args):
        raise OSError("broker unavailable")

    monkeypatch.setattr(worker, "_publish", fail)
    # Use actual dispatch rather than fixture's stub.
    # The saved implementation is available below without reloading global task registration.
    await REAL_DISPATCH(db, job)
    inventory = await db.get(Inventory, "platform")
    assert inventory.status == "pending" and inventory.lease_until is not None
    inventory.lease_until = svc.now() - timedelta(seconds=1)
    await db.commit()
    await worker.recover(db)
    await run_all(db, job)
    assert job.status == "completed"


REAL_DISPATCH = worker.dispatch


async def test_unknown_partial_stale_and_scope_identity(ready, monkeypatch):
    db, _ = ready
    row = await seed_success(db)
    row.measured_at = svc.now() - timedelta(days=2)
    await db.commit()
    result = await svc.read_usage(db)
    assert result.total_size_bytes is None and result.measured_bucket_count == 1
    assert result.items[0].status == "stale"
    monkeypatch.setattr(svc.settings, "minio_endpoint", "changed.invalid")
    result = await svc.read_usage(db)
    assert result.measured_bucket_count == 0


async def test_inventory_api_and_job_authority(
    ready, httpx_client, auth_headers, project_admin
):
    db, actor = ready
    response = await httpx_client.post(
        "/api/v1/storage/usage-inventory", headers=auth_headers
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert (
        await httpx_client.post("/api/v1/storage/usage-inventory", headers=auth_headers)
    ).json() == {"job_id": job_id, "reused": True}
    job = await db.get(AsyncJob, uuid.UUID(job_id))
    assert set(job.payload) == {"scope", "bucket_count"}
    actor.role = "project_admin"
    await db.commit()
    for path in ["/api/v1/storage/buckets", f"/api/v1/async-jobs/{job_id}"]:
        assert (await httpx_client.get(path, headers=auth_headers)).status_code == 403
    assert (
        await httpx_client.post(
            f"/api/v1/async-jobs/{job_id}/cancel", headers=auth_headers
        )
    ).status_code == 403
    response = await httpx_client.get(
        "/api/v1/async-jobs?kind=storage_usage", headers=auth_headers
    )
    assert response.json()["total"] == 0
    assert (
        await httpx_client.post(
            "/api/v1/storage/usage-inventory",
            headers={"Authorization": f"Bearer {project_admin[1]}"},
        )
    ).status_code == 403


@pytest.mark.parametrize("scopes", [[], ["datasets:read", "datasets:write"], ["*"]])
async def test_api_key_scope_applies_to_snapshots_and_jobs(
    ready, httpx_client, monkeypatch, scopes
):
    db, actor = ready
    from app.core import security

    key = ApiKey(
        user_id=actor.id,
        name="test",
        key_prefix="ak_scope",
        key_hash=security.hash_password("ak_inventory-secret"),
        scopes=scopes,
    )
    db.add(key)
    await db.commit()
    # Match authentication's normal prefix lookup.
    key.key_prefix = "ak_inventory-secret"[:12]
    await db.commit()
    job, _ = await svc.start(db, actor)
    await db.commit()
    headers = {"Authorization": "Bearer ak_inventory-secret"}
    expected = 200 if scopes == ["*"] else 403
    for path in [
        "/api/v1/storage/buckets",
        "/api/v1/admin/ml-integrations/overview",
        f"/api/v1/async-jobs/{job.id}",
    ]:
        response = await httpx_client.get(path, headers=headers)
        assert response.status_code == expected, response.text
    response = await httpx_client.get(
        "/api/v1/async-jobs?kind=storage_usage", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["total"] == (1 if scopes == ["*"] else 0)
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel", headers=headers
    )
    assert response.status_code == (200 if scopes == ["*"] else 403)


async def test_migration_roundtrip(db_session):
    path = Path(__file__).parents[1] / "alembic/versions/0209_storage_usage.py"
    spec = importlib.util.spec_from_file_location("usage_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()

    def roundtrip(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            assert "storage_usage_snapshots" not in inspect(conn).get_table_names()
            migration.upgrade()
            assert "storage_usage_inventories" in inspect(conn).get_table_names()

    await connection.run_sync(roundtrip)


async def test_chunk_continuation_and_duplicate_delivery(ready, monkeypatch):
    db, actor = ready
    calls = []

    def page(name, token):
        number = int(token or 0)
        calls.append((name, number))
        return {
            "count": 1,
            "bytes": 2,
            "token": str(number + 1) if number < 11 else None,
        }

    monkeypatch.setattr(worker, "list_page", page)
    job, _ = await svc.start(db, actor)
    await db.commit()
    original_task = job.celery_task_id
    await worker.execute(db, job.id, original_task)
    assert len(calls) == 10
    assert job.celery_task_id != original_task
    await worker.execute(db, job.id, original_task)
    assert len(calls) == 10
    for _ in range(20):
        await db.refresh(job)
        if job.status not in svc.ACTIVE:
            break
        await worker.execute(db, job.id, job.celery_task_id)
    assert job.status == "completed"
    assert len(calls) == 9 * 12
    result = await svc.read_usage(db)
    assert result.total_object_count == 108 and result.total_size_bytes == 216


async def test_page_budget_never_publishes_partial_counts(ready, monkeypatch):
    db, actor = ready
    original = await seed_success(db)
    monkeypatch.setattr(svc, "MAX_PAGES", 1)
    monkeypatch.setattr(
        worker, "list_page", lambda *args: {"count": 9, "bytes": 999, "token": "next"}
    )
    job, _ = await svc.start(db, actor)
    await db.commit()
    await run_all(db, job)
    await db.refresh(original)
    assert original.total_size_bytes == 123
    assert original.error_code == "inventory_budget_exceeded"
    assert job.status == "failed"


@pytest.mark.parametrize("change", ["role", "key", "config"])
async def test_authority_rechecked_after_io_before_publication(
    ready, monkeypatch, change
):
    db, actor = ready
    original = await seed_success(db)
    key = ApiKey(
        user_id=actor.id,
        name="inventory",
        key_prefix="ak_inventory",
        key_hash="proof",
        scopes=["*"],
    )
    db.add(key)
    await db.flush()
    job, _ = await svc.start(db, actor, key.id)
    await db.commit()
    original_to_thread = worker.asyncio.to_thread

    async def changed_during_io(fn, *args):
        if fn is worker.list_page:
            if change == "role":
                actor.role = "project_admin"
            elif change == "key":
                key.revoked_at = svc.now()
            else:
                monkeypatch.setattr(svc.storage_service, "bucket", "changed")
            await db.commit()
            return {"count": 90, "bytes": 999, "token": None}
        return await original_to_thread(fn, *args)

    monkeypatch.setattr(worker.asyncio, "to_thread", changed_during_io)
    await worker.execute(db, job.id, job.celery_task_id)
    await db.refresh(job)
    await db.refresh(original)
    assert job.status == "failed"
    assert original.total_size_bytes == 123


async def test_process_loss_signal_fences_old_delivery_and_recovers_checkpoint(ready):
    db, actor = ready
    job, _ = await svc.start(db, actor)
    await db.commit()
    inventory = await svc.locked(db)
    inventory.status, job.status = "running", "running"
    inventory.checkpoint = {
        "count": 2,
        "bytes": 5,
        "pages": 1,
        "token": "resume",
        "started_at": svc.now().isoformat(),
    }
    old_task, generation = job.celery_task_id, inventory.generation
    await db.commit()
    await svc.signal_interrupted(db, job.id, old_task, cancelled=False)
    await db.commit()
    assert inventory.status == "pending" and job.status == "running"
    assert inventory.checkpoint["token"] == "resume"
    assert job.celery_task_id != old_task and inventory.generation > generation
    task_id = job.celery_task_id
    await svc.signal_interrupted(db, job.id, old_task, cancelled=False)
    assert job.celery_task_id == task_id
    await worker.recover(db)
    await run_all(db, job)
    assert job.status == "completed"
    assert (await svc.read_usage(db)).items[0].total_size_bytes == 5


async def test_completed_response_reads_snapshots_after_terminal_inventory_state(
    ready, monkeypatch
):
    db, actor = ready
    job, _ = await svc.start(db, actor)
    await db.commit()
    original_get = db.get
    raced = False

    async def completed_between_queries(model, identity, **kwargs):
        nonlocal raced
        if model is Inventory and not raced:
            raced = True
            await run_all(db, job)
        return await original_get(model, identity, **kwargs)

    monkeypatch.setattr(db, "get", completed_between_queries)
    response = await svc.read_usage(db)
    assert response.inventory_status == "completed"
    assert response.measured_bucket_count == response.bucket_count == 9
    assert response.total_size_bytes == 0
