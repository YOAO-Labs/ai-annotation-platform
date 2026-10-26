from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kombu.utils.json import dumps, loads
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_video_inputs import execution as video_execution
from aap_assets_video_inputs import values as video_values
from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
)
from app.services.prediction import PredictionService
from app.workers import frame_preannotate
from tests.test_video_input_ledger_lifecycle import (
    _frame_claim,
    _publish_frame,
    _ready_plan,
)


from aap_inference_contract.execution import (
    ExecutionProvenance,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
    thaw_json,
)


def _install_frame_execution_fake(
    monkeypatch,
    predict,
    *,
    actual_instance_id=None,
):
    class FakePort:
        async def execute(self, operation):
            inputs = tuple(thaw_json(item) for item in operation.task_inputs)
            context = thaw_json(operation.context)
            responses = await predict(inputs, context)
            instance_id = actual_instance_id or operation.requested_registry_id
            return ModelOperationResult(
                request_id=operation.request_id,
                operation=operation.operation,
                results=tuple(
                    ModelTaskResult(
                        task_id=getattr(response, "task_id", inputs[index]["id"]),
                        result=response.result,
                        score=response.score,
                        model_version=response.model_version,
                        inference_time_ms=response.inference_time_ms,
                        meta=response.meta,
                    )
                    for index, response in enumerate(responses)
                ),
                provenance=ExecutionProvenance(
                    requested_registry_id=operation.requested_registry_id,
                    requested_pool_id=operation.requested_pool_id,
                    registry_id=instance_id,
                    pool_id=operation.requested_pool_id,
                    instance_id=instance_id,
                    route_scope_id=uuid.uuid4(),
                    attempt_id=operation.request_id,
                ),
                observation=ModelExecutionObservation(
                    backend_id=str(instance_id),
                    operation="predict",
                    outcome="success",
                    elapsed_seconds=0.0,
                ),
                wall_time_ms=0,
            )

    monkeypatch.setattr(
        "app.services.inference_model_execution.model_execution_port",
        lambda *_args, **_kwargs: FakePort(),
    )


