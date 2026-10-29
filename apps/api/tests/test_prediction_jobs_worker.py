"""v0.10.49 · preannotation application 写入单测（async_jobs 单一真值）.

v0.10.49 收敛后 prediction_jobs 专表已删，application command 直接以 async_jobs 为工作状态。
覆盖：
1. Worker failure hook 把 running async_job 翻成 failed + 写 error_message
2. Worker failure hook 跳过已 completed async_job 不覆盖
3. Application command 把每条 PredictionResult.meta.total_cost 累加进 async_job.result

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
from app.workers import tasks as worker_tasks


async def _run_batch(**kwargs):
    """Exercise workflow behavior with legacy model stubs; admission is separate."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import _execute_preannotation
    from app.config import settings

    execution_id = kwargs.pop("celery_task_id", None)
    frame_dispatcher = kwargs.pop("frame_dispatcher", None)
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        return await _execute_preannotation(
            BatchPredictionRequest(**kwargs),
            execution_id=execution_id,
            frame_dispatcher=frame_dispatcher,
            SessionLocal=async_sessionmaker(engine, expire_on_commit=False),
        )
    finally:
        await engine.dispose()


def test_retryable_request_context_has_hard_size_limit():
    from app.services.prediction_retry import retryable_request_context

    small = {"type": "text", "text": "car"}
    assert retryable_request_context(small) == small
    assert retryable_request_context({"text": "x" * (8 * 1024)}) is None
    utf8_limit = {"text": "中" * 2727}
    assert retryable_request_context(utf8_limit) == utf8_limit
    assert retryable_request_context({"text": utf8_limit["text"] + "x"}) is None


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
@pytest.mark.parametrize("predict_mode", ["overwrite", "append"])
async def test_legacy_frame_execution_rejects_missing_retained_manifest_before_overwrite(
    db_session: AsyncSession, monkeypatch, super_admin, predict_mode
):
    from unittest.mock import AsyncMock

    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import run_preannotation
    from app.db.models.prediction import Prediction
    from app.db.models.task import Task

    user, _ = super_admin
    project, backend = await _seed_project_and_backend(db_session, user.id)
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id="T-LEGACY-VIDEO",
        file_name="video.mp4",
        file_path="mutable/video.mp4",
        file_type="video",
        status="pending",
        total_predictions=1,
    )
    db_session.add(task)
    await db_session.flush()
    previous = Prediction(task_id=task.id, project_id=project.id, result=[])
    db_session.add(previous)
    await db_session.flush()
    previous_id = previous.id
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)
    metadata = AsyncMock(
        side_effect=AssertionError("legacy input must not be re-resolved")
    )
    monkeypatch.setattr(
        "app.services.video_frame_service.build_context_from_task",
        metadata,
    )
    dispatch = AsyncMock()
    with pytest.raises(ValueError, match="requires a retained video manifest"):
        await run_preannotation(
            BatchPredictionRequest(
                project_id=str(project.id),
                user_id=str(user.id),
                ml_backend_id=str(backend.id),
                task_ids=[str(task.id)],
                execution_unit="frame",
                predict_mode=predict_mode,
            ),
            execution_id=None,
            frame_dispatcher=dispatch,
        )
    metadata.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert (
        await db_session.scalar(
            select(Prediction.id).where(Prediction.id == previous_id)
        )
        == previous_id
    )
    assert task.total_predictions == 1


