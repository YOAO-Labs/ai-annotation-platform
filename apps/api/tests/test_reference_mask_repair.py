"""Reference repairs cross committed sessions and retain strict original identity."""

from copy import deepcopy
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
import uuid

import httpx
from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    native_mask_candidate_id,
)
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.deps import get_db
from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.db.models.dataset_source import (
    DatasetSource,
    DatasetScanRun,
    DatasetSourceRevision,
    DatasetSourceChange,
    DatasetRegistrationRun,
)
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.notification import Notification
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.async_job import AsyncJobOut
from app.schemas.mask_repair import MaskRepairDryRunRequest
from app.services.content_snapshot import ContentError
from app.services.dataset_references import register_entry
from app.services.mask_repair import (
    create_repair_plan,
    execute_repair_plan,
    record_repair_dispatch_task,
    request_repair_rollback,
    resume_repair_batch,
    result_digest,
    store_repair_result_rles,
)
from app.services.mask_repair_execution import start_attempt
from aap_inference import extract_capabilities
from aap_inference_contract.backend_control import BackendControlSetupResult
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from app.services.raster_mask_storage import (
    build_rle_reference,
    lock_raster_mask_references,
    reserve_raster_mask_upload,
    store_coco_rle,
)
from app.services.reference_mask_repair import (
    ReferenceRepairUnit,
    validate_receipt,
    unit_items,
)
from app.utils.raster_mask_rle import encode_coco_rle
from tests.factory import create_user
from tests.inference_execution_test_ports import FakeModelExecutionPort
from tests.test_dataset_content import content_world
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_mask_repair_m5 import _seed_repair_issue
from tests.test_ml_backend_project_scope import _seed_backend
from tests.test_prediction_inputs import input_world as input_world


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def reference_repair(test_engine, tmp_path, monkeypatch, input_world):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    request = SimpleNamespace(state=SimpleNamespace(api_key_id=None))
    async with factory() as db:
        actor = await create_user(
            db,
            "super_admin",
            f"ref-repair-{uuid.uuid4()}@example.test",
            "Reference repair",
        )
        content = await content_world.__wrapped__(
            db, (actor, None), tmp_path, monkeypatch
        )
        registration = await register_entry(
            db, content[6].id, content[7].path_digest, actor
        )
        reference_item = await db.get(DatasetItem, registration["item_id"])
        pixels = [0] * (48 * 32)
        pixels[49:52] = [1, 1, 1]
        pixels[-1] = 1
        source = encode_coco_rle(pixels, 48, 32)
        island = encode_coco_rle([0] * (48 * 32 - 1) + [1], 48, 32)
        project, task, annotation, issue, _, _ = await _seed_repair_issue(
            db, owner_id=actor.id, source=source, island=island
        )
        old_dataset = await db.scalar(
            select(DatasetItem.dataset_id).where(DatasetItem.id == task.dataset_item_id)
        )
        task.dataset_item_id, task.file_path = (
            reference_item.id,
            reference_item.file_path,
        )
        project.ai_interactive_enabled = False
        source_ref, island_ref = (
            build_rle_reference(source),
            build_rle_reference(island),
        )
        issue.region_bbox = {"x0": 47 / 48, "y0": 31 / 32, "x1": 1.0, "y1": 1.0}
        issue.region_mask_ref, issue.region_digest = island_ref, island_ref["sha256"]
        backend = await _seed_backend(db, project.id)
        backend.health_meta = {
            "capabilities": extract_capabilities(_setup(["mask", "polygon"]))
        }
        pool = await db.scalar(
            select(MLBackendServicePool).where(
                MLBackendServicePool.legacy_instance_id == backend.id
            )
        )
        pool.enabled = True
        for rle in (source, island):
            await store_coco_rle(rle, db=db)
        await reserve_raster_mask_upload(
            db, task_id=task.id, object_key=source_ref["object_key"], limit=100
        )
        await lock_raster_mask_references(db, annotation.geometry, task_id=task.id)
        await db.commit()
        w = SimpleNamespace(
            factory=factory,
            request=request,
            actor_id=actor.id,
            author_id=annotation.user_id,
            project_id=project.id,
            task_id=task.id,
            annotation_id=annotation.id,
            issue_id=issue.id,
            backend_id=backend.id,
            pool_id=pool.id,
            old_dataset=old_dataset,
            content=content,
            inputs=input_world,
            calls=[],
            during=None,
            setup_during=None,
            setup_value=_setup(["mask", "polygon"]),
            source=source,
            stored=[],
        )

    async def store(db, *, results, **kwargs):
        await store_repair_result_rles(db, results=results, **kwargs)
        w.stored.extend(reference["sha256"] for _, reference, _ in results)

    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(
        "app.application.mask_repair_execution.store_repair_result_rles", store
    )

    async def setup(registry_id):
        if w.setup_during:
            await w.setup_during()
        return BackendControlSetupResult(
            registry_id=uuid.UUID(str(registry_id)),
            setup=deepcopy(w.setup_value),
        )

    async def interactive(operation, task_input):
        task_data = thaw_json(task_input)
        context = thaw_json(operation.context)
        parsed = urlsplit(task_data["file_path"])
        bucket, key = parsed.path.lstrip("/").split("/", 1)
        assert input_world.objects[bucket, key] == content[1]
        assert key.startswith("prediction-inputs/")
        w.calls.append(deepcopy(context))
        if w.during:
            await w.during()
        rle = CocoRlePayload(encoding="coco_rle", size=[32, 48], counts=[49, 3, 1484])
        result = NativeMaskCandidate(
            value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
            score=0.9,
            candidate_id=native_mask_candidate_id(
                rle, prompt_revision=context["prompt_revision"], candidate_index=0
            ),
        )
        return ModelTaskResult(
            task_id=task_data["id"],
            result=[result.model_dump(mode="json")],
            score=0.9,
            inference_time_ms=12,
        )

    class Control:
        async def setup(self, registry_id):
            return await setup(registry_id)

    control = Control()
    monkeypatch.setattr(
        "app.application.mask_repair_execution.backend_control_service",
        lambda _db: control,
    )

    class Execution:
        def __init__(self, db):
            self.db = db

        async def execute(self, operation, *, before_dispatch=None):
            from app.services.ml_backend import MLBackendService

            reply = await setup(operation.requested_registry_id)
            pool_id = await MLBackendService(self.db).pool_id_for_registry(
                operation.requested_registry_id
            )
            port = FakeModelExecutionPort(
                interactive,
                pool_id=pool_id,
                capabilities=extract_capabilities(reply.setup),
            )
            return await port.execute(operation, before_dispatch=before_dispatch)

    monkeypatch.setattr(
        "app.application.mask_repair_execution.model_execution_port",
        lambda db: Execution(db),
    )
    try:
        yield w
    finally:
        async with factory() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_id.in_([w.actor_id, w.author_id]))
            )
            await db.execute(
                delete(PredictionMeta).where(
                    PredictionMeta.prediction_id.in_(
                        select(Prediction.id).where(Prediction.task_id == w.task_id)
                    )
                )
            )
            await db.execute(delete(Prediction).where(Prediction.task_id == w.task_id))
            await db.execute(delete(Annotation).where(Annotation.task_id == w.task_id))
            await db.execute(delete(Task).where(Task.id == w.task_id))
            await db.execute(delete(Project).where(Project.id == w.project_id))
            await db.execute(
                delete(MLBackendServicePool).where(MLBackendServicePool.id == w.pool_id)
            )
            await db.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id == w.backend_id)
            )
            source_id, dataset_id, location_id = (
                content[5].id,
                content[3].id,
                content[4].id,
            )
            await db.execute(
                delete(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
            )
            runs = select(DatasetRegistrationRun.id).where(
                DatasetRegistrationRun.source_id == source_id
            )
            await db.execute(
                delete(DatasetSourceChange).where(DatasetSourceChange.run_id.in_(runs))
            )
            await db.execute(
                delete(DatasetSourceRevision).where(
                    DatasetSourceRevision.source_id == source_id
                )
            )
            await db.execute(
                delete(DatasetRegistrationRun).where(
                    DatasetRegistrationRun.source_id == source_id
                )
            )
            await db.execute(
                delete(DatasetScanRun).where(DatasetScanRun.source_id == source_id)
            )
            await db.execute(delete(DatasetSource).where(DatasetSource.id == source_id))
            await db.execute(
                delete(Dataset).where(Dataset.id.in_([dataset_id, w.old_dataset]))
            )
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location_id)
            )
            await db.execute(
                delete(PredictionInputLease).where(
                    PredictionInputLease.task_id == w.task_id
                )
            )
            await db.execute(
                delete(Notification).where(
                    Notification.user_id.in_([w.actor_id, w.author_id])
                )
            )
            await db.execute(
                delete(AsyncJob).where(AsyncJob.user_id.in_([w.actor_id, w.author_id]))
            )
            await db.execute(delete(User).where(User.id.in_([w.actor_id, w.author_id])))
            await db.commit()
        assert not input_world.objects