@pytest.fixture
async def frame_world(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        owner_id, project_id, plan = await _ready_plan(db, count=3)
        input_record = video_execution.VideoInputRecord(
            input_id=plan.input.input_id,
            job_id=plan.input.job_id,
            actor_id=plan.input.actor_id,
            task=plan.input.task,
            source=plan.input.source,
            sampling=plan.sampling,
            status="ready",
            plan=plan,
            error=None,
            created_at=plan.input.created_at,
            retire_after=None,
        )
        entries = tuple(
            video_values.FrameTimeEntry(
                ordinal=ordinal,
                frame_index=ordinal - 1,
                pts_ms=(ordinal - 1) * 10,
                is_keyframe=ordinal == 1,
            )
            for ordinal in range(1, 4)
        )
        frames = {}
        references = {}
        for entry in entries:
            request = video_execution.VideoFrameRequest(
                actor_id=owner_id,
                job_id=plan.input.job_id,
                task_id=plan.input.task.task_id,
                input_id=plan.input.input_id,
                plan_digest=plan.plan_digest,
                ordinal=entry.ordinal,
            )
            pending = await db.scalar(
                text(
                    "SELECT aap_assets_v1.require_accepted_video_frame("
                    "CAST(:actor AS uuid),CAST(:job AS uuid),CAST(:task AS uuid),"
                    "CAST(:input AS uuid),CAST(:plan AS text),CAST(:ordinal AS bigint))"
                ),
                {
                    "actor": owner_id,
                    "job": plan.input.job_id,
                    "task": plan.input.task.task_id,
                    "input": plan.input.input_id,
                    "plan": plan.plan_digest,
                    "ordinal": entry.ordinal,
                },
            )
            frame = video_execution.parse_native(
                "require_accepted_video_frame",
                request,
                pending,
            )
            ready = await _publish_frame(db, await _frame_claim(db, frame))
            frames[entry.ordinal] = ready
            assert ready.blob is not None
            references[entry.ordinal] = video_values.FrameReference(
                plan=plan,
                entry=entry,
                blob=ready.blob,
                url=(
                    f"https://objects.test/{ready.blob.key}"
                    f"?versionId={ready.blob.version_id}"
                ),
                expires_in=300,
            )

        class RetainedVideoGateway:
            def __init__(self):
                self.reference_calls = 0

            async def input(self, _request):
                return input_record

            async def verify(self, _request):
                return input_record

            async def times(self, request):
                page_entries = entries[
                    request.cursor : request.cursor + request.page_size
                ]
                end = request.cursor + len(page_entries)
                return video_values.FrameTimePage(
                    plan=plan,
                    cursor=request.cursor,
                    next_cursor=end if end < plan.selected_count else None,
                    entries=page_entries,
                )

            async def ensure_reference(self, request):
                self.reference_calls += 1
                return references[request.ordinal]

            async def frame(self, request):
                return frames[request.ordinal]

        backend_suffix = uuid.uuid4().hex
        backend = MLBackendRegistry(
            name=f"frame-test-{backend_suffix[:8]}",
            url=f"http://frame-{backend_suffix}.test",
        )
        db.add(backend)
        job = await db.get(AsyncJob, plan.input.job_id)
        manifest = (
            (
                await db.execute(
                    text(
                        "SELECT selection,selection_digest,sealed_digest,candidate_count,"
                        "selected_count FROM public.asset_accepted_video_manifests "
                        "WHERE job_id=:job"
                    ),
                    {"job": plan.input.job_id},
                )
            )
            .mappings()
            .one()
        )
        selection = manifest["selection"]
        job.payload = {
            **(job.payload or {}),
            "batch_id": selection["batch_id"],
            "execution_scope": selection["execution_scope"],
            "accepted_video_inputs_protocol": "aap-assets-video-inputs/v1",
            "accepted_video_manifest_id": str(job.id),
            "accepted_video_selection_digest": manifest["selection_digest"],
            "accepted_video_selection": selection,
            "execution_unit": "frame",
        }
        job.status = "running"
        job.result = {
            **(job.result or {}),
            "execution_unit": "frame",
            "frame_total": plan.selected_count,
            "video_manifest": {
                "protocol": "aap-assets-video-inputs/v1",
                "manifest_id": str(job.id),
                "selection_digest": manifest["selection_digest"],
                "sealed_digest": manifest["sealed_digest"],
                "selected_count": manifest["selected_count"],
                "candidate_count": manifest["candidate_count"],
            },
        }
        await db.commit()
        world = SimpleNamespace(
            maker=maker,
            owner_id=owner_id,
            actor_id=owner_id,
            dataset_id=plan.input.source.dataset_id,
            item_id=plan.input.source.dataset_item_id,
            project_id=project_id,
            task_id=plan.input.task.task_id,
            backend_id=backend.id,
            job_id=job.id,
            input_id=plan.input.input_id,
            plan=plan,
            entries=entries,
            frames=frames,
            references=references,
            gateway=RetainedVideoGateway(),
            selection_digest=manifest["selection_digest"],
            sealed_digest=manifest["sealed_digest"],
        )
    try:
        yield world
    finally:
        async with maker() as db:
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
            failed_ids = select(FailedPrediction.id).where(
                FailedPrediction.task_id == world.task_id
            )
            await db.execute(
                delete(PredictionMeta).where(
                    PredictionMeta.failed_prediction_id.in_(failed_ids)
                )
            )
            await db.execute(
                delete(FailedPrediction).where(
                    FailedPrediction.task_id == world.task_id
                )
            )
            # Accepted video receipts are immutable audit records. Keep them
            # until the disposable test database is destroyed.
            await db.execute(delete(AsyncJob).where(AsyncJob.id == world.job_id))
            await db.execute(delete(Task).where(Task.id == world.task_id))
            await db.execute(delete(DatasetItem).where(DatasetItem.id == world.item_id))
            await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
            await db.execute(delete(Project).where(Project.id == world.project_id))
            await db.execute(delete(User).where(User.id == world.owner_id))
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id == world.backend_id
                )
            )
            await db.commit()


def _install_video_gateway(monkeypatch, world, *, gateway=None):
    @asynccontextmanager
    async def context(*, session_factory=None):
        yield gateway or world.gateway

    monkeypatch.setattr(
        "app.services.accepted_video_inputs.video_input_gateway", context
    )


def _video_segment(world, ordinals):
    return {
        "task_id": str(world.task_id),
        "input_id": str(world.input_id),
        "input_job_id": str(world.job_id),
        "plan_digest": world.plan.plan_digest,
        "frame_ordinals": ordinals,
    }