@pytest.mark.asyncio
async def test_run_batch_accumulates_total_cost(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.10.49 · _run_batch 把每条 PredictionResult.meta.total_cost 累加进 async_job.result."""
    from app.application import preannotation_execution as execution
    from app.db.models.task import Task
    from aap_inference_contract.execution import ModelTaskResult
    from tests.inference_execution_test_ports import FakeModelExecutionPort

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

    def respond(_operation, task_input):
        return ModelTaskResult(
            task_id=task_input["id"],
            result=(),
            score=0.9,
            model_version="stub-v1",
            inference_time_ms=10,
            meta={"total_cost": 0.0012},
        )

    monkeypatch.setattr(
        execution,
        "_model_execution_port",
        lambda *_args, **_kwargs: FakeModelExecutionPort(respond),
    )

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await _run_batch(
        project_id=str(proj.id),
        user_id=str(user.id),
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
    # 0.0012 × 2 = 0.0024 (格式化到 4 位)
    assert job.result["total_cost"] == "0.0024"


@pytest.mark.asyncio
async def test_manifest_page_failure_after_success_marks_job_failed_with_progress(
    db_session: AsyncSession, monkeypatch, super_admin
):
    from types import SimpleNamespace

    from aap_assets_input_manifests.contract import ManifestScope
    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import (
        _PreparedAcceptedManifest,
        _execute_preannotation,
    )
    from app.db.models.prediction import Prediction
    from app.db.models.task import Task
    from app.services.labeling_prediction_contract import ApplyPredictionResultReceipt
    from app.services.prediction import PredictionService
    from aap_inference_contract.execution import ModelTaskResult
    from tests.inference_execution_test_ports import patch_batch_execution

    user, _ = super_admin
    project, backend = await _seed_project_and_backend(db_session, user.id)
    first_task, second_task = [
        Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id=f"T-MANIFEST-{index}",
            file_name=f"{index}.jpg",
            file_path=f"/legacy/{index}.jpg",
            file_type="image",
            status="pending",
        )
        for index in (1, 2)
    ]
    db_session.add_all([first_task, second_task])
    await db_session.flush()

    job_id = uuid.uuid4()
    digest = "c" * 64
    job = AsyncJob(
        id=job_id,
        kind="batch_predict",
        project_id=project.id,
        user_id=user.id,
        status=AsyncJobStatus.PENDING.value,
        payload={
            "batch_id": None,
            "execution_scope": "bulk",
            "total_tasks": 2,
            "accepted_input_manifest_protocol": "aap-assets-input-manifest/v1",
            "accepted_input_manifest_id": str(job_id),
            "accepted_input_manifest_digest": digest,
            "input_selection": {
                "batch_id": None,
                "task_ids": None,
                "predict_mode": "append",
            },
        },
    )
    db_session.add(job)
    await db_session.flush()

    input_id = uuid.uuid4()
    input_record = SimpleNamespace(
        input_id=input_id,
        job_id=job_id,
        task_id=first_task.id,
        project_id=project.id,
        actor_id=user.id,
        task_version=first_task.version,
        task_source_generation=first_task.media_source_generation or 1,
        dataset_item_id=first_task.dataset_item_id,
        task_source_snapshot="a" * 64,
        source_owner=SimpleNamespace(kind="task", id=first_task.id),
        source_generation=1,
        source_snapshot="b" * 64,
        status="ready",
        sha256="d" * 64,
        byte_size=6,
    )
    reference = SimpleNamespace(
        input=input_record,
        url="https://inputs.test/source?versionId=frozen-v1",
    )
    member = SimpleNamespace(
        ordinal=1,
        task_id=first_task.id,
        input=input_record,
        state="ready",
        error=None,
    )

    class Header:
        selected_count = 2
        candidate_count = 2
        sealed_digest = "e" * 64

        def model_dump(self, *, mode):
            return {"sealed": True, "mode": mode}

    header = Header()
    page_calls = []

    class InputGateway:
        async def ensure_reference(self, _request):
            return reference

        async def download(self, _reference, destination):
            destination.write_bytes(b"frozen")

        async def verify_source(self, _request):
            return input_record

    class ManifestGateway:
        inputs = InputGateway()

        async def page(self, _request):
            page_calls.append(True)
            if len(page_calls) > 1:
                raise RuntimeError("control page transport unavailable")
            return SimpleNamespace(
                manifest=header,
                cursor=0,
                next_cursor=1,
                members=[member],
            )

    scope = ManifestScope(job_id=job_id, actor_id=user.id, project_id=project.id)
    accepted_manifest = _PreparedAcceptedManifest(
        gateway=ManifestGateway(),
        scope=scope,
        header=header,
        selection_digest=digest,
    )
    request = BatchPredictionRequest(
        project_id=str(project.id),
        ml_backend_id=str(backend.id),
        task_ids=None,
        user_id=str(user.id),
        predict_mode="append",
        execution_scope="bulk",
        async_job_id=str(job_id),
        accepted_input_manifest_protocol="aap-assets-input-manifest/v1",
        accepted_input_manifest_id=str(job_id),
        accepted_input_manifest_digest=digest,
    )

    def respond(_operation, task_input):
        assert task_input["file_path"] == reference.url
        return ModelTaskResult(
            task_id=task_input["id"],
            result=(),
            score=0.9,
            model_version="frozen-v1",
            inference_time_ms=1,
            meta={},
        )

    patch_batch_execution(monkeypatch, respond, instance_id=backend.id)

    async def persist_success(self, write_request):
        assert write_request.accepted_input_job_id == job_id
        prediction = Prediction(
            task_id=write_request.task_id,
            project_id=write_request.project_id,
            ml_backend_id=write_request.ml_backend_id,
            result=write_request.result,
        )
        db_session.add(prediction)
        task = await db_session.get(Task, write_request.task_id)
        task.total_predictions = (task.total_predictions or 0) + 1
        await db_session.flush()
        return ApplyPredictionResultReceipt(
            id=prediction.id,
            created_at=datetime.now(timezone.utc),
            tool_unit_id="bbox",
        )

    monkeypatch.setattr(PredictionService, "apply_prediction_result", persist_success)

    _fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    await _execute_preannotation(
        request,
        execution_id=str(job_id),
        frame_dispatcher=None,
        SessionLocal=fake_factory(),
        accepted_manifest=accepted_manifest,
    )

    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.FAILED.value
    assert job.error_message == "manifest_page_unavailable"
    assert job.result["success_count"] == 1
    assert job.result["failed_count"] == 0
    assert job.result["last_completed_ordinal"] == 1
    assert job.result["unprocessed_count"] == 1
    assert job.result["unprocessed_failure_records"] == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == first_task.id)
        )
        == 1
    )
    assert len(page_calls) == 2


@pytest.mark.asyncio
async def test_run_batch_all_failed_marks_job_failed(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """B-45 · batch_predict 全部子项失败时 job 落 failed（不再「失败也显示已完成」），
    且仍把 failed_count / failed_prediction_ids 写进 result 供失败重试链路使用。"""
    from app.db.models.task import Task
    from tests.inference_execution_test_ports import patch_batch_execution

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

    def fail_execution(_operation, _task_input):
        raise RuntimeError("ml backend down")

    patch_batch_execution(monkeypatch, fail_execution, instance_id=backend.id)

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await _run_batch(
        project_id=str(proj.id),
        user_id=str(user.id),
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
    from aap_inference.gpu.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )

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

    from tests.inference_execution_test_ports import patch_batch_execution

    def reject_operation(_operation, _task_input):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            message="backend busy",
            retry_after_s=4,
        )

    patch_batch_execution(monkeypatch, reject_operation, instance_id=backend.id)
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await _run_batch(
        project_id=str(proj.id),
        user_id=str(user.id),
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
    from aap_inference_contract.execution import ModelTaskResult
    from tests.inference_execution_test_ports import patch_batch_execution

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

    model_calls = 0

    async def respond(_operation, task_input):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            res = await db_session.execute(
                select(AsyncJob).where(
                    AsyncJob.kind == "batch_predict",
                    AsyncJob.project_id == proj.id,
                )
            )
            job = res.scalar_one()
            job.payload = {**(job.payload or {}), "cancel_requested": True}
            await db_session.flush()
        return ModelTaskResult(
            task_id=task_input["id"],
            result=(),
            score=0.9,
            model_version="stub-v1",
            inference_time_ms=10,
            meta={"total_cost": 0.0012},
        )

    patch_batch_execution(monkeypatch, respond, instance_id=backend.id)

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await _run_batch(
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
    assert model_calls == 1
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
@pytest.mark.parametrize("denial", ["actor_disabled", "write_denied"])
async def test_run_batch_stops_writing_after_actor_disabled_mid_run(
    db_session: AsyncSession, monkeypatch, super_admin, denial
):
    """推理是长耗时操作：账号在推理期间被停用时，本条任务的 Prediction
    写入必须被回滚，job 以 preannotation_actor_revoked 终止，不再继续后续任务。"""
    from app.db.models.prediction import Prediction
    from app.db.models.task import Task
    from app.db.models.user import User
    from aap_inference_contract.execution import ModelTaskResult
    from tests.inference_execution_test_ports import patch_batch_execution
    from app.services.labeling_prediction_contract import ApplyPredictionResultError
    from app.services.prediction import PredictionService

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

    model_calls = 0

    async def respond(_operation, task_input):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1 and denial == "actor_disabled":
            row = await db_session.get(User, user.id)
            row.is_active = False
            await db_session.flush()
            await db_session.commit()
        return ModelTaskResult(
            task_id=task_input["id"],
            result=(),
            score=0.9,
            model_version="stub-v1",
            inference_time_ms=10,
            meta=None,
        )

    patch_batch_execution(monkeypatch, respond, instance_id=backend.id)
    if denial == "write_denied":

        async def reject_write(_self, _request):
            raise ApplyPredictionResultError(
                code="labeling_permission_changed",
                status_code=403,
                detail="项目写入权限已变化",
            )

        monkeypatch.setattr(PredictionService, "apply_prediction_result", reject_write)

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    # The worker's rollback path expires shared-session ORM state; capture ids
    # before running so post-run asserts stay plain column queries.
    proj_id = proj.id

    await _run_batch(
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
    assert model_calls == 1
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
    from aap_inference.gpu.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )

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

    from tests.inference_execution_test_ports import patch_batch_execution

    async def reject_after_cancel(_operation, _task_input):
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
        await db_session.commit()
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="ledger rebuilding",
        )

    patch_batch_execution(monkeypatch, reject_after_cancel, instance_id=backend.id)
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await _run_batch(
        project_id=str(proj.id),
        user_id=str(user.id),
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
async def test_delete_backend_blocked_by_running_batch_predict(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """v0.10.49 · 拓扑删除服务读 async_jobs(payload.ml_backend_id) 判断是否有
    running batch_predict，有则拒删；job 落终态后同一条链路真正删除 Registry。"""
    from types import SimpleNamespace

    from app.application.ml_registry_deletion import delete_registry
    from app.services.inference_topology_data import InferenceTopologyData
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

    class QuiescentRoutingOwner:
        """Routing-owner port reporting the topology this Registry actually has."""

        async def quiescence(self, registry_id):
            topology = InferenceTopologyData(db_session)
            pool = await topology.pool_for_registry(registry_id)
            member = None
            if pool is not None:
                for row in await topology.pool_members(pool.id):
                    if row.registry_id == registry_id:
                        member = row
                        break
            return SimpleNamespace(
                status="ok",
                topology=SimpleNamespace(
                    member_id=member.id if member is not None else None,
                    pool_id=pool.id if pool is not None else None,
                    routing_generation=(
                        pool.routing_generation if pool is not None else None
                    ),
                    traffic_state=(
                        member.traffic_state if member is not None else None
                    ),
                ),
            )

    monkeypatch.setattr(
        "app.services.ml_routing.safety.RoutingAuthority",
        lambda: QuiescentRoutingOwner(),
    )

    # The Native guard raises inside the caller transaction, so scope it to a
    # SAVEPOINT to keep the rest of this test's seeded world usable.
    with pytest.raises(MLBackendDeleteBlocked):
        async with db_session.begin_nested():
            await delete_registry(db_session, backend.id)
    assert await MLBackendService(db_session).get_registry(backend.id) is not None

    # 把 job 翻成终态后可删
    job.status = AsyncJobStatus.COMPLETED.value
    await db_session.flush()
    assert await delete_registry(db_session, backend.id) is True
    assert await MLBackendService(db_session).get_registry(backend.id) is None


@pytest.mark.asyncio
async def test_run_batch_workbench_scope_runs_in_progress_task(
    db_session: AsyncSession, monkeypatch, super_admin
):
    """Issue #121 · workbench 单题允许 in_progress + 本人编辑锁, 且不被 pending 过滤掉。"""
    from datetime import datetime, timedelta, timezone

    from app.db.models.task import Task
    from app.db.models.task_lock import TaskLock
    from aap_inference_contract.execution import ModelTaskResult
    from tests.inference_execution_test_ports import patch_batch_execution

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

    predicted: list[tuple[dict, ...]] = []

    def respond(operation, task_input):
        predicted.append(tuple(operation.task_inputs))
        return ModelTaskResult(
            task_id=task_input["id"],
            result=(),
            score=0.9,
            model_version="stub-v1",
            inference_time_ms=10,
            meta={},
        )

    patch_batch_execution(monkeypatch, respond, instance_id=backend.id)

    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)

    await _run_batch(
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
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "assignment",
        "foreign_lock",
        "completed",
        "disabled",
        "draft",
        "assignment_before_dispatch",
        "forged_scope",
    ],
)
async def test_workbench_annotator_execution_rechecks_authority_at_write(
    change, db_session: AsyncSession, monkeypatch, super_admin, annotator
):
    from datetime import timedelta

    from fastapi import HTTPException

    from aap_inference_contract.execution import ModelTaskResult
    from app.db.models.prediction import Prediction
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.db.models.task_lock import TaskLock
    from tests.factory import create_membership
    from tests.inference_execution_test_ports import patch_batch_execution

    owner, _ = super_admin
    actor, _ = annotator
    project, backend = await _seed_project_and_backend(db_session, owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role="annotator"
    )
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-{uuid.uuid4().hex[:8]}",
        name="assigned",
        status="active",
        annotator_id=actor.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="in_progress",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=actor.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    job = AsyncJob(
        kind="batch_predict",
        project_id=project.id,
        user_id=actor.id,
        status="pending",
        payload={
            "execution_scope": "workbench",
            "batch_id": None,
            "task_ids": [str(task.id)],
        },
    )
    db_session.add(job)
    await db_session.flush()
    project_id, task_id, job_id = project.id, task.id, job.id
    if change == "assignment_before_dispatch":
        batch.annotator_id = owner.id
    elif change == "forged_scope":
        job.payload = {**job.payload, "execution_scope": "bulk"}
    await db_session.flush()

    model_calls = 0

    async def respond(_operation, task_input):
        nonlocal model_calls
        model_calls += 1
        if change == "assignment":
            batch.annotator_id = owner.id
        elif change == "foreign_lock":
            db_session.add(
                TaskLock(
                    task_id=task_id,
                    user_id=owner.id,
                    expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
            )
        elif change == "completed":
            task.status = "completed"
        elif change == "disabled":
            actor.is_active = False
        elif change == "draft":
            batch.status = "draft"
        await db_session.flush()
        await db_session.commit()
        return ModelTaskResult(
            task_id=task_input["id"],
            result=(),
            score=0.9,
            model_version="stub-v1",
            inference_time_ms=10,
            meta={},
        )

    patch_batch_execution(monkeypatch, respond, instance_id=backend.id)
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)
    kwargs = dict(
        project_id=str(project_id),
        ml_backend_id=str(backend.id),
        task_ids=[str(task_id)],
        user_id=str(actor.id),
        async_job_id=str(job_id),
        prompt="x",
        execution_scope="workbench",
    )
    if change in {"assignment_before_dispatch", "forged_scope"}:
        with pytest.raises((ValueError, HTTPException)):
            await _run_batch(**kwargs)
        assert model_calls == 0
        return
    await _run_batch(**kwargs)
    job = await db_session.get(AsyncJob, job_id, populate_existing=True)
    predictions = await db_session.scalar(
        select(func.count())
        .select_from(Prediction)
        .where(Prediction.task_id == task_id)
    )
    if change == "none":
        assert job.status == "completed"
        assert job.result["success_count"] == 1
        assert predictions == 1
    else:
        assert predictions == 0
        assert job.status == "failed" or job.result["failed_count"] == 1


