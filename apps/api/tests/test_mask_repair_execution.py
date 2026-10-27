"""Committed-session repair tests: delivery ownership and atomic unit results."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.notification import Notification
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services import async_job as jobs
from app.services.mask_repair import (
    MaskRepairError,
    canonical_digest,
    request_repair_rollback,
    resume_repair_batch,
    result_digest,
)
from app.services.mask_repair_execution import start_attempt
from app.services.reference_bindings import task_binding
from app.workers import mask_repair as worker
from tests.factory import create_user
from tests.test_mask_repair_m5 import _seed_repair_issue

pytestmark = pytest.mark.asyncio


class _UnitRepairReceipt:
    def __init__(self, owner, kind, payload):
        self.owner = owner
        self.session = owner.db.sync_session
        self.transaction = self.session.get_transaction()
        self.kind = kind
        self.payload = deepcopy(payload)


def _install_unit_owner(monkeypatch):
    """Substitute only original I/O qualification in the control-flow tests.

    These tests exercise real attempts, Annotation/Prediction writes and commit
    loss. Full source capture, model dispatch and physical receipts are covered
    separately by the managed repair integration tests.
    """
    from app.services import managed_mask_repair as managed
    from app.services.content_snapshot import ContentError

    class UnitOwner:
        is_managed = True
        provenance_key = "original_input"
        width, height = 5, 3

        def __init__(self, db, task_id, actor_id):
            self.db, self.task_id, self.actor_id = db, task_id, actor_id
            self.opened = self.closed = False
            self._unsettled = False

        async def open(self):
            self.opened = True

        async def final(self):
            assert self.opened and not self.closed
            return await worker._repair_task(
                self.db, self.task_id, SimpleNamespace(id=self.actor_id)
            )

        def receipt(self, kind, payload):
            assert self.opened and not self.closed
            assert not self.db.sync_session.in_nested_transaction()
            assert self.db.sync_session.get_transaction() is not None
            return _UnitRepairReceipt(self, kind, payload)

        def evidence(self):
            return {"schema": 1, "width": self.width, "height": self.height}

        async def note_committed_mutation(self, payload, response):
            assert response.operation_id is not None
            # Match the owner's async post-write refresh before the worker
            # serializes server-managed Annotation columns into after facts.
            for identifier in response.result_versions:
                await self.db.get(
                    Annotation, uuid.UUID(identifier), populate_existing=True
                )

        async def recheck_credentials(self):
            # Account/phase/native checks still run in the actual caller session.
            await self.final()

        async def close(self, failure=None):
            assert self.db.sync_session.get_transaction() is None
            self.closed = True

    original_validate = managed.validate_receipt
    original_payload = managed.receipt_payload

    async def validate(
        db, task_id, actor_id, receipt, kind, payload, *, project_id=None
    ):
        if type(receipt) is not _UnitRepairReceipt:
            return await original_validate(
                db, task_id, actor_id, receipt, kind, payload, project_id=project_id
            )
        if (
            type(receipt.owner) is not UnitOwner
            or receipt.session is not db.sync_session
            or receipt.transaction is not db.sync_session.get_transaction()
            or db.sync_session.in_nested_transaction()
            or receipt.kind != kind
            or receipt.payload != payload
            or receipt.owner.task_id != task_id
            or (actor_id is not None and receipt.owner.actor_id != actor_id)
            or receipt.owner.closed
        ):
            raise ContentError("repair_original_receipt_invalid")
        task = await receipt.owner.final()
        if project_id is not None and task.project_id != project_id:
            raise ContentError("repair_original_receipt_invalid")
        return receipt.owner.evidence()

    def payload(receipt):
        if type(receipt) is _UnitRepairReceipt:
            return receipt.kind, deepcopy(receipt.payload)
        return original_payload(receipt)

    monkeypatch.setattr(managed, "validate_receipt", validate)
    monkeypatch.setattr(managed, "receipt_payload", payload)
    return UnitOwner


@pytest.fixture
async def repair_world(test_db_url, apply_migrations, monkeypatch):
    engine = create_async_engine(test_db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        owner = await create_user(
            db, "super_admin", f"repair-{uuid.uuid4()}@test.local", "Repair"
        )
        project, task, annotation, issue, source, island = await _seed_repair_issue(
            db, owner_id=owner.id
        )
        item = {
            "kind": "rerun_local_sam",
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "issue_id": str(issue.id),
            "source_version": 1,
            "class_name": "object",
            "tool_unit_id": "region",
        }
        plan = {"items": [item], "shards": []}
        batch = MaskRepairBatch(
            project_id=project.id,
            requested_by_id=owner.id,
            token_hash=uuid.uuid4().hex * 2,
            status="pending",
            request_json={},
            plan_json=plan,
            plan_digest=canonical_digest(plan),
            result_json={},
            receipt_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(batch)
        await db.flush()
        job = await jobs.create_job(
            db,
            kind="mask_repair",
            project_id=project.id,
            user_id=owner.id,
            payload={"mask_repair_batch_id": str(batch.id)},
        )
        job.celery_task_id = str(job.id)
        batch.async_job_id = job.id
        world = SimpleNamespace(
            factory=factory,
            owner_id=owner.id,
            author_id=annotation.user_id,
            project_id=project.id,
            dataset_id=(await db.get(Task, task.id)).dataset_item_id,
            task_id=task.id,
            annotation_id=annotation.id,
            batch_id=batch.id,
            job_id=job.id,
            item=item,
            source=source,
            island=island,
            binding=task_binding(task),
        )
        from app.db.models.dataset import DatasetItem

        world.dataset_id = (await db.get(DatasetItem, task.dataset_item_id)).dataset_id
        await db.commit()

    async def no_store(*args, **kwargs):
        pass

    async def prepare(*args, **kwargs):
        return {
            "binding": world.binding,
            "candidates": [{"value": {"rle": source}, "score": 0.9}] * 2,
            "diagnostic": None,
            "backend_id": None,
            "pool_id": None,
            "model_id": "sam",
            "model_version": "test",
            "inference_time_ms": 1,
        }

    world.prepare = prepare
    world.real_owner_selector = worker._reference_owner
    make_owner = _install_unit_owner(monkeypatch)
    world.make_owner = make_owner

    async def owner_for(db, factory, attempt, batch, section, unit, node_id):
        if unit.get("kind") == "rerun_tracker" or not worker.unit_items(
            batch, section, unit
        ):
            return None
        return make_owner(db, world.task_id, attempt.actor_id)

    async def unit_authority(*args, **kwargs):
        return None

    # Fake original qualification is local to these committed control-flow
    # fixtures; replacement delivery/Job/actor checks remain production code.
    monkeypatch.setattr(worker, "_reference_owner", owner_for)
    monkeypatch.setattr(
        "app.services.reference_mask_repair.capture_job_authority", unit_authority
    )
    monkeypatch.setattr(
        "app.services.managed_mask_repair.capture_job_authority", unit_authority
    )
    monkeypatch.setattr(worker, "_prepare_sam_candidate", prepare)
    monkeypatch.setattr(worker, "_store_result_rle", no_store)
    monkeypatch.setattr(
        "app.services.prediction.prepare_mask_payload_for_write", no_store
    )
    try:
        yield world
    finally:
        async with factory() as db:
            await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id.in_([world.owner_id, world.author_id])
                )
            )
            await db.execute(
                delete(VideoTrackerJob).where(VideoTrackerJob.task_id == world.task_id)
            )
            await db.execute(
                delete(PredictionMeta).where(
                    PredictionMeta.prediction_id.in_(
                        select(Prediction.id).where(Prediction.task_id == world.task_id)
                    )
                )
            )
            await db.execute(
                delete(Prediction).where(Prediction.task_id == world.task_id)
            )
            await db.execute(
                delete(Annotation).where(Annotation.task_id == world.task_id)
            )
            await db.execute(delete(Task).where(Task.id == world.task_id))
            await db.execute(
                delete(Notification).where(
                    Notification.user_id.in_([world.owner_id, world.author_id])
                )
            )
            await db.execute(delete(Project).where(Project.id == world.project_id))
            await db.execute(
                delete(AsyncJob).where(
                    AsyncJob.user_id.in_([world.owner_id, world.author_id])
                )
            )
            await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
            await db.execute(
                delete(User).where(User.id.in_([world.owner_id, world.author_id]))
            )
            await db.commit()
        await engine.dispose()


async def _state(world):
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        job = await db.get(AsyncJob, batch.async_job_id)
        count = await db.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == world.task_id)
        )
        return batch, job, count


async def _forward(world):
    await worker._run_forward(
        world.factory, str(world.batch_id), str(world.job_id), str(world.job_id)
    )


def _lose_next_unit_commit(world, section):
    state = {"raised": False}

    class LostCommitResponse(AsyncSession):
        async def commit(self):
            matching = [
                row
                for row in self.identity_map.values()
                if isinstance(row, MaskRepairBatch) and row.id == world.batch_id
            ]
            lose = False
            if not state["raised"]:
                for row in matching:
                    saved = (row.result_json or {}).get(section) or {}
                    if section == "rollback":
                        saved = saved.get("shards") or {}
                    lose = any(
                        value.get("status") in {"completed", "dispatch_pending"}
                        for value in saved.values()
                    )
            await super().commit()
            if lose:
                state["raised"] = True
                raise ConnectionError("commit_response_lost")

    world.factory = async_sessionmaker(
        world.factory.kw["bind"], class_=LostCommitResponse, expire_on_commit=False
    )
    return state


@pytest.mark.parametrize("lose_commit", [False, True])
async def test_sam_results_and_parent_marker_commit_once(repair_world, lose_commit):
    world = repair_world
    fault = _lose_next_unit_commit(world, "candidates") if lose_commit else None
    await _forward(world)
    if fault:
        assert fault["raised"]
    batch, job, count = await _state(world)
    assert (batch.status, job.status, count) == ("completed", "completed", 2)
    ids = batch.result_json["candidates"]["0"]["prediction_ids"]
    assert len(set(ids)) == 2
    records = batch.result_json["candidates"]["0"]["candidate_records"]
    assert set(records) == set(ids)
    async with world.factory() as db:
        for identifier in ids:
            prediction = await db.scalar(
                select(Prediction).where(Prediction.id == uuid.UUID(identifier))
            )
            metadata = await db.scalar(
                select(PredictionMeta).where(
                    PredictionMeta.prediction_id == prediction.id
                )
            )
            assert records[identifier] == {
                "result_digest": canonical_digest(prediction.result),
                "metadata_digest": canonical_digest(metadata.extra),
            }
    await _forward(world)
    again, _, count = await _state(world)
    assert count == 2
    assert again.result_json == batch.result_json


async def test_legacy_managed_delivery_cannot_recapture_source(
    repair_world, monkeypatch
):
    world = repair_world
    monkeypatch.setattr(worker, "_reference_owner", world.real_owner_selector)
    reached = []

    async def unexpected(*args, **kwargs):
        reached.append(True)
        raise AssertionError("Legacy delivery must stop before model or RLE work")

    monkeypatch.setattr(worker, "_prepare_sam_candidate", unexpected)
    monkeypatch.setattr(worker, "_store_result_rle", unexpected)
    await _forward(world)
    batch, job, count = await _state(world)
    assert (batch.status, job.status, count) == ("failed", "failed", 0)
    assert batch.result_json["candidates"]["0"]["reason"] == (
        "repair_original_requires_new_plan"
    )
    assert reached == []


async def test_second_candidate_failure_rolls_back_first_and_marker(
    repair_world, monkeypatch
):
    original = worker.PredictionService.create_from_ml_result
    calls = 0

    async def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second_candidate_failed")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(worker.PredictionService, "create_from_ml_result", fail_second)
    await _forward(repair_world)
    batch, job, count = await _state(repair_world)
    assert (batch.status, job.status, count) == ("failed", "failed", 0)
    assert batch.result_json["candidates"]["0"]["reason"] == "second_candidate_failed"


@pytest.mark.parametrize(
    "change", ["cancel", "disable_actor", "source_version", "disable_native_gate"]
)
async def test_sam_rechecks_after_unlocked_model_io(repair_world, monkeypatch, change):
    world = repair_world
    entered, release = asyncio.Event(), asyncio.Event()
    stores = []

    async def store(*args, **kwargs):
        stores.append(kwargs)

    monkeypatch.setattr(worker, "_store_result_rle", store)

    async def blocked_prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return await world.prepare()

    monkeypatch.setattr(worker, "_prepare_sam_candidate", blocked_prepare)
    running = asyncio.create_task(_forward(world))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with world.factory() as db:
            job = await jobs.lock_job(db, world.job_id)
            assert job.status == "running"
            if change == "cancel":
                await jobs.request_cancel(db, job.id)
            elif change == "disable_actor":
                (await db.get(User, world.owner_id)).is_active = False
            elif change == "disable_native_gate":
                (
                    await db.get(Project, world.project_id)
                ).raster_mask_native_editing_enabled = False
            else:
                (await db.get(Annotation, world.annotation_id)).version += 1
            await asyncio.wait_for(db.commit(), 3)
    finally:
        release.set()
        await asyncio.wait_for(running, 5)
    batch, job, count = await _state(world)
    assert count == 0
    assert not stores
    assert job.status == ("cancelled" if change == "cancel" else "failed")
    assert batch.status == ("cancelled" if change == "cancel" else "failed")


@pytest.mark.parametrize("legacy", [False, True])
async def test_old_delivery_cannot_claim_replacement_attempt(repair_world, legacy):
    world = repair_world
    async with world.factory() as db:
        assert (
            await start_attempt(db, world.batch_id, world.job_id, "wrong-task-id")
            is None
        )
        attempt = await start_attempt(
            db, world.batch_id, world.job_id, str(world.job_id)
        )
        assert attempt is not None
        await db.commit()
        assert (
            await start_attempt(db, world.batch_id, world.job_id, str(world.job_id))
            is None
        )
        batch = await db.get(MaskRepairBatch, world.batch_id)
        batch.status = "failed"
        await jobs.mark_failed(db, world.job_id, error="interrupted")
        await db.commit()
        actor = await db.get(User, world.owner_id)
        batch = await resume_repair_batch(db, batch_id=world.batch_id, actor=actor)
        replacement_id = batch.async_job_id
        await db.commit()
    async with world.factory() as db:
        assert (
            await start_attempt(
                db, world.batch_id, None if legacy else world.job_id, str(world.job_id)
            )
            is None
        )
        await db.commit()
        assert (await db.get(AsyncJob, replacement_id)).status == "pending"
        assert (
            await start_attempt(db, world.batch_id, replacement_id, str(replacement_id))
            is not None
        )
        await db.commit()


@pytest.mark.parametrize("rollback", [False, True])
async def test_preloaded_batch_cannot_replace_new_attempt(repair_world, rollback):
    world = repair_world
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        batch.status = "completed" if rollback else "failed"
        batch.rollback_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        await db.commit()
    async with world.factory() as stale, world.factory() as winner:
        cached = await stale.get(MaskRepairBatch, world.batch_id)
        actor = await stale.get(User, world.owner_id)
        winner_actor = await winner.get(User, world.owner_id)

        async def request(db, user):
            if rollback:
                return await request_repair_rollback(
                    db,
                    batch_id=world.batch_id,
                    actor=user,
                    expected_result_digest=result_digest(cached),
                )
            return await resume_repair_batch(db, batch_id=world.batch_id, actor=user)

        new = await request(winner, winner_actor)
        new_id = new.rollback_async_job_id if rollback else new.async_job_id
        await winner.commit()
        with pytest.raises(MaskRepairError) as error:
            await request(stale, actor)
        assert error.value.status_code == 409
        await stale.rollback()
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        assert (
            batch.rollback_async_job_id if rollback else batch.async_job_id
        ) == new_id


@pytest.mark.parametrize("state", ["pending", "running", "completed"])
async def test_dispatch_response_loss_does_not_overwrite_worker(
    repair_world, monkeypatch, state
):
    from app.api.v1.mask_qc import _dispatch_repair_or_fail

    world = repair_world

    async def lost_response(*args, **kwargs):
        async with world.factory() as db:
            job = await jobs.lock_job(db, world.job_id)
            batch = await db.get(MaskRepairBatch, world.batch_id)
            job.status = state
            batch.status = state
            await db.commit()
        raise MaskRepairError(
            status_code=503,
            reason="repair_dispatch_failed",
            message="test",
            retryable=True,
        )

    monkeypatch.setattr("app.api.v1.mask_qc.dispatch_repair_batch", lost_response)
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        with pytest.raises(HTTPException):
            await _dispatch_repair_or_fail(db, batch=batch, rollback=False)
    batch, job, _ = await _state(world)
    assert job.status == ("failed" if state == "pending" else state)
    assert batch.status == ("failed" if state == "pending" else state)


@pytest.mark.parametrize("state", ["pending", "running"])
async def test_cancel_is_durable_when_revoke_fails(repair_world, monkeypatch, state):
    from app.api.v1.async_jobs import cancel_async_job

    world = repair_world
    async with world.factory() as db:
        (await db.get(AsyncJob, world.job_id)).status = state
        (await db.get(MaskRepairBatch, world.batch_id)).status = state
        await db.commit()

    def unavailable(*args, **kwargs):
        raise ConnectionError("broker_unavailable")

    monkeypatch.setattr(worker.celery_app.control, "revoke", unavailable)
    async with world.factory() as db:
        actor = await db.get(User, world.owner_id)
        result = await cancel_async_job(
            world.job_id,
            request=Request({"type": "http", "headers": []}),
            db=db,
            current_user=actor,
        )
    batch, job, _ = await _state(world)
    assert result["status"] == (
        "cancelled" if state == "pending" else "cancel_requested"
    )
    assert job.status == ("cancelled" if state == "pending" else "running")
    assert batch.status == ("cancelled" if state == "pending" else "running")
    if state == "running":
        assert job.payload["cancel_requested"]


@pytest.mark.parametrize("lose_commit", [False, True])
async def test_tracker_unknown_publish_resumes_same_durable_child(
    repair_world, monkeypatch, lose_commit
):
    world = repair_world
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        plan = {"items": [{**world.item, "kind": "rerun_tracker"}], "shards": []}
        batch.plan_json, batch.plan_digest = plan, canonical_digest(plan)
        await db.commit()
    created = []

    async def tracker(db, *, item, actor):
        task = await db.get(Task, world.task_id)
        child = VideoTrackerJob(
            task_id=task.id,
            dataset_item_id=task.dataset_item_id,
            annotation_id=world.annotation_id,
            created_by=actor.id,
            model_key="test",
            direction="forward",
            from_frame=0,
            to_frame=1,
            event_channel=f"test:{uuid.uuid4()}",
        )
        db.add(child)
        await db.flush()
        child.celery_task_id = str(child.id)
        created.append(child.id)
        return {
            "status": "dispatch_pending",
            "kind": "rerun_tracker",
            "tracker_job_id": str(child.id),
            "candidate_count": 1,
            "dispatch_state": "pending",
        }

    dispatches = []

    def publish(*args, **kwargs):
        dispatches.append(kwargs["task_id"])
        if len(dispatches) == 1:
            raise ConnectionError("response_lost")

    monkeypatch.setattr(worker, "_run_tracker_candidate", tracker)
    monkeypatch.setattr(worker.celery_app, "send_task", publish)
    fault = _lose_next_unit_commit(world, "candidates") if lose_commit else None
    await _forward(world)
    if fault:
        assert fault["raised"]
    batch, job, _ = await _state(world)
    assert (batch.status, job.status) == ("partial", "failed")
    assert batch.result_json["candidates"]["0"]["dispatch_state"] == "unknown"
    async with world.factory() as db:
        actor = await db.get(User, world.owner_id)
        batch = await resume_repair_batch(db, batch_id=world.batch_id, actor=actor)
        world.job_id = batch.async_job_id
        await db.commit()
    await _forward(world)
    batch, job, _ = await _state(world)
    assert (batch.status, job.status) == ("completed", "completed")
    assert len(created) == 1
    assert dispatches == [str(created[0])] * 2

    # A late unknown response must not demote a confirmed child publication.
    def late_failure(*args, **kwargs):
        raise ConnectionError("late_unknown")

    monkeypatch.setattr(worker.celery_app, "send_task", late_failure)
    await worker._dispatch_tracker_handoff(
        world.factory, world.batch_id, world.job_id, "0", str(created[0])
    )
    batch, _, _ = await _state(world)
    assert batch.result_json["candidates"]["0"]["dispatch_state"] == "published"


@pytest.mark.parametrize("lose_commit", [False, True])
async def test_rollback_checkpoint_survives_failure_and_uses_new_actor(
    repair_world, monkeypatch, lose_commit
):
    world = repair_world
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        plan = {
            "items": [world.item],
            "shards": [{"id": key} for key in ("first", "second")],
        }
        batch.plan_json, batch.plan_digest = plan, canonical_digest(plan)
        batch.result_json = {
            "shards": {
                key: {"status": "completed", "key": key, "items": []}
                for key in ("first", "second")
            }
        }
        batch.status = "completed"
        batch.rollback_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        actor = await db.get(User, world.owner_id)
        batch = await request_repair_rollback(
            db,
            batch_id=world.batch_id,
            actor=actor,
            expected_result_digest=result_digest(batch),
        )
        rollback_id = batch.rollback_async_job_id
        await db.commit()
    calls = []

    async def rollback(db, *, plan_items, shard_result, actor):
        key = shard_result["key"]
        calls.append((key, actor.id))
        if key == "first" and len(calls) == 2:
            raise RuntimeError("rollback_interrupted")
        annotation = await db.get(Annotation, world.annotation_id)
        annotation.version += 1
        return 1

    monkeypatch.setattr(worker, "_rollback_shard", rollback)
    fault = _lose_next_unit_commit(world, "rollback") if lose_commit else None
    await worker._run_rollback(
        world.factory, world.batch_id, str(rollback_id), rollback_id
    )
    if fault:
        assert fault["raised"]
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        assert batch.status == "rollback_failed"
        assert set(batch.result_json["rollback"]["shards"]) == {"second"}
        assert (await db.get(Annotation, world.annotation_id)).version == 2
        # A different current reviewer owns the continuation, not the original
        # requested_by account. The source author is a distinct existing user.
        actor = await db.get(User, world.author_id)
        batch = await request_repair_rollback(
            db,
            batch_id=world.batch_id,
            actor=actor,
            expected_result_digest=result_digest(batch),
        )
        next_id = batch.rollback_async_job_id
        await db.commit()
    await worker._run_rollback(
        world.factory, world.batch_id, str(rollback_id), rollback_id
    )
    await worker._run_rollback(world.factory, world.batch_id, str(next_id), next_id)
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        assert batch.status == "rolled_back"
        assert batch.result_json["rollback"]["mutation_count"] == 2
        assert "reason" not in batch.result_json["rollback"]
        assert (await db.get(Annotation, world.annotation_id)).version == 3
    assert calls == [
        ("second", world.owner_id),
        ("first", world.owner_id),
        ("first", world.author_id),
    ]


@pytest.mark.parametrize("outside_edit", [False, True])
async def test_deterministic_shards_follow_only_committed_versions(
    repair_world, monkeypatch, outside_edit
):
    from app.schemas.mask_repair import MaskRepairDryRunRequest
    from app.core.security import create_access_token
    from app.services.mask_repair import create_repair_plan
    from app.services.raster_mask_storage import build_rle_reference

    world = repair_world
    rles = {
        build_rle_reference(rle)["sha256"]: rle for rle in (world.source, world.island)
    }

    async def load(reference):
        return rles[reference["sha256"]]

    async def no_prepare(*args, **kwargs):
        pass

    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", load)
    monkeypatch.setattr("app.services.mask_mutation.load_coco_rle", load)
    monkeypatch.setattr(
        "app.services.mask_mutation.prepare_mask_payload_for_write", no_prepare
    )
    monkeypatch.setattr("app.services.mask_repair.MAX_SHARD_MUTATIONS", 1)
    async with world.factory() as db:
        source = await db.get(Annotation, world.annotation_id)
        second = Annotation(
            task_id=source.task_id,
            project_id=source.project_id,
            user_id=source.user_id,
            source="manual",
            annotation_type="raster_mask",
            tool_unit_id=source.tool_unit_id,
            class_name=source.class_name,
            geometry=source.geometry,
            is_active=True,
            was_cancelled=False,
            version=1,
        )
        db.add(second)
        await db.flush()
        old_issue = await db.scalar(
            select(MaskQCIssue).where(MaskQCIssue.annotation_id == source.id)
        )
        values = {
            column.name: getattr(old_issue, column.name)
            for column in MaskQCIssue.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        values.update(
            annotation_id=second.id,
            related_annotation_ids=[second.id],
            source_versions={str(second.id): 1},
            dedupe_key=uuid.uuid4().hex * 2,
        )
        issue = MaskQCIssue(**values)
        db.add(issue)
        await db.flush()
        actor = await db.get(User, world.owner_id)
        request = Request(
            {
                "type": "http",
                "headers": [
                    (
                        b"authorization",
                        (
                            "Bearer " + create_access_token(str(actor.id), actor.role)
                        ).encode(),
                    )
                ],
            }
        )
        response = await create_repair_plan(
            db,
            project=await db.get(Project, world.project_id),
            actor=actor,
            request=MaskRepairDryRunRequest(
                actions=[
                    {"issue_id": old_issue.id, "kind": "delete_small_islands"},
                    {"issue_id": issue.id, "kind": "delete_small_islands"},
                ]
            ),
            http_request=request,
        )
        planned = await db.scalar(
            select(MaskRepairBatch).where(
                MaskRepairBatch.plan_digest == response.plan_digest
            )
        )
        batch = await db.get(MaskRepairBatch, world.batch_id)
        batch.plan_json, batch.plan_digest = planned.plan_json, planned.plan_digest
        assert len(batch.plan_json["shards"]) == 2
        for item in batch.plan_json["items"]:
            rles[item["result_reference"]["sha256"]] = item["result_rle"]
        first = batch.plan_json["shards"][0]
        owner = world.make_owner(db, world.task_id, actor.id)
        await owner.open()
        await owner.final()
        value = await worker._execute_shard(
            db, batch=batch, shard=first, actor=actor, reference_owner=owner
        )
        first_result = value["items"][0]
        assert first_result["after_facts"]["id"] == first_result["annotation_id"]
        assert first_result["after_facts"]["version"] == first_result["after_version"]
        batch.result_json = {"shards": {first["id"]: value}}
        if outside_edit:
            second.version += 1
        await db.commit()
    await _forward(world)
    batch, _, _ = await _state(world)
    statuses = [value["status"] for value in batch.result_json["shards"].values()]
    if outside_edit:
        assert batch.status == "partial"
        assert sorted(statuses) == ["completed", "failed"]
    else:
        assert batch.status == "completed"
        assert statuses == ["completed", "completed"]
        async with world.factory() as db:
            assert list(
                (
                    await db.scalars(
                        select(Annotation.version).where(
                            Annotation.task_id == world.task_id
                        )
                    )
                )
            ) == [2, 2]
