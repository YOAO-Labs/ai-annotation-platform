"""v0.10.49 · batch_predict worker 写入单测（async_jobs 单一真值）.

v0.10.49 收敛后 prediction_jobs 专表已删，worker 直接以 async_jobs 为工作状态。
覆盖：
1. _mark_job_failed 把 running async_job 翻成 failed + 写 error_message
2. _mark_job_failed 跳过已 completed async_job 不覆盖
3. _run_batch 把每条 PredictionResult.meta.total_cost 累加进 async_job.result
4. _run_batch 把请求 params 合并进 /predict context
5. _BatchPredictTask.on_failure 调用 _mark_job_failed (mock 验证 dispatch 路径)

AsyncJob ORM / celery_task_id 反查由 test_async_jobs.py 覆盖，本文件不重复。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.notification import Notification
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project


def test_retryable_request_context_has_hard_size_limit():
    from app.workers.tasks import _retryable_request_context

    small = {"type": "text", "text": "car"}
    assert _retryable_request_context(small) == small
    assert _retryable_request_context({"text": "x" * (8 * 1024)}) is None


async def _seed_project_and_backend(
    db: AsyncSession, owner_id: uuid.UUID
) -> tuple[Project, MLBackendRegistry]:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-{suffix}",
        name=f"job-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()

    # v0.19.0 ADR-0044 · backend 现为全局注册项; worker 直接 db.get(registry), 无需项目启用关联
    backend = MLBackendRegistry(
        id=uuid.uuid4(),
        name="g-sam2",
        url=f"http://test-{suffix}/",
        is_interactive=True,
        state="connected",
    )
    db.add(backend)
    await db.flush()
    return proj, backend


def _passthrough_engine_and_factory(db_session: AsyncSession):
    """让 worker 内部 create_async_engine / async_sessionmaker 复用 db_session,
    避免开新 engine 对测试 SAVEPOINT 不可见。"""

    class _PassThroughEngine:
        async def dispose(self):
            pass

    class _PassThroughSessionFactory:
        def __init__(self, *_a, **_kw):
            pass

        def __call__(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return db_session

                async def __aexit__(self_inner, *args):
                    return False

            return _Ctx()

    return (lambda *_a, **_kw: _PassThroughEngine()), _PassThroughSessionFactory


@pytest.mark.asyncio
async def test_mark_job_failed_updates_running_row(
    db_session: AsyncSession, monkeypatch, super_admin
):
    user, _ = super_admin
    proj, _backend = await _seed_project_and_backend(db_session, user.id)

    job = AsyncJob(
        kind="batch_predict",
        project_id=proj.id,
        user_id=user.id,
        celery_task_id="celery-fail",
        status=AsyncJobStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc),
        payload={"prompt": "hello"},
    )
    db_session.add(job)
    await db_session.flush()
    job_id = job.id

    from app.workers import tasks as worker_tasks  # noqa: F401

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._mark_job_failed("celery-fail", "kaboom: boom")

    fresh = await db_session.get(AsyncJob, job_id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.error_message == "kaboom: boom"
    assert fresh.completed_at is not None


@pytest.mark.asyncio
async def test_mark_job_failed_skips_already_completed(
    db_session: AsyncSession, monkeypatch, super_admin
):
    user, _ = super_admin
    proj, _backend = await _seed_project_and_backend(db_session, user.id)

    job = AsyncJob(
        kind="batch_predict",
        project_id=proj.id,
        user_id=user.id,
        celery_task_id="celery-already",
        status=AsyncJobStatus.COMPLETED.value,
        completed_at=datetime.now(timezone.utc),
        payload={"prompt": "hello"},
    )
    db_session.add(job)
    await db_session.flush()
    job_id = job.id

    from app.workers import tasks as worker_tasks

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._mark_job_failed("celery-already", "should be ignored")

    fresh = await db_session.get(AsyncJob, job_id)
    assert fresh is not None
    assert fresh.status == "completed"  # 不被覆盖
    assert fresh.error_message is None


@pytest.mark.asyncio
async def test_run_batch_accumulates_total_cost(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.10.49 · _run_batch 把每条 PredictionResult.meta.total_cost 累加进 async_job.result."""
    from app.db.models.task import Task
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)

    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-1",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="pending",
    )
    t2 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-2",
        file_name="b.jpg",
        file_path="http://x/b.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add_all([t1, t2])
    await db_session.flush()
    authority_marker = object()
    built_from: list[object] = []
    client_kwargs: list[dict] = []

    def build_authority(session_factory):
        built_from.append(session_factory)
        return authority_marker

    monkeypatch.setattr(
        "app.services.gpu_arbitration.dispatch.build_gpu_dispatch_context_factory",
        build_authority,
    )

    class _StubClient:
        def __init__(self, _backend, **_kwargs):
            self._backend = _backend
            client_kwargs.append(_kwargs)

        async def predict(self, tasks_payload, context=None):
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta={"total_cost": 0.0012},
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t1.id), str(t2.id)],
        prompt="x",
    )

    res = await db_session.execute(
        select(AsyncJob).where(
            AsyncJob.kind == "batch_predict", AsyncJob.project_id == proj.id
        )
    )
    job = res.scalar_one()
    assert job.status == "completed"
    assert job.result["success_count"] == 2
    assert len(built_from) == 1
    assert client_kwargs == [
        {
            "shadow_session_factory": built_from[0],
            "dispatch_context_factory": authority_marker,
        }
    ]
    # 0.0012 × 2 = 0.0024 (格式化到 4 位)
    assert job.result["total_cost"] == "0.0024"


