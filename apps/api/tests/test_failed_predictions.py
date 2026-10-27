"""v0.8.6 F6 · 失败预测列表与重试端点单测。

覆盖：
- GET /admin/failed-predictions 分页 + 角色守卫
- POST /admin/failed-predictions/{id}/retry 投递 Celery task；retry_count>=3 返 409
- 非管理员 403
"""

from __future__ import annotations

import uuid

import pytest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import attributes

from app.db.models.audit_log import AuditLog
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.notification import Notification
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.task import Task


async def _seed_project(
    db: AsyncSession, owner_id: uuid.UUID, *, data_type: str = "image"
) -> Project:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-FP-{suffix}",
        name=f"fp-{suffix}",
        type_label="image-det" if data_type == "image" else "scene-3d",
        type_key="image-det" if data_type == "image" else "scene-3d",
        data_type=data_type,
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()
    return proj


async def _seed_task(
    db: AsyncSession, project_id: uuid.UUID, *, file_type: str = "image"
) -> Task:
    suffix = uuid.uuid4().hex[:8]
    image = file_type == "image"
    t = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"T-FP-{suffix}",
        file_name="x.jpg" if image else "x.txt",
        file_path="/tmp/x.jpg" if image else "/tmp/x.txt",
        file_type=file_type,
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


@pytest.mark.parametrize("controlled", [False, True])
async def test_retry_failed_prediction_queues_celery_and_returns_202(
    httpx_client, super_admin, db_session, controlled
):
    user, token = super_admin
    # A legacy eligible retry is one without retained image/video evidence.
    proj = await _seed_project(db_session, user.id, data_type="scene")
    task = await _seed_task(db_session, proj.id, file_type="text")
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    if controlled:
        backend.health_meta = {
            "compute": {"effective_device": "cpu"},
            "capabilities": {},
        }
        fp.extra = {
            "request_context": {
                "model_id": "trained:" + str(uuid.uuid4()),
                "frozen_model_ref": {"version": "fixed"},
            }
        }
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
    assert call_kwargs["queue"] == ("ml.cpu" if controlled else "ml")

    repeated = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
    )
    assert repeated.status_code == 409
    mock_apply.assert_called_once()


async def test_retry_dispatch_uncertainty_keeps_durable_claim_without_spending_attempt(
    db_session, super_admin, monkeypatch
):
    from app.application.prediction_retry import SqlPredictionRetry
    from app.application.async_job_contracts import RetryCredentialAuthority
    from app.workers.prediction_retry_dispatch import CeleryPredictionRetryDispatcher
    from app.workers import predictions_retry

    user, token = super_admin
    project = await _seed_project(db_session, user.id, data_type="scene")
    task = await _seed_task(db_session, project.id, file_type="text")
    backend = await _seed_backend(db_session, project.id)
    fp = await _seed_failed(
        db_session, project_id=project.id, task_id=task.id, backend_id=backend.id
    )
    context = {"type": "text", "text": "car"}
    fp.extra = {"request_context": context}
    await db_session.commit()

    def publish(**kwargs):
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(
        predictions_retry.retry_failed_prediction, "apply_async", publish
    )
    await SqlPredictionRetry(
        db_session, dispatcher=CeleryPredictionRetryDispatcher()
    ).retry_prediction(
        failed_id=fp.id,
        actor_id=user.id,
        credential=RetryCredentialAuthority(
            actor_id=user.id, authorization=f"Bearer {token}"
        ),
    )

    await db_session.refresh(fp)
    assert fp.extra.get("retry_pending") is True
    assert uuid.UUID(fp.extra["retry_job_id"])
    assert fp.extra["request_context"] == context
    assert fp.retry_count == 0


async def test_retry_rejects_legacy_image_failure_without_retained_input(
    httpx_client, super_admin, db_session
):
    """An image failure without owner evidence must ask for a new preannotation."""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "prediction_original_requires_new_request"
    await db_session.refresh(fp)
    assert not (fp.extra or {}).get("retry_pending")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AsyncJob)
            .where(AsyncJob.kind == "prediction_retry")
        )
        == 0
    )