@pytest.fixture
async def repair_client(reference_repair, app_module):
    w = reference_repair
    previous = app_module.dependency_overrides.get(get_db)

    async def database():
        async with w.factory() as db:
            yield db

    app_module.dependency_overrides[get_db] = database
    token = create_access_token(subject=str(w.actor_id), role="super_admin")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            yield client
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


async def plan(w, *, sam=False):
    async with w.factory() as db:
        project, actor = (
            await db.get(Project, w.project_id),
            await db.get(User, w.actor_id),
        )
        if sam:
            project.ai_interactive_enabled = True
            (await db.get(MaskQCIssue, w.issue_id)).code = "boundary_noise"
            await db.flush()
        response = await create_repair_plan(
            db,
            project=project,
            actor=actor,
            request=MaskRepairDryRunRequest(
                actions=[
                    {
                        "issue_id": str(w.issue_id),
                        "kind": "rerun_local_sam" if sam else "delete_small_islands",
                        **({"backend_id": str(w.backend_id)} if sam else {}),
                    }
                ]
            ),
            http_request=w.request,
        )
        assert response.summary.executable_count == 1, [
            (item.skip_code, item.skip_detail) for item in response.items
        ]
        await db.commit()
        w.receipt, w.plan_digest = response.receipt, response.plan_digest
    return response