@pytest.mark.asyncio
async def test_run_batch_all_failed_marks_job_failed(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """B-45 · batch_predict 全部子项失败时 job 落 failed（不再「失败也显示已完成」），
    且仍把 failed_count / failed_prediction_ids 写进 result 供失败重试链路使用。"""
    from app.db.models.task import Task
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)

    tasks = [
        Task(
            id=uuid.uuid4(),
            project_id=proj.id,
            display_id=f"T-FAIL-{i}",
            file_name=f"{i}.jpg",
            file_path=f"http://x/{i}.jpg",
            file_type="image",
            status="pending",
        )
        for i in range(2)
    ]
    db_session.add_all(tasks)
    await db_session.flush()

    class _FailingClient:
        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            raise RuntimeError("ml backend down")

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _FailingClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t.id) for t in tasks],
        prompt="x",
    )

    res = await db_session.execute(
        select(AsyncJob).where(
            AsyncJob.kind == "batch_predict", AsyncJob.project_id == proj.id
        )
    )
    job = res.scalar_one()
    assert job.status == AsyncJobStatus.FAILED.value
    assert job.result["success_count"] == 0
    assert job.result["failed_count"] == 2
    assert len(job.result["failed_prediction_ids"]) == 2
    assert "gpu_arbiter_failures" not in job.result
    assert job.error_message
    failed_rows = (
        (
            await db_session.execute(
                select(FailedPrediction).where(FailedPrediction.project_id == proj.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(failed_rows) == 2
    for row in failed_rows:
        request_context = row.extra["request_context"]
        assert request_context["type"] == "text"
        assert request_context["text"] == "x"
        assert request_context["output"] == "mask"


@pytest.mark.asyncio
async def test_run_batch_persists_stable_gpu_arbiter_failures(
    db_session: AsyncSession, monkeypatch, super_admin
):
    from app.db.models.prediction import FailedPrediction
    from app.db.models.task import Task
    from app.services.gpu_arbitration.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    tasks = [
        Task(
            id=uuid.uuid4(),
            project_id=proj.id,
            display_id=f"T-GPU-{i}",
            file_name=f"{i}.jpg",
            file_path=f"http://x/{i}.jpg",
            file_type="image",
            status="pending",
        )
        for i in range(2)
    ]
    db_session.add_all(tasks)
    await db_session.flush()

    class _RejectedClient:
        def __init__(self, _backend, **_kwargs):
            pass

        async def predict(self, tasks_payload, context=None):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
                message="backend busy",
                retry_after_s=4,
            )

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _RejectedClient, raising=True
    )
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(task.id) for task in tasks],
        prompt="x",
    )

    failures = list(
        (
            await db_session.execute(
                select(FailedPrediction).where(FailedPrediction.project_id == proj.id)
            )
        ).scalars()
    )
    assert len(failures) == 2
    assert {failure.error_type for failure in failures} == {
        "gpu_backend_concurrency_saturated"
    }
    assert {failure.message for failure in failures} == {"backend busy"}
    assert all(
        failure.extra["gpu_arbiter_error"]
        == {
            "error_code": "gpu_backend_concurrency_saturated",
            "status_code": 503,
            "retry_after_s": 4,
            "message": "backend busy",
        }
        for failure in failures
    )
    job = (
        (
            await db_session.execute(
                select(AsyncJob).where(
                    AsyncJob.kind == "batch_predict", AsyncJob.project_id == proj.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert job.result["gpu_arbiter_failures"] == [
        {
            "error_code": "gpu_backend_concurrency_saturated",
            "status_code": 503,
            "retry_after_s": 4,
            "count": 2,
        }
    ]


@pytest.mark.asyncio
async def test_run_batch_stops_on_cooperative_cancel(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.10.51 · batch_predict 在任务边界看到 cancel_requested 后落 cancelled。"""
    from app.db.models.task import Task
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)

    tasks = [
        Task(
            id=uuid.uuid4(),
            project_id=proj.id,
            display_id=f"T-CANCEL-{i}",
            file_name=f"{i}.jpg",
            file_path=f"http://x/{i}.jpg",
            file_type="image",
            status="pending",
        )
        for i in range(3)
    ]
    db_session.add_all(tasks)
    await db_session.flush()

    class _StubClient:
        calls = 0

        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            self.__class__.calls += 1
            if self.__class__.calls == 1:
                res = await db_session.execute(
                    select(AsyncJob).where(
                        AsyncJob.kind == "batch_predict",
                        AsyncJob.project_id == proj.id,
                    )
                )
                job = res.scalar_one()
                job.payload = {**(job.payload or {}), "cancel_requested": True}
                await db_session.flush()
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta={"total_cost": 0.0012},
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t.id) for t in tasks],
        prompt="x",
        user_id=str(user.id),
    )

    res = await db_session.execute(
        select(AsyncJob).where(
            AsyncJob.kind == "batch_predict", AsyncJob.project_id == proj.id
        )
    )
    job = res.scalar_one()
    assert _StubClient.calls == 1
    assert job.status == AsyncJobStatus.CANCELLED.value
    assert job.progress_pct == 33
    assert job.result["success_count"] == 1
    assert job.result["failed_count"] == 0
    assert job.result["done_count"] == 1
    assert job.result["skipped_count"] == 2
    assert job.result["cancelled_at_index"] == 1
    assert job.result["total_cost"] == "0.0012"

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.type for row in rows] == ["job.cancelled"]
    assert rows[0].payload["done_count"] == 1
    assert rows[0].payload["skipped_count"] == 2


@pytest.mark.asyncio
async def test_run_batch_stops_writing_after_actor_disabled_mid_run(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """推理是长耗时操作：账号在推理期间被停用时，本条任务的 Prediction
    写入必须被回滚，job 以 preannotation_actor_revoked 终止，不再继续后续任务。"""
    from app.db.models.prediction import Prediction
    from app.db.models.task import Task
    from app.db.models.user import User
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)

    tasks = [
        Task(
            id=uuid.uuid4(),
            project_id=proj.id,
            display_id=f"T-REVOKE-{i}",
            file_name=f"{i}.jpg",
            file_path=f"http://x/{i}.jpg",
            file_type="image",
            status="pending",
        )
        for i in range(2)
    ]
    db_session.add_all(tasks)
    await db_session.flush()

    class _StubClient:
        calls = 0

        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            self.__class__.calls += 1
            if self.__class__.calls == 1:
                # Mid-inference revocation: disable the initiating account.
                row = await db_session.get(User, user.id)
                row.is_active = False
                await db_session.flush()
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta=None,
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    # The worker's rollback path expires shared-session ORM state; capture ids
    # before running so post-run asserts stay plain column queries.
    proj_id = proj.id

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t.id) for t in tasks],
        prompt="x",
        user_id=str(user.id),
    )

    row = (
        await db_session.execute(
            select(AsyncJob.status, AsyncJob.error_message).where(
                AsyncJob.kind == "batch_predict",
                AsyncJob.project_id == proj_id,
            )
        )
    ).one()
    assert _StubClient.calls == 1
    assert row.status == AsyncJobStatus.FAILED.value
    assert row.error_message == "preannotation_actor_revoked"

    # The mid-run revocation rolls back the staged predictions; the second
    # task never runs, so nothing was written for this project.
    pred_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.project_id == proj_id)
        )
    ).scalar_one()
    assert pred_count == 0


@pytest.mark.asyncio
async def test_cancelled_batch_keeps_prior_gpu_arbiter_failure_summary(
    db_session: AsyncSession, monkeypatch, super_admin
):
    from app.db.models.task import Task
    from app.services.gpu_arbitration.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    tasks = [
        Task(
            id=uuid.uuid4(),
            project_id=proj.id,
            display_id=f"T-CANCEL-GPU-{i}",
            file_name=f"{i}.jpg",
            file_path=f"http://x/{i}.jpg",
            file_type="image",
            status="pending",
        )
        for i in range(2)
    ]
    db_session.add_all(tasks)
    await db_session.flush()

    class _RejectedClient:
        def __init__(self, _backend, **_kwargs):
            pass

        async def predict(self, tasks_payload, context=None):
            job = (
                (
                    await db_session.execute(
                        select(AsyncJob).where(
                            AsyncJob.kind == "batch_predict",
                            AsyncJob.project_id == proj.id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            job.payload = {**(job.payload or {}), "cancel_requested": True}
            await db_session.flush()
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.NOT_READY,
                message="ledger rebuilding",
            )

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _RejectedClient, raising=True
    )
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(task.id) for task in tasks],
        prompt="x",
    )

    job = (
        (
            await db_session.execute(
                select(AsyncJob).where(
                    AsyncJob.kind == "batch_predict", AsyncJob.project_id == proj.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert job.status == AsyncJobStatus.CANCELLED.value
    assert job.result["failed_count"] == 1
    assert job.result["skipped_count"] == 1
    assert job.result["gpu_arbiter_failures"] == [
        {
            "error_code": "gpu_arbiter_not_ready",
            "status_code": 503,
            "retry_after_s": None,
            "count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_run_batch_merges_params_into_context(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.10.38 · _run_batch 把请求 params 合并进 /predict context, 覆盖项目级阈值兜底."""
    from app.db.models.task import Task
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    proj.box_threshold = 0.35
    proj.text_threshold = 0.25
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-P1",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(t1)
    await db_session.flush()

    captured: dict = {}

    class _StubClient:
        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            captured["context"] = context
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta={},
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t1.id)],
        prompt="cars",
        params={"box_threshold": 0.7, "sam_variant": "large", "ignored": None},
    )

    ctx = captured["context"]
    assert ctx["box_threshold"] == 0.7  # params 覆盖项目级 0.35
    assert ctx["text_threshold"] == 0.25  # 未被 params 覆盖, 保留项目级兜底
    assert ctx["sam_variant"] == "large"  # params 透传
    assert "ignored" not in ctx  # None 值被过滤


@pytest.mark.asyncio
async def test_run_batch_passes_model_id_and_task_type_into_context(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.14.9 · 协议 v2: 无 prompt 的纯图片 OCR task 也写 context type / model_id。"""
    from app.db.models.task import Task
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-OCR1",
        file_name="doc.png",
        file_path="http://x/doc.png",
        file_type="image",
        status="pending",
    )
    db_session.add(t1)
    await db_session.flush()

    captured: dict = {}

    class _StubClient:
        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            captured["context"] = context
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta={},
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    # 无 prompt: 纯图片 OCR, 仅靠 task_type / model_id 起 context
    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t1.id)],
        task_type="ocr",
        model_id="pp-ocrv4",
    )

    ctx = captured["context"]
    assert ctx is not None
    assert ctx["type"] == "ocr"
    assert ctx["model_id"] == "pp-ocrv4"
    # 纯 OCR 无文本 prompt, 不应混入 text/output 键
    assert "text" not in ctx


# v0.18.12 删除 test_run_batch_task_type_overrides_text_type_with_prompt:
# 原契约「带 prompt 时 task_type 覆盖默认的 'text' type」在 v0.18.12 model-first
# 重构后失效——文本路径 (prompt 非空) 现在直接 return type=text, task_type 不再
# 覆盖。新拓扑下文本路径专属 gsam2/sam3 开放词表 (task=segmentation/detection),
# OCR/doc_layout 走 flat 路径 (无 prompt),由 test_predict_context_builder.py 的
# test_flat_task_type_override_for_ocr 覆盖等价行为。


@pytest.mark.asyncio
async def test_run_batch_pure_text_prompt_unchanged_without_v2_args(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.14.9 · 回归: 无 model_id / task_type 时纯文本 prompt 现状不变 (type=text)。"""
    from app.db.models.task import Task
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-TXT1",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(t1)
    await db_session.flush()

    captured: dict = {}

    class _StubClient:
        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            captured["context"] = context
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta={},
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t1.id)],
        prompt="cars",
    )

    ctx = captured["context"]
    assert ctx["type"] == "text"
    assert ctx["text"] == "cars"
    assert "model_id" not in ctx