async def test_retry_binds_owner_authority_into_the_admitted_job_once(
    httpx_client, super_admin, db_session, monkeypatch
):
    """Admission writes the captured proof and snapshot into the native Job."""
    from app.services import async_job as async_job_svc
    from app.services.prediction_execution import create_snapshot

    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    snapshot = await create_snapshot(
        db_session,
        proj.id,
        {
            "execution_scope": "bulk",
            "execution_unit": "image",
            "stages": [
                {
                    "stage": 0,
                    "parent_stage": None,
                    "ml_backend_id": str(backend.id),
                    "model_id": None,
                }
            ],
            "prompt": None,
            "output_mode": "boxes",
        },
    )
    binding = {"task_id": str(task.id), "original": {"sha256": "a" * 64}}
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    fp.extra = {
        "original_input": binding,
        "execution_scope": "bulk",
        "execution_unit": "image",
        "execution_snapshot": {"id": str(snapshot.id), "digest": snapshot.digest},
    }
    fp.execution_snapshot_id = snapshot.id
    await db_session.commit()

    proof = {
        "schema": 1,
        "job_kind": "prediction_retry",
        "snapshot_id": str(snapshot.id),
        "snapshot_digest": snapshot.digest,
        "execution_scope": "bulk",
        "execution_unit": "image",
        "actor_id": str(user.id),
        "project_id": str(proj.id),
        "batch_id": None,
        "tasks": [binding],
    }
    captured = []

    async def capture(*_args, **_kwargs):
        captured.append(proof)
        return proof

    async def managed_binding(_db, selected):
        assert selected.id == task.id
        return binding["original"]

    bound = []

    async def bind_snapshot(_db, job, *, actor_id, snapshot_id):
        bound.append((job.id, actor_id, snapshot_id))
        attributes.set_committed_value(job, "execution_snapshot_id", snapshot_id)

    monkeypatch.setattr("app.services.prediction_references.capture_authority", capture)
    monkeypatch.setattr(
        "app.services.prediction_originals.managed_binding", managed_binding
    )
    monkeypatch.setattr(async_job_svc, "bind_execution_snapshot", bind_snapshot)

    headers = {"Authorization": f"Bearer {token}"}
    with patch(
        "app.workers.predictions_retry.retry_failed_prediction.apply_async"
    ) as mock_apply:
        resp = await httpx_client.post(
            f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
        )
        repeated = await httpx_client.post(
            f"/api/v1/admin/failed-predictions/{fp.id}/retry", headers=headers
        )

    assert resp.status_code == 202, resp.text
    assert repeated.status_code == 409
    assert mock_apply.call_count == 1
    assert len(captured) == 1 and bound and bound[0][1] == user.id
    assert bound[0][2] == snapshot.id
    jobs = (
        await db_session.scalars(
            select(AsyncJob).where(AsyncJob.kind == "prediction_retry")
        )
    ).all()
    assert len(jobs) == 1
    payload = jobs[0].payload
    assert payload["prediction_authority"] == proof
    assert "video_prediction_authority" not in payload


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
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


@pytest.mark.parametrize("controlled", [False, True])
async def test_retry_worker_tracks_success_in_async_jobs(
    db_session, super_admin, monkeypatch, controlled
):
    from aap_inference_contract.execution import ModelTaskResult
    from app.workers import predictions_retry as retry_worker
    from tests.inference_execution_test_ports import (
        patch_retry_execution,
        pipeline_output,
    )

    user, _ = super_admin
    proj = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, proj.id)
    backend = await _seed_backend(db_session, proj.id)
    fp = await _seed_failed(
        db_session, project_id=proj.id, task_id=task.id, backend_id=backend.id
    )
    frozen = {"model_version_id": str(uuid.uuid4()), "sha256": "a" * 64}
    request_context = (
        {
            "type": "detection",
            "model_id": "trained:" + frozen["model_version_id"],
            "frozen_model_ref": frozen,
        }
        if controlled
        else {"type": "ocr", "model_id": "ocr-e2e"}
    )
    fp.extra = {"request_context": request_context}
    await db_session.commit()
    session_factory = _passthrough_session_factory(db_session)

    monkeypatch.setattr(
        "app.services.storage.resolve_task_url",
        lambda received_task: f"http://minio.test/{received_task.file_path}",
    )

    async def fake_execute(task_id, _stages, _ports, _contexts, **_kwargs):
        return pipeline_output(
            [
                ModelTaskResult(
                    task_id=str(task_id),
                    result=(),
                    score=0.95,
                    model_version="retry-v1",
                    inference_time_ms=12,
                    meta={"retry_receipt": "preserved"},
                )
            ],
            instance_id=backend.id,
        )

    patch_retry_execution(monkeypatch, fake_execute)

    result = await retry_worker._do_retry_with_factory(
        session_factory, str(fp.id), str(user.id)
    )

    assert result["status"] == "succeeded", result
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

    db_session.expunge(fp)
    assert await db_session.get(FailedPrediction, fp.id) is None
    if controlled:
        from app.db.models.prediction import PredictionMeta

        meta = await db_session.scalar(
            select(PredictionMeta).where(
                PredictionMeta.prediction_id == uuid.UUID(result["prediction_id"])
            )
        )
        assert meta.extra["trained_model"] == frozen
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


