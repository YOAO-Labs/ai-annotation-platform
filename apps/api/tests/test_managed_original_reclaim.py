"""Owned committed schemas exercise real claims, callbacks, permits and API CAS."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import threading
import io
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.base import Base
from app.db.models.async_job import AsyncJob
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.managed_original_reclaim import (
    ManagedOriginalReclaimAttempt as Attempt,
    ManagedOriginalReclaimClaim as Claim,
)
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.user import User
from app.deps import get_db
from app.main import app
from app.schemas.managed_original_reclaim import (
    ManagedOriginalReclaimReview as Review,
    ManagedOriginalReclaimApply as Apply,
)
from app.services import (
    managed_original_reclaim as service,
    managed_original_creation as creations,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_reclaim_io import ReclaimResult
from app.services.storage import storage_service
from app.workers import managed_original_reclaim as worker
from tests import live_provider_guard
from tests.factory import create_user
from tests.test_managed_original_creation import migration
from tests.test_managed_original_reclamation import assertion, declared_hold


@pytest.fixture
async def reclaim_db(test_engine):
    # Clone only empty schema structure, never development rows. Independent
    # callback commits and permanent trigger facts are removed by namespace DROP.
    schema = "reclaim_test_" + uuid.uuid4().hex
    excluded = {
        "alembic_version",
        "managed_original_reclaim_attempts",
        "managed_original_reclaim_claims",
        "managed_original_reference_reconciliation",
    }
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        names = await connection.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
        )
        for name in list(names):
            if name not in excluded and name in Base.metadata.tables:
                await connection.execute(
                    text(
                        f'CREATE TABLE "{schema}"."{name}" (LIKE public."{name}" INCLUDING ALL)'
                    )
                )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: migration(
                    sync, "0260_managed_original_reclaim.py"
                ).upgrade()
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            actor = await create_user(
                db, "super_admin", f"{uuid.uuid4()}@reclaim.test", "Reclaim operator"
            )
            await db.commit()
            yield db, actor
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def seed(db, *, size=12, version="immutable-version-1", real=False):
    """Real writer option is shared with the small live acceptance setup."""
    stamp = datetime.now(timezone.utc) - timedelta(days=1)
    payload = b"original-data"[:size] if size <= 13 else b"x" * size
    if real:
        assert size == len(payload)
        allocation = await creations.allocate(
            db,
            dataset_id=uuid.uuid4(),
            logical_path=f"reclaim/{uuid.uuid4()}.bin",
            origin="zip",
            max_bytes=len(payload),
            content_type="application/octet-stream",
        )
        created = await creations.create_stream(
            db, allocation, io.BytesIO(payload), content_type="application/octet-stream"
        )
        creation = await db.get(Creation, created.creation_id, populate_existing=True)
        version = creation.version_id
    else:
        sha = hashlib.sha256(payload).hexdigest()
        binding = await ensure_legacy_bindings(db)
        identity = creations.managed_storage_identity(
            storage_service.client.meta.endpoint_url, binding.bucket
        )
        creation = Creation(
            id=uuid.uuid4(),
            owner_kind="dataset",
            dataset_id=uuid.uuid4(),
            origin="zip",
            location_id=LEGACY_LOCATION_ID,
            bucket=binding.bucket,
            object_key="fixture/" + uuid.uuid4().hex,
            logical_path="fixture.bin",
            storage_identity=identity,
            max_bytes=len(payload),
            timeout_seconds=60,
            content_type="application/octet-stream",
            authority={},
            configuration={"schema": 1},
            input_fingerprint="a" * 64,
            maintenance_revision=1,
            status="verified",
            operation_id=uuid.uuid4(),
            prepared={"fixture": True},
            sha256=sha,
            md5=hashlib.md5(payload).hexdigest(),
            size_bytes=len(payload),
            receipt={
                "etag": '"fixture"',
                "version_id": version,
                "response": {
                    "VersionId": version,
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                },
            },
            etag='"fixture"',
            version_id=version,
            verification={"sha256": sha, "size_bytes": len(payload)},
            claimed_at=stamp,
            frozen_at=stamp,
            write_started_at=stamp,
            acknowledged_at=stamp,
            readback_at=stamp,
            settled_at=stamp,
            completed_at=stamp,
        )
        db.add(creation)
        await db.flush()
    anchor = await assertion(
        db,
        creation_id=creation.id,
        dataset_id=creation.dataset_id,
        object_key=creation.object_key,
        bucket=creation.bucket,
        storage_identity=creation.storage_identity,
        sha256=creation.sha256,
        size_bytes=creation.size_bytes,
        retain_until=stamp,
    )
    gate = await db.get(Gate, LEGACY_LOCATION_ID)
    if gate is None:
        gate = Gate(location_id=LEGACY_LOCATION_ID, state="stopped", revision=7)
        db.add(gate)
    else:
        gate.state, gate.revision = "stopped", 7
    await db.commit()
    return anchor, creation, payload


async def start(db, actor, anchor, *, retry=None):
    await db.refresh(actor)
    await db.refresh(anchor)
    request = Review(reason="Reviewed exact version expiry", retry_of_id=retry)
    preview = await service.preview(db, anchor.id, request, actor)
    assert preview.eligible, preview.blockers
    row, job, _ = await service.start(
        db,
        anchor.id,
        Apply(**request.model_dump(), impact_token=preview.impact_token),
        actor,
    )
    await db.commit()
    return row, job, preview


async def running(db, actor, anchor):
    row, job, preview = await start(db, actor, anchor)
    assert await service.claim(db, job.id, row.task_id)
    await db.commit()
    return row, job, preview


def verified(row):
    return {
        "version_id": row.version_id,
        "sha256": row.expected_sha256,
        "size_bytes": row.expected_size_bytes,
        "etag": '"fixture"',
    }


async def test_preview_cas_reason_actor_full_group_and_claim_replay(reclaim_db):
    db, actor = reclaim_db
    anchor, creation, _ = await seed(db)
    request = Review(reason="Reviewed exact version expiry")
    preview = await service.preview(db, anchor.id, request, actor)
    assert preview.version_id == creation.version_id and preview.eligible
    await declared_hold(db, anchor, actor)
    await db.commit()
    with pytest.raises(Exception) as changed:
        await service.start(
            db,
            anchor.id,
            Apply(**request.model_dump(), impact_token=preview.impact_token),
            actor,
        )
    assert changed.value.detail["code"] == "reclaim_preview_changed"
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 0


async def test_cancel_pending_releases_only_no_delete_claim_and_invalidates_token(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, preview = await start(db, actor, anchor)
    assert await service.cancel(db, job.id) == "cancelled"
    await db.commit()
    claim = await db.scalar(select(Claim))
    assert claim.state == "released" and row.physical_settled
    with pytest.raises(Exception) as replay:
        await service.start(
            db,
            anchor.id,
            Apply(
                reason="Reviewed exact version expiry",
                impact_token=preview.impact_token,
            ),
            actor,
        )
    assert replay.value.detail["code"] == "reclaim_preview_changed"


async def test_exact_task_redispatch_and_gate_change_never_acquire(reclaim_db):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await start(db, actor, anchor)
    assert (await service.redispatch(db, row.id, actor))[1].id == job.id
    assert await service.claim(db, job.id, "stale-task") is None
    gate = await db.get(Gate, LEGACY_LOCATION_ID)
    gate.revision += 1
    await db.commit()
    assert await service.claim(db, job.id, row.task_id) is None
    await db.commit()
    assert row.status == "failed" and row.operation_id is None


async def test_intent_late_ack_and_absence_survive_revocation_and_cancellation(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await running(db, actor, anchor)
    permit = await service.acquire(db, row.id, row.task_id)
    data = verified(row)
    await service.checkpoint(db, row.id, row.task_id, "read_verified", data)
    await service.checkpoint(db, row.id, row.task_id, "delete_started", data)
    await db.execute(update(User).where(User.id == actor.id).values(is_active=False))
    await db.commit()
    await service.checkpoint(
        db,
        row.id,
        row.task_id,
        "delete_ack",
        {
            "version_id": row.version_id,
            "http_status": 204,
            "request_id": "real-receipt",
            "delete_marker": False,
        },
    )
    await service.checkpoint(
        db,
        row.id,
        row.task_id,
        "version_absent",
        {"version_id": row.version_id, "recovered": False},
    )
    await permit._settle()
    await service.publish(db, job.id, row.task_id, cancelled=True, settled=True)
    await db.commit()
    await db.refresh(row)
    assert (
        row.status == "completed"
        and row.outcome == "deleted"
        and row.delete_ack["request_id"] == "real-receipt"
    )
    assert (await db.scalar(select(Claim))).state == "deleted"


async def test_intent_commit_then_callback_reply_failure_keeps_permanent_tombstone(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await running(db, actor, anchor)
    permit = await service.acquire(db, row.id, row.task_id)
    await service.checkpoint(db, row.id, row.task_id, "delete_started", verified(row))
    await permit._settle()
    # Native may still report delete_started=False after its callback committed.
    await service.publish(
        db,
        job.id,
        row.task_id,
        result=ReclaimResult("failed", "reclaim_failed"),
        settled=True,
    )
    await db.commit()
    await db.refresh(row)
    claim = await db.scalar(select(Claim))
    assert row.status == "unknown" and claim.state == "tombstone"
    retry, retry_job, _ = await start(db, actor, anchor, retry=row.id)
    assert claim.state == "tombstone" and retry.target == row.target
    assert await service.cancel(db, retry_job.id) == "cancelled"
    await db.commit()
    assert claim.state == "tombstone"


async def test_unsettled_operation_has_no_ttl_retry_or_resume(reclaim_db):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await running(db, actor, anchor)
    permit = await service.acquire(db, row.id, row.task_id)
    await service.publish(
        db, job.id, row.task_id, error_code="reclaim_failed", settled=False
    )
    await db.commit()
    await db.refresh(row)
    preview = await service.preview(
        db,
        anchor.id,
        Review(reason="Reviewed unknown operation", retry_of_id=row.id),
        actor,
    )
    assert not preview.eligible and not row.physical_settled
    assert await db.get(StorageOperation, permit.id)
    from app.services.managed_original_reclaim_fence import require_resumable

    with pytest.raises(Exception):
        await require_resumable(db, LEGACY_LOCATION_ID)


@pytest.mark.parametrize(
    "table,statement",
    [
        ("attempt", "DELETE FROM managed_original_reclaim_attempts"),
        ("claim", "DELETE FROM managed_original_reclaim_claims"),
        (
            "version",
            "UPDATE managed_original_reclaim_attempts SET version_id='replacement'",
        ),
        (
            "job",
            "UPDATE managed_original_reclaim_attempts SET async_job_id=gen_random_uuid()",
        ),
    ],
)
async def test_durable_identity_cannot_be_erased_or_repointed(
    reclaim_db, table, statement
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    await start(db, actor, anchor)
    params = {}
    if table == "job":
        replacement = await service.jobs.create_job(
            db,
            kind=service.KIND,
            user_id=actor.id,
            celery_task_id=str(uuid.uuid4()),
            payload={},
        )
        await db.flush()
        statement = (
            "UPDATE managed_original_reclaim_attempts SET async_job_id=:replacement"
        )
        params["replacement"] = replacement.id
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(text(statement), params)


async def test_tombstone_cannot_be_reset_to_claimed_or_released(reclaim_db):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, _, _ = await running(db, actor, anchor)
    await service.acquire(db, row.id, row.task_id)
    await service.checkpoint(db, row.id, row.task_id, "delete_started", verified(row))
    for state in ("claimed", "released"):
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "UPDATE managed_original_reclaim_claims SET state=:state,revision=revision+1"
                    ),
                    {"state": state},
                )


async def test_reference_audit_real_sql_and_terminal_job_recovery(reclaim_db):
    db, actor = reclaim_db
    await seed(db)
    job = await service.start_reconciliation(db, actor)
    await db.commit()
    assert await service.reconcile(db, job.id, job.celery_task_id)
    result = await service.reconciliation_out(db)
    assert result.status == "completed" and result.counts["total_assertions"] == 1
    measured = result.last_success_at
    await db.refresh(actor)
    new_job = await service.start_reconciliation(db, actor)
    new_job.status = "failed"
    await db.commit()
    replacement = await service.start_reconciliation(db, actor)
    assert replacement.id != new_job.id
    assert (await service.reconciliation_out(db)).last_success_at == measured


async def test_http_preview_commit_and_worker_real_small_version_delete(
    reclaim_db, monkeypatch
):
    db, actor = reclaim_db
    bucket = live_provider_guard.owned_bucket(
        storage_service.datasets_bucket, purpose="datasets"
    )
    storage_service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    anchor, creation, payload = await seed(db, size=12, real=True)
    key = creation.object_key
    later = storage_service.client.put_object(
        Bucket=bucket, Key=key, Body=b"later-version"
    )

    async def independent():
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as session:
            yield session

    app.dependency_overrides[get_db] = independent
    monkeypatch.setattr(worker, "_publish", lambda *args, **kwargs: None)
    token = create_access_token(str(actor.id), actor.role)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            base = f"/api/v1/storage/managed-original-retirements/{anchor.id}"
            response = await client.post(
                base + "/reclaim-preview",
                json={"reason": "Reviewed exact version expiry"},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            preview = response.json()
            response = await client.post(
                base + "/reclaims",
                json={
                    "reason": "Reviewed exact version expiry",
                    "impact_token": preview["impact_token"],
                },
                headers=headers,
            )
            assert response.status_code == 202, response.text
            job_id = uuid.UUID(response.json()["async_job_id"])
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as worker_db:
            job = await worker_db.get(AsyncJob, job_id)
            assert await worker.execute(worker_db, job.id, job.celery_task_id)
        await db.rollback()
        row = await db.scalar(select(Attempt))
        assert (
            row.status == "completed"
            and row.outcome == "deleted"
            and row.expected_size_bytes == len(payload)
        )
        assert row.physical_settled and row.delete_ack and row.absence_verified_at
        versions = storage_service.client.list_object_versions(
            Bucket=bucket, Prefix=key
        )
        exact = [value for value in versions.get("Versions", []) if value["Key"] == key]
        assert [value["VersionId"] for value in exact] == [later["VersionId"]]
        assert not await db.scalar(select(func.count()).select_from(StorageOperation))
    finally:
        app.dependency_overrides.pop(get_db, None)
        versions = storage_service.client.list_object_versions(
            Bucket=bucket, Prefix=key
        )
        for value in versions.get("Versions", []) + versions.get("DeleteMarkers", []):
            if value["Key"] == key:
                storage_service.client.delete_object(
                    Bucket=bucket, Key=value["Key"], VersionId=value["VersionId"]
                )


async def test_worker_repeated_cancel_drains_real_thread_before_permit_and_prevents_delete(
    reclaim_db, monkeypatch
):
    import app.services.managed_original_reclaim_io as physical

    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await start(db, actor, anchor)
    identity, task_id, attempt_id = job.id, row.task_id, row.id
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    deletes = []

    def native(candidate, *, checkpoint, **kwargs):
        checkpoint(
            "read_verified",
            {
                "version_id": candidate.version_id,
                "sha256": candidate.sha256,
                "size_bytes": candidate.size_bytes,
                "etag": '"fixture"',
            },
        )
        entered.set()
        assert release.wait(10)
        try:
            checkpoint(
                "delete_started",
                {
                    "version_id": candidate.version_id,
                    "sha256": candidate.sha256,
                    "size_bytes": candidate.size_bytes,
                    "etag": '"fixture"',
                },
            )
            deletes.append(True)
        except Exception:
            return ReclaimResult("failed", "reclaim_cancelled")
        finally:
            ended.set()

    async def options(*args):
        return {}

    monkeypatch.setattr(physical, "run_reclaim", native)
    monkeypatch.setattr(service, "io_options", options)

    async def execute():
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as owned:
            return await worker.execute(owned, identity, task_id)

    task = asyncio.create_task(execute())
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        async with async_sessionmaker(db.bind)() as observer:
            assert (
                await observer.scalar(
                    select(func.count()).select_from(StorageOperation)
                )
                == 1
            )
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set() and not deletes
    await db.rollback()
    row = await db.get(Attempt, attempt_id, populate_existing=True)
    assert (
        row.status == "cancelled"
        and row.physical_settled
        and row.delete_intent_at is None
    )
    assert not await db.scalar(select(func.count()).select_from(StorageOperation))


async def test_authority_change_before_delete_preserves_read_fact_and_no_intent(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await running(db, actor, anchor)
    permit = await service.acquire(db, row.id, row.task_id)
    data = verified(row)
    await service.checkpoint(db, row.id, row.task_id, "read_verified", data)
    await db.execute(update(User).where(User.id == actor.id).values(is_active=False))
    await db.commit()
    with pytest.raises(Exception) as revoked:
        await service.checkpoint(db, row.id, row.task_id, "delete_started", data)
    assert revoked.value.status_code == 403
    await permit._settle()
    await service.publish(
        db, job.id, row.task_id, error_code="reclaim_authority_changed", settled=True
    )
    await db.commit()
    await db.refresh(row)
    assert row.read_verified and row.delete_intent_at is None
    assert (await db.scalar(select(Claim))).state == "released"


async def test_audit_failure_keeps_start_mutations_atomic(reclaim_db, monkeypatch):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    review = Review(reason="Reviewed exact version expiry")
    preview = await service.preview(db, anchor.id, review, actor)

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service.AuditService, "log", fail)
    with pytest.raises(RuntimeError):
        await service.start(
            db,
            anchor.id,
            Apply(**review.model_dump(), impact_token=preview.impact_token),
            actor,
        )
    assert not await db.scalar(select(func.count()).select_from(Attempt))
    assert not await db.scalar(select(func.count()).select_from(Claim))
    assert not await db.scalar(
        select(func.count()).select_from(AsyncJob).where(AsyncJob.kind == service.KIND)
    )


@pytest.mark.parametrize("version", [None, ""])
async def test_unpinned_creation_is_observationally_blocked(reclaim_db, version):
    db, actor = reclaim_db
    # The storage ledger permits historical ACKs without an immutable version.
    anchor, _, _ = await seed(db, version=version or None)
    value = await service.preview(
        db, anchor.id, Review(reason="Reviewed exact version expiry"), actor
    )
    assert not value.eligible and "reclaim_immutable_version_required" in value.blockers
    assert value.impact_token is None


async def test_cross_actor_confirmation_and_concurrent_group_claim_are_fenced(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    other = await create_user(
        db, "super_admin", f"{uuid.uuid4()}@reclaim.test", "Other operator"
    )
    await db.commit()
    actor_id, other_id, anchor_id = actor.id, other.id, anchor.id
    review = Review(reason="Reviewed exact version expiry")
    preview = await service.preview(db, anchor.id, review, actor)
    with pytest.raises(Exception) as wrong_actor:
        await service.start(
            db,
            anchor.id,
            Apply(**review.model_dump(), impact_token=preview.impact_token),
            other,
        )
    assert wrong_actor.value.detail["code"] == "reclaim_preview_changed"
    await db.rollback()
    async with (
        async_sessionmaker(db.bind, expire_on_commit=False)() as first,
        async_sessionmaker(db.bind, expire_on_commit=False)() as second,
    ):
        actor = await first.get(User, actor_id)
        other = await second.get(User, other_id)
        p1 = await service.preview(first, anchor_id, review, actor)
        p2 = await service.preview(second, anchor_id, review, other)
        await service.start(
            first,
            anchor_id,
            Apply(**review.model_dump(), impact_token=p1.impact_token),
            actor,
        )
        with pytest.raises(Exception) as competing:
            await service.start(
                second,
                anchor_id,
                Apply(**review.model_dump(), impact_token=p2.impact_token),
                other,
            )
        assert competing.value.status_code == 409
        await first.commit()
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 1


async def test_new_cancel_during_permit_settlement_overrides_original_failure(
    reclaim_db, monkeypatch
):
    import app.services.managed_original_reclaim_io as physical
    from app.services import storage_access

    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await start(db, actor, anchor)
    identity, task_id = job.id, row.task_id
    entered, release = asyncio.Event(), asyncio.Event()
    original = storage_access.Permit._settle

    async def settle(permit):
        entered.set()
        await release.wait()
        await original(permit)

    async def options(*args):
        return {}

    def native(*args, **kwargs):
        raise RuntimeError("original native business failure")

    monkeypatch.setattr(storage_access.Permit, "_settle", settle)
    monkeypatch.setattr(service, "io_options", options)
    monkeypatch.setattr(physical, "run_reclaim", native)

    async def execute():
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as owned:
            return await worker.execute(owned, identity, task_id)

    task = asyncio.create_task(execute())
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db.rollback()
    assert not await db.scalar(select(func.count()).select_from(StorageOperation))


async def test_real_delete_lost_ack_publication_then_terminal_retry_never_deletes_twice(
    reclaim_db, monkeypatch
):
    from contextlib import contextmanager

    db, actor = reclaim_db
    bucket = live_provider_guard.owned_bucket(
        storage_service.datasets_bucket, purpose="datasets"
    )
    storage_service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    anchor, creation, _ = await seed(db, size=12, real=True)
    first, job, _ = await start(db, actor, anchor)
    first_id, identity, task_id, key = (
        first.id,
        job.id,
        first.task_id,
        creation.object_key,
    )
    checkpoint, io_options = service.checkpoint, service.io_options
    sends = []

    async def options(*args):
        value = await io_options(*args)
        base = value["client_context"]

        @contextmanager
        def observed():
            with base() as client:

                def count(**kwargs):
                    sends.append(True)

                client.meta.events.register("before-send.s3.DeleteObject", count)
                yield client

        return {"client_context": observed}

    async def lose_ack(db, attempt_id, task_id, phase, payload):
        if phase == "delete_ack":
            raise RuntimeError("injected receipt callback outage")
        return await checkpoint(db, attempt_id, task_id, phase, payload)

    monkeypatch.setattr(service, "io_options", options)
    monkeypatch.setattr(service, "checkpoint", lose_ack)
    try:
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as owned:
            assert await worker.execute(owned, identity, task_id)
        await db.rollback()
        first = await db.get(Attempt, first_id, populate_existing=True)
        assert (
            first.status == "unknown"
            and first.delete_intent_at
            and first.delete_ack
            and first.physical_settled
        )
        assert len(sends) == 1
        monkeypatch.setattr(service, "checkpoint", checkpoint)
        retry, retry_job, _ = await start(db, actor, anchor, retry=first.id)
        retry_id, retry_job_id, retry_task = retry.id, retry_job.id, retry.task_id
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as owned:
            assert await worker.execute(owned, retry_job_id, retry_task)
        await db.rollback()
        retry = await db.get(Attempt, retry_id, populate_existing=True)
        assert retry.status == "completed" and retry.outcome == "already_absent"
        assert (
            retry.absence_verified_at
            and retry.delete_intent_at is None
            and len(sends) == 1
        )
        assert (await db.scalar(select(Claim))).state == "deleted"
    finally:
        values = storage_service.client.list_object_versions(Bucket=bucket, Prefix=key)
        for value in values.get("Versions", []) + values.get("DeleteMarkers", []):
            if value["Key"] == key:
                storage_service.client.delete_object(
                    Bucket=bucket, Key=key, VersionId=value["VersionId"]
                )


async def test_worker_preparation_failure_before_permit_finishes_without_stuck_claim(
    reclaim_db, monkeypatch
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await start(db, actor, anchor)
    identity, task_id, attempt_id = job.id, row.task_id, row.id

    async def fail(*args):
        raise RuntimeError("preparation query unavailable")

    monkeypatch.setattr(service, "recover_may_delete", fail)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as owned:
        assert await worker.execute(owned, identity, task_id)
    await db.rollback()
    row = await db.get(Attempt, attempt_id, populate_existing=True)
    assert row.status == "failed" and row.operation_id is None and row.physical_settled
    assert (await db.scalar(select(Claim))).state == "released"


async def test_permit_commit_then_lost_return_remains_unknown_and_blocks_retry(
    reclaim_db, monkeypatch
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await start(db, actor, anchor)
    identity, task_id, attempt_id = job.id, row.task_id, row.id
    acquire = service.acquire

    async def interrupted(*args):
        await acquire(*args)
        raise RuntimeError("permit commit reply lost")

    monkeypatch.setattr(service, "acquire", interrupted)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as owned:
        assert await worker.execute(owned, identity, task_id)
    await db.rollback()
    row = await db.get(Attempt, attempt_id, populate_existing=True)
    assert row.status == "unknown" and not row.physical_settled and row.operation_id
    assert await db.get(StorageOperation, row.operation_id)


async def test_cancel_running_before_permit_is_atomic_against_future_acquisition(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await running(db, actor, anchor)
    assert await service.cancel(db, job.id) == "cancelled"
    await db.commit()
    with pytest.raises(Exception):
        await service.acquire(db, row.id, row.task_id)
    assert (
        row.status == "cancelled"
        and (await db.scalar(select(Claim))).state == "released"
    )
    assert not await db.scalar(select(func.count()).select_from(StorageOperation))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE managed_original_reclaim_attempts SET phase='claimed'",
        "UPDATE managed_original_reclaim_attempts SET read_verified='{}'::jsonb",
        "UPDATE managed_original_reclaim_attempts SET delete_intent_at=NULL",
        "UPDATE managed_original_reclaim_claims SET revision=revision-1",
    ],
)
async def test_physical_facts_and_phase_revision_cannot_regress(reclaim_db, statement):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, _, _ = await running(db, actor, anchor)
    await service.acquire(db, row.id, row.task_id)
    await service.checkpoint(db, row.id, row.task_id, "read_verified", verified(row))
    await service.checkpoint(db, row.id, row.task_id, "delete_started", verified(row))
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(text(statement))


async def test_downgrade_refuses_durable_reclaim_facts(reclaim_db):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    await start(db, actor, anchor)
    connection = await db.connection()
    with pytest.raises(RuntimeError, match="Reclamation facts are retained"):
        await connection.run_sync(
            lambda sync: migration(sync, "0260_managed_original_reclaim.py").downgrade()
        )


async def test_job_retention_only_unlinks_job_and_preserves_terminal_claim_history(
    reclaim_db,
):
    db, actor = reclaim_db
    anchor, _, _ = await seed(db)
    row, job, _ = await start(db, actor, anchor)
    job_id, attempt_id = job.id, row.id
    await service.cancel(db, job_id)
    await db.commit()
    from sqlalchemy import delete

    await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
    await db.commit()
    await db.refresh(row)
    assert (
        row.id == attempt_id and row.async_job_id is None and row.status == "cancelled"
    )
    assert (await db.scalar(select(Claim))).current_attempt_id == attempt_id
