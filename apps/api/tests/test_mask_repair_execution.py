"""Canonical repair runner: delivery fences, commit loss, handoff and resume.

Every test drives ``app.application.mask_repair_execution`` against committed
sessions. Model inference, object storage and the tracker child factory are the
only substituted boundaries; attempt fencing, journal writes, mutation commits,
handoff accounting and terminal transitions are the production ones.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from aap_inference import extract_capabilities
from aap_inference_contract.backend_control import BackendControlSetupResult
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    native_mask_candidate_id,
)
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.application.mask_repair_execution import (
    run_mask_repair,
    run_mask_repair_rollback,
)
from app.config import settings
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.notification import Notification
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob
from app.schemas.mask_repair import MaskRepairDryRunRequest
from app.services import async_job as jobs
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.mask_repair import (
    MaskRepairError,
    canonical_digest,
    create_repair_plan,
    execute_repair_shard,
    prepare_repair_batch_dispatch,
    record_repair_dispatch_task,
    request_repair_rollback,
    resume_repair_batch,
    result_digest,
)
from app.services.mask_repair_execution import authorize_repair_task, start_attempt
from app.services.ml_interaction_proxy import prepare_interactive_context
from app.services.raster_mask_storage import build_rle_reference
from app.services.reference_bindings import task_binding
from app.workers.celery_app import celery_app
from tests.factory import create_user
from tests.inference_execution_test_ports import FakeModelExecutionPort
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_mask_repair_m5 import _seed_repair_issue
from tests.test_ml_backend_project_scope import _seed_backend

pytestmark = pytest.mark.asyncio

_WORKER_NODE = "worker:repair-control@test"


def _http_request(actor):
    """A real admitted bearer request for the exact actor credential facts."""
    role = getattr(actor.role, "value", actor.role)
    token = create_access_token(str(actor.id), role)
    return Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )


class _UnitRepairReceipt:
    def __init__(self, owner, kind, payload):
        self.owner = owner
        self.session = owner.db.sync_session
        self.transaction = self.session.get_transaction()
        self.kind = kind
        self.payload = deepcopy(payload)


def _install_unit_owner(monkeypatch, *, context, model_id, item_kind):
    """Substitute only original-source capture and physical receipt evidence.

    These tests exercise real attempts, authority re-checks, Prediction writes
    and commit loss; full source capture is covered by the managed repair
    integration tests.
    """
    from app.services import managed_mask_repair as managed
    from app.services.content_snapshot import ContentError

    class UnitOwner:
        is_managed = True
        provenance_key = "original_input"
        width, height = 5, 3

        def __init__(self, db, factory, attempt, batch, items, node_id):
            self.db = db
            self.task_id = uuid.UUID(str(items[0]["task_id"]))
            self.actor_id = attempt.actor_id
            self.opened = self.closed = False
            self._unsettled = False
            self.model = {"context": deepcopy(context), "model_id": model_id}
            self.url = "original://repair.png"
            self.route = None

        async def open(self):
            self.opened = True

        async def final(self):
            assert self.opened and not self.closed
            task = await get_core_row(
                self.db, task_row, self.task_id, populate_existing=True
            )
            from app.services.identity_account_lock_data import IdentityAccountLockData

            actor = await IdentityAccountLockData(self.db).read_actor_status(
                self.actor_id
            )
            if actor is None:
                raise ContentError("repair_actor_unavailable", 403)
            await authorize_repair_task(
                self.db, task, actor, ai=item_kind == "rerun_local_sam"
            )
            return task

        def receipt(self, kind, payload):
            assert self.opened and not self.closed
            assert not self.db.sync_session.in_nested_transaction()
            assert self.db.sync_session.get_transaction() is not None
            return _UnitRepairReceipt(self, kind, payload)

        def evidence(self):
            return {"schema": 1, "width": self.width, "height": self.height}

        async def validate(self):
            await self.final()

        def validate_capabilities(self, capabilities):
            return None

        async def note_committed_mutation(self, payload, response):
            assert response.operation_id is not None
            # Match the owner's async post-write refresh before the worker
            # serializes server-managed Annotation columns into after facts.
            for identifier in response.result_versions:
                await self.db.get(
                    Annotation, uuid.UUID(identifier), populate_existing=True
                )

        async def recheck_credentials(self):
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
    monkeypatch.setattr(managed, "ManagedRepairUnit", UnitOwner)
    return UnitOwner


class _TrackerDispatcher:
    """External tracker transport: records identity and can lose one reply."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.failures = 0

    def dispatch_tracker_job(self, job_id, *, execution_id):
        self.calls.append((str(job_id), execution_id))
        if self.failures:
            self.failures -= 1
            raise ConnectionError("broker_response_lost")
        return execution_id


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
        backend = await _seed_backend(db, project.id)
        setup_value = _setup(["mask", "polygon"])
        backend.health_meta = {"capabilities": extract_capabilities(setup_value)}
        pool = await db.scalar(
            select(MLBackendServicePool).where(
                MLBackendServicePool.legacy_instance_id == backend.id
            )
        )
        pool.enabled = True
        context, model_id = prepare_interactive_context(
            {
                "type": "interactive_box",
                "bbox": [
                    issue.region_bbox["x0"],
                    issue.region_bbox["y0"],
                    issue.region_bbox["x1"],
                    issue.region_bbox["y1"],
                ],
                "output_geometry": "mask",
            },
            extract_capabilities(setup_value),
            task_id=str(task.id),
            requested_backend_id=str(backend.id),
        )
        item = {
            "kind": "rerun_local_sam",
            "task_id": str(task.id),
            "annotation_id": str(annotation.id),
            "issue_id": str(issue.id),
            "source_version": 1,
            "class_name": "object",
            "tool_unit_id": "region",
            "region_bbox": dict(issue.region_bbox),
            "action": {
                "backend_id": str(backend.id),
                "model_id": "native-interactive",
            },
        }
        from app.services.managed_mask_repair import freeze_item

        request = _http_request(owner)
        item["source_geometry_digest"] = canonical_digest(annotation.geometry)
        await freeze_item(db, task, owner, item, request)
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
        jobs.bind_execution_id(job, str(job.id))
        batch.async_job_id = job.id
        world = SimpleNamespace(
            engine=engine,
            factory=factory,
            request=request,
            owner_id=owner.id,
            author_id=annotation.user_id,
            project_id=project.id,
            task_id=task.id,
            annotation_id=annotation.id,
            issue_id=issue.id,
            batch_id=batch.id,
            job_id=job.id,
            backend_id=backend.id,
            pool_id=pool.id,
            item=item,
            source=source,
            island=island,
            binding=task_binding(task),
            setup_value=setup_value,
            context=context,
            model_id=model_id,
            during=None,
            tracker_dispatcher=_TrackerDispatcher(),
            child_ids=[],
            stores=[],
            extra_user_ids=[],
        )
        world.dataset_id = (await db.get(DatasetItem, task.dataset_item_id)).dataset_id
        await db.commit()

    rles = {
        build_rle_reference(rle)["sha256"]: rle for rle in (world.source, world.island)
    }
    from app.services.mask_repair import store_repair_result_rles

    async def store(db, *, task_id, results):
        for _geometry, reference, rle in results:
            rles[reference["sha256"]] = deepcopy(rle)
        await store_repair_result_rles(db, task_id=task_id, results=results)
        world.stores.extend(reference["sha256"] for _, reference, _ in results)

    async def load(reference):
        return deepcopy(rles[reference["sha256"]])

    async def no_prepare(*args, **kwargs):
        return None

    def candidate(ctx, index):
        rle = CocoRlePayload(
            encoding="coco_rle",
            size=[world.source["size"][0], world.source["size"][1]],
            counts=list(world.source["counts"]),
        )
        return NativeMaskCandidate(
            value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
            score=0.9 if index == 0 else 0.7,
            candidate_id=native_mask_candidate_id(
                rle, prompt_revision=ctx["prompt_revision"], candidate_index=index
            ),
        ).model_dump(mode="json")

    async def interactive(operation, task_input):
        task_data = thaw_json(task_input)
        ctx = thaw_json(operation.context)
        if world.during is not None:
            await world.during()
        return ModelTaskResult(
            task_id=task_data["id"],
            result=[candidate(ctx, 0), candidate(ctx, 1)],
            score=0.9,
            inference_time_ms=1,
        )

    class Control:
        async def setup(self, registry_id):
            return BackendControlSetupResult(
                registry_id=uuid.UUID(str(registry_id)),
                setup=deepcopy(world.setup_value),
            )

    class Execution:
        def __init__(self, db):
            self.db = db

        async def execute(self, operation, *, before_dispatch=None):
            port = FakeModelExecutionPort(
                interactive,
                instance_id=world.backend_id,
                pool_id=world.pool_id,
                capabilities=extract_capabilities(world.setup_value),
            )
            return await port.execute(operation, before_dispatch=before_dispatch)

    _install_unit_owner(
        monkeypatch,
        context=world.context,
        model_id=world.model_id,
        item_kind="rerun_local_sam",
    )
    monkeypatch.setattr(
        "app.application.mask_repair_execution.backend_control_service",
        lambda _db: Control(),
    )
    monkeypatch.setattr(
        "app.application.mask_repair_execution.model_execution_port",
        lambda db: Execution(db),
    )
    monkeypatch.setattr(
        "app.application.mask_repair_execution.store_repair_result_rles", store
    )
    monkeypatch.setattr("app.services.mask_repair.store_repair_result_rles", store)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", load)
    monkeypatch.setattr("app.services.mask_mutation.load_coco_rle", load)
    monkeypatch.setattr(
        "app.services.prediction.prepare_mask_payload_for_write", no_prepare
    )
    monkeypatch.setattr(
        "app.services.mask_mutation.prepare_mask_payload_for_write", no_prepare
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    world.rles = rles
    try:
        yield world
    finally:
        async with factory() as db:
            actor_ids = [world.owner_id, world.author_id, *world.extra_user_ids]
            await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await db.execute(delete(AuditLog).where(AuditLog.actor_id.in_(actor_ids)))
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
                delete(Notification).where(Notification.user_id.in_(actor_ids))
            )
            await db.execute(
                delete(ProjectMLBackendPool).where(
                    ProjectMLBackendPool.project_id == world.project_id
                )
            )
            await db.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id == world.pool_id
                )
            )
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id == world.backend_id
                )
            )
            await db.execute(delete(Project).where(Project.id == world.project_id))
            await db.execute(delete(AsyncJob).where(AsyncJob.user_id.in_(actor_ids)))
            await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
            await db.execute(delete(User).where(User.id.in_(actor_ids)))
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