async def test_retry_uses_retained_input_version_and_verifies_before_write(
    db_session, super_admin, monkeypatch
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from aap_inference_contract.execution import ModelTaskResult
    from app.services.prediction import PredictionService
    from app.workers import predictions_retry as retry_worker
    from tests.inference_execution_test_ports import (
        patch_retry_execution,
        pipeline_output,
    )

    user, _ = super_admin
    project = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, project.id)
    backend = await _seed_backend(db_session, project.id)
    failed = await _seed_failed(
        db_session,
        project_id=project.id,
        task_id=task.id,
        backend_id=backend.id,
    )
    input_id, origin_job_id = uuid.uuid4(), uuid.uuid4()
    digest = "d" * 64
    failed.extra = {
        "execution_scope": "workbench",
        "accepted_input": {
            "input_id": str(input_id),
            "job_id": str(origin_job_id),
            "task_id": str(task.id),
            "sha256": digest,
            "byte_size": 256,
        },
    }
    await db_session.commit()
    events = []
    version_url = "https://inputs.test/accepted/source?versionId=original-version"
    input_record = SimpleNamespace(
        input_id=input_id,
        job_id=origin_job_id,
        task_id=task.id,
        project_id=project.id,
        sha256=digest,
        byte_size=256,
    )
    reference = SimpleNamespace(input=input_record, url=version_url)

    class Gateway:
        async def reference(self, request):
            assert request.input_id == input_id
            assert request.job_id == origin_job_id
            return reference

        async def download(self, _reference, destination):
            destination.write_bytes(b"accepted bytes")
            events.append("download")

        async def verify_source(self, _request):
            events.append("verify-source")
            return input_record

    @asynccontextmanager
    async def fake_gateway(**_kwargs):
        yield Gateway()

    async def fake_execute(task_id, _stages, _ports, _contexts, **kwargs):
        events.append(("predict", kwargs["source_url"]))
        return pipeline_output(
            [
                ModelTaskResult(
                    task_id=str(task_id),
                    result=(),
                    score=0.95,
                    model_version="retry-v1",
                    inference_time_ms=12,
                    meta={"retry_receipt": "preserved"},
                )
            ],
            instance_id=backend.id,
        )

    patch_retry_execution(monkeypatch, fake_execute)

    original_apply = PredictionService.apply_prediction_result_bundle

    async def fake_apply(service, requests):
        request = requests[0]
        events.append(("write", request.write_scope.accepted_input_id))
        assert request.pipeline_extra["accepted_input"] == {
            "input_id": str(input_id),
            "job_id": str(origin_job_id),
            "task_id": str(task.id),
            "sha256": digest,
            "byte_size": 256,
        }
        return await original_apply(service, requests)

    async def lock_frozen_source(*_args, **_kwargs):
        return input_record

    monkeypatch.setattr(
        "app.services.accepted_inputs.lock_input_source", lock_frozen_source
    )
    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", fake_gateway)
    monkeypatch.setattr(PredictionService, "apply_prediction_result_bundle", fake_apply)
    monkeypatch.setattr(
        "app.services.storage.resolve_task_url",
        lambda _task: pytest.fail("frozen retry must not resolve the current Task URL"),
    )

    result = await retry_worker._do_retry_with_factory(
        _passthrough_session_factory(db_session), str(failed.id), str(user.id)
    )

    assert result["status"] == "succeeded", result
    assert events == [
        "download",
        "verify-source",
        ("predict", version_url),
        "verify-source",
        ("write", input_id),
    ]