async def execute(w):
    async with w.factory() as db:
        batch, _ = await execute_repair_plan(
            db,
            project=await db.get(Project, w.project_id),
            actor=await db.get(User, w.actor_id),
            receipt=w.receipt,
            plan_digest=w.plan_digest,
            http_request=w.request,
        )
        w.batch_id, w.job_id = batch.id, batch.async_job_id
        await record_repair_dispatch_task(
            db,
            batch_id=w.batch_id,
            async_job_id=w.job_id,
            rollback=False,
            celery_task_id=str(w.job_id),
        )
        await db.commit()


async def forward(w, *, node="worker:content@test"):
    from app.application.mask_repair_execution import run_mask_repair
    from app.services.jobs_queue import build_video_tracker_job_dispatcher

    async with w.factory() as db:
        celery_task_id = (await db.get(AsyncJob, w.job_id)).celery_task_id
    await run_mask_repair(
        str(w.batch_id),
        celery_task_id,
        session_factory=w.factory,
        tracker_dispatcher=build_video_tracker_job_dispatcher(),
        worker_node_id=node,
    )


async def state(w):
    async with w.factory() as db:
        batch = await db.get(MaskRepairBatch, w.batch_id)
        job = await db.get(AsyncJob, batch.async_job_id)
        annotation = await db.get(Annotation, w.annotation_id)
        predictions = (
            await db.scalars(select(Prediction).where(Prediction.task_id == w.task_id))
        ).all()
        inputs = list(
            await db.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.task_id == w.task_id
                )
            )
        )
        assert all(
            row.state == "deleted" and row.deleted_at is not None for row in inputs
        )
        return batch, job, annotation, predictions


