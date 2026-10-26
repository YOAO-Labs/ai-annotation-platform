"""API/Celery consumers against the independently running retained video owner."""

import asyncio
from contextlib import AsyncExitStack
from hashlib import sha256
from io import BytesIO
import json
import os
import re
import sys
import threading
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

from celery.contrib.testing.worker import start_worker
import httpx
from PIL import Image
import pytest
from sqlalchemy import delete, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aap_assets_video_inputs.evidence import (
    VideoInputEvidence,
    VideoInputFailureEvidence,
    parse_prediction_evidence,
)
from aap_assets_video_inputs.execution import VideoInputFailure
from aap_inference_contract.execution import (
    ExecutionFailure,
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionError,
    ModelExecutionObservation,
    ModelExecutionPort,
    ModelOperationResult,
    ModelTaskResult,
    thaw_json,
)
from app.config import settings
from app.core.security import create_access_token
from app.deps import get_db
from app.db.models.async_job import AsyncJob
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.task import Task
from app.db.models.project import Project
from app.services.accepted_video_inputs import VideoInputGateway
from app.workers.celery_app import celery_app
from tests.conftest import create_registry_with_pool
from tests.test_assets_media_live import (
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _stop_process,
)
from tests.test_video_input_authorities_native import _seed_video_task
from tests.test_video_input_live import _video_bytes, _video_runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_FRAME_CELERY_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKTREE_MODE") != "test",
        reason="requires explicitly enabled owned SQL/S3/Celery qualification",
    ),
]


def _install_video_model_port(monkeypatch, predict, *, backend_id):
    class FixtureModelExecutionPort:
        async def execute(
            self, operation: FrozenModelOperation
        ) -> ModelOperationResult:
            inputs = tuple(thaw_json(value) for value in operation.task_inputs)
            context = (
                None if operation.context is None else thaw_json(operation.context)
            )
            provenance = ExecutionProvenance(
                requested_registry_id=operation.requested_registry_id,
                requested_pool_id=operation.requested_pool_id,
                registry_id=backend_id,
                pool_id=operation.requested_pool_id,
                instance_id=backend_id,
                route_scope_id=uuid4(),
                attempt_id=operation.request_id,
            )
            observation_operation = (
                "predict"
                if operation.operation == "batch_predict"
                else "predict_interactive"
            )
            try:
                responses = await predict(inputs, context)
            except RuntimeError as error:
                observation = ModelExecutionObservation(
                    backend_id=str(backend_id),
                    operation=observation_operation,
                    outcome="error",
                    elapsed_seconds=0.0,
                )
                raise ModelExecutionError(
                    ExecutionFailure(
                        request_id=operation.request_id,
                        kind="backend",
                        code="fixture_backend_failure",
                        status_code=500,
                        detail={"message": str(error)},
                        provenance=provenance,
                        observation=observation,
                    )
                ) from error
            return ModelOperationResult(
                request_id=operation.request_id,
                operation=operation.operation,
                results=tuple(
                    ModelTaskResult(
                        task_id=response.task_id,
                        result=response.result,
                        score=response.score,
                        model_version=response.model_version,
                        inference_time_ms=response.inference_time_ms,
                        meta=response.meta,
                    )
                    for response in responses
                ),
                provenance=provenance,
                observation=ModelExecutionObservation(
                    backend_id=str(backend_id),
                    operation=observation_operation,
                    outcome="success",
                    elapsed_seconds=0.0,
                ),
                wall_time_ms=0,
            )

    def port_factory(*_args, **_kwargs) -> ModelExecutionPort:
        return FixtureModelExecutionPort()

    monkeypatch.setattr(
        "app.services.inference_model_execution.model_execution_port",
        port_factory,
    )


def _start_polling_video_worker(runtime, directory):
    environment = _runtime_environment(
        tmp_path=directory,
        role="worker",
        database_url=_runtime_database_url(runtime.worker_url),
        endpoint=runtime.endpoint,
        public_endpoint=runtime.endpoint,
        datasets_bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
        cache_bucket=runtime.buckets["MINIO_MEDIA_CACHE_BUCKET"],
        access_key=runtime.worker_access,
        secret_key=runtime.worker_secret,
    )
    environment["ASSETS_MEDIA_POLL_SECONDS"] = "0.1"
    process, stream = _start_process(
        [sys.executable, "-m", "aap_assets_media_runtime.worker"],
        environment,
        directory / "video-consumer-worker.log",
    )
    runtime.processes.append(process)
    runtime.streams.append(stream)