@pytest.mark.asyncio
async def test_delete_backend_blocked_by_running_batch_predict(
    db_session: AsyncSession, super_admin
):
    """v0.10.49 · MLBackendService.delete 改读 async_jobs(payload.ml_backend_id)
    判断是否有 running batch_predict，有则拒删。"""
    from app.services.ml_backend import MLBackendDeleteBlocked, MLBackendService

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)

    job = AsyncJob(
        kind="batch_predict",
        project_id=proj.id,
        user_id=user.id,
        status=AsyncJobStatus.RUNNING.value,
        payload={"ml_backend_id": str(backend.id)},
    )
    db_session.add(job)
    await db_session.flush()

    with pytest.raises(MLBackendDeleteBlocked):
        await MLBackendService(db_session).delete(backend.id)

    # 把 job 翻成终态后可删
    job.status = AsyncJobStatus.COMPLETED.value
    await db_session.flush()
    assert await MLBackendService(db_session).delete(backend.id) is True


@pytest.mark.asyncio
async def test_run_batch_workbench_scope_runs_in_progress_task(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """Issue #121 · workbench 单题允许 in_progress + 本人编辑锁, 且不被 pending 过滤掉。"""
    from datetime import datetime, timedelta, timezone

    from app.db.models.task import Task
    from app.db.models.task_lock import TaskLock
    from app.services.ml_client import PredictionResult
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-WB-1",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="in_progress",
    )
    db_session.add(t1)
    await db_session.flush()
    db_session.add(
        TaskLock(
            task_id=t1.id,
            user_id=user.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.flush()

    predicted: list[list[dict]] = []

    class _StubClient:
        def __init__(self, _backend, **_kwargs):
            self._backend = _backend

        async def predict(self, tasks_payload, context=None):
            predicted.append(tasks_payload)
            return [
                PredictionResult(
                    task_id=tasks_payload[0]["id"],
                    result=[],
                    score=0.9,
                    model_version="stub-v1",
                    inference_time_ms=10,
                    meta={},
                )
            ]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", _StubClient, raising=True
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await worker_tasks._run_batch(
        project_id=str(proj.id),
        ml_backend_id=str(backend.id),
        task_ids=[str(t1.id)],
        prompt="x",
        user_id=str(user.id),
        predict_mode="overwrite",
        execution_scope="workbench",
    )

    assert len(predicted) == 1
    job = (
        await db_session.execute(
            select(AsyncJob).where(
                AsyncJob.kind == "batch_predict", AsyncJob.project_id == proj.id
            )
        )
    ).scalar_one()
    assert job.status == "completed"
    assert job.result["success_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploading", "review", "completed"])
async def test_run_batch_workbench_scope_rejects_non_editable_status(
    status, db_session: AsyncSession, monkeypatch, super_admin
):
    """review #121 · workbench 白名单外状态 (uploading / 终态) 一律拒绝。"""
    from app.db.models.task import Task
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id=f"T-WB-{status}",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status=status,
    )
    db_session.add(t1)
    await db_session.flush()

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    with pytest.raises(ValueError, match="not editable"):
        await worker_tasks._run_batch(
            project_id=str(proj.id),
            ml_backend_id=str(backend.id),
            task_ids=[str(t1.id)],
            prompt="x",
            user_id=str(user.id),
            execution_scope="workbench",
        )