@pytest.mark.asyncio
async def test_bulk_annotator_cannot_use_workbench_authority(
    db_session: AsyncSession, monkeypatch, super_admin, annotator
):
    from app.db.models.task import Task
    from tests.factory import create_membership

    owner, _ = super_admin
    actor, _ = annotator
    project, backend = await _seed_project_and_backend(db_session, owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role="annotator"
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        assignee_id=actor.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="a.jpg",
        file_path="http://x/a.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    import sqlalchemy.ext.asyncio as sa_async

    monkeypatch.setattr(sa_async, "create_async_engine", fake_engine)
    monkeypatch.setattr(sa_async, "async_sessionmaker", fake_factory)
    with pytest.raises(ValueError, match="actor is unavailable"):
        await _run_batch(
            project_id=str(project.id),
            ml_backend_id=str(backend.id),
            task_ids=[str(task.id)],
            user_id=str(actor.id),
            execution_scope="bulk",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploading", "review", "completed"])
async def test_run_batch_workbench_scope_rejects_non_editable_status(
    status, db_session: AsyncSession, monkeypatch, super_admin
):
    """review #121 · workbench 白名单外状态 (uploading / 终态) 一律拒绝。"""
    from app.db.models.task import Task

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
        await _run_batch(
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
        await _run_batch(
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
        await _run_batch(
            project_id=str(proj.id),
            ml_backend_id=str(backend.id),
            task_ids=[str(t1.id)],
            prompt="x",
            user_id=str(user.id),
        )