async def _celery_id(world, job_id):
    async with world.factory() as db:
        return (await db.get(AsyncJob, job_id)).celery_task_id


async def _forward(world):
    await run_mask_repair(
        str(world.batch_id),
        await _celery_id(world, world.job_id),
        session_factory=world.factory,
        tracker_dispatcher=world.tracker_dispatcher,
        worker_node_id=_WORKER_NODE,
    )


async def _rollback(world, rollback_job_id):
    await run_mask_repair_rollback(
        str(world.batch_id),
        await _celery_id(world, rollback_job_id),
        session_factory=world.factory,
        worker_node_id=_WORKER_NODE,
    )


async def _bind_dispatch(world, job_id, *, rollback=False):
    async with world.factory() as db:
        await record_repair_dispatch_task(
            db,
            batch_id=world.batch_id,
            async_job_id=job_id,
            rollback=rollback,
            celery_task_id=str(job_id),
        )
        await db.commit()


async def _set_plan(world, items, shards):
    plan = {"items": items, "shards": shards}
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        batch.plan_json, batch.plan_digest = plan, canonical_digest(plan)
        await db.commit()


def _lose_next_unit_commit(world, section):
    """Simulate a lost COMMIT response exactly when a unit became durable."""
    state = {"raised": False}
    observer = async_sessionmaker(world.engine, expire_on_commit=False)

    class LostCommitResponse(AsyncSession):
        async def commit(self):
            inspect = not state["raised"]
            await super().commit()
            if not inspect:
                return
            async with observer() as db:
                batch = await db.get(MaskRepairBatch, world.batch_id)
                saved = (batch.result_json or {}).get(section) or {}
                if section == "rollback":
                    saved = saved.get("shards") or {}
            if any(
                isinstance(value, dict)
                and value.get("status") in {"completed", "dispatch_pending"}
                for value in saved.values()
            ):
                state["raised"] = True
                raise ConnectionError("commit_response_lost")

    world.factory = async_sessionmaker(
        world.engine, class_=LostCommitResponse, expire_on_commit=False
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
    assert (batch.status, job.status, count) == ("completed", "completed", 2), (
        batch.result_json
    )
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


async def test_repair_without_frozen_original_never_reaches_model_io(
    repair_world, monkeypatch
):
    world = repair_world
    reached = []

    async def unexpected(*args, **kwargs):
        reached.append(True)
        raise AssertionError("Recapture must stop before model or storage work")

    monkeypatch.setattr(
        "app.application.mask_repair_execution.model_execution_port", unexpected
    )
    monkeypatch.setattr(
        "app.application.mask_repair_execution.store_repair_result_rles", unexpected
    )
    await _set_plan(
        world,
        [{key: value for key, value in world.item.items() if key != "original_input"}],
        [],
    )
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
    world = repair_world
    from app.services.prediction import PredictionService

    original = PredictionService.apply_prediction_result
    calls = 0

    async def fail_second(self, request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second_candidate_failed")
        return await original(self, request)

    monkeypatch.setattr(PredictionService, "apply_prediction_result", fail_second)
    await _forward(world)
    batch, job, count = await _state(world)
    assert (batch.status, job.status, count) == ("failed", "failed", 0)
    assert batch.result_json["candidates"]["0"]["reason"] == "second_candidate_failed"


@pytest.mark.parametrize(
    "change", ["cancel", "disable_actor", "source_version", "disable_native_gate"]
)
async def test_sam_rechecks_after_unlocked_model_io(repair_world, change):
    world = repair_world
    entered, release = asyncio.Event(), asyncio.Event()

    async def during():
        entered.set()
        await release.wait()

    world.during = during
    running = asyncio.create_task(_forward(world))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with world.factory() as db:
            job = await db.scalar(
                job_lock_statement(world.job_id, mode="update", populate_existing=True)
            )
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
    assert world.stores == []
    assert job.status == ("cancelled" if change == "cancel" else "failed")
    assert batch.status == ("cancelled" if change == "cancel" else "failed")
    if change != "cancel":
        assert batch.result_json["candidates"]["0"]["status"] == "failed"


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
        batch = await resume_repair_batch(
            db, batch_id=world.batch_id, actor=actor, http_request=world.request
        )
        replacement_id = batch.async_job_id
        await db.commit()
    await _bind_dispatch(world, replacement_id)
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
                    http_request=world.request,
                )
            return await resume_repair_batch(
                db, batch_id=world.batch_id, actor=user, http_request=world.request
            )

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


@pytest.mark.parametrize("state", ["pending", "running"])
async def test_cancel_is_durable_when_revoke_fails(repair_world, monkeypatch, state):
    from app.api.v1.async_jobs import cancel_async_job
    from app.deps import get_job_cancellation_port, get_job_execution_control

    world = repair_world
    async with world.factory() as db:
        (await db.get(AsyncJob, world.job_id)).status = state
        (await db.get(MaskRepairBatch, world.batch_id)).status = state
        await db.commit()

    def unavailable(*args, **kwargs):
        raise ConnectionError("broker_unavailable")

    monkeypatch.setattr(celery_app.control, "revoke", unavailable)
    async with world.factory() as db:
        cancellation = get_job_cancellation_port(
            db=db, execution_control=get_job_execution_control()
        )
        response = await cancel_async_job(
            job_id=world.job_id,
            db=db,
            actor_id=world.owner_id,
            cancellation=cancellation,
        )
    result = response.model_dump(mode="json")
    batch, job, _ = await _state(world)
    assert result["status"] == (
        "cancelled" if state == "pending" else "cancel_requested"
    )
    assert job.status == ("cancelled" if state == "pending" else "running")
    assert batch.status == ("cancelled" if state == "pending" else "running")
    if state == "running":
        assert job.payload["cancel_requested"]


@pytest.mark.parametrize("state", ["pending", "running", "completed"])
async def test_dispatch_response_loss_does_not_overwrite_worker(
    repair_world, monkeypatch, state
):
    """A lost publish reply may only fail a delivery no worker claimed.

    The broker exception is not proof that the task was undelivered, so the API
    must fail an unclaimed attempt and leave a claimed or settled one alone.
    """
    from app.api.v1.mask_qc import _dispatch_repair_or_fail

    world = repair_world

    async def lost_response(batch_id, *, dispatcher, execution_id, rollback=False):
        # A worker that already claimed or settled the job wins the race.
        async with world.factory() as db:
            job = await db.scalar(
                job_lock_statement(world.job_id, mode="update", populate_existing=True)
            )
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
        lease = await prepare_repair_batch_dispatch(db, batch, rollback=False)
        # The API route holds the caller UOW only while preparing the publication;
        # release it so the concurrent worker can take its own row locks.
        await db.commit()
        with pytest.raises(HTTPException):
            await _dispatch_repair_or_fail(
                db,
                batch=batch,
                rollback=False,
                dispatcher=SimpleNamespace(),
                dispatch_lease=lease,
            )
    batch, job, _ = await _state(world)
    assert job.status == ("failed" if state == "pending" else state)
    assert batch.status == ("failed" if state == "pending" else state)


@pytest.mark.parametrize("lose_commit", [False, True])
async def test_tracker_unknown_publish_resumes_same_durable_child(
    repair_world, monkeypatch, lose_commit
):
    world = repair_world
    tracker_item = {
        "kind": "rerun_tracker",
        "task_id": str(world.task_id),
        "annotation_id": str(world.annotation_id),
        "issue_id": str(world.issue_id),
        "source_version": 1,
        "frame_index": 0,
        "action": {
            "from_frame": 0,
            "to_frame": 1,
            "model_key": "test-tracker",
            "model_id": "mask-tracker",
            "backend_id": str(world.backend_id),
            "direction": "forward",
        },
    }
    async with world.factory() as db:
        (await db.get(Task, world.task_id)).file_type = "video"
        annotation = await db.get(Annotation, world.annotation_id)
        annotation.annotation_type = "video_track_mask"
        annotation.geometry = {
            "type": "video_track_mask",
            "track_id": "repair-control",
            "keyframes": [
                {
                    "frame_index": 0,
                    "mask": build_rle_reference(world.source),
                    "source": "manual",
                    "occluded": False,
                }
            ],
            "outside": [],
        }
        await db.commit()
        await db.refresh(annotation)
        tracker_item["source_version"] = annotation.version
    await _set_plan(world, [tracker_item], [])

    async def create_child(db, *, task, ctx, annotation_id, payload, user):
        child = VideoTrackerJob(
            task_id=task.id,
            dataset_item_id=task.dataset_item_id,
            annotation_id=annotation_id,
            created_by=user.id,
            model_key=payload.model_key,
            direction=payload.direction,
            from_frame=payload.from_frame,
            to_frame=payload.to_frame,
            event_channel=f"test:{uuid.uuid4()}",
        )
        db.add(child)
        await db.flush()
        world.child_ids.append(child.id)
        return SimpleNamespace(id=child.id)

    monkeypatch.setattr(
        "app.application.mask_repair_execution.create_video_mask_correction_job",
        create_child,
    )

    async def video_context(db, task):
        return SimpleNamespace()

    monkeypatch.setattr(
        "app.application.mask_repair_execution.build_context_from_task", video_context
    )
    world.tracker_dispatcher.failures = 1
    fault = _lose_next_unit_commit(world, "candidates") if lose_commit else None
    await _forward(world)
    if fault:
        assert fault["raised"]
    batch, job, _ = await _state(world)
    assert (batch.status, job.status) == ("partial", "failed"), batch.result_json
    pending = batch.result_json["candidates"]["0"]
    assert pending["status"] == "dispatch_pending"
    assert pending["dispatch_state"] == "unknown"
    assert len(world.child_ids) == 1
    child_id = world.child_ids[0]
    assert world.tracker_dispatcher.calls == [
        (str(child_id), pending["celery_task_id"])
    ]
    async with world.factory() as db:
        assert (await db.get(VideoTrackerJob, child_id)).status == "queued"

    # The owner ledger acknowledges an unknown reply as accepted=False and backs
    # off (2^attempts seconds, so 2s after this first attempt). The resume may
    # only republish once the exact envelope is due; a pending intent is its
    # scheduled retry, not a new publication.
    await asyncio.sleep(2.5)
    async with world.factory() as db:
        actor = await db.get(User, world.owner_id)
        batch = await resume_repair_batch(
            db, batch_id=world.batch_id, actor=actor, http_request=world.request
        )
        world.job_id = batch.async_job_id
        await db.commit()
    await _bind_dispatch(world, world.job_id)
    await _forward(world)
    batch, job, _ = await _state(world)
    assert (batch.status, job.status) == ("completed", "completed")
    assert len(world.child_ids) == 1
    assert (
        world.tracker_dispatcher.calls
        == [(str(child_id), pending["celery_task_id"])] * 2
    )
    assert batch.result_json["candidates"]["0"]["dispatch_state"] == "published"
    assert "reason" not in batch.result_json["candidates"]["0"]

    # A late unknown reply on a still-live delivery must not demote a confirmed
    # child publication, and the settled candidate must not republish.
    from app.application.mask_repair_execution import _dispatch_tracker_handoff
    from app.services.mask_repair_execution import RepairAttempt

    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        job = await db.get(AsyncJob, world.job_id)
        job.status = "running"
        batch.status = "running"
        await db.commit()
        late_attempt = RepairAttempt(
            batch_id=world.batch_id,
            job_id=world.job_id,
            actor_id=job.user_id,
            celery_task_id=job.celery_task_id,
            plan_digest=batch.plan_digest,
        )
    world.tracker_dispatcher.failures = 1
    await _dispatch_tracker_handoff(
        world.factory,
        dispatcher=world.tracker_dispatcher,
        attempt=late_attempt,
        item_index=0,
        child_id=str(child_id),
        celery_task_id=pending["celery_task_id"],
    )
    batch, _, _ = await _state(world)
    assert len(world.tracker_dispatcher.calls) == 2
    assert batch.result_json["candidates"]["0"]["dispatch_state"] == "published"


@pytest.mark.parametrize("lose_commit", [False, True])
async def test_rollback_checkpoint_survives_failure_and_uses_new_actor(
    repair_world, monkeypatch, lose_commit
):
    world = repair_world
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
            http_request=world.request,
        )
        planned = await db.scalar(
            select(MaskRepairBatch).where(
                MaskRepairBatch.plan_digest == response.plan_digest
            )
        )
        batch = await db.get(MaskRepairBatch, world.batch_id)
        batch.plan_json, batch.plan_digest = planned.plan_json, planned.plan_digest
        assert len(batch.plan_json["shards"]) == 2
        await db.commit()

    # Produce the real two-shard forward journal, including committed versions
    # and after facts, before asking for a reverse-order rollback.
    await _forward(world)
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        assert batch.status == "completed", batch.result_json
        actor = await db.get(User, world.owner_id)
        batch = await request_repair_rollback(
            db,
            batch_id=world.batch_id,
            actor=actor,
            expected_result_digest=result_digest(batch),
            http_request=world.request,
        )
        rollback_id = batch.rollback_async_job_id
        await db.commit()
    await _bind_dispatch(world, rollback_id, rollback=True)
    calls = []

    async def rollback(db, *, plan_items, shard_result, actor, **kwargs):
        entry = shard_result["items"][0]
        key = (
            "first" if entry["annotation_id"] == str(world.annotation_id) else "second"
        )
        calls.append((key, actor.id))
        if key == "first" and len(calls) == 2:
            raise RuntimeError("rollback_interrupted")
        annotation = await db.get(Annotation, uuid.UUID(entry["annotation_id"]))
        annotation.version += 1
        return 1

    monkeypatch.setattr(
        "app.application.mask_repair_execution.rollback_repair_shard", rollback
    )
    fault = _lose_next_unit_commit(world, "rollback") if lose_commit else None
    await _rollback(world, rollback_id)
    if fault:
        assert fault["raised"]
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        assert batch.status == "rollback_failed"
        first_id, second_id = (str(row["id"]) for row in batch.plan_json["shards"])
        assert [row["item_indexes"] for row in batch.plan_json["shards"]] == [[0], [1]]
        assert set(batch.result_json["rollback"]["shards"]) == {second_id}
        assert first_id not in batch.result_json["rollback"]["shards"]
        assert (await db.get(Annotation, world.annotation_id)).version == 2
        # Ownership changes before continuation; the new manager, not the
        # original requester or source author, must own the rollback writes.
        reviewer = await create_user(
            db,
            "project_admin",
            f"repair-reviewer-{uuid.uuid4()}@test.local",
            "Repair Manager",
        )
        world.extra_user_ids.append(reviewer.id)
        (await db.get(Project, world.project_id)).owner_id = reviewer.id
        await db.flush()
        actor = await db.get(User, reviewer.id)
        batch = await request_repair_rollback(
            db,
            batch_id=world.batch_id,
            actor=actor,
            expected_result_digest=result_digest(batch),
            http_request=_http_request(actor),
        )
        next_id = batch.rollback_async_job_id
        await db.commit()
    await _rollback(world, rollback_id)
    await _bind_dispatch(world, next_id, rollback=True)
    await _rollback(world, next_id)
    async with world.factory() as db:
        batch = await db.get(MaskRepairBatch, world.batch_id)
        assert batch.status == "rolled_back"
        assert batch.result_json["rollback"]["mutation_count"] == 2
        assert "reason" not in batch.result_json["rollback"]
        assert (await db.get(Annotation, world.annotation_id)).version == 3
    assert calls == [
        ("second", world.owner_id),
        ("first", world.owner_id),
        ("first", reviewer.id),
    ]