async def test_deterministic_ai_off_executes_and_strictly_rolls_back(reference_repair):
    w = reference_repair
    await plan(w)
    await execute(w)
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "completed",
        "completed",
        2,
        0,
    ), batch.result_json
    assert not w.inputs.puts and not w.calls
    async with w.factory() as db:
        batch = await db.get(MaskRepairBatch, w.batch_id)
        batch = await request_repair_rollback(
            db,
            batch_id=w.batch_id,
            actor=await db.get(User, w.actor_id),
            expected_result_digest=result_digest(batch),
            http_request=w.request,
        )
        rollback_id = batch.rollback_async_job_id
        await record_repair_dispatch_task(
            db,
            batch_id=w.batch_id,
            async_job_id=rollback_id,
            rollback=True,
            celery_task_id=str(rollback_id),
        )
        await db.commit()
    from app.application.mask_repair_execution import run_mask_repair_rollback

    async with w.factory() as db:
        celery_task_id = (await db.get(AsyncJob, rollback_id)).celery_task_id
    await run_mask_repair_rollback(
        str(w.batch_id),
        celery_task_id,
        session_factory=w.factory,
        worker_node_id="worker:content@test",
    )
    batch, _, annotation, _ = await state(w)
    assert (batch.status, annotation.version) == ("rolled_back", 3), batch.result_json
    expected = build_rle_reference(w.source)
    assert all(
        annotation.geometry["mask"][key] == value for key, value in expected.items()
    )


@pytest.mark.parametrize(
    "change", ["bytes", "node", "missing_node", "probe", "source", "gate"]
)
async def test_reference_worker_fails_closed_before_any_write(reference_repair, change):
    w = reference_repair
    await plan(w)
    await execute(w)
    node = "worker:content@test"
    if change == "bytes":
        (w.content[0] / w.content[7].relative_path).write_bytes(b"changed")
    elif change in {"node", "missing_node"}:
        node = "api:content@test" if change == "node" else None
    else:
        async with w.factory() as db:
            if change == "probe":
                await db.execute(
                    delete(StorageLocationProbe).where(
                        StorageLocationProbe.location_id == w.content[4].id,
                        StorageLocationProbe.node_id == node,
                    )
                )
            elif change == "source":
                (await db.get(DatasetSource, w.content[5].id)).enabled = False
            else:
                (
                    await db.get(Project, w.project_id)
                ).raster_mask_native_editing_enabled = False
            await db.commit()
    await forward(w, node=node)
    batch, job, annotation, predictions = await state(w)
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "failed",
        "failed",
        1,
        0,
    ), batch.result_json
    assert not w.stored and not w.inputs.puts and not w.calls


async def test_resume_uses_new_attempt_but_original_content(reference_repair):
    w = reference_repair
    await plan(w)
    await execute(w)
    await forward(w, node=None)
    previous = w.job_id
    async with w.factory() as db:
        batch = await resume_repair_batch(
            db,
            batch_id=w.batch_id,
            actor=await db.get(User, w.actor_id),
            http_request=w.request,
        )
        w.job_id = batch.async_job_id
        await record_repair_dispatch_task(
            db,
            batch_id=w.batch_id,
            async_job_id=w.job_id,
            rollback=False,
            celery_task_id=str(w.job_id),
        )
        await db.commit()
    assert w.job_id != previous
    await forward(w)
    batch, job, annotation, _ = await state(w)
    assert (batch.status, job.status, annotation.version) == (
        "completed",
        "completed",
        2,
    ), batch.result_json