async def _run_video_segment(world, ordinals, *, resume=None):
    return await frame_preannotate._run_segment(
        segment=_video_segment(world, ordinals),
        project_id=str(world.project_id),
        ml_backend_id=str(world.backend_id),
        stage0={},
        actor_id=str(world.actor_id),
        job_id=str(world.job_id),
        accepted_video_manifest_id=str(world.job_id),
        accepted_video_selection_digest=world.selection_digest,
        accepted_video_sealed_digest=world.sealed_digest,
        celery_root_task_id=None,
        resume=resume,
    )


@pytest.mark.parametrize(
    "change",
    [
        None,
        "task_status",
        "job_terminal",
        "account_disabled",
        "still_busy",
        "source_path",
        "resumed_source_path",
    ],
)
async def test_frame_persistence_retries_contention_and_rechecks_scope(
    frame_world, test_db_url, monkeypatch, change
):
    world = frame_world
    raw = [
        {
            "type": "rectanglelabels",
            "value": {
                "x": 10,
                "y": 20,
                "width": 30,
                "height": 40,
                "rectanglelabels": ["car"],
            },
        }
    ]
    predict = AsyncMock(
        return_value=[
            SimpleNamespace(
                result=raw,
                score=0.9,
                model_version="model",
                inference_time_ms=12,
                meta={"total_tokens": 7},
            )
        ]
    )
    monkeypatch.setattr(settings, "database_url", test_db_url)
    _install_frame_execution_fake(
        monkeypatch, predict, actual_instance_id=world.backend_id
    )
    _install_video_gateway(monkeypatch, world)
    monkeypatch.setattr(frame_preannotate, "_bump_progress", lambda *_a: (1, 1))
    monkeypatch.setattr(
        frame_preannotate, "_publish_frame_progress", lambda *_a, **_k: None
    )
    monkeypatch.setattr(frame_preannotate, "_write_job_progress", AsyncMock())
    contended = asyncio.Event()
    failures = []
    apply_result = PredictionService.apply_prediction_result

    async def observed_apply(self, request):
        try:
            return await apply_result(self, request)
        except ApplyPredictionResultError as exc:
            failures.append(exc)
            if exc.code == "task_busy":
                contended.set()
            raise

    async with world.maker() as writer:
        task = await writer.get(Task, world.task_id)
        scope = capture_labeling_write_scope(
            task,
            actor_id=world.owner_id,
            actor_known=True,
            operation="preannotation",
            execution_scope="bulk",
            execution_scope_known=True,
            job_id=world.job_id,
            job_id_known=True,
        )

    monkeypatch.setattr(PredictionService, "apply_prediction_result", observed_apply)
    async with world.maker() as locker:
        # Hold only the Task row. The baseline prediction and its authority
        # locks were committed above, so the contention reaches the intended
        # task-write NOWAIT fence instead of an earlier scope lock.
        task = await locker.scalar(
            select(Task).where(Task.id == world.task_id).with_for_update()
        )
        runner = asyncio.create_task(_run_video_segment(world, [1]))
        try:
            await asyncio.wait_for(contended.wait(), timeout=5)
            if change == "task_status":
                task.status = "review"
            elif change == "job_terminal":
                job = await locker.get(AsyncJob, world.job_id)
                job.status = "cancelled"
            elif change == "account_disabled":
                owner = await locker.get(User, world.owner_id)
                owner.is_active = False
            elif change == "source_path":
                task.file_path = "replacement/video.mp4"
            if change in {
                "still_busy",
                "task_status",
                "job_terminal",
                "source_path",
                "resumed_source_path",
            }:
                with pytest.raises(frame_preannotate._FrameWriteDeferred) as deferred:
                    await asyncio.wait_for(runner, timeout=10)
                resume = loads(dumps(deferred.value.resume))
                assert resume["stats"]["failed"] == 0
                assert resume["pending_write"]["ml_backend_id"] == world.backend_id
                assert resume["write_scope"]["task_version"] == scope.task_version
                assert (
                    resume["write_scope"]["task_source_generation"]
                    == scope.task_source_generation
                )
                assert resume["write_scope"]["source_dataset_item_id"] == world.item_id
                assert (
                    resume["write_scope"]["source_file_path"] == scope.source_file_path
                )
                if change == "resumed_source_path":
                    task.file_path = "replacement/video.mp4"
                await locker.commit()
                before_reference = world.gateway.reference_calls
                try:
                    stats = await _run_video_segment(world, [1], resume=resume)
                except frame_preannotate._FrameWriteDeferred as failure_deferred:
                    assert failure_deferred.resume["pending_failure"] is not None
                    stats = await _run_video_segment(
                        world, [1], resume=failure_deferred.resume
                    )
                assert world.gateway.reference_calls == before_reference
            else:
                await locker.commit()
                stats = await asyncio.wait_for(runner, timeout=10)
        finally:
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    succeeded = change in {None, "still_busy"}
    assert stats["frames_done"] == int(succeeded)
    assert stats["boxes"] == int(succeeded)
    assert stats["failed"] == 0
    predict.assert_awaited_once()
    assert all(exc.detail == {"reason": "task_busy"} for exc in failures[:-1])
    if change == "still_busy":
        assert len(failures) == frame_preannotate._WRITE_ATTEMPTS
    elif change in {"task_status", "source_path", "resumed_source_path"}:
        assert failures[-1].detail in (
            {"reason": "prediction_task_scope_changed"},
            {"reason": "prediction_source_changed"},
        )
    elif change == "job_terminal":
        assert failures[-1].detail in (
            {"reason": "prediction_execution_not_writable"},
            {"reason": "task_busy"},
        )
    elif change == "account_disabled":
        assert failures[-1].status_code == 401
    async with world.maker() as db:
        rows = list(
            (
                await db.scalars(
                    select(Prediction).where(Prediction.task_id == world.task_id)
                )
            ).all()
        )
        assert len(rows) == int(succeeded)
        task = await db.get(Task, world.task_id)
        assert task.total_predictions == len(rows)
        if change == "job_terminal":
            assert (await db.get(AsyncJob, world.job_id)).status == "cancelled"
        assert await db.scalar(
            select(func.count())
            .select_from(PredictionMeta)
            .where(PredictionMeta.prediction_id.in_([row.id for row in rows]))
        ) == len(rows)
        if succeeded:
            retried = next(
                row for row in rows if row.result[0]["geometry"]["frame_index"] == 0
            )
            assert retried.ml_backend_id == world.backend_id
            assert retried.score == 0.9 and retried.model_version == "model"
            meta = await db.scalar(
                select(PredictionMeta).where(PredictionMeta.prediction_id == retried.id)
            )
            assert meta.inference_time_ms == 12 and meta.total_tokens == 7


