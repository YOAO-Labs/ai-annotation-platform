"""A separate prediction worker consumes a complete cache without source access."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import uuid

from celery.utils.nodenames import worker_direct
import httpx
import pytest
from redis import Redis
from sqlalchemy import delete, select
from training_contract.model_return import segmentation_identity

from app.config import settings
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageLocationProbe
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.notification import Notification
from app.db.models.original_cache import (
    OriginalCacheEntry,
    OriginalCacheReadLease,
)
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.deps import get_db
from app.services import original_cache as cache, trained_models
from app.services.raster_mask_storage import (
    build_rle_reference,
    lock_raster_mask_references,
)
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest
from app.utils.raster_mask_rle import encode_coco_rle
from app.workers import tasks as worker_tasks
from app.workers.celery_app import celery_app
from app.workers.cleanup import _is_raster_mask_key_referenced
from tests.test_reference_background_native import response_for
from tests.test_reference_original_cache import (
    _REAL_STORAGE,
    referenced_cache as referenced_cache,
    seed_cache,
)
from tests.test_trained_models import ready_deployment, seed_source
from tests.prediction_input_live_assertions import (
    assert_retired_input_versions,
    retire_known_inputs,
)


def _unique_mask():
    width, height = 48, 32
    pixels = [
        int(
            (3 <= x < 18 and 3 <= y < 18 and not (7 <= x < 14 and 7 <= y < 14))
            or (35 <= x < 40 and 23 <= y < 28)
        )
        for y in range(height)
        for x in range(width)
    ]
    # Keep this test's content key independent of other mask fixtures.
    marker = int.from_bytes(uuid.uuid4().bytes, "big")
    for bit in range(128):
        pixels[29 * width + bit] = (marker >> bit) & 1
    rle = encode_coco_rle(pixels, width, height)
    return rle, build_rle_reference(rle)


async def _model(world, url, rle, mask):
    async with world.factory() as db:
        project = await db.get(Project, world.project.id)
        project.raster_mask_native_editing_enabled = True
        project.tool_bindings = {
            **project.tool_bindings,
            "region": {"enabled": True, "classes": [{"name": "car"}]},
        }
        task = await db.get(Task, world.task.id)
        task.status, task.assignee_id = "pending", None
        _, run, attempt = await seed_source(db, world.manager, project=project)
        version = TrainedModelVersion(
            project_id=project.id,
            run_id=run.id,
            attempt_id=attempt.id,
            creator_id=world.manager.id,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            name="Cached reference prediction",
            framework="ultralytics",
            framework_version="8.4.177",
            artifact=attempt.artifacts["best.pt"],
            classes=["native"],
            bindings=["car"],
            binding_digest=content_digest(["car"]),
            provenance={
                "task": "segment",
                "base_model": {"id": "yolo11n-seg"},
                "model_return": segmentation_identity("yolo11n-seg", "8.4.177"),
            },
            evaluation={},
        )
        db.add(version)
        await db.flush()
        backend, deployment = await ready_deployment(db, project, version)
        db.add(
            ModelRelease(
                project_id=project.id,
                model_version_id=version.id,
                deployment_id=deployment.id,
                task="segment",
                creator_id=world.manager.id,
                request_id=uuid.uuid4(),
                request_digest="b" * 64,
                generation=1,
                snapshot={},
            )
        )
        backend.url = url
        await db.commit()
        # The endpoint update invalidates its earlier health observation.
        await db.refresh(backend)
        backend.state = "connected"
        backend.last_checked_at = datetime.now(timezone.utc)
        backend.health_meta = {
            "capabilities": {"task_contract_revision": 1},
            "compute": {"effective_device": "cpu"},
        }
        await db.commit()
        return SimpleNamespace(
            task=task,
            backend_id=backend.id,
            version=version,
            ref=trained_models.frozen_reference(version, deployment),
            rle=rle,
            mask=mask,
        )


@asynccontextmanager
async def _http_client(app, world):
    async def database():
        async with world.factory() as db:
            yield db

    missing = object()
    previous = app.dependency_overrides.get(get_db, missing)
    app.dependency_overrides[get_db] = database
    try:
        token = create_access_token(subject=str(world.manager.id), role="super_admin")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            yield client
    finally:
        if previous is missing:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


@asynccontextmanager
async def _owned_worker(tmp_path, test_db_url, queue, node, published):
    storage_endpoint = urlsplit(storage_service.client.meta.endpoint_url)
    alternate_host = (
        "localhost" if storage_endpoint.hostname == "127.0.0.1" else "127.0.0.1"
    )
    storage_host = f"{alternate_host}:{storage_endpoint.port}"
    assert storage_host != storage_endpoint.netloc
    environment = dict(
        os.environ,
        DATABASE_URL=test_db_url,
        STORAGE_FILESYSTEM_ROOTS="{}",
        ML_BACKEND_STORAGE_HOST=storage_host,
        RASTER_MASK_READ_ENABLED="true",
        RASTER_MASK_CREATE_ENABLED="true",
        ML_BACKEND_ROUTER_MODE="off",
        PYTHONDONTWRITEBYTECODE="1",
    )
    process = None
    log_path = tmp_path / "cache-prediction-worker.log"
    try:
        with log_path.open("wb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "app.workers.celery_app",
                    "worker",
                    "--pool=solo",
                    "--concurrency=1",
                    "--loglevel=WARNING",
                    "--without-gossip",
                    "--without-mingle",
                    "--without-heartbeat",
                    "-Q",
                    queue,
                    "--hostname",
                    node,
                ],
                cwd=Path(__file__).parents[1],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = asyncio.get_running_loop().time() + 40
            while asyncio.get_running_loop().time() < deadline:
                registered = await asyncio.to_thread(
                    lambda: (
                        celery_app.control.inspect(
                            destination=[node], timeout=1
                        ).registered()
                        or {}
                    )
                )
                if "app.workers.tasks.batch_predict" in registered.get(node, []):
                    break
                assert process.poll() is None, "Owned cache worker exited at startup"
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("Owned cache worker did not register")
            queues = await asyncio.to_thread(
                lambda: celery_app.control.inspect(
                    destination=[node], timeout=1
                ).active_queues()
            )
            assert {item["name"] for item in queues[node]} == {
                queue,
                worker_direct(node).name,
            }
            yield process
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        try:
            for result in published:
                await asyncio.to_thread(result.forget)
        finally:
            try:
                with celery_app.connection_for_write() as connection:
                    with connection.channel() as channel:
                        channel.queue_delete(worker_direct(node).name)
                        channel.queue_delete(f"{node}.celery.pidbox")
                        channel.queue_delete(queue)
                with Redis.from_url(settings.redis_url) as redis:
                    redis.delete(f"celery:hb:{node}")
            finally:
                log_path.unlink(missing_ok=True)


async def _cleanup_inputs(world):
    # All writers have stopped. Only the owner's actual cleanup deadline is used.
    await retire_known_inputs(world.factory, world.task.id)


async def _cleanup_cache(world):
    async with world.factory() as db:
        entries = list(
            await db.scalars(
                select(OriginalCacheEntry).where(
                    OriginalCacheEntry.scope_location_id == world.location_id
                )
            )
        )
        if entries:
            await db.execute(
                delete(OriginalCacheReadLease).where(
                    OriginalCacheReadLease.entry_id.in_([entry.id for entry in entries])
                )
            )
            for entry in entries:
                entry.retain_until = cache.now() - timedelta(seconds=1)
            await db.commit()
    for entry in entries:
        assert await cache.retire(world.factory, entry.id)
        assert await cache.purge(world.factory, entry.id) == "removed"


async def _cleanup_rows(world):
    project_id, task_id = world.project.id, world.task.id
    async with world.factory() as db:
        pools = list(
            await db.scalars(
                select(ProjectMLBackendPool.pool_id).where(
                    ProjectMLBackendPool.project_id == project_id
                )
            )
        )
        versions = select(TrainedModelVersion.id).where(
            TrainedModelVersion.project_id == project_id
        )
        backends = list(
            await db.scalars(
                select(ModelDeployment.registry_id).where(
                    ModelDeployment.model_version_id.in_(versions)
                )
            )
        )
        predictions = select(Prediction.id).where(Prediction.project_id == project_id)
        failures = select(FailedPrediction.id).where(
            FailedPrediction.project_id == project_id
        )
        jobs = select(AsyncJob.id).where(AsyncJob.project_id == project_id)
        await db.execute(delete(Annotation).where(Annotation.task_id == task_id))
        await db.execute(delete(TaskLock).where(TaskLock.task_id == task_id))
        await db.execute(
            delete(PredictionMeta).where(
                PredictionMeta.prediction_id.in_(predictions)
                | PredictionMeta.failed_prediction_id.in_(failures)
            )
        )
        for model in (Prediction, FailedPrediction):
            await db.execute(delete(model).where(model.project_id == project_id))
        await db.execute(delete(Notification).where(Notification.target_id.in_(jobs)))
        await db.execute(delete(AsyncJob).where(AsyncJob.project_id == project_id))
        await db.execute(
            delete(PredictionExecutionSnapshot).where(
                PredictionExecutionSnapshot.project_id == project_id
            )
        )
        await db.execute(
            delete(RasterMaskUpload).where(RasterMaskUpload.task_id == task_id)
        )
        await db.execute(
            delete(ProjectModelSelection).where(
                ProjectModelSelection.project_id == project_id
            )
        )
        await db.execute(
            delete(ModelRelease).where(ModelRelease.project_id == project_id)
        )
        await db.execute(
            delete(ModelDeployment).where(
                ModelDeployment.model_version_id.in_(versions)
            )
        )
        for model in (TrainedModelVersion, TrainingRun, TrainingDatasetVersion):
            await db.execute(delete(model).where(model.project_id == project_id))
        await db.execute(
            delete(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == project_id
            )
        )
        if pools:
            project = await db.get(Project, project_id)
            project.ml_backend_pool_id = None
            await db.flush()
            await db.execute(
                delete(MLBackendServicePool).where(MLBackendServicePool.id.in_(pools))
            )
        if backends:
            await db.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id.in_(backends))
            )
        await db.commit()


async def _cleanup_mask(world, mask, preexisting):
    if preexisting:
        return
    key = mask["object_key"]
    async with world.factory() as db:
        await lock_raster_mask_references(db, {"object_key": key}, verify=False)
        upload = await db.scalar(
            select(RasterMaskUpload.id)
            .where(RasterMaskUpload.object_key == key)
            .limit(1)
        )
        if upload is None and not await _is_raster_mask_key_referenced(db, key):
            await asyncio.to_thread(storage_service.delete_object, key)
        await db.commit()


async def test_independent_worker_predicts_and_adopts_cached_reference_without_source(
    referenced_cache, app_module, test_db_url, tmp_path, monkeypatch
):
    world = referenced_cache
    monkeypatch.setattr(cache, "make_storage", _REAL_STORAGE)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(settings, "ml_backend_router_mode", "off")
    if os.environ.get("AAP_WORKTREE_MODE") == "test":
        assert storage_service.import_bucket.startswith("aap-wt-")
        await asyncio.to_thread(
            storage_service.client.put_bucket_versioning,
            Bucket=storage_service.import_bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
    elif (
        await asyncio.to_thread(
            storage_service.client.get_bucket_versioning,
            Bucket=storage_service.import_bucket,
        )
    ).get("Status") != "Enabled":
        pytest.skip("Live prediction requires a versioned disposable import bucket")
    rle, mask = _unique_mask()
    preexisting = await asyncio.to_thread(
        storage_service.verify_upload, mask["object_key"]
    )
    queue = f"cached-prediction-{uuid.uuid4().hex}"
    node = f"cached-prediction-{uuid.uuid4().hex}@local"
    seen, inputs, errors, published, observed = [], [], [], [], []
    response_model = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/predict"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert body["context"]["task"] == "segment"
                assert body["context"][
                    "frozen_model_ref"
                ] == trained_models.wire_reference(response_model.ref)
                expected = response_for(response_model)
                results = []
                for task in body["tasks"]:
                    parsed = urlsplit(task["file_path"])
                    assert (
                        parsed.netloc
                        != urlsplit(storage_service.client.meta.endpoint_url).netloc
                    )
                    assert parse_qs(parsed.query)["X-Amz-SignedHeaders"] == ["host"]
                    bucket, key = parsed.path.lstrip("/").split("/", 1)
                    assert bucket == storage_service.import_bucket
                    assert key.startswith("prediction-inputs/") and "/original/" in key
                    version = parse_qs(parsed.query)["versionId"][0]
                    assert version not in {"", "null"}
                    inputs.append((bucket, key))
                    observed.append((bucket, key, version))
                    with urlopen(task["file_path"], timeout=5) as response:
                        raw = response.read()
                    assert raw == world.data
                    seen.append(hashlib.sha256(raw).hexdigest())
                    results.append(
                        {
                            "task": task["id"],
                            "result": expected.result,
                            "meta": expected.meta,
                            "score": expected.score,
                            "inference_time_ms": expected.inference_time_ms,
                        }
                    )
                raw = json.dumps({"results": results}).encode()
                self.send_response(200)
            except Exception as exc:
                errors.append(type(exc).__name__)
                raw = b'{"error":"cache prediction fixture failed"}'
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    async with AsyncExitStack() as stack:
        stack.push_async_callback(_cleanup_mask, world, mask, preexisting is not None)
        stack.push_async_callback(_cleanup_rows, world)
        stack.push_async_callback(_cleanup_cache, world)
        stack.push_async_callback(_cleanup_inputs, world)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def stop_http():
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()

        stack.push_async_callback(stop_http)
        entry = await seed_cache(world)
        response_model = await _model(
            world, f"http://127.0.0.1:{server.server_port}", rle, mask
        )
        source_path = world.content[0] / world.content[7].relative_path
        source_path.unlink()
        async with world.factory() as db:
            await db.execute(
                delete(StorageLocationProbe).where(
                    StorageLocationProbe.location_id == world.location_id
                )
            )
            await db.commit()
        assert not source_path.exists()
        original_publish = worker_tasks.batch_predict.apply_async

        def publish(*args, **kwargs):
            kwargs["queue"] = queue
            result = original_publish(*args, **kwargs)
            published.append(result)
            return result

        monkeypatch.setattr(worker_tasks.batch_predict, "apply_async", publish)
        process = await stack.enter_async_context(
            _owned_worker(tmp_path, test_db_url, queue, node, published)
        )
        client = await stack.enter_async_context(_http_client(app_module, world))
        response = await client.post(
            f"/api/v1/projects/{world.project.id}/preannotate",
            json={
                "ml_backend_id": str(response_model.backend_id),
                "model_id": f"trained:{response_model.version.id}",
                "task": "segment",
                "task_ids": [str(world.task.id)],
                "execution_scope": "bulk",
                "predict_mode": "overwrite",
                "pipeline_stages": [
                    {
                        "stage": 7,
                        "parent_stage": None,
                        "ml_backend_id": str(response_model.backend_id),
                        "model_id": f"trained:{response_model.version.id}",
                        "task": "segment",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        job_id = uuid.UUID(response.json()["job_id"])
        assert len(published) == 1 and published[0].id == str(job_id)
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            async with world.factory() as db:
                job = await db.get(AsyncJob, job_id)
                if job.status not in {"pending", "running"}:
                    failures = list(
                        await db.scalars(
                            select(FailedPrediction.error_type).where(
                                FailedPrediction.project_id == world.project.id
                            )
                        )
                    )
                    assert job.status == "completed", (job.status, failures, errors)
                    break
            assert process.poll() is None, "Owned prediction worker exited"
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Cached prediction job did not finish")
        await asyncio.to_thread(
            published[0].get, timeout=10, disable_sync_subtasks=False
        )
        assert not errors and seen == [world.sha]
        async with world.factory() as db:
            prediction = await db.scalar(
                select(Prediction).where(Prediction.task_id == world.task.id)
            )
            metadata = await db.scalar(
                select(PredictionMeta).where(
                    PredictionMeta.prediction_id == prediction.id
                )
            )
            proof = metadata.extra["reference_native"]
            assert proof["reference_input"]["sha256"] == world.sha
            assert proof["reference_input"]["node_id"] == f"worker:{node}"
            assert proof["registry_id"] == str(response_model.backend_id)
            assert prediction.result[0]["geometry"]["mask"]["sha256"] == mask["sha256"]
            assert await db.get(OriginalCacheEntry, entry.id) is not None
            assert (
                await db.scalar(
                    select(OriginalCacheReadLease.id).where(
                        OriginalCacheReadLease.entry_id == entry.id
                    )
                )
                is None
            )
            assert (
                await db.scalar(
                    select(StorageLocationProbe.node_id).where(
                        StorageLocationProbe.location_id == world.location_id
                    )
                )
                is None
            )
        path = f"/api/v1/tasks/{world.task.id}/predictions/{prediction.id}"
        content = await client.get(path + "/mask-content/0")
        assert content.status_code == 200 and content.json() == rle
        adopted = await client.post(path + "/accept")
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()[0]["geometry"]["mask"]["sha256"] == mask["sha256"]
        assert inputs and not source_path.exists()
        await retire_known_inputs(world.factory, world.task.id)
        await assert_retired_input_versions(
            world.factory,
            world.task.id,
            storage_service,
            observed=observed,
        )