@pytest.mark.parametrize("outside_edit", [False, True])
async def test_deterministic_shards_follow_only_committed_versions(
    repair_world, monkeypatch, outside_edit
):
    world = repair_world
    rles = world.rles
    monkeypatch.setattr("app.services.mask_repair.MAX_SHARD_MUTATIONS", 1)

    async def freeze(db, task, actor, item, http_request=None):
        # Physical original capture is covered by the managed integration tests;
        # the plan only needs its frozen identity marker here.
        item["original_input"] = {"schema": 1, "digest": "frozen-original"}

    monkeypatch.setattr("app.services.reference_mask_repair.freeze_item", freeze)
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
        for planned_item in batch.plan_json["items"]:
            rles[planned_item["result_reference"]["sha256"]] = planned_item[
                "result_rle"
            ]
        first = batch.plan_json["shards"][0]
        from app.services.managed_mask_repair import ManagedRepairUnit as Owner

        owner = Owner(
            db,
            world.factory,
            SimpleNamespace(actor_id=actor.id),
            batch,
            [batch.plan_json["items"][0]],
            _WORKER_NODE,
        )
        await owner.open()
        await owner.final()
        value = await execute_repair_shard(
            db,
            batch_id=world.batch_id,
            shard=first,
            actor=actor,
            async_job_id=world.job_id,
            source_owner=owner,
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