async def _wait_job(runtime, job_id, *, terminal=True, timeout=180):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        async with runtime.sessions() as db:
            job = await db.get(AsyncJob, job_id)
            last = job.status if job is not None else None
            if job is not None and (
                (terminal and last in {"completed", "failed", "cancelled"})
                or (
                    not terminal
                    and await db.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM public.asset_accepted_video_inputs WHERE job_id=:job)"
                        ),
                        {"job": job_id},
                    )
                )
            ):
                return job
        await asyncio.sleep(0.1)
    pytest.fail(f"video consumer Job did not reach expected state: {last}")


async def _wait_verification(runtime, job_id, event, directory):
    deadline = time.monotonic() + 60
    state = {}
    while time.monotonic() < deadline:
        if event.is_set():
            return
        async with runtime.sessions() as db:
            job = await db.get(AsyncJob, job_id)
            inputs = (
                await db.execute(
                    text(
                        "SELECT status,error_code FROM public.asset_accepted_video_inputs WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            ).all()
            state = {
                "job": job.status,
                "error": job.error_message,
                "inputs": [tuple(row) for row in inputs],
                "worker_exit": runtime.processes[-1].poll(),
            }
        if (
            state["job"] in {"failed", "cancelled", "completed"}
            or state["worker_exit"] is not None
        ):
            break
        await asyncio.sleep(0.1)
    state["worker_log"] = (directory / "video-consumer-worker.log").read_text(
        errors="replace"
    )[-2000:]
    safe = re.sub(
        r"(?:postgresql(?:\+[a-z]+)?|https?)://[^\s]+", "[endpoint]", json.dumps(state)
    )
    pytest.fail(f"retained video verification was not reached: {safe}")


async def _cleanup_consumer_fixture(
    runtime, *, project_id, dataset_id, item_id, task_id, backend_id, pool_id
):
    for process in reversed(runtime.processes):
        await asyncio.to_thread(_stop_process, process)
    # Native receipts, audits and their actors remain until DB destruction.
    # Only this fixture's mutable business rows are removed between tests.
    async with runtime.sessions.begin() as db:
        pred_ids = select(Prediction.id).where(Prediction.task_id == task_id)
        failed_ids = select(FailedPrediction.id).where(
            FailedPrediction.task_id == task_id
        )
        await db.execute(
            delete(PredictionMeta).where(
                or_(
                    PredictionMeta.prediction_id.in_(pred_ids),
                    PredictionMeta.failed_prediction_id.in_(failed_ids),
                )
            )
        )
        await db.execute(delete(Annotation).where(Annotation.task_id == task_id))
        await db.execute(delete(Prediction).where(Prediction.task_id == task_id))
        await db.execute(
            delete(FailedPrediction).where(FailedPrediction.task_id == task_id)
        )
        await db.execute(delete(AsyncJob).where(AsyncJob.project_id == project_id))
        await db.execute(delete(Task).where(Task.id == task_id))
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
        await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
        await db.execute(
            delete(MLBackendServicePool).where(MLBackendServicePool.id == pool_id)
        )
        await db.execute(
            delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
        )


@pytest.mark.parametrize(
    ("source_missing", "model_failure", "source_drift"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
    ids=["ready", "source_missing", "backend_failure", "source_drift"],
)
async def test_api_video_overwrite_waits_for_original_then_runs_real_celery_frames(
    test_engine,
    test_db_url,
    app_module,
    tmp_path,
    monkeypatch,
    source_missing,
    model_failure,
    source_drift,
):
    def database_target(value):
        url = make_url(value)
        return url.get_backend_name(), url.host, url.port, url.database

    assert database_target(settings.database_url) == database_target(test_db_url)
    assert not celery_app.conf.task_always_eager
    async with AsyncExitStack() as resources:
        runtime = await resources.enter_async_context(
            _video_runtime(test_engine, tmp_path)
        )
        api_engine = create_async_engine(
            settings.database_url, poolclass=NullPool, hide_parameters=True
        )
        resources.push_async_callback(api_engine.dispose)
        api_sessions = async_sessionmaker(api_engine, expire_on_commit=False)
        body = await asyncio.to_thread(_video_bytes, tmp_path)
        key = f"video-consumers/{uuid4()}.mp4"
        if not source_missing:
            runtime.owner.put_object(
                Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"], Key=key, Body=body
            )
        async with runtime.sessions.begin() as db:
            actor_id, project, dataset_id, item_id, task_id = await _seed_video_task(db)
            project.ai_enabled = True
            backend, pool = await create_registry_with_pool(
                db,
                name=f"video-consumer-{uuid4().hex[:8]}",
                is_interactive=False,
                enabled_pool=True,
            )
            project.ml_backend_pool_id = pool.id
            db.add(
                ProjectMLBackendPool(
                    project_id=project.id, pool_id=pool.id, enabled=True
                )
            )
            await db.execute(
                text(
                    "UPDATE public.dataset_items SET file_path=:key,file_size=:size,content_hash=:sha WHERE id=:item"
                ),
                {
                    "key": key,
                    "size": len(body),
                    "sha": sha256(body).hexdigest(),
                    "item": item_id,
                },
            )
            previous = Prediction(
                id=uuid4(),
                task_id=task_id,
                project_id=project.id,
                result=[],
                source="ml_backend",
            )
            db.add(previous)
            task = await db.get(Task, task_id)
            task.total_predictions = 1
            previous_id, project_id, backend_id = previous.id, project.id, backend.id
            pool_id = pool.id

        resources.push_async_callback(
            _cleanup_consumer_fixture,
            runtime,
            project_id=project_id,
            dataset_id=dataset_id,
            item_id=item_id,
            task_id=task_id,
            backend_id=backend_id,
            pool_id=pool_id,
        )

        calls = []
        drifted = threading.Event()

        async def predict(tasks, context=None):
            assert len(tasks) == 1
            task = tasks[0]
            url = task["file_path"]
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(url)
            assert response.status_code == 200
            with Image.open(BytesIO(response.content)) as image:
                assert image.size == (80, 48)
                red, green, blue = image.convert("RGB").getpixel((40, 24))
                assert red > 240 and green < 20 and blue < 20
            calls.append(
                {
                    "id": task["id"],
                    "sha256": sha256(response.content).hexdigest(),
                    "empty": (len(calls) + 1) % 2 == 0,
                }
            )
            if source_drift:
                if not any(call.get("drifted") for call in calls):
                    calls[-1]["drifted"] = True
                    runtime.owner.put_object(
                        Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
                        Key=key,
                        Body=body + b"changed",
                    )
                    drifted.set()
                assert await asyncio.to_thread(drifted.wait, 5)
            if model_failure and task["id"].endswith(":3"):
                calls[-1]["failed"] = True
                raise RuntimeError("fixture frame model failure")
            # Empty results must also leave a durable completion receipt.
            result = (
                []
                if len(calls) % 2 == 0
                else [
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 10,
                            "y": 10,
                            "width": 30,
                            "height": 20,
                            "rectanglelabels": ["car"],
                        },
                    }
                ]
            )
            return [
                SimpleNamespace(
                    task_id=task["id"],
                    result=result,
                    score=0.9,
                    model_version="video-fixture",
                    inference_time_ms=3,
                    meta={},
                )
            ]

        _install_video_model_port(monkeypatch, predict, backend_id=backend_id)
        monkeypatch.setattr(settings, "assets_media_url", runtime.origin)
        monkeypatch.setattr(settings, "assets_media_token", runtime.token)
        monkeypatch.setattr(settings, "frame_preannotate_chunk_size", 2)
        verification_deferred = threading.Event()
        if source_drift:
            verification_deferred.set()
        original_verify = VideoInputGateway.verify

        async def contended_verify(gateway, request):
            if not verification_deferred.is_set():
                verification_deferred.set()
                raise VideoInputFailure("input_busy")
            return await original_verify(gateway, request)

        monkeypatch.setattr(VideoInputGateway, "verify", contended_verify)

        async def application_db():
            async with api_sessions() as db:
                yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, application_db)
        token = create_access_token(subject=str(actor_id), role="project_admin")
        started = time.monotonic()
        with start_worker(
            celery_app,
            pool="threads",
            concurrency=2,
            queues=("default", "ml", "ml.cpu", "media"),
            perform_ping_check=False,
            shutdown_timeout=20,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module),
                base_url="http://test",
                trust_env=False,
            ) as api:
                response = await api.post(
                    f"/api/v1/projects/{project_id}/preannotate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "task_ids": [str(task_id)],
                        "predict_mode": "overwrite",
                        "pipeline_stages": [
                            {
                                "stage": 7,
                                "ml_backend_id": str(backend_id),
                                "source": {
                                    "kind": "dataset",
                                    "data_type": "video",
                                    "execution_unit": "frame",
                                },
                            }
                        ],
                    },
                )
            assert response.status_code == 200, response.text
            job_id = UUID(response.json()["job_id"])
            if not source_missing:
                await _wait_job(runtime, job_id, terminal=False, timeout=20)
                async with runtime.sessions() as db:
                    assert (
                        await db.scalar(
                            select(Prediction).where(Prediction.id == previous_id)
                        )
                        is not None
                    )
                    assert not calls
                _start_polling_video_worker(runtime, tmp_path)
                await _wait_verification(
                    runtime, job_id, verification_deferred, tmp_path
                )
                async with runtime.sessions() as db:
                    assert (await db.get(AsyncJob, job_id)).status == "pending"
                    assert (
                        await db.scalar(
                            select(Prediction).where(Prediction.id == previous_id)
                        )
                        is not None
                    )
                    assert not calls
            job = await _wait_job(runtime, job_id)
            retry_job_id = None
            if model_failure:
                assert job.status == "completed", job.error_message
                failed_ids = [
                    UUID(value) for value in job.result["failed_prediction_ids"]
                ]
                assert len(failed_ids) == 1
                failed_id = failed_ids[0]
                async with runtime.sessions.begin() as db:
                    failed = await db.get(FailedPrediction, failed_id)
                    failure = parse_prediction_evidence(
                        failed.extra["accepted_video_input"]
                    )
                    assert isinstance(failure, VideoInputFailureEvidence)
                    assert failure.job_id == job_id and failure.entry.frame_index == 3
                    await db.execute(
                        text("DELETE FROM public.async_jobs WHERE id=:job"),
                        {"job": job_id},
                    )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_module),
                    base_url="http://test",
                    trust_env=False,
                ) as api:
                    retried = await api.post(
                        f"/api/v1/admin/failed-predictions/{failed_id}/retry",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                assert retried.status_code == 202, retried.text
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    async with runtime.sessions() as db:
                        retry_job_id = await db.scalar(
                            select(AsyncJob.id).where(
                                AsyncJob.kind == "prediction_retry",
                                AsyncJob.payload["failed_prediction_id"].as_string()
                                == str(failed_id),
                            )
                        )
                    if retry_job_id is not None:
                        break
                    await asyncio.sleep(0.1)
                assert retry_job_id is not None and retry_job_id != job_id
                retried_job = await _wait_job(runtime, retry_job_id, timeout=30)
                assert retried_job.status == "completed", retried_job.error_message
                async with runtime.sessions() as db:
                    assert await db.get(FailedPrediction, failed_id) is None

        async with runtime.sessions() as db:
            rows = (
                await db.execute(
                    select(Prediction, PredictionMeta.extra)
                    .join(PredictionMeta, PredictionMeta.prediction_id == Prediction.id)
                    .where(Prediction.task_id == task_id)
                )
            ).all()
            if source_missing:
                assert job.status == "failed"
                assert (
                    await db.scalar(
                        select(Prediction).where(Prediction.id == previous_id)
                    )
                    is not None
                )
                assert not rows and not calls
                return
            if source_drift:
                assert job.status == "failed"
                assert "source_changed" in job.error_message
                assert not rows and calls
                return
            assert job.status == "completed", job.error_message
            assert (
                await db.scalar(select(Prediction).where(Prediction.id == previous_id))
                is None
            )
            assert len(rows) == 6
            assert len(calls) == (7 if model_failure else 6)
            proofs = [
                parse_prediction_evidence(extra["accepted_video_input"])
                for _prediction, extra in rows
            ]
            assert all(isinstance(proof, VideoInputEvidence) for proof in proofs)
            assert {proof.entry.ordinal for proof in proofs} == set(range(1, 7))
            assert {proof.entry.frame_index for proof in proofs} == set(range(6))
            assert {proof.job_id for proof in proofs} == {job_id}
            assert {proof.source_sha256 for proof in proofs} == {
                sha256(body).hexdigest()
            }
            assert {proof.frame_sha256 for proof in proofs} == {
                call["sha256"] for call in calls
            }
            execution_ids = {
                extra["accepted_video_execution_job_id"] for _prediction, extra in rows
            }
            assert execution_ids == (
                {str(job_id), str(retry_job_id)} if model_failure else {str(job_id)}
            )
            assert sum(not prediction.result for prediction, _extra in rows) == sum(
                call["empty"] for call in calls if not call.get("failed")
            )
            candidate_id = next(
                prediction.id
                for prediction, extra in rows
                if prediction.result
                and (
                    not model_failure
                    or extra["accepted_video_execution_job_id"] == str(retry_job_id)
                )
            )
        async with runtime.sessions.begin() as db:
            await db.execute(
                text("DELETE FROM public.async_jobs WHERE id=:job"), {"job": job_id}
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://test",
            trust_env=False,
        ) as api:
            adopted = await api.post(
                f"/api/v1/tasks/{task_id}/predictions/{candidate_id}/accept",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert adopted.status_code == 200, adopted.text
        annotation_ids = [UUID(row["id"]) for row in adopted.json()]
        assert len(annotation_ids) == 1
        async with runtime.sessions() as db:
            pinned = await db.scalar(
                text(
                    "SELECT count(*) FROM public.annotation_video_input_sources WHERE annotation_id=:annotation"
                ),
                {"annotation": annotation_ids[0]},
            )
            assert pinned == 1
        (tmp_path / "video-consumer-observation.json").write_text(
            json.dumps(
                {
                    "frames": len(calls),
                    "segments": 3,
                    "seconds": round(time.monotonic() - started, 3),
                    "source_bytes": len(body),
                }
            )
        )
