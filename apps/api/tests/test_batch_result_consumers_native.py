"""Actual Labeling consumer commits and handoff against disposable native state."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.durable_preannotation import _apply_ready_result, _overwrite
from app.application.preannotation_contract import BatchPredictionRequest
from app.application.preannotation_results import (
    BatchResultExecution,
    PreannotationResultDeferred,
    preannotation_request_digest,
    scope_json,
)
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.task import Task
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.prediction import PredictionService
from tests.conftest import create_registry_with_pool
from tests.factory import create_project, create_task
from tests.test_assets_catalog_native import _seed_actor


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


@asynccontextmanager
async def _world(
    test_engine,
    *,
    result_count=2,
    predict_mode="append",
    masks=False,
    staged=True,
    task_count=1,
):
    urls = {}
    for role, key in (
        ("legacy_runtime", "AAP_MODULE_DATA_LEGACY_RUNTIME_URL"),
        ("preannotation_result_client", "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"),
    ):
        raw = os.environ.get(key)
        if not raw:
            pytest.fail("module-data verifier omitted the required result role")
        value = make_url(raw)
        assert value.username == role
        assert value.database == test_engine.url.database
        assert value.database.endswith("_test") and value.host == "127.0.0.1"
        urls[role] = value
    owner_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner_factory() as owner:
        actor_id = await _seed_actor(owner)
        project = await create_project(owner, owner_id=actor_id, type_key="text")
        project.ai_enabled = True
        tasks = [
            await create_task(owner, project_id=project.id) for _ in range(task_count)
        ]
        task = tasks[0]
        if masks:
            from app.db.models.dataset import Dataset, DatasetItem

            suffix = uuid4().hex[:12]
            dataset = Dataset(
                display_id=f"BRD-{suffix}",
                name="Batch result Masks",
                data_type="image",
                created_by=actor_id,
            )
            owner.add(dataset)
            await owner.flush()
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="source.png",
                file_path=f"batch-result/{suffix}.png",
                file_type="image",
                width=2048,
                height=2048,
            )
            owner.add(item)
            await owner.flush()
            task.dataset_item_id = item.id
            task.file_path = item.file_path
            project.raster_mask_native_editing_enabled = True
            project.tool_bindings = {
                "region": {
                    "enabled": True,
                    "classes": [{"name": "object"}],
                    "attribute_schema": {"fields": []},
                }
            }
        else:
            for selected in tasks:
                selected.file_type = "text"
        await owner.flush()
        await owner.refresh(task)
        backend, pool = await create_registry_with_pool(
            owner,
            name=f"batch-result-{uuid4().hex}",
            url=f"http://batch-{uuid4().hex}.example.test/",
            state="connected",
        )
        job_id = uuid4()
        request = BatchPredictionRequest(
            project_id=str(project.id),
            ml_backend_id=str(backend.id),
            user_id=str(actor_id),
            task_ids=[str(selected.id) for selected in tasks],
            predict_mode=predict_mode,
            async_job_id=str(job_id),
            task_type="text",
            prompt="Classify the supplied text.",
        )
        owner.add(
            AsyncJob(
                id=job_id,
                kind="batch_predict",
                project_id=project.id,
                user_id=actor_id,
                status="pending",
                celery_task_id=str(job_id),
                result={},
                payload={
                    "batch_id": None,
                    "execution_scope": "bulk",
                    "predict_mode": predict_mode,
                    "task_ids": [str(selected.id) for selected in tasks],
                    "scope": {"task_ids": [str(selected.id) for selected in tasks]},
                    "preannotation_request_digest": preannotation_request_digest(
                        request
                    ),
                },
            )
        )
        scope = capture_labeling_write_scope(
            task,
            actor_id=actor_id,
            actor_known=True,
            operation="preannotation",
            execution_scope="bulk",
            execution_scope_known=True,
            job_id=job_id,
            job_id_known=True,
        )
        await owner.commit()
        requests = tuple(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project.id,
                ml_backend_id=backend.id,
                ml_backend_pool_id=pool.id,
                write_scope=scope,
                result=[{"type": "choices", "value": {"choices": [f"class-{index}"]}}],
                score=0.75,
                model_version="test-model",
                inference_time_ms=17,
                token_meta={"total_tokens": 13, "total_cost": 0.75},
                pipeline_extra={"test": "durable-multi-result"},
            )
            for index in range(result_count)
        )
    engines = {role: create_async_engine(url) for role, url in urls.items()}
    writers = async_sessionmaker(engines["legacy_runtime"], expire_on_commit=False)
    executors = async_sessionmaker(
        engines["preannotation_result_client"], expire_on_commit=False
    )
    execution = BatchResultExecution(
        request, writers, execution_id=str(job_id), executor_session_factory=executors
    )
    try:
        if staged:
            await execution.claim()
            await execution.call(
                "stage_batch_result_members",
                after_ordinal=0,
                complete=True,
                members=[
                    {
                        "ordinal": 1,
                        "task_id": task.id,
                        "write_scope": scope_json(scope),
                        "manifest_member": None,
                    }
                ],
            )
            await execution.call("start_batch_result_execution")
            await execution.call("start_batch_result_member", ordinal=1)
        yield SimpleNamespace(
            execution=execution,
            writers=writers,
            executors=executors,
            owner_factory=owner_factory,
            request=request,
            requests=requests,
            task_id=task.id,
            project_id=project.id,
            task_ids=[selected.id for selected in tasks],
            actor_id=actor_id,
            backend_id=backend.id,
            pool_id=pool.id,
        )
    finally:
        await execution.close()
        for engine in engines.values():
            await engine.dispose()


async def test_one_large_mask_response_exceeds_16mib_and_transfers_pending_roots(
    test_engine, monkeypatch
):
    from dataclasses import replace
    from app.application.raster_mask_retention import referenced_raster_mask_keys
    from app.config import settings
    from app.services.batch_result_masks import normalize_batch_prediction_masks
    from app.services.raster_mask_storage import (
        canonical_rle_bytes,
        collect_mask_references,
        validate_coco_rle,
    )

    monkeypatch.setattr(settings, "assets_mask_content_url", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    async with _world(test_engine, result_count=1, masks=True) as world:
        shapes = []
        for index in range(11):
            counts = [index + 1] + [1] * 800_000
            counts.append(2048 * 2048 - sum(counts))
            rle = {"encoding": "coco_rle", "size": [2048, 2048], "counts": counts}
            validate_coco_rle(rle)
            assert len(canonical_rle_bytes(rle)) < 4 * 1024 * 1024
            shapes.append(
                {
                    "type": "raster_mask",
                    "class_name": "object",
                    "confidence": 0.9,
                    "geometry": {"type": "raster_mask", "mask": rle},
                    "tool_unit_id": "region",
                    "attributes": {},
                }
            )
        world.requests = (replace(world.requests[0], result=shapes),)
        context = {
            "task_id": str(world.task_id),
            "total_cost": "0.75",
            "pipeline_stages": {},
            "gpu_arbiter_failures": [],
        }
        await world.execution.capture(1, world.requests, stage="raw", context=context)
        metadata = await world.execution.read_result(1)
        assert metadata.result.raw_complete
        assert metadata.result.result_count == 1
        assert metadata.payload.byte_size > 16 * 1024 * 1024
        assert metadata.payload.part_count > 32
        _raw, requests = await world.execution.load(1, stage="raw")
        async with world.writers() as db:
            prepared = await normalize_batch_prediction_masks(db, requests)
            await db.commit()
        keys = {
            reference["object_key"]
            for reference in collect_mask_references(prepared[0].result)
        }
        # These are valid newly uploaded bytes after an earlier GC epoch. A
        # snapshot must verify the current bytes and obtain a fresh permit.
        async with world.owner_factory() as owner:
            await owner.execute(
                text(
                    "UPDATE mask_gc_key_epochs SET epoch=epoch+1 WHERE object_key=ANY(:keys)"
                ),
                {"keys": list(keys)},
            )
            await owner.commit()
        await world.execution.capture(1, prepared, stage="prepared", context=context)
        keys = {
            reference["object_key"]
            for reference in collect_mask_references(prepared[0].result)
        }
        assert len(keys) == 11
        async with world.owner_factory() as owner:
            assert keys <= await referenced_raster_mask_keys(owner)
            pending = set(
                (
                    await owner.execute(
                        text(
                            "SELECT object_key FROM mask_gc_roots WHERE root_kind='batch_result_payloads' AND object_key=ANY(:keys)"
                        ),
                        {"keys": list(keys)},
                    )
                ).scalars()
            )
            assert pending == keys
        result, requests = await world.execution.load(1, stage="prepared")
        member = [item async for item in world.execution.members()][0]
        async with world.writers() as db:
            await _apply_ready_result(
                world.execution,
                db,
                PredictionService(db),
                member,
                result,
                requests,
                manifest_backed=False,
            )
        assert await _counts(world) == (1, 1, 0, 1)
        async with world.owner_factory() as owner:
            assert keys <= await referenced_raster_mask_keys(owner)
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM mask_gc_roots WHERE root_kind='batch_result_payloads' AND object_key=ANY(:keys)"
                    ),
                    {"keys": list(keys)},
                )
                == 0
            )


async def _ready(world):
    context = {
        "task_id": str(world.task_id),
        "total_cost": str(0.75 * len(world.requests)),
        "pipeline_stages": {"0": {"detected": len(world.requests)}},
        "gpu_arbiter_failures": [],
    }
    await world.execution.capture(1, world.requests, stage="raw", context=context)
    await world.execution.capture(1, world.requests, stage="prepared", context=context)
    result, requests = await world.execution.load(1, stage="prepared")
    member = [item async for item in world.execution.members()][0]
    return member, result, requests


async def _counts(world):
    async with world.owner_factory() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.task_id == world.task_id)
            ),
            await db.scalar(
                select(func.count())
                .select_from(PredictionMeta)
                .join(
                    Prediction,
                    (PredictionMeta.prediction_id == Prediction.id)
                    & (PredictionMeta.prediction_created_at == Prediction.created_at),
                )
                .where(Prediction.task_id == world.task_id)
            ),
            await db.scalar(
                select(func.count())
                .select_from(FailedPrediction)
                .where(FailedPrediction.task_id == world.task_id)
            ),
            (await db.get(Task, world.task_id)).total_predictions,
        )


async def test_actual_multi_result_write_transfers_receipts_counts_and_releases_bodies(
    test_engine,
):
    async with _world(test_engine) as world:
        member, result, requests = await _ready(world)
        ids = [request.batch_result_identity.prediction_id for request in requests]
        assert len(set(ids)) == 2
        async with world.writers() as db:
            await _apply_ready_result(
                world.execution,
                db,
                PredictionService(db),
                member,
                result,
                requests,
                manifest_backed=False,
            )
        assert await _counts(world) == (2, 2, 0, 2)
        first = await world.execution.read_result(1, stage="prepared")
        second = await world.execution.read_result(1, stage="prepared", request_index=1)
        assert first.result.state == "applied" and first.result.receipt_count == 2
        assert first.payload.receipt.id == ids[0]
        assert second.payload.receipt.id == ids[1]
        assert world.execution.progress.success_count == 1
        assert world.execution.progress.next_ordinal == 2
        async with world.owner_factory() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM batch_result_parts WHERE payload_id IN (SELECT payload_id FROM batch_result_payloads WHERE job_id=:job_id)"
                    ),
                    {"job_id": world.execution.scope["job_id"]},
                )
                == 0
            )
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM batch_result_payloads WHERE job_id=:job_id AND body IS NOT NULL"
                    ),
                    {"job_id": world.execution.scope["job_id"]},
                )
                == 0
            )


def _runtime_connections(monkeypatch, world):
    from app.config import settings

    writer = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    executor = make_url(os.environ["AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"])
    monkeypatch.setattr(
        settings, "database_url", writer.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(
        settings,
        "preannotation_results_database_url",
        executor.set(drivername="postgresql").render_as_string(hide_password=False),
    )
    monkeypatch.setattr(settings, "preannotation_results_database_url_file", "")


async def test_managed_image_authority_runs_only_after_native_selection_start(
    test_engine, monkeypatch
):
    from copy import deepcopy

    from app.application import preannotation_execution as execution
    from app.core.security import create_access_token
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.services import async_job as jobs, prediction_progress
    from app.services.content_snapshot import ContentError
    from app.services.dataset_assets import ensure_legacy_bindings
    from app.services.prediction_execution import create_snapshot
    from app.services.prediction_references import (
        ReferencePredictionJob,
        capture_authority,
    )
    from tests.test_managed_prediction_requests import managed_task

    async with _world(test_engine, staged=False) as world:
        _runtime_connections(monkeypatch, world)
        async with world.owner_factory() as owner:
            await ensure_legacy_bindings(owner)
            project = await owner.get(Project, world.project_id)
            project.data_type = "image"
            actor = await owner.get(User, world.actor_id)
            task = await owner.get(Task, world.task_id)
            task.file_type = "image"
            await managed_task(owner, project, actor, task=task)
            snapshot = await create_snapshot(
                owner,
                project.id,
                {
                    "stages": [
                        {
                            "stage": 0,
                            "parent_stage": None,
                            "ml_backend_id": str(world.backend_id),
                        }
                    ],
                    "prompt": world.request.prompt,
                    "output_mode": world.request.output_mode,
                    "execution_scope": "bulk",
                    "execution_unit": None,
                    "predict_mode": world.request.predict_mode,
                    "box_threshold": project.box_threshold,
                    "text_threshold": project.text_threshold,
                },
            )
            credential = SimpleNamespace(
                state=SimpleNamespace(api_key_id=None, api_key_scopes=None),
                headers={
                    "authorization": "Bearer "
                    + create_access_token(subject=str(actor.id), role=actor.role)
                },
            )
            proof = await capture_authority(
                owner, project, actor, credential, [task], snapshot,
                scope="bulk", batch_id=None, predict_mode=world.request.predict_mode,
            )
            job = await jobs.get_job(owner, world.execution.scope["job_id"])
            await jobs.bind_execution_snapshot(
                owner, job, actor_id=actor.id, snapshot_id=snapshot.id
            )
            job.payload = {**job.payload, "prediction_authority": proof}
            await owner.commit()
            snapshot_id, snapshot_digest = snapshot.id, snapshot.digest

        async with world.writers() as db:
            authority = ReferencePredictionJob(
                db, world.execution.scope["job_id"], proof,
                project_id=world.project_id, actor_id=world.actor_id,
                task_ids=[world.task_id], batch_id=None, scope="bulk",
                predict_mode=world.request.predict_mode, snapshot_id=snapshot_id,
                snapshot_digest=snapshot_digest, node_id="native-test",
            )
            with pytest.raises(ContentError, match="prediction_authority_revoked"):
                await authority.authorize()

        observed = []
        real_authorize = ReferencePredictionJob.authorize

        async def authorize_running(self, db=None):
            job = await jobs.get_job(db, self.job_id, populate_existing=True)
            observed.append((job.status, job.started_at))
            async with world.writers() as cancelled_db:
                cancelled_job = await jobs.get_job(cancelled_db, self.job_id)
                cancelled_job.payload = {
                    **cancelled_job.payload,
                    "cancel_requested": True,
                }
                await cancelled_db.flush()
                with pytest.raises(ContentError, match="prediction_authority_revoked"):
                    await real_authorize(self, cancelled_db)
                await cancelled_db.rollback()
            return await real_authorize(self, db)

        async def fail_after_start(*_args, **_kwargs):
            raise ValueError("model failure after authorization")

        monkeypatch.setattr(ReferencePredictionJob, "authorize", authorize_running)
        monkeypatch.setattr(execution, "_model_execution_port", lambda *_a, **_k: object())
        monkeypatch.setattr(execution, "_run_task_pipeline", fail_after_start)
        monkeypatch.setattr(prediction_progress, "publish_progress", lambda *_a, **_k: None)
        await execution.run_preannotation(world.request, execution_id="managed-start")
        assert len(observed) == 1 and observed[0][0] == "running"
        assert observed[0][1] is not None
        async with world.owner_factory() as owner:
            job = await owner.get(AsyncJob, world.execution.scope["job_id"])
            assert job.status == "failed" and job.result["failed_count"] == 1
            assert job.execution_snapshot_id == snapshot_id
            assert job.payload["prediction_authority"] == deepcopy(proof)
        async with world.writers() as db:
            authority.db = db
            with pytest.raises(ContentError, match="prediction_authority_revoked"):
                await real_authorize(authority)


async def test_real_runner_continues_after_a_failed_member_and_publishes_durable_progress(
    test_engine,
    monkeypatch,
):
    from app.application import preannotation_execution as execution
    from aap_inference.backend_transport import PredictionResult
    from app.services import prediction_progress
    from app.db.models.notification import Notification

    async with _world(test_engine, staged=False, task_count=2) as world:
        _runtime_connections(monkeypatch, world)
        calls, updates = [], []

        from sqlalchemy import inspect as sa_inspect

        from aap_inference.backend_transport import PredictionResult
        from tests.inference_execution_test_ports import pipeline_output

        async def infer(task, stages, _model_ports, *_args, **_kwargs):
            assert str(stages[0]["ml_backend_id"]) == str(world.backend_id)
            task_id = sa_inspect(task).identity[0]
            calls.append(task_id)
            if len(calls) == 1:
                raise ValueError("first member model failed")
            async with world.owner_factory() as owner:
                current = await owner.get(AsyncJob, world.execution.scope["job_id"])
                assert current.progress_pct == 50 and current.status == "running"
            return pipeline_output(
                [
                    PredictionResult(
                        task_id=str(task_id),
                        result=[],
                        score=0.8,
                        model_version="after-model-error",
                        inference_time_ms=7,
                        meta={"total_cost": 0.125},
                    )
                ],
                instance_id=world.backend_id,
                pool_id=world.pool_id,
                stage_stats={0: {"detected": 1, "ok": 1}},
            )

        monkeypatch.setattr(
            execution, "_model_execution_port", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(execution, "_run_task_pipeline", infer)
        monkeypatch.setattr(
            prediction_progress,
            "publish_progress",
            lambda *_args, **values: updates.append(values),
        )
        await execution.run_preannotation(
            world.request, execution_id="actual-two-member-run"
        )
        assert len(calls) == 2 and len(set(calls)) == 2
        async with world.owner_factory() as owner:
            job = await owner.get(AsyncJob, world.execution.scope["job_id"])
            assert job.status == "completed" and job.progress_pct == 100
            assert job.result["success_count"] == 1 and job.result["failed_count"] == 1
            assert job.result["total_cost"] == "0.1250"
            assert job.result["pipeline_stages"] == [
                {"stage": 0, "detected": 1, "ok": 1}
            ]
            failure = await owner.scalar(
                select(FailedPrediction).where(
                    FailedPrediction.task_id == calls[0],
                )
            )
            assert failure is not None and failure.ml_backend_id == world.backend_id
            assert failure.ml_backend_pool_id == world.pool_id
            assert failure.extra["request_context"]["text"] == world.request.prompt
            notices = list(
                (
                    await owner.scalars(
                        select(Notification).where(
                            Notification.user_id == world.actor_id,
                        )
                    )
                ).all()
            )
            assert len(notices) == 1
        assert any(
            value.get("pipeline_stages") == job.result["pipeline_stages"]
            for value in updates
        )


async def test_legacy_delivery_without_job_id_resumes_the_same_captured_job(
    test_engine,
    monkeypatch,
):
    from dataclasses import replace
    from app.application import durable_preannotation as workflow
    from app.application import preannotation_execution as execution
    from sqlalchemy import inspect as sa_inspect

    from aap_inference.backend_transport import PredictionResult
    from tests.inference_execution_test_ports import pipeline_output

    async with _world(test_engine, staged=False, result_count=1) as world:
        _runtime_connections(monkeypatch, world)
        async with world.owner_factory.begin() as owner:
            await owner.execute(
                delete(AsyncJob).where(AsyncJob.id == world.execution.scope["job_id"])
            )
        request = replace(world.request, async_job_id=None)
        delivery = f"legacy-delivery-{uuid4()}"
        calls = []

        async def infer(task, *_args, **_kwargs):
            task_id = sa_inspect(task).identity[0]
            calls.append(task_id)
            return pipeline_output(
                [
                    PredictionResult(
                        task_id=str(task_id),
                        result=[],
                        score=0.7,
                        model_version="legacy-resume",
                        inference_time_ms=1,
                    )
                ],
                instance_id=world.backend_id,
                pool_id=world.pool_id,
            )

        original_apply = workflow._apply_ready_result

        async def blocked(*_args, **_kwargs):
            raise PreannotationResultDeferred("task_busy")

        monkeypatch.setattr(
            execution, "_model_execution_port", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(execution, "_run_task_pipeline", infer)
        monkeypatch.setattr(workflow, "_apply_ready_result", blocked)
        with pytest.raises(PreannotationResultDeferred, match="task_busy"):
            await execution.run_preannotation(request, execution_id=delivery)
        monkeypatch.setattr(workflow, "_apply_ready_result", original_apply)
        await execution.run_preannotation(request, execution_id=delivery)
        assert calls == [world.task_id]
        assert await _counts(world) == (1, 1, 0, 1)
        async with world.owner_factory() as owner:
            jobs = list(
                (
                    await owner.scalars(
                        select(AsyncJob).where(
                            AsyncJob.celery_task_id == delivery,
                        )
                    )
                ).all()
            )
            assert len(jobs) == 1 and jobs[0].status == "completed"


async def test_large_failed_batch_has_a_bounded_summary_and_complete_paged_retry_history(
    test_engine,
    monkeypatch,
):
    from sqlalchemy import inspect as sa_inspect

    from app.application import preannotation_execution as execution
    from app.application.prediction_retry import _recorded_failed_ids

    async with _world(test_engine, staged=False, task_count=101) as world:
        _runtime_connections(monkeypatch, world)
        calls = []

        async def failed_model(task, *_args, **_kwargs):
            calls.append(sa_inspect(task).identity[0])
            raise ValueError("model rejected this member")

        monkeypatch.setattr(
            execution, "_model_execution_port", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(execution, "_run_task_pipeline", failed_model)
        await execution.run_preannotation(
            world.request, execution_id="large-failed-batch"
        )
        assert len(calls) == 101 and len(set(calls)) == 101
        async with world.owner_factory() as owner:
            job = await owner.get(AsyncJob, world.execution.scope["job_id"])
            assert job.status == "failed" and job.result["failed_count"] == 101
            assert len(job.result["failed_prediction_ids"]) == 100
            assert job.result["failed_prediction_ids_truncated"] is True
        async with world.writers() as writer:
            job = await writer.get(AsyncJob, world.execution.scope["job_id"])
            ids = await _recorded_failed_ids(writer, job)
        assert len(ids) == 101 and len(set(ids)) == 101
        assert [str(value) for value in ids[:100]] == job.result[
            "failed_prediction_ids"
        ]


@pytest.mark.parametrize("captured", [False, True])
async def test_cancelled_redelivery_applies_the_captured_member_then_stops(
    test_engine,
    monkeypatch,
    captured,
):
    from app.application import preannotation_execution as execution
    from unittest.mock import AsyncMock

    async with _world(test_engine, result_count=1) as world:
        if captured:
            await _ready(world)
        await world.execution.close()
        async with world.owner_factory.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE async_jobs SET payload=payload || jsonb_build_object('cancel_requested',true) WHERE id=:id"
                ),
                {"id": world.execution.scope["job_id"]},
            )
        _runtime_connections(monkeypatch, world)
        model = AsyncMock(
            side_effect=AssertionError("captured result must not be inferred again")
        )
        monkeypatch.setattr(
            execution, "_model_execution_port", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(execution, "_run_task_pipeline", model)
        await execution.run_preannotation(
            world.request, execution_id="cancelled-redelivery"
        )
        model.assert_not_awaited()
        assert await _counts(world) == ((1, 1, 0, 1) if captured else (0, 0, 0, 0))
        async with world.owner_factory() as owner:
            job = await owner.get(AsyncJob, world.execution.scope["job_id"])
            assert job.status == "cancelled"
            assert (
                job.result["success_count"] == int(captured)
                and job.result["failed_count"] == 0
            )


async def test_stale_worker_cannot_commit_ready_prediction_after_lease_takeover(
    test_engine,
):
    async with _world(test_engine, result_count=1, predict_mode="overwrite") as world:
        async with world.owner_factory() as owner:
            old = Prediction(
                task_id=world.task_id, project_id=world.project_id, result=[]
            )
            owner.add(old)
            task = await owner.get(Task, world.task_id)
            task.total_predictions = 1
            await owner.commit()
        async with world.writers() as db:
            await _overwrite(world.execution, db, world.request)
        assert await _counts(world) == (0, 0, 0, 0)
        member, result, requests = await _ready(world)
        old_lease = world.execution.lease
        async with world.owner_factory() as owner:
            await owner.execute(
                text(
                    "UPDATE batch_result_executions SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job_id"
                ),
                {"job_id": world.execution.scope["job_id"]},
            )
            await owner.commit()
        successor = BatchResultExecution(
            world.request,
            world.writers,
            execution_id="successor",
            executor_session_factory=world.executors,
        )
        try:
            await successor.claim()
            assert successor.lease.generation == old_lease.generation + 1
            async with world.writers() as db:
                with pytest.raises(PreannotationResultDeferred):
                    await _apply_ready_result(
                        world.execution,
                        db,
                        PredictionService(db),
                        member,
                        result,
                        requests,
                        manifest_backed=False,
                    )
                await db.rollback()
            assert await _counts(world) == (0, 0, 0, 0)
            actual_result, actual_requests = await successor.load(1, stage="prepared")
            assert actual_requests == requests
            async with world.writers() as db:
                await _apply_ready_result(
                    successor,
                    db,
                    PredictionService(db),
                    member,
                    actual_result,
                    actual_requests,
                    manifest_backed=False,
                )
            assert await _counts(world) == (1, 1, 0, 1)
            async with world.writers() as db:
                await _overwrite(successor, db, world.request)
            assert await _counts(world) == (1, 1, 0, 1)
        finally:
            await successor.close()


async def test_lost_real_commit_ack_reads_exact_receipt_without_duplicate_predictions(
    test_engine,
):
    async with _world(test_engine, result_count=1) as world:
        member, result, requests = await _ready(world)
        from sqlalchemy.exc import SQLAlchemyError

        async with world.writers() as db:
            actual_commit = db.commit

            async def lost_ack():
                await actual_commit()
                raise SQLAlchemyError("injected lost commit acknowledgement")

            db.commit = lost_ack
            await _apply_ready_result(
                world.execution,
                db,
                PredictionService(db),
                member,
                result,
                requests,
                manifest_backed=False,
            )
        assert await _counts(world) == (1, 1, 0, 1)