async def test_sam_uses_owned_original_and_frozen_model_then_cleans(reference_repair):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "completed",
        "completed",
        1,
        1,
    ), batch.result_json
    assert w.calls[0]["model_id"] == "native-interactive"
    async with w.factory() as db:
        metadata = await db.scalar(
            select(PredictionMeta).where(
                PredictionMeta.prediction_id == predictions[0].id
            )
        )
        evidence = metadata.extra["reference_input"]
        assert evidence["node_id"] == "worker:content@test"
        assert (
            evidence["item_id"]
            == batch.plan_json["items"][0]["reference_input"]["item_id"]
        )
        assert (
            evidence["sha256"]
            == batch.plan_json["items"][0]["reference_input"]["reference"]["sha256"]
        )
        assert evidence["model"]["identity_guarantee"] == "declared_configuration"
        public = AsyncJobOut.model_validate(
            await db.get(AsyncJob, w.job_id)
        ).model_dump(mode="json")
        assert "repair_authority" not in str(public)
    assert w.inputs.puts and not w.inputs.objects


@pytest.mark.parametrize(
    "change",
    [
        "declared_model",
        "actual_model",
        "ai_off",
        "cancel",
        "source",
        "backend_disabled",
    ],
)
async def test_sam_revalidates_actual_dispatch_and_final_authority(
    reference_repair, change
):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)

    async def revoke():
        async with w.factory() as db:
            if change == "ai_off":
                (await db.get(Project, w.project_id)).ai_interactive_enabled = False
            elif change == "source":
                (await db.get(DatasetSource, w.content[5].id)).enabled = False
            elif change == "backend_disabled":
                assoc = await db.scalar(
                    select(ProjectMLBackendPool).where(
                        ProjectMLBackendPool.project_id == w.project_id,
                        ProjectMLBackendPool.pool_id == w.pool_id,
                    )
                )
                assoc.enabled = False
            else:
                job = await db.get(AsyncJob, w.job_id)
                job.payload = {**job.payload, "cancel_requested": True}
            await db.commit()

    if change == "declared_model":
        async with w.factory() as db:
            backend = await db.get(MLBackendRegistry, w.backend_id)
            meta = deepcopy(backend.health_meta)
            meta["capabilities"]["models"][0]["supported_prompts"] = ["point"]
            backend.health_meta = meta
            await db.commit()
    elif change == "actual_model":
        w.setup_value["models"][0]["supported_prompts"] = ["point"]
    else:
        w.during = revoke
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    expected = "cancelled" if change == "cancel" else "failed"
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        expected,
        expected,
        1,
        0,
    ), batch.result_json
    assert not w.stored
    assert len(w.calls) == (0 if change in {"declared_model", "actual_model"} else 1)


async def test_cancellation_during_setup_prevents_reference_model_dispatch(
    reference_repair,
):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)

    async def cancel():
        async with w.factory() as db:
            job = await db.get(AsyncJob, w.job_id)
            job.payload = {**job.payload, "cancel_requested": True}
            await db.commit()

    w.setup_during = cancel
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert not w.calls
    assert not w.stored and not predictions
    assert annotation.version == 1
    assert batch.status != "completed" and job.status != "completed"