async def test_media_owner_busy_defers_before_prediction(
    frame_world, test_db_url, monkeypatch
):
    from aap_assets_video_inputs.execution import VideoInputFailure

    world = frame_world
    predict = AsyncMock()
    monkeypatch.setattr(settings, "database_url", test_db_url)
    _install_frame_execution_fake(
        monkeypatch, predict, actual_instance_id=world.backend_id
    )
    gateway = world.gateway
    gateway.ensure_reference = AsyncMock(side_effect=VideoInputFailure("input_busy"))
    _install_video_gateway(monkeypatch, world, gateway=gateway)

    with pytest.raises(frame_preannotate._FrameWriteDeferred) as deferred:
        await _run_video_segment(world, [1])

    resume = deferred.value.resume
    assert resume["frame_ordinals"] == [1]
    assert resume["pending_write"] is None
    assert resume["stats"]["failed"] == 0
    predict.assert_not_awaited()
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        assert job.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["mask_content_deleting", "mask_content_epoch_changed"]
)
async def test_mask_content_write_contention_defers_accepted_frame_result(
    frame_world, test_db_url, monkeypatch, code
):
    world = frame_world
    raw = [
        {
            "type": "rectanglelabels",
            "value": {
                "x": 10,
                "y": 20,
                "width": 30,
                "height": 40,
                "rectanglelabels": ["car"],
            },
        }
    ]
    predict = AsyncMock(
        return_value=[
            SimpleNamespace(
                result=raw,
                score=0.9,
                model_version="model",
                inference_time_ms=12,
                meta={"total_tokens": 7},
            )
        ]
    )
    monkeypatch.setattr(settings, "database_url", test_db_url)
    _install_frame_execution_fake(
        monkeypatch, predict, actual_instance_id=world.backend_id
    )
    _install_video_gateway(monkeypatch, world)
    monkeypatch.setattr(frame_preannotate, "_bump_progress", lambda *_a: (1, 1))
    monkeypatch.setattr(
        frame_preannotate, "_publish_frame_progress", lambda *_a, **_k: None
    )

    async def retain_segment_stats(
        _job_id, _done_delta, _project_id, *, segment_id, segment_stats
    ):
        assert segment_id
        return segment_stats

    monkeypatch.setattr(frame_preannotate, "_write_job_progress", retain_segment_stats)
    apply_result = PredictionService.apply_prediction_result
    write_calls = []

    async def contend_then_apply(self, request):
        write_calls.append(request)
        if len(write_calls) <= frame_preannotate._WRITE_ATTEMPTS:
            raise ApplyPredictionResultError(
                code=code,
                status_code=409,
                detail={"reason": code},
            )
        return await apply_result(self, request)

    monkeypatch.setattr(
        PredictionService, "apply_prediction_result", contend_then_apply
    )

    with pytest.raises(frame_preannotate._FrameWriteDeferred) as deferred:
        await _run_video_segment(world, [1])

    resume = loads(dumps(deferred.value.resume))
    assert len(write_calls) == frame_preannotate._WRITE_ATTEMPTS
    assert resume["pending_write"]["result"]
    assert resume["pending_write"]["ml_backend_id"] == world.backend_id
    assert resume["video_evidence"]["entry"]["ordinal"] == 1
    assert resume["write_scope"]["task_id"] == world.task_id
    predict.assert_awaited_once()

    stats = await _run_video_segment(world, [1], resume=resume)

    assert len(write_calls) == frame_preannotate._WRITE_ATTEMPTS + 1
    assert stats["frames_done"] == 1
    assert stats["failed"] == 0
    predict.assert_awaited_once()
    async with world.maker() as db:
        rows = list(
            (
                await db.scalars(
                    select(Prediction).where(Prediction.task_id == world.task_id)
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].result[0]["geometry"]["frame_index"] == 0


@pytest.mark.parametrize("status", ["cancelled", "failed", "completed"])
async def test_late_frame_progress_and_finalize_preserve_terminal_job(
    frame_world, test_db_url, monkeypatch, status
):
    world = frame_world
    monkeypatch.setattr(settings, "database_url", test_db_url)
    publish = Mock()
    progress = Mock()
    monkeypatch.setattr(frame_preannotate, "_publish_frame_progress", publish)
    monkeypatch.setattr(frame_preannotate, "_bump_progress", progress)
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        job.status = status
        job.progress_pct = 42
        job.result = {"retained": True}
        await db.commit()
    await frame_preannotate._finish_segment(
        {"frames_done": 9, "skipped": 0, "failed": 0},
        [],
        project_id=str(world.project_id),
        job_id=str(world.job_id),
        segment_id="segment",
    )
    await frame_preannotate._finalize(
        [{"frames_done": 9, "boxes": 9}],
        project_id=str(world.project_id),
        job_id=str(world.job_id),
    )
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        assert job.status == status
        assert job.progress_pct == 42
        assert job.result == {"retained": True}
    publish.assert_not_called()
    progress.assert_not_called()


@pytest.mark.parametrize("missing", ["task", "backend"])
async def test_missing_frame_resources_are_counted_as_failures(
    frame_world, test_db_url, monkeypatch, missing
):
    world = frame_world
    monkeypatch.setattr(settings, "database_url", test_db_url)
    port_factory = Mock(
        side_effect=AssertionError("No inference for missing resources")
    )
    monkeypatch.setattr(
        "app.services.inference_model_execution.model_execution_port", port_factory
    )
    _install_video_gateway(monkeypatch, world)
    missing_task = str(uuid.uuid4()) if missing == "task" else str(world.task_id)
    stats = await frame_preannotate._run_segment(
        segment={
            **_video_segment(world, [1, 2]),
            "task_id": missing_task,
        },
        project_id=str(world.project_id),
        ml_backend_id=str(uuid.uuid4() if missing == "backend" else world.backend_id),
        stage0={},
        actor_id=str(world.actor_id),
        job_id=str(world.job_id),
        accepted_video_manifest_id=str(world.job_id),
        accepted_video_selection_digest=world.selection_digest,
        accepted_video_sealed_digest=world.sealed_digest,
        celery_root_task_id=None,
    )
    assert stats["failed"] == 0 and stats["frames_done"] == 0
    port_factory.assert_not_called()
    async with world.maker() as db:
        assert (await db.get(AsyncJob, world.job_id)).status == "failed"
