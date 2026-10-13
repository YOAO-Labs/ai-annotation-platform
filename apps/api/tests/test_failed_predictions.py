"""v0.8.6 F6 · 失败预测列表与重试端点单测。

覆盖：
- GET /admin/failed-predictions 分页 + 角色守卫
- POST /admin/failed-predictions/{id}/retry 投递 Celery task；retry_count>=3 返 409
- 非管理员 403
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.notification import Notification
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.task import Task


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID) -> Project:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-FP-{suffix}",
        name=f"fp-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()
    return proj


async def _seed_task(db: AsyncSession, project_id: uuid.UUID) -> Task:
    suffix = uuid.uuid4().hex[:8]
    t = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"T-FP-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status="in_progress",
    )
    db.add(t)
    await db.flush()
    return t


async def _seed_backend(db: AsyncSession, project_id: uuid.UUID) -> MLBackendRegistry:
    # v0.19.0 ADR-0044 · backend 上提为全局注册项 (url unique); 列表/重试按 ml_backend_id
    # join / db.get registry, 无需项目启用关联。
    b = MLBackendRegistry(
        id=uuid.uuid4(),
        name="bk",
        url=f"http://example/bk-{uuid.uuid4().hex[:8]}",
        is_interactive=True,
    )
    db.add(b)
    await db.flush()
    return b


async def _seed_failed(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    backend_id: uuid.UUID,
    retry_count: int = 0,
    error_type: str = "TIMEOUT",
    message: str = "boom",
) -> FailedPrediction:
    fp = FailedPrediction(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=backend_id,
        error_type=error_type,
        message=message,
        retry_count=retry_count,
    )
    db.add(fp)
    await db.flush()
    return fp


async def test_list_failed_predictions_basic_fields(
    httpx_client, super_admin, db_session
):
    """两条 failed → 列表 total=2，关键字段（backend_name / project_name / retry_count）正确。"""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp1 = await _seed_failed(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        backend_id=backend.id,
        message="first",
    )
    fp2 = await _seed_failed(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        backend_id=backend.id,
        retry_count=1,
        message="second",
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.get(
        "/api/v1/admin/failed-predictions?page=1&page_size=10", headers=headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    by_id = {item["id"]: item for item in data["items"]}
    assert str(fp1.id) in by_id and str(fp2.id) in by_id
    assert by_id[str(fp2.id)]["retry_count"] == 1
    assert by_id[str(fp1.id)]["retry_count"] == 0
    for item in data["items"]:
        assert item["backend_name"] == "bk"
        assert item["project_name"] == proj.name
        assert item["task_display_id"] == task.display_id


async def test_list_failed_predictions_requires_manager(httpx_client, annotator):
    _, token = annotator
    resp = await httpx_client.get(
        "/api/v1/admin/failed-predictions", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_retry_failed_prediction_queues_celery_and_returns_202(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    # v0.19.5 起 retry 改 apply_async(queue=...) 以走设备感知路由(原 .delay() 静态落 ml/gpu)。
    with patch(
        "app.workers.predictions_retry.retry_failed_prediction.apply_async"
    ) as mock_apply:
        resp = await httpx_client.post(
            f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["failed_id"] == str(fp.id)
    mock_apply.assert_called_once()
    call_kwargs = mock_apply.call_args.kwargs
    assert call_kwargs["args"] == [str(fp.id), str(user.id)]
    # 未自报 device 的 backend 保守落 gpu(ml)队列。
    assert call_kwargs["queue"] == "ml"

    repeated = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
    )
    assert repeated.status_code == 409
    mock_apply.assert_called_once()


async def test_retry_blocked_when_max_exceeded(httpx_client, super_admin, db_session):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        backend_id=backend.id,
        retry_count=3,  # 已到上限
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
    )
    assert resp.status_code == 409, resp.text
    assert "Max retries" in resp.text


async def test_retry_404_for_unknown_id(httpx_client, super_admin):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    fake = uuid.uuid4()
    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fake}/retry", headers=headers
    )
    assert resp.status_code == 404


def _passthrough_session_factory(db_session: AsyncSession):
    class _Ctx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    return lambda: _Ctx()


async def test_retry_worker_tracks_success_in_async_jobs(
    db_session, super_admin, monkeypatch
):
    from app.services.ml_client import PredictionResult
    from app.workers import predictions_retry as retry_worker

    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    fp.extra = {"request_context": {"type": "ocr", "model_id": "ocr-e2e"}}
    await db_session.commit()
    session_factory = _passthrough_session_factory(db_session)
    authority_marker = object()
    built_from: list[object] = []

    def build_authority(received_factory):
        built_from.append(received_factory)
        return authority_marker

    monkeypatch.setattr(
        "app.services.gpu_arbitration.dispatch.build_gpu_dispatch_context_factory",
        build_authority,
    )

    monkeypatch.setattr(
        "app.services.storage.resolve_task_url",
        lambda received_task: f"http://minio.test/{received_task.file_name}",
    )

    async def fake_predict(self, tasks_payload, context=None):
        assert self._shadow_session_factory is session_factory
        assert self._dispatch_context_factory is authority_marker
        assert tasks_payload[0]["file_path"] == "http://minio.test/x.jpg"
        assert context == {"type": "ocr", "model_id": "ocr-e2e"}
        return [
            PredictionResult(
                task_id=tasks_payload[0]["id"],
                result=[],
                score=0.95,
                model_version="retry-v1",
                inference_time_ms=12,
            )
        ]

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.predict", fake_predict)

    result = await retry_worker._do_retry_with_factory(
        session_factory, str(fp.id), str(user.id)
    )

    assert result["status"] == "succeeded"
    assert built_from == [session_factory]
    assert result["prediction_id"]
    jobs = (
        (
            await db_session.execute(
                select(AsyncJob).where(AsyncJob.kind == "prediction_retry")
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == AsyncJobStatus.COMPLETED.value
    assert job.user_id == user.id
    assert job.project_id == proj.id
    assert job.payload["failed_prediction_id"] == str(fp.id)
    assert job.payload["task_display_id"] == task.display_id
    assert job.payload["ml_backend_name"] == backend.name
    assert job.result["success_count"] == 1
    assert job.result["failed_count"] == 0

    assert await db_session.get(FailedPrediction, fp.id) is None
    notifications = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    types = [row.type for row in notifications]
    assert "failed_prediction.retry.started" in types
    assert "job.completed" in types
    assert "failed_prediction.retry.succeeded" not in types


async def test_retry_worker_tracks_backend_failure_in_async_jobs(
    db_session, super_admin, monkeypatch
):
    from app.workers import predictions_retry as retry_worker

    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    async def fake_predict(self, tasks_payload):
        raise RuntimeError("backend down")

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.predict", fake_predict)

    result = await retry_worker._do_retry_with_factory(
        _passthrough_session_factory(db_session), str(fp.id), str(user.id)
    )

    assert result["status"] == "failed"
    assert result["reason"] == "backend down"
    assert "gpu_arbiter_error" not in result
    job = (
        (
            await db_session.execute(
                select(AsyncJob).where(AsyncJob.kind == "prediction_retry")
            )
        )
        .scalars()
        .one()
    )
    assert job.status == AsyncJobStatus.FAILED.value
    assert job.error_message == "backend down"
    assert job.result["success_count"] == 0
    assert job.result["failed_count"] == 1
    assert "gpu_arbiter_error" not in job.result

    await db_session.refresh(fp)
    assert fp.retry_count == 1
    notifications = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    types = [row.type for row in notifications]
    assert "failed_prediction.retry.started" in types
    assert "job.failed" in types
    assert "failed_prediction.retry.failed" not in types


async def test_retry_worker_preserves_gpu_arbiter_failure_in_job_and_source(
    db_session, super_admin, monkeypatch
):
    from app.services.gpu_arbitration.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )
    from app.workers import predictions_retry as retry_worker

    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session,
        project_id=proj.id,
        task_id=task.id,
        backend_id=backend.id,
        error_type="ORIGINAL",
        message="original failure",
    )
    await db_session.commit()

    async def fake_predict(self, tasks_payload):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.DRAIN_TIMEOUT,
            message="victim still busy",
            retry_after_s=6,
        )

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.predict", fake_predict)

    result = await retry_worker._do_retry_with_factory(
        _passthrough_session_factory(db_session), str(fp.id), str(user.id)
    )

    expected = {
        "error_code": "gpu_drain_timeout",
        "status_code": 503,
        "retry_after_s": 6,
        "message": "victim still busy",
    }
    assert result["status"] == "failed"
    assert result["reason"] == "victim still busy"
    assert result["gpu_arbiter_error"] == expected
    job = (
        (
            await db_session.execute(
                select(AsyncJob).where(AsyncJob.kind == "prediction_retry")
            )
        )
        .scalars()
        .one()
    )
    assert job.status == AsyncJobStatus.FAILED.value
    assert job.result["gpu_arbiter_error"] == expected
    await db_session.refresh(fp)
    assert fp.error_type == "ORIGINAL"
    assert fp.message == "original failure"
    assert fp.retry_count == 1
    assert fp.extra["last_gpu_arbiter_error"] == expected


async def test_retry_requires_manager(httpx_client, annotator, db_session, super_admin):
    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    _, token = annotator
    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ─── v0.8.8 · dismiss / restore ──────────────────────────────────────────────


async def test_dismiss_marks_failed_prediction_and_audit_logged(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/dismiss", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "dismissed"
    assert body["dismissed_at"] is not None

    # 默认列表不再返回该行
    list_resp = await httpx_client.get(
        "/api/v1/admin/failed-predictions", headers=headers
    )
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert all(i["id"] != str(fp.id) for i in items)

    # include_dismissed=true 时回归
    list_resp2 = await httpx_client.get(
        "/api/v1/admin/failed-predictions?include_dismissed=true", headers=headers
    )
    assert list_resp2.status_code == 200
    by_id = {i["id"]: i for i in list_resp2.json()["items"]}
    assert str(fp.id) in by_id
    assert by_id[str(fp.id)]["dismissed_at"] is not None


async def test_dismiss_blocks_retry(httpx_client, super_admin, db_session):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/dismiss", headers=headers
    )

    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
    )
    assert resp.status_code == 409
    assert "dismissed" in resp.text.lower()


async def test_restore_clears_dismissed_at(httpx_client, super_admin, db_session):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/dismiss", headers=headers
    )

    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/restore", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "restored"
    assert body["dismissed_at"] is None

    # 默认列表又能看到
    list_resp = await httpx_client.get(
        "/api/v1/admin/failed-predictions", headers=headers
    )
    items = list_resp.json()["items"]
    assert any(i["id"] == str(fp.id) for i in items)


async def test_dismiss_is_idempotent(httpx_client, super_admin, db_session):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    r1 = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/dismiss", headers=headers
    )
    r2 = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/dismiss", headers=headers
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # dismissed_at 第二次调用不应被刷新
    assert r1.json()["dismissed_at"] == r2.json()["dismissed_at"]


async def test_dismiss_requires_manager(
    httpx_client, annotator, db_session, super_admin
):
    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    _, token = annotator
    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/dismiss", headers=headers
    )
    assert resp.status_code == 403
