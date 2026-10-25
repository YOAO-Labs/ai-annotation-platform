"""Cold Celery worker acceptance through the Native Workspace Project capability."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
from urllib.parse import urlsplit
import uuid

from celery import Celery
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.task import Task
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
    endpoint_url,
)
from tests.factory import create_project, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_module_data_roles import _role_url
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_WORKER_ASSEMBLY_LIVE") != "1",
        reason="requires the explicitly enabled owned Workspace worker verifier",
    ),
]


@contextmanager
def _prediction_backend(*, token: str):
    """Serve a deterministic retry protocol fixture; this is not ML qualification."""
    calls: list[dict] = []
    request_seen = threading.Event()
    release_first = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {
                "status": "ok",
                "supported_prompts": ["bbox", "text"],
                "supported_trackers": ["sam2_video"],
            }
            self._reply(200, body)

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(400, {"detail": "invalid JSON"})
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._reply(401, {"detail": "invalid test backend token"})
                return
            tasks = payload.get("tasks")
            if not isinstance(tasks, list) or len(tasks) != 1:
                self._reply(400, {"detail": "expected one retry task"})
                return
            task_id = str(tasks[0].get("id"))
            calls.append({"path": self.path, "task_id": task_id})
            call_number = len(calls)
            request_seen.set()
            if call_number == 1:
                release_first.wait(timeout=30)
            self._reply(
                200,
                {
                    "results": [
                        {
                            "task": task_id,
                            "result": [],
                            "score": 0.91,
                            "model_version": "workspace-worker-assembly-fixture",
                            "inference_time_ms": 2,
                            "meta": {"total_cost": 0.001},
                        }
                    ]
                },
            )

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}",
            calls,
            request_seen,
            release_first,
        )
    finally:
        release_first.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def _assert_api_consumer_workspace_closure(test_engine, project_id) -> None:
    url = make_url(
        _role_url("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer")
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert (
                await db.scalar(text("SELECT current_user")) == "identity_api_consumer"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "'aap_workspace_v1.read_project_row(uuid)'::regprocedure, 'EXECUTE')"
                    )
                )
                is True
            )
            assert (
                await db.scalar(
                    text("SELECT aap_workspace_v1.read_project_row(CAST(:id AS uuid))"),
                    {"id": project_id},
                )
                is not None
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, "
                        "'public.projects', 'SELECT')"
                    )
                )
                is False
            )
        async with engine.connect() as db:
            with pytest.raises(DBAPIError):
                await db.execute(text("SELECT id FROM public.projects LIMIT 1"))
    finally:
        await engine.dispose()


async def _seed_retry_world(test_engine, *, backend_url, backend_token, index):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    backend_id, task_id, failed_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"workspace-worker-{index}-{suffix}@example.test",
            f"Workspace worker actor {index}",
        )
        project = await create_project(
            db,
            owner_id=actor.id,
            name=f"Workspace worker project {index} {suffix}",
            type_key="ocr",
        )
        backend = MLBackendRegistry(
            id=backend_id,
            name=f"workspace-worker-backend-{index}-{suffix}",
            url=f"{backend_url}/model/{index}-{suffix}",
            state="connected",
            is_interactive=True,
            auth_method="token",
            auth_token=backend_token,
            extra_params={},
        )
        db.add(backend)
        await db.flush()
        db.add(
            Task(
                id=task_id,
                project_id=project.id,
                display_id=f"T-WWA-{suffix}",
                file_name=f"worker-{index}.txt",
                file_path=f"projects/{project.id}/guide/worker-input-{index}.txt",
                file_type="text",
                status="pending",
            )
        )
        await db.flush()
        db.add(
            FailedPrediction(
                id=failed_id,
                project_id=project.id,
                task_id=task_id,
                ml_backend_id=backend_id,
                retry_count=0,
                error_type="TIMEOUT",
                message="seeded terminal retry for cold worker acceptance",
                extra={
                    "retry_pending": True,
                    "request_context": {"type": "text", "text": "fixture"},
                },
            )
        )
    return {
        "actor_id": actor.id,
        "project_id": project.id,
        "project_display_id": project.display_id,
        "project_name": project.name,
        "backend_id": backend_id,
        "task_id": task_id,
        "failed_id": failed_id,
        "job_id": uuid.uuid4(),
    }


async def _wait_worker_ready(sender: Celery, worker: WorkspaceCeleryWorker) -> None:
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        worker.assert_runtime()
        inspect = sender.control.inspect(timeout=1, destination=[worker.node_name])
        response = await asyncio.to_thread(inspect.ping)
        if response and response.get(worker.node_name) == {"ok": "pong"}:
            return
        await asyncio.sleep(0.25)
    raise AssertionError("installed Celery worker did not answer broker ping")


async def _wait_broker_ack(sender: Celery, worker: WorkspaceCeleryWorker, task_id):
    result = sender.AsyncResult(str(task_id))
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(lambda: result.state) == "SUCCESS":
            broker = await asyncio.to_thread(_broker_state, sender, worker.queue)
            inspect = sender.control.inspect(timeout=1, destination=[worker.node_name])
            active = await asyncio.to_thread(inspect.active)
            reserved = await asyncio.to_thread(inspect.reserved)
            outstanding = [
                task
                for snapshot in (active or {}, reserved or {})
                for tasks in snapshot.values()
                for task in tasks or []
                if task.get("id") == str(task_id)
            ]
            if (
                active is not None
                and reserved is not None
                and worker.node_name in active
                and worker.node_name in reserved
                and broker["queue_size"] == 0
                and broker["unacknowledged"] == 0
                and not outstanding
            ):
                return {"task_id": str(task_id), **broker, "worker_answered": True}
        await asyncio.sleep(0.25)
    raise AssertionError(
        "Celery result was stored but the broker message stayed outstanding"
    )


async def _wait_backend_request(event: threading.Event, worker) -> None:
    if not await asyncio.to_thread(event.wait, 60):
        raise AssertionError("cold worker did not call the configured model endpoint")
    worker.assert_runtime()


def _broker_state(sender: Celery, queue: str) -> dict[str, int]:
    with sender.connection_for_read() as connection:
        channel = connection.default_channel
        return {
            "queue_size": channel._size(queue),
            "unacknowledged": channel.client.hlen(channel.unacked_key),
        }


async def _assert_worker_database_identity(test_engine, worker) -> None:
    client_address = worker.assert_runtime()["database_client_address"]
    assert client_address
    async with test_engine.connect() as db:
        active = await db.scalar(
            text(
                "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                "WHERE datname=current_database() "
                "AND usename='identity_api_consumer' "
                "AND client_addr=CAST(:client_address AS inet) "
                "AND state IN ('active','idle in transaction','idle')"
            ),
            {"client_address": client_address},
        )
    assert active >= 1, "worker did not hold a session under identity_api_consumer"


async def _assert_terminal_retry(test_engine, world, task_result_value) -> None:
    assert isinstance(task_result_value, dict)
    assert task_result_value.get("status") == "succeeded"
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        job = await db.get(AsyncJob, world["job_id"])
        assert job is not None and job.status == "completed"
        assert job.payload["failed_prediction_id"] == str(world["failed_id"])
        assert job.payload["project_display_id"] == world["project_display_id"]
        assert job.payload["project_name"] == world["project_name"]
        assert job.result["success_count"] == 1
        assert job.result["failed_count"] == 0
        prediction = await db.scalar(
            select(Prediction).where(Prediction.task_id == world["task_id"])
        )
        assert prediction is not None
        assert prediction.model_version == "workspace-worker-assembly-fixture"
        meta = await db.scalar(
            select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
        )
        assert meta is not None
        assert meta.prediction_created_at == prediction.created_at
        assert meta.inference_time_ms == 2
        assert meta.total_cost == pytest.approx(0.001)
        assert await db.get(FailedPrediction, world["failed_id"]) is None


async def test_real_celery_retry_worker_uses_native_workspace_acl_and_restarts(
    test_engine, tmp_path, record_property
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    _verify_postgres(test_engine.url)
    backend_token = secrets.token_urlsafe(32)
    worlds = []
    worker = None
    sender = None
    minio_origin = endpoint_url()
    minio_scheme = urlsplit(minio_origin).scheme
    if minio_scheme not in {"http", "https"}:
        raise AssertionError("owned object storage endpoint must use HTTP or HTTPS")
    with _prediction_backend(token=backend_token) as (
        backend_origin,
        backend_calls,
        first_request_seen,
        release_first,
    ):
        resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
        guide_scope, destroy_guide_scope = create_guide_storage_scope(
            resources,
            owner_s3,
            guide_bucket,
            f"outside-guide/{uuid.uuid4()}.txt",
        )
        try:
            worker = WorkspaceCeleryWorker(
                test_engine,
                tmp_path,
                backend_token=backend_token,
                object_store_endpoint=minio_origin,
                object_store_bucket=guide_bucket,
                object_store_use_ssl=minio_scheme == "https",
                object_store_access_key=guide_scope["access_key"],
                object_store_secret_key=guide_scope["secret_key"],
            )
            if worker.object_store_access_key == os.environ.get(
                "MINIO_ACCESS_KEY"
            ) or worker.object_store_secret_key == os.environ.get("MINIO_SECRET_KEY"):
                raise AssertionError("Worker received owner object storage credentials")
            worlds.append(
                await _seed_retry_world(
                    test_engine,
                    backend_url=backend_origin.replace(
                        "127.0.0.1", "host.docker.internal"
                    ),
                    backend_token=backend_token,
                    index=1,
                )
            )
            worlds.append(
                await _seed_retry_world(
                    test_engine,
                    backend_url=backend_origin.replace(
                        "127.0.0.1", "host.docker.internal"
                    ),
                    backend_token=backend_token,
                    index=2,
                )
            )
            await _assert_api_consumer_workspace_closure(
                test_engine, worlds[0]["project_id"]
            )
            sender_url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
            sender = Celery(
                "workspace-worker-assembly-sender",
                broker=sender_url,
                backend=sender_url,
            )
            sender.conf.update(
                task_serializer="json",
                result_serializer="json",
                accept_content=["json"],
            )
            worker.start()
            await _wait_worker_ready(sender, worker)
            first_delivery = await asyncio.to_thread(
                sender.send_task,
                "app.workers.predictions_retry.retry_failed_prediction",
                args=[str(worlds[0]["failed_id"]), str(worlds[0]["actor_id"])],
                queue=worker.queue,
                task_id=str(worlds[0]["job_id"]),
            )
            await _wait_backend_request(first_request_seen, worker)
            await _assert_worker_database_identity(test_engine, worker)
            release_first.set()
            first_value = await asyncio.to_thread(
                first_delivery.get, timeout=120, propagate=False
            )
            assert first_delivery.state == "SUCCESS"
            record_property(
                "first_delivery_ack",
                await _wait_broker_ack(sender, worker, worlds[0]["job_id"]),
            )
            await _assert_terminal_retry(test_engine, worlds[0], first_value)
            before_restart = worker.assert_runtime()

            worker.restart()
            await _wait_worker_ready(sender, worker)
            after_restart = worker.assert_runtime()
            assert after_restart["started_at"] != before_restart["started_at"]
            assert after_restart["container_id"] == before_restart["container_id"]
            assert after_restart["image_id"] == before_restart["image_id"]
            assert (
                await asyncio.to_thread(
                    sender.AsyncResult(str(worlds[0]["job_id"])).get,
                    timeout=5,
                    propagate=False,
                )
                == first_value
            )

            second_delivery = await asyncio.to_thread(
                sender.send_task,
                "app.workers.predictions_retry.retry_failed_prediction",
                args=[str(worlds[1]["failed_id"]), str(worlds[1]["actor_id"])],
                queue=worker.queue,
                task_id=str(worlds[1]["job_id"]),
            )
            second_value = await asyncio.to_thread(
                second_delivery.get, timeout=120, propagate=False
            )
            assert second_delivery.state == "SUCCESS"
            record_property(
                "second_delivery_ack",
                await _wait_broker_ack(sender, worker, worlds[1]["job_id"]),
            )
            await _assert_terminal_retry(test_engine, worlds[1], second_value)
            assert len(backend_calls) == 2
            assert all(call["path"].endswith("/predict") for call in backend_calls)
            record_property("workspace_worker_runtime", worker.assert_runtime())
            record_property("celery_task_result", "success")
            record_property(
                "inference_peer",
                "deterministic HTTP protocol fixture; not real ML qualification",
            )
            for delivery in (first_delivery, second_delivery):
                await asyncio.to_thread(delivery.forget)
        except BaseException:
            if worker is not None:
                try:
                    record_property(
                        "workspace_worker_diagnostics", worker.capture_diagnostics()
                    )
                except Exception as diagnostic_error:
                    record_property(
                        "workspace_worker_diagnostics_error",
                        type(diagnostic_error).__name__,
                    )
            if sender is not None:
                for world in worlds:
                    result = sender.AsyncResult(str(world["job_id"]))
                    if worker is not None:
                        record_property(
                            f"celery_result_{world['job_id']}",
                            worker.redact(
                                f"state={result.state}; result={result.result!r}"
                            ),
                        )
            raise
        finally:
            try:
                if worker is not None:
                    worker.stop()
            finally:
                try:
                    if sender is not None:
                        try:
                            await asyncio.to_thread(
                                _delete_owned_queue, sender, worker.queue
                            )
                        finally:
                            sender.close()
                finally:
                    destroy_guide_scope(guide_scope)


def _delete_owned_queue(sender: Celery, queue: str) -> None:
    with sender.connection_for_write() as connection:
        connection.default_channel.queue_delete(queue)
