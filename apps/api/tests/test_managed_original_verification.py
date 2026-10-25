"""Historical observations never grant ownership or bypass physical settlement."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, func, select, update

from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageBinding
from app.db.models.managed_original_verification import (
    ManagedOriginalVerification as Verification,
)
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.services import managed_original_verification as service
from app.services.managed_original_probe import ProbeResult
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage import storage_service
from app.workers import managed_original_verification as worker
from tests.test_dataset_content import headers
from tests.test_managed_original_reclamation import (
    assertion,
    declared_hold,
    forbid_object_io,
)


def path(anchor):
    return f"/api/v1/storage/managed-original-retirements/{anchor.id}/verifications"


async def attempt(db, actor, **fields):
    anchor = await assertion(db, **fields)
    row, job, existing = await service.start(db, anchor.id, actor)
    assert not existing
    return anchor, row, job


async def claimed(db, actor):
    anchor, row, job = await attempt(db, actor)
    assert await service.claim(db, job.id, row.task_id) is row
    return anchor, row, job


async def settled_receipt(db, row):
    """Model the state after a real permit settled; worker integration owns I/O."""
    row.operation_id = uuid.uuid4()
    await db.flush()


def matched(row):
    return ProbeResult(
        "matched", None, row.expected_sha256, row.expected_size_bytes, {}
    )


async def test_get_is_locator_free_sql_only_and_start_is_atomic_audited(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    anchor = await assertion(db)
    forbid_object_io(monkeypatch)
    before = await service.history(db, anchor.id, actor)
    assert before.can_start and before.total == 0
    assert before.execution_permission is False
    row, job, existing = await service.start(db, anchor.id, actor)
    assert not existing and row.status == job.status == "pending"
    audit = await db.scalar(
        select(AuditLog).where(AuditLog.action == "managed_original.verification_start")
    )
    assert audit.detail_json == {
        "verification_id": str(row.id),
        "async_job_id": str(job.id),
        "execution_permission": False,
    }
    result = await service.history(db, anchor.id, actor)
    assert not result.can_start and result.blockers == ["verification_active"]
    assert result.items[0].context_matches
    rendered = result.model_dump_json()
    for secret in (
        anchor.object_key,
        anchor.bucket,
        "observation",
        "configuration",
        "storage_identity",
    ):
        assert secret not in rendered


async def test_audit_failure_rolls_back_job_and_attempt_together(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    anchor = await assertion(db)
    monkeypatch.setattr(
        service.AuditService,
        "log",
        AsyncMock(side_effect=RuntimeError("audit unavailable")),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.start(db, anchor.id, actor)
    assert not await db.scalar(select(func.count()).select_from(Verification))
    assert not await db.scalar(
        select(func.count()).select_from(AsyncJob).where(AsyncJob.kind == service.KIND)
    )


@pytest.mark.parametrize(
    "fields,blocker",
    [
        (
            {"asset_verification_state": "unverified", "sha256": None},
            "historical_content_unverified",
        ),
        ({"size_bytes": None}, "historical_content_unverified"),
        ({"size_bytes": service.MAX_BYTES + 1}, "verification_size_budget"),
        (
            {
                "identity_state": "unknown",
                "storage_identity": None,
                "identity_error_code": "historical_identity_unknown",
            },
            "platform_identity_unproven",
        ),
        ({"storage_identity": "f" * 64}, "platform_configuration_changed"),
    ],
)
async def test_ineligible_history_and_start_agree_before_io(
    db_session, super_admin, monkeypatch, fields, blocker
):
    db, actor = db_session, super_admin[0]
    anchor = await assertion(db, **fields)
    forbid_object_io(monkeypatch)
    history = await service.history(db, anchor.id, actor)
    assert not history.can_start and blocker in history.blockers
    with pytest.raises(HTTPException) as error:
        await service.start(db, anchor.id, actor)
    assert error.value.status_code == 409 and error.value.detail["code"] == blocker
    assert not await db.scalar(select(func.count()).select_from(Verification))


async def test_zero_byte_is_eligible_and_local_or_persisted_configuration_changes_are_pinned(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await attempt(db, actor, size_bytes=0)
    original = row.input_fingerprint
    monkeypatch.setattr(storage_service, "datasets_bucket", "different-current-bucket")
    history = await service.history(db, anchor.id, actor)
    assert not history.items[0].context_matches and row.input_fingerprint == original
    assert "platform_configuration_changed" in history.blockers
    assert await service.claim(db, job.id, row.task_id) is None
    assert row.status == "failed" and row.error_code == "verification_context_changed"


async def test_policy_and_declared_holds_do_not_rewrite_content_context(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    anchor, row, _ = await attempt(db, actor)
    anchor.policy_revision += 1
    anchor.retain_until = datetime.now(timezone.utc) + timedelta(days=1)
    await declared_hold(db, anchor, actor)
    await db.flush()
    history = await service.history(db, anchor.id, actor)
    assert history.items[0].context_matches
    assert row.anchor["sha256"] == anchor.sha256 and "policy_revision" not in row.anchor
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(generation=2)
    )
    history = await service.history(db, anchor.id, actor)
    assert not history.items[0].context_matches


async def test_duplicate_start_reuses_pending_and_running_without_ttl(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await attempt(db, actor)
    for claimed_status in (False, True):
        if claimed_status:
            assert await service.claim(db, job.id, row.task_id) is row
            assert await service.claim(db, job.id, row.task_id) is None
        second, same_job, existing = await service.start(db, anchor.id, actor)
        assert existing and second.id == row.id and same_job.id == job.id
    assert await db.scalar(select(func.count()).select_from(Verification)) == 1
    assert job.result == {"stage": "reading", "execution_permission": False}


async def test_exact_task_claim_and_pre_read_configuration_fence(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    _, row, job = await attempt(db, actor)
    assert await service.claim(db, job.id, "other-task") is None
    assert row.status == "pending"
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(generation=2)
    )
    assert await service.claim(db, job.id, row.task_id) is None
    assert (
        row.status == job.status == "failed"
        and row.error_code == "verification_context_changed"
    )


async def test_missing_job_keeps_history_and_prevents_restarting_active_attempt(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await attempt(db, actor)
    await db.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
    history = await service.history(db, anchor.id, actor)
    assert (
        history.items[0].async_job_id is None and history.items[0].status == "pending"
    )
    assert "verification_job_unavailable" in history.blockers
    with pytest.raises(HTTPException) as error:
        await service.start(db, anchor.id, actor)
    assert error.value.detail["code"] == "verification_job_unavailable"


async def test_running_cancel_is_a_request_until_reader_settles(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await claimed(db, actor)
    await settled_receipt(db, row)
    assert await service.cancel(db, job.id) == "cancel_requested"
    assert (
        row.status == job.status == "running" and job.result["cancel_requested"] is True
    )
    assert row.completed_at is None
    assert await service.publish(db, job.id, row.task_id, result=matched(row))
    assert row.status == job.status == "cancelled" and row.outcome == "cancelled"
    assert row.observed_sha256 is None
    history = await service.history(db, anchor.id, actor)
    assert history.can_start and not history.items[0].unsettled_operation


async def test_pending_cancel_and_wrong_signal_are_safe_and_terminal(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    _, row, job = await attempt(db, actor)
    assert not await service.signal_interrupted(
        db, job.id, "other-task", cancelled=True
    )
    assert row.status == "pending"
    assert await service.cancel(db, job.id) == "cancelled"
    assert row.status == job.status == "cancelled" and row.operation_id is None
    assert await service.claim(db, job.id, row.task_id) is None


async def test_lost_running_reader_keeps_unsettled_receipt_and_rejects_late_result(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await claimed(db, actor)
    if await db.get(Gate, LEGACY_LOCATION_ID) is None:
        db.add(Gate(location_id=LEGACY_LOCATION_ID, state="active", revision=1))
    operation = StorageOperation(
        id=uuid.uuid4(),
        location_id=LEGACY_LOCATION_ID,
        revision=1,
        purpose=service.KIND,
        node_id="synthetic-worker",
    )
    db.add(operation)
    await db.flush()
    row.operation_id = operation.id
    await db.flush()
    assert await service.signal_interrupted(db, job.id, row.task_id, cancelled=False)
    assert (
        row.status == job.status == "failed"
        and row.error_code == "verification_interrupted"
    )
    assert not await service.publish(db, job.id, row.task_id, result=matched(row))
    history = await service.history(db, anchor.id, actor)
    assert (
        history.items[0].unsettled_operation
        and "physical_operation_unsettled" in history.blockers
    )
    with pytest.raises(HTTPException) as error:
        await service.start(db, anchor.id, actor)
    assert error.value.detail["code"] == "physical_operation_unsettled"
    assert await db.get(StorageOperation, operation.id)


@pytest.mark.parametrize(
    "outcome", ["matched", "missing", "changed", "unverifiable", "failed"]
)
async def test_settled_results_are_historical_and_cannot_be_republished(
    db_session, super_admin, monkeypatch, outcome
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await claimed(db, actor)
    await settled_receipt(db, row)
    check = AsyncMock(return_value=1)
    monkeypatch.setattr(service.storage_access, "check", check)
    result = (
        matched(row)
        if outcome == "matched"
        else ProbeResult(
            outcome,
            "managed_original_read_failed" if outcome == "failed" else None,
            None,
            None,
            {},
        )
    )
    assert await service.publish(db, job.id, row.task_id, result=result)
    check.assert_awaited_once_with(
        db, LEGACY_LOCATION_ID, expected_revision=1, lock=True
    )
    assert row.status == ("failed" if outcome == "failed" else "completed")
    assert row.outcome == outcome and job.result["execution_permission"] is False
    assert not await service.publish(db, job.id, row.task_id, result=matched(row))
    history = await service.history(db, anchor.id, actor)
    assert history.can_start and history.items[0].context_matches


async def test_result_without_receipt_and_changed_context_are_discarded(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await claimed(db, actor)
    monkeypatch.setattr(service.storage_access, "check", AsyncMock(return_value=1))
    assert await service.publish(db, job.id, row.task_id, result=matched(row))
    assert (
        row.error_code == "verification_operation_missing" and row.outcome == "failed"
    )
    row2, job2, _ = await service.start(db, anchor.id, actor)
    await service.claim(db, job2.id, row2.task_id)
    await settled_receipt(db, row2)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(generation=2)
    )
    assert await service.publish(db, job2.id, row2.task_id, result=matched(row2))
    assert (
        row2.error_code == "verification_context_changed"
        and row2.observed_sha256 is None
    )


async def test_api_requires_superadmin_star_scope_and_bounds_history(
    db_session, super_admin, project_admin, httpx_client, monkeypatch
):
    from app.services import api_key_service

    anchor = await assertion(db_session)
    monkeypatch.setattr(worker, "dispatch", AsyncMock())
    for method in (httpx_client.get, httpx_client.post):
        assert (
            await method(path(anchor), headers=headers(project_admin))
        ).status_code == 403
    _, raw = await api_key_service.create_key(
        db_session, super_admin[0], name="restricted", scopes=["datasets:read"]
    )
    assert (
        await httpx_client.get(path(anchor), headers={"Authorization": f"Bearer {raw}"})
    ).status_code == 403
    response = await httpx_client.post(path(anchor), headers=headers(super_admin))
    assert response.status_code == 202 and not response.json()["existing"]
    duplicate = await httpx_client.post(path(anchor), headers=headers(super_admin))
    assert duplicate.status_code == 202 and duplicate.json()["existing"]
    assert duplicate.json()["verification_id"] == response.json()["verification_id"]
    assert (
        await httpx_client.get(
            path(anchor) + "?limit=101", headers=headers(super_admin)
        )
    ).status_code == 422
    history = await httpx_client.get(path(anchor), headers=headers(super_admin))
    assert (
        history.status_code == 200 and history.json()["execution_permission"] is False
    )


async def test_dispatch_failure_preserves_pending_same_task_and_safe_log(
    monkeypatch, caplog
):
    db = SimpleNamespace(commit=AsyncMock())
    job = SimpleNamespace(
        id=uuid.uuid4(), celery_task_id=str(uuid.uuid4()), status="pending"
    )
    publish = MagicMock(side_effect=RuntimeError("s3://private-locator secret"))
    monkeypatch.setattr(worker, "_publish", publish)
    await worker.dispatch(db, job)
    await worker.dispatch(db, job)
    assert job.status == "pending" and publish.call_count == 2
    assert publish.call_args.args == (job.id, job.celery_task_id)
    assert "private-locator" not in caplog.text and "secret" not in caplog.text


async def test_repeated_cancellation_cannot_skip_rollback_and_final_publication(
    monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        maintenance_revision=1,
        anchor={
            "location_id": str(LEGACY_LOCATION_ID),
            "bucket": "managed",
            "object_key": "private",
            "storage_identity": "a" * 64,
        },
        expected_sha256="a" * 64,
        expected_size_bytes=0,
        configuration={},
    )

    async def rollback():
        entered.set()
        await release.wait()

    db = SimpleNamespace(commit=AsyncMock(), rollback=rollback)
    monkeypatch.setattr(service, "claim", AsyncMock(return_value=row))
    monkeypatch.setattr(
        worker.storage_access, "run", AsyncMock(side_effect=asyncio.CancelledError())
    )
    publish = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "publish", publish)
    monkeypatch.setattr(worker, "notify_job_terminal", AsyncMock())
    job_id, task_id = uuid.uuid4(), str(uuid.uuid4())
    task = asyncio.create_task(worker.execute(db, job_id, task_id))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not publish.called
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    publish.assert_awaited_once_with(
        db, job_id, task_id, result=None, error_code=None, cancelled=True
    )
    assert db.commit.await_count == 2


async def test_acquired_receipt_must_belong_to_expected_gate_and_revision(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    _, row, _ = await claimed(db, actor)
    if await db.get(Gate, LEGACY_LOCATION_ID) is None:
        db.add(Gate(location_id=LEGACY_LOCATION_ID, state="active", revision=1))
    operation = StorageOperation(
        id=uuid.uuid4(),
        location_id=LEGACY_LOCATION_ID,
        revision=1,
        purpose=service.KIND,
        node_id="synthetic-worker",
    )
    db.add(operation)
    await db.flush()
    for location_id, revision in ((uuid.uuid4(), 1), (LEGACY_LOCATION_ID, 2)):
        with pytest.raises(HTTPException) as error:
            await service.acquired(
                db,
                row.id,
                SimpleNamespace(
                    id=operation.id, location_id=location_id, revision=revision
                ),
            )
        assert error.value.detail["code"] == "verification_operation_mismatch"
        assert row.operation_id is None
    await service.acquired(
        db,
        row.id,
        SimpleNamespace(id=operation.id, location_id=LEGACY_LOCATION_ID, revision=1),
    )
    await db.refresh(row)
    assert row.operation_id == operation.id
    assert await db.get(StorageOperation, operation.id)


async def test_history_paginates_only_its_stable_anchor(db_session, super_admin):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await attempt(db, actor)
    identifiers = {row.id}
    await service.cancel(db, job.id)
    for _ in range(2):
        row, job, _ = await service.start(db, anchor.id, actor)
        identifiers.add(row.id)
        await service.cancel(db, job.id)
    _, other, _ = await attempt(db, actor)
    first = await service.history(db, anchor.id, actor, limit=2)
    second = await service.history(db, anchor.id, actor, limit=2, offset=2)
    assert first.total == second.total == 3 and first.has_more and not second.has_more
    assert {item.id for item in first.items}.isdisjoint(
        item.id for item in second.items
    )
    assert {item.id for item in first.items + second.items} == identifiers
    assert other.id not in identifiers


async def test_active_job_status_mismatch_is_visible_and_cannot_be_redispatched(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    anchor, row, job = await attempt(db, actor)
    job.status = "failed"
    await db.flush()
    history = await service.history(db, anchor.id, actor)
    assert "verification_job_unavailable" in history.blockers
    with pytest.raises(HTTPException) as error:
        await service.start(db, anchor.id, actor)
    assert error.value.detail["code"] == "verification_job_unavailable"
    assert row.status == "pending"