@pytest.mark.parametrize(
    ("failure_point", "failure_code", "expected_events"),
    [
        ("reference", "input_expired", ["reference"]),
        ("download", "input_corrupt", ["reference", "download"]),
        ("verify", "source_changed", ["reference", "download", "verify"]),
    ],
)
async def test_permanent_frozen_retry_failure_uses_job_and_counter_path(
    db_session, super_admin, monkeypatch, failure_point, failure_code, expected_events
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from aap_assets_inputs.contract import InputFailure
    from app.workers import predictions_retry as retry_worker

    user, _ = super_admin
    project = await _seed_project(db_session, user.id)
    task = await _seed_task(db_session, project.id)
    backend = await _seed_backend(db_session, project.id)
    input_id, origin_job_id = uuid.uuid4(), uuid.uuid4()
    failed = await _seed_failed(
        db_session, project_id=project.id, task_id=task.id, backend_id=backend.id
    )
    failed.extra = {
        "retry_pending": True,
        "execution_scope": "workbench",
        "accepted_input": {
            "input_id": str(input_id),
            "job_id": str(origin_job_id),
            "task_id": str(task.id),
            "sha256": "e" * 64,
            "byte_size": 256,
        },
    }
    await db_session.commit()
    events = []

    class Gateway:
        async def reference(self, request):
            assert request.input_id == input_id
            assert request.job_id == origin_job_id
            events.append("reference")
            if failure_point == "reference":
                raise InputFailure(failure_code)
            return SimpleNamespace(
                input=SimpleNamespace(
                    input_id=input_id,
                    job_id=origin_job_id,
                    task_id=task.id,
                    project_id=project.id,
                    sha256="e" * 64,
                    byte_size=256,
                ),
                url="https://inputs.test/source?versionId=original-version",
            )

        async def download(self, _reference, _destination):
            events.append("download")
            if failure_point == "download":
                raise InputFailure(failure_code)

        async def verify_source(self, _request):
            events.append("verify")
            if failure_point == "verify":
                raise InputFailure(failure_code)

    @asynccontextmanager
    async def fake_gateway(**_kwargs):
        yield Gateway()

    async def unexpected_predict(*_args, **_kwargs):
        events.append("predict")
        pytest.fail("an expired frozen input must not fall back to Task.file_path")

    from tests.inference_execution_test_ports import patch_retry_execution

    patch_retry_execution(monkeypatch, unexpected_predict)

    async def lock_frozen_source(*_args, **_kwargs):
        return SimpleNamespace(job_id=origin_job_id)

    monkeypatch.setattr(
        "app.services.accepted_inputs.lock_input_source", lock_frozen_source
    )
    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", fake_gateway)
    monkeypatch.setattr(
        "app.services.storage.resolve_task_url",
        lambda _task: pytest.fail(
            "an expired frozen input must not resolve the current URL"
        ),
    )

    result = await retry_worker._do_retry_with_factory(
        _passthrough_session_factory(db_session), str(failed.id), str(user.id)
    )

    assert result["status"] == (
        "cancelled" if failure_code == "source_changed" else "failed"
    )
    assert result["reason"] == failure_code
    assert events == expected_events
    await db_session.refresh(failed)
    assert failed.retry_count == (0 if failure_code == "source_changed" else 1)
    assert "retry_pending" not in failed.extra
    job = (
        (
            await db_session.execute(
                select(AsyncJob).where(AsyncJob.kind == "prediction_retry")
            )
        )
        .scalars()
        .one()
    )
    assert job.status == (
        AsyncJobStatus.CANCELLED.value
        if failure_code == "source_changed"
        else AsyncJobStatus.FAILED.value
    )
    assert job.result["reason"] == failure_code


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

    from tests.inference_execution_test_ports import patch_retry_execution

    async def fake_execute(*_args, **_kwargs):
        raise RuntimeError("backend down")

    patch_retry_execution(monkeypatch, fake_execute)

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
    from aap_inference.gpu.contracts import (
        GPUArbiterDispatchError,
        GPUArbiterErrorCode,
    )
    from app.workers import predictions_retry as retry_worker
    from tests.inference_execution_test_ports import patch_retry_execution

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

    async def fake_execute(*_args, **_kwargs):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.DRAIN_TIMEOUT,
            message="victim still busy",
            retry_after_s=6,
        )

    patch_retry_execution(monkeypatch, fake_execute)

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
    repeated = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fp.id}/restore", headers=headers
    )
    assert repeated.status_code == 200
    assert repeated.json()["dismissed_at"] is None
    restore_audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "failed_prediction.restored",
                    AuditLog.target_id == str(fp.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(restore_audits) == 1

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
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "failed_prediction.dismissed",
                    AuditLog.target_id == str(fp.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


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