@pytest.mark.parametrize(
    "change", ["type", "session", "transaction", "nested", "actor", "body", "closed"]
)
async def test_private_receipt_cannot_escape_live_unit(reference_repair, change):
    w = reference_repair
    await plan(w)
    await execute(w)
    async with w.factory() as db:
        attempt = await start_attempt(db, w.batch_id, w.job_id, str(w.job_id))
        await db.commit()
        batch = await db.get(MaskRepairBatch, w.batch_id)
        shard = batch.plan_json["shards"][0]
        owner = ReferenceRepairUnit(
            db,
            w.factory,
            attempt,
            batch,
            unit_items(batch, "shards", shard),
            "worker:content@test",
        )
        other = nested = None
        try:
            await owner.open()
            await owner.final()
            body = {"sample": "exact"}
            receipt = owner.receipt("unit", body)
            await validate_receipt(db, w.task_id, w.actor_id, receipt, "unit", body)
            actor_id, session = w.actor_id, db
            if change == "type":
                receipt = SimpleNamespace(
                    **{name: getattr(receipt, name) for name in receipt.__slots__}
                )
            elif change == "session":
                other = session = w.factory()
            elif change == "transaction":
                await db.commit()
            elif change == "nested":
                nested = await db.begin_nested()
            elif change == "actor":
                actor_id = uuid.uuid4()
            elif change == "body":
                body["sample"] = "changed"
            else:
                await owner.close()
            with pytest.raises(ContentError, match="repair_reference_receipt_invalid"):
                await validate_receipt(
                    session, w.task_id, actor_id, receipt, "unit", body
                )
        finally:
            if nested:
                await nested.rollback()
            if other:
                await other.close()
            await db.rollback()
            await owner.close()


@pytest.mark.parametrize("parent_status", ["completed", "partial", "cancelled"])
async def test_sam_candidate_accepts_through_real_api(
    reference_repair, repair_client, parent_status
):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)
    await forward(w)
    batch, _, _, predictions = await state(w)
    assert batch.status == "completed", batch.result_json
    prediction = predictions[0]
    async with w.factory() as db:
        (await db.get(MaskRepairBatch, w.batch_id)).status = parent_status
        # Current task version may change during normal assignment. Original
        # content and the exact source annotation remain authoritative.
        task = await db.get(Task, w.task_id)
        task.version += 1
        await db.commit()
    response = await repair_client.post(
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}/accept"
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    accepted_id = uuid.UUID(response.json()[0]["id"])
    replay = await repair_client.post(
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}/accept"
    )
    assert replay.status_code == 409, replay.text
    async with w.factory() as db:
        accepted = await db.get(Annotation, accepted_id)
        assert accepted.parent_prediction_id == prediction.id
        assert (
            accepted.geometry["mask"]["sha256"]
            == prediction.result[0]["geometry"]["mask"]["sha256"]
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == w.task_id)
            )
            == 2
        )


@pytest.mark.parametrize(
    "change",
    [
        "source_version",
        "source_locked",
        "source_missing",
        "result",
        "metadata",
        "ledger",
        "ordinary",
        "geometry",
        "original",
        "object",
        "gate",
    ],
)
async def test_sam_candidate_acceptance_rejects_changed_or_unowned_input(
    reference_repair, repair_client, change
):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)
    await forward(w)
    batch, _, _, predictions = await state(w)
    assert batch.status == "completed", batch.result_json
    prediction = predictions[0]
    deleted_object = None
    async with w.factory() as db:
        source = await db.get(Annotation, w.annotation_id)
        if change == "source_version":
            source.version += 1
        elif change == "source_locked":
            source.is_locked = True
        elif change == "source_missing":
            source.is_active = False
        elif change in {"result", "ordinary", "geometry"}:
            row = await db.scalar(
                select(Prediction).where(Prediction.id == prediction.id)
            )
            if change == "ordinary":
                row.source = "ml_backend"
            else:
                value = deepcopy(row.result)
                if change == "result":
                    value[0]["class_name"] = "tampered"
                else:
                    value[0]["geometry"] = {
                        "type": "bbox",
                        "x": 0.1,
                        "y": 0.1,
                        "w": 0.2,
                        "h": 0.2,
                    }
                row.result = value
        elif change == "metadata":
            meta = await db.scalar(
                select(PredictionMeta).where(
                    PredictionMeta.prediction_id == prediction.id
                )
            )
            extra = deepcopy(meta.extra)
            extra["mask_repair"]["source_version"] += 1
            meta.extra = extra
        elif change == "ledger":
            row = await db.get(MaskRepairBatch, w.batch_id)
            saved = deepcopy(row.result_json)
            saved["candidates"]["0"].pop("candidate_records")
            row.result_json = saved
        elif change == "gate":
            (
                await db.get(Project, w.project_id)
            ).raster_mask_native_editing_enabled = False
        elif change == "original":
            (w.content[0] / w.content[7].relative_path).write_bytes(b"changed")
        else:
            from app.services.storage import storage_service
            from app.db.models.raster_mask_object import RasterMaskObject

            owner = await db.scalar(
                select(RasterMaskObject).where(
                    RasterMaskObject.logical_key
                    == prediction.result[0]["geometry"]["mask"]["object_key"],
                    RasterMaskObject.state == "ready",
                )
            )
            bucket, key = owner.storage_ref["bucket"], owner.storage_ref["object_key"]
            body = storage_service.client.get_object(Bucket=bucket, Key=key)["Body"]
            try:
                deleted_object = (bucket, key, body.read())
            finally:
                body.close()
            storage_service.client.delete_object(Bucket=bucket, Key=key)
        await db.commit()
    try:
        response = await repair_client.post(
            f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}/accept"
        )
    finally:
        if deleted_object is not None:
            bucket, key, content = deleted_object
            storage_service.client.put_object(Bucket=bucket, Key=key, Body=content)
    assert response.status_code == 409, response.text
    async with w.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == w.task_id)
            )
            == 1
        )