@pytest.mark.asyncio
async def test_run_batch_workbench_scope_rejects_foreign_lock_alongside_own(
    db_session: AsyncSession, monkeypatch, super_admin, annotator
):
    """review #121 · 同 task 多行锁时, 他人锁不能被本人更新的锁掩盖。"""
    from datetime import datetime, timedelta, timezone

    from app.db.models.task import Task
    from app.db.models.task_lock import TaskLock
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    other, _ = annotator
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-WB-LOCK",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="in_progress",
    )
    db_session.add(t1)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            TaskLock(
                task_id=t1.id, user_id=other.id, expire_at=now + timedelta(minutes=5)
            ),
            TaskLock(
                task_id=t1.id, user_id=user.id, expire_at=now + timedelta(minutes=10)
            ),
        ]
    )
    await db_session.flush()

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    with pytest.raises(ValueError, match="locked for editing"):
        await worker_tasks._run_batch(
            project_id=str(proj.id),
            ml_backend_id=str(backend.id),
            task_ids=[str(t1.id)],
            prompt="x",
            user_id=str(user.id),
            execution_scope="workbench",
        )


@pytest.mark.asyncio
async def test_run_batch_bulk_scope_rejects_in_progress_task(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """Issue #121 · bulk 路径仍拒绝非 pending 任务 (防止批量前置条件退化)。"""
    from app.db.models.task import Task
    from app.workers import tasks as worker_tasks

    user, _ = super_admin
    proj, backend = await _seed_project_and_backend(db_session, user.id)
    t1 = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id="T-BULK-1",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="in_progress",
    )
    db_session.add(t1)
    await db_session.flush()

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    with pytest.raises(ValueError, match="no longer pending"):
        await worker_tasks._run_batch(
            project_id=str(proj.id),
            ml_backend_id=str(backend.id),
            task_ids=[str(t1.id)],
            prompt="x",
            user_id=str(user.id),
        )


def test_batch_predict_task_on_failure_dispatches_mark_helper(monkeypatch):
    """_BatchPredictTask.on_failure 同步调用 _mark_job_failed (asyncio.run 包裹)."""
    from app.workers import tasks as worker_tasks

    captured: dict = {}

    async def _stub_mark(celery_task_id: str, error_message: str):
        captured["celery_task_id"] = celery_task_id
        captured["error_message"] = error_message

    monkeypatch.setattr(worker_tasks, "_mark_job_failed", _stub_mark)
    monkeypatch.setattr(worker_tasks, "_publish_progress", lambda *a, **kw: None)

    task = worker_tasks._BatchPredictTask()
    task.on_failure(
        ValueError("oops"),
        "celery-task-id-xyz",
        ("project-uuid",),
        {},
        None,
    )

    assert captured.get("celery_task_id") == "celery-task-id-xyz"
    assert "ValueError" in captured.get("error_message", "")
    assert "oops" in captured.get("error_message", "")
