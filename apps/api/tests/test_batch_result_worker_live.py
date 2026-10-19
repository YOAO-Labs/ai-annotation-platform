"""Real Celery redelivery after a durable batch result and prefork child loss."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4

import pytest
from celery import Celery
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("RUN_BATCH_RESULT_WORKER_LIVE_TEST") != "1"
        or os.environ.get("AAP_WORKTREE_MODE") != "test",
        reason="requires explicit verifier-owned batch result worker acceptance",
    ),
]


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"batch result worker acceptance requires {name}", pytrace=False)
    return value


def _read_json_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _same_database_target(left, right) -> bool:
    loopback = {"localhost", "127.0.0.1", "::1"}
    same_host = left.host == right.host or (
        left.host in loopback and right.host in loopback
    )
    return same_host and left.port == right.port and left.database == right.database


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.05)
    return not _pid_exists(pid)


async def _wait_for_capture(path: Path, worker, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if worker.returncode is not None:
            pytest.fail(
                f"owned Celery worker exited before raw capture (exit {worker.returncode})",
                pytrace=False,
            )
        await asyncio.sleep(0.1)
    pytest.fail("worker did not reach the durable raw-result barrier", pytrace=False)


async def _wait_for_worker_ready(path: Path, worker, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if worker.returncode is not None:
            pytest.fail(
                f"owned Celery worker exited during startup (exit {worker.returncode})",
                pytrace=False,
            )
        await asyncio.sleep(0.1)
    pytest.fail("owned Celery worker did not become ready", pytrace=False)


async def _wait_for_completed_job(session_factory, job_id, timeout: float) -> None:
    from app.db.models.async_job import AsyncJob

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with session_factory() as db:
            job = await db.get(AsyncJob, job_id)
            if job is not None and job.status == "completed":
                return
            if job is not None and job.status in {"failed", "cancelled"}:
                pytest.fail(f"recovered batch Job reached {job.status}", pytrace=False)
        await asyncio.sleep(0.2)
    pytest.fail(
        "redelivered batch Job did not complete before its deadline", pytrace=False
    )


async def _cleanup_database(
    test_engine, *, actor_id, project_id, backend_id, task_id, job_id
):
    from app.db.models.async_job import AsyncJob
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        await db.execute(
            update(AsyncJob)
            .where(AsyncJob.id == job_id, AsyncJob.status.in_(["pending", "running"]))
            .values(status="failed", completed_at=datetime.now(timezone.utc))
        )
        prediction_ids = select(Prediction.id).where(Prediction.task_id == task_id)
        failure_ids = select(FailedPrediction.id).where(
            FailedPrediction.task_id == task_id
        )
        await db.execute(
            delete(PredictionMeta).where(
                PredictionMeta.prediction_id.in_(prediction_ids)
                | PredictionMeta.failed_prediction_id.in_(failure_ids)
            )
        )
        await db.execute(delete(Prediction).where(Prediction.task_id == task_id))
        await db.execute(
            delete(FailedPrediction).where(FailedPrediction.task_id == task_id)
        )
        await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
        await db.execute(delete(Task).where(Task.id == task_id))
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(
            delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
        )
        await db.execute(delete(User).where(User.id == actor_id))
        await db.commit()


def _delete_owned_queue(sender: Celery, queue: str) -> None:
    with sender.connection_for_write() as connection:
        connection.default_channel.queue_delete(queue)


async def test_prefork_loss_resumes_captured_result_without_second_inference(
    test_engine, test_db_url, tmp_path
):
    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_results import (
        BatchResultExecution,
        preannotation_request_digest,
    )
    from app.db.models.async_job import AsyncJob
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User
    from app.workers.tasks import batch_predict

    assert batch_predict.acks_late is True
    assert batch_predict.reject_on_worker_lost is True
    owner_url = make_url(test_db_url)
    legacy_url = make_url(_required_env("AAP_MODULE_DATA_LEGACY_RUNTIME_URL"))
    result_url = make_url(_required_env("AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"))
    for role_url, expected_user in (
        (legacy_url, "legacy_runtime"),
        (result_url, "preannotation_result_client"),
    ):
        assert role_url.username == expected_user
        assert _same_database_target(role_url, owner_url), (
            "verified worker role does not target the test database"
        )

    actor_id, project_id, backend_id, task_id, job_id = (uuid4() for _ in range(5))
    suffix = uuid4().hex[:12]
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(backend_id),
        task_ids=[str(task_id)],
        prompt="Classify the supplied text.",
        output_mode="mask",
        user_id=str(actor_id),
        predict_mode="append",
        execution_scope="bulk",
        async_job_id=str(job_id),
        task_type="text",
    )
    digest = preannotation_request_digest(request)
    legacy_database_url = legacy_url.set(
        drivername="postgresql+asyncpg"
    ).render_as_string(hide_password=False)
    result_database_url = result_url.set(
        drivername="postgresql+asyncpg"
    ).render_as_string(hide_password=False)
    result_runtime_url = result_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    legacy_engine = create_async_engine(legacy_database_url, echo=False)
    result_engine = create_async_engine(result_database_url, echo=False)
    legacy_sessions = async_sessionmaker(legacy_engine, expire_on_commit=False)
    result_sessions = async_sessionmaker(result_engine, expire_on_commit=False)
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    queue = f"batch_result_recovery_{uuid4().hex}"
    broker_url = _required_env("CELERY_BROKER_URL")
    sender = Celery(
        "batch-result-worker-acceptance", broker=broker_url, backend=broker_url
    )
    captured_path = tmp_path / "captured.json"
    model_calls_path = tmp_path / "model-calls.jsonl"
    claims_path = tmp_path / "claims.jsonl"
    deliveries_path = tmp_path / "deliveries.jsonl"
    native_events_path = tmp_path / "native-events.jsonl"
    worker_ready_path = tmp_path / "worker-ready"
    worker_log_path = tmp_path / "celery-worker.log"
    environment = {
        **os.environ,
        "DATABASE_URL": legacy_database_url,
        "MIGRATION_DATABASE_URL": "",
        "TEST_DATABASE_URL": "",
        "PREANNOTATION_RESULTS_DATABASE_URL": result_runtime_url,
        "PREANNOTATION_RESULTS_DATABASE_URL_FILE": "",
        "AAP_TEST_BATCH_RESULT_CAPTURED_PATH": str(captured_path),
        "AAP_TEST_BATCH_RESULT_MODEL_CALLS_PATH": str(model_calls_path),
        "AAP_TEST_BATCH_RESULT_CLAIMS_PATH": str(claims_path),
        "AAP_TEST_BATCH_RESULT_DELIVERIES_PATH": str(deliveries_path),
        "AAP_TEST_BATCH_RESULT_NATIVE_EVENTS_PATH": str(native_events_path),
        "AAP_TEST_BATCH_RESULT_WORKER_READY_PATH": str(worker_ready_path),
    }
    worker = None
    worker_log = None
    delivery = None
    seeded = False
    succeeded = False
    try:
        async with owner_sessions.begin() as db:
            db.add(
                User(
                    id=actor_id,
                    email=f"batch-result-{suffix}@example.invalid",
                    name="Batch result acceptance",
                    password_hash="acceptance-only",
                    role="project_admin",
                    is_active=True,
                )
            )
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    display_id=f"P-BR-{suffix}",
                    name="Batch result worker acceptance",
                    type_label="OCR",
                    type_key="ocr",
                    data_type="image",
                    owner_id=actor_id,
                    ai_enabled=True,
                )
            )
            db.add(
                MLBackendRegistry(
                    id=backend_id,
                    name=f"batch-result-{suffix}",
                    url=f"http://unused-{suffix}.invalid/",
                    state="connected",
                    is_interactive=True,
                )
            )
            await db.flush()
            db.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    display_id=f"T-BR-{suffix}",
                    file_name="acceptance.txt",
                    file_path="text://batch-result-acceptance",
                    file_type="text",
                    status="pending",
                )
            )
            await db.flush()
            db.add(
                AsyncJob(
                    id=job_id,
                    kind="batch_predict",
                    project_id=project_id,
                    user_id=actor_id,
                    status="pending",
                    celery_task_id=str(job_id),
                    payload={
                        "execution_scope": "bulk",
                        "task_ids": [str(task_id)],
                        "scope": {"task_ids": [str(task_id)]},
                        "preannotation_request_digest": digest,
                    },
                )
            )
        seeded = True
        worker_log = worker_log_path.open("wb")
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.workers.celery_app:celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--prefetch-multiplier=1",
            f"--queues={queue}",
            f"--hostname={queue}@%h",
            "--include=tests.fixtures.preannotation_result_worker_peer",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=INFO",
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=worker_log,
            stderr=worker_log,
        )
        await _wait_for_worker_ready(worker_ready_path, worker, timeout=60)
        delivery = await asyncio.to_thread(
            sender.send_task,
            "app.workers.tasks.batch_predict",
            kwargs={
                "project_id": str(project_id),
                "ml_backend_id": str(backend_id),
                "task_ids": [str(task_id)],
                "prompt": request.prompt,
                "output_mode": request.output_mode,
                "user_id": str(actor_id),
                "predict_mode": request.predict_mode,
                "execution_scope": request.execution_scope,
                "async_job_id": str(job_id),
                "task_type": request.task_type,
            },
            queue=queue,
            task_id=str(job_id),
        )
        captured = await _wait_for_capture(captured_path, worker, timeout=90)
        assert captured["raw_complete"] is True
        assert captured["state"] == "captured"
        assert captured["ordinal"] == 1 and captured["result_count"] == 1
        killed_child_pid = captured["pid"]
        assert worker.returncode is None and _pid_exists(killed_child_pid)
        os.kill(killed_child_pid, signal.SIGKILL)
        assert await asyncio.to_thread(_wait_for_pid_exit, killed_child_pid, 8)

        await _wait_for_completed_job(legacy_sessions, job_id, timeout=45)

        model_calls = _read_json_lines(model_calls_path)
        claims = _read_json_lines(claims_path)
        deliveries = _read_json_lines(deliveries_path)
        assert model_calls == [{"pid": killed_child_pid, "task_id": str(task_id)}]
        assert any(
            item["pid"] != killed_child_pid
            and item["task_id"] == str(job_id)
            and item["retries"] == 0
            for item in deliveries
        ), "broker did not redeliver the original late-ack task after child loss"
        assert len(claims) >= 2
        assert claims[0]["pid"] == killed_child_pid
        assert any(
            claim["generation"] > claims[0]["generation"]
            and claim["pid"] != killed_child_pid
            for claim in claims[1:]
        ), "redelivery did not take over with a new execution generation"

        async with owner_sessions() as db:
            job = await db.get(AsyncJob, job_id)
            assert job is not None and job.status == "completed"
            assert job.result["success_count"] == 1
            assert job.result["failed_count"] == 0
            assert job.result["total_cost"] == "0.0042"
            predictions = list(
                (
                    await db.scalars(
                        select(Prediction).where(Prediction.task_id == task_id)
                    )
                ).all()
            )
            assert len(predictions) == 1
            prediction = predictions[0]
            assert prediction.model_version == "worker-recovery-fixture"
            assert (
                await db.scalar(
                    select(func.count(PredictionMeta.id)).where(
                        PredictionMeta.prediction_id == prediction.id
                    )
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count(FailedPrediction.id)).where(
                        FailedPrediction.task_id == task_id
                    )
                )
                == 0
            )

        inspection = BatchResultExecution(
            request,
            legacy_sessions,
            execution_id=str(job_id),
            executor_session_factory=result_sessions,
        )
        page = await inspection.call(
            "read_batch_result_execution",
            leased=False,
            after_ordinal=0,
            limit=100,
        )
        assert len(page.members) == 1 and page.members[0].state == "applied"
        saved = await inspection.read_result(1, stage="prepared")
        assert saved.result is not None and saved.result.state == "applied"
        assert saved.payload is not None and saved.payload.receipt is not None
        assert saved.payload.receipt.id == prediction.id
        assert saved.payload.receipt.created_at == prediction.created_at
        assert saved.payload.receipt.tool_unit_id == prediction.tool_unit_id
        succeeded = True
    finally:
        if worker is not None and worker.returncode is None:
            try:
                worker.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(worker.wait(), timeout=10)
            except TimeoutError:
                worker.kill()
                await worker.wait()
        owned_pids = {item["pid"] for item in _read_json_lines(claims_path)}
        if captured_path.exists():
            owned_pids.add(json.loads(captured_path.read_text(encoding="utf-8"))["pid"])
        closed_children = {}
        for pid in owned_pids:
            closed = await asyncio.to_thread(_wait_for_pid_exit, pid, 5)
            if not closed:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                closed = await asyncio.to_thread(_wait_for_pid_exit, pid, 5)
            closed_children[pid] = closed
        worker_closed = worker is None or worker.returncode is not None
        if worker_log is not None:
            worker_log.close()
        if not succeeded and worker_log_path.exists():
            diagnostic = worker_log_path.read_text(encoding="utf-8", errors="replace")
            for role_url in (legacy_url, result_url):
                if role_url.password:
                    diagnostic = diagnostic.replace(role_url.password, "<redacted>")
            print("Owned worker diagnostic:\n" + diagnostic[-20_000:])
            print("Native events:", _read_json_lines(native_events_path)[-40:])
            print("Model calls:", _read_json_lines(model_calls_path))
        cleanup_errors = []
        if delivery is not None:
            try:
                await asyncio.to_thread(delivery.forget)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        try:
            await asyncio.to_thread(_delete_owned_queue, sender, queue)
        except Exception as exc:
            cleanup_errors.append(type(exc).__name__)
        finally:
            sender.close()
            dispose_results = await asyncio.gather(
                legacy_engine.dispose(), result_engine.dispose(), return_exceptions=True
            )
            cleanup_errors.extend(
                type(result).__name__
                for result in dispose_results
                if isinstance(result, BaseException)
            )
            if seeded:
                await _cleanup_database(
                    test_engine,
                    actor_id=actor_id,
                    project_id=project_id,
                    backend_id=backend_id,
                    task_id=task_id,
                    job_id=job_id,
                )
        assert worker_closed, "owned Celery worker did not exit"
        assert all(closed_children.values()), (
            "owned Celery child survived worker shutdown"
        )
        assert not cleanup_errors, "owned acceptance resource cleanup failed"