async def test_reference_candidate_rejects_offline_then_cannot_accept(
    reference_repair, repair_client, monkeypatch
):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)
    await forward(w)
    batch, _, _, predictions = await state(w)
    assert batch.status == "completed", batch.result_json
    (w.content[0] / w.content[7].relative_path).unlink()
    async with w.factory() as db:
        (await db.get(DatasetSource, w.content[5].id)).enabled = False
        (await db.get(Project, w.project_id)).raster_mask_native_editing_enabled = False
        await db.commit()
    prediction_id = predictions[0].id
    load = AsyncMock(return_value=w.source)
    monkeypatch.setattr("app.api.v1.tasks.predictions.load_coco_rle", load)
    content_url = (
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction_id}/mask-content/0"
    )
    readable = await repair_client.get(content_url)
    assert readable.status_code == 200, readable.text
    load.assert_awaited_once()
    blocked_accept = await repair_client.post(
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction_id}/accept"
    )
    assert blocked_accept.status_code in {404, 409}, blocked_accept.text
    rejected = await repair_client.post(
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction_id}/reject"
    )
    assert rejected.status_code == 204, rejected.text
    hidden = await repair_client.get(content_url, headers={"If-None-Match": "*"})
    assert hidden.status_code == 404
    async with w.factory() as db:
        prediction = await db.scalar(
            select(Prediction).where(Prediction.id == prediction_id)
        )
        assert prediction.rejected_shape_indexes == [0]
        (await db.get(DatasetSource, w.content[5].id)).enabled = True
        (await db.get(Project, w.project_id)).raster_mask_native_editing_enabled = True
        await db.commit()
    (w.content[0] / w.content[7].relative_path).write_bytes(w.content[1])
    response = await repair_client.post(
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction_id}/accept"
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "repair_candidate_rejected"


@pytest.mark.parametrize("change", ["revoked", "scope", "during"])
async def test_original_key_revalidated_and_resume_uses_fresh_key(
    reference_repair, repair_client, change
):
    w = reference_repair

    async def new_key():
        response = await repair_client.post(
            "/api/v1/me/api-keys",
            json={
                "name": "repair-test",
                "scopes": ["tasks:read", "annotations:read", "annotations:write"],
            },
        )
        assert response.status_code == 201, response.text
        return uuid.UUID(response.json()["id"])

    key_id = await new_key()
    w.request.state.api_key_id = key_id
    await plan(w, sam=True)
    await execute(w)

    async def revoke():
        async with w.factory() as db:
            key = await db.get(ApiKey, key_id)
            if change == "scope":
                key.scopes = ["tasks:read", "annotations:write"]
            else:
                key.revoked_at = datetime.now(timezone.utc)
            await db.commit()

    if change == "during":
        w.during = revoke
    else:
        await revoke()
    await forward(w)
    batch, job, _, predictions = await state(w)
    assert (batch.status, job.status, len(predictions)) == ("failed", "failed", 0), (
        batch.result_json
    )
    w.during = None
    w.request.state.api_key_id = await new_key()
    async with w.factory() as db:
        batch = await resume_repair_batch(
            db,
            batch_id=w.batch_id,
            actor=await db.get(User, w.actor_id),
            http_request=w.request,
        )
        w.job_id = batch.async_job_id
        await record_repair_dispatch_task(
            db,
            batch_id=w.batch_id,
            async_job_id=w.job_id,
            rollback=False,
            celery_task_id=str(w.job_id),
        )
        await db.commit()
    await forward(w)
    batch, job, _, predictions = await state(w)
    assert (batch.status, job.status, len(predictions)) == (
        "completed",
        "completed",
        1,
    ), batch.result_json


async def test_concurrent_sam_acceptance_creates_one_annotation(
    reference_repair, repair_client
):
    w = reference_repair
    await plan(w, sam=True)
    await execute(w)
    await forward(w)
    batch, _, _, predictions = await state(w)
    assert batch.status == "completed", batch.result_json
    url = f"/api/v1/tasks/{w.task_id}/predictions/{predictions[0].id}/accept"
    responses = await asyncio.wait_for(
        asyncio.gather(repair_client.post(url), repair_client.post(url)), 15
    )
    assert sorted(row.status_code for row in responses) == [200, 409], [
        row.text for row in responses
    ]
    async with w.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(
                    Annotation.task_id == w.task_id,
                    Annotation.parent_prediction_id == predictions[0].id,
                )
            )
            == 1
        )


async def test_reference_repair_api_captures_actual_request_key(
    reference_repair, repair_client, monkeypatch
):
    w = reference_repair
    created = await repair_client.post(
        "/api/v1/me/api-keys",
        json={
            "name": "repair-api",
            "scopes": ["tasks:read", "annotations:read", "annotations:write"],
        },
    )
    assert created.status_code == 201, created.text
    key = created.json()
    auth = {"Authorization": "Bearer " + key["plaintext"]}
    base = f"/api/v1/projects/{w.project_id}/mask-qc/repairs"
    dry = await repair_client.post(
        base + ":dry-run",
        json={
            "actions": [{"issue_id": str(w.issue_id), "kind": "delete_small_islands"}]
        },
        headers=auth,
    )
    assert dry.status_code == 200, dry.text

    async def dispatch(batch_id, *, dispatcher, execution_id, rollback=False):
        return execution_id

    monkeypatch.setattr("app.api.v1.mask_qc.dispatch_repair_batch", dispatch)
    executed = await repair_client.post(
        base,
        json={
            "receipt": dry.json()["receipt"],
            "plan_digest": dry.json()["plan_digest"],
        },
        headers=auth,
    )
    assert executed.status_code == 202, executed.text
    w.batch_id, w.job_id = (
        uuid.UUID(executed.json()["id"]),
        uuid.UUID(executed.json()["async_job_id"]),
    )
    async with w.factory() as db:
        job = await db.get(AsyncJob, w.job_id)
        assert job.payload["repair_authority"]["key_id"] == key["id"]
        assert "repair_authority" not in str(
            AsyncJobOut.model_validate(job).model_dump()
        )
    await forward(w)
    batch, job, annotation, _ = await state(w)
    assert (batch.status, job.status, annotation.version) == (
        "completed",
        "completed",
        2,
    ), batch.result_json
