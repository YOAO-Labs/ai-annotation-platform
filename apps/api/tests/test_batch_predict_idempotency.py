"""v0.11.24 · 批量预标幂等：跳过已预标 / 覆盖历史预标。

- endpoint 层：predict_mode 透传给 batch_predict.delay；skip_predicted 下进度条分母排除已预标 task。
- service 层：clean_task_predictions(task_ids) 删 AI 预标、保留人工标注、归零 total_predictions
  （overwrite 模式 worker 调用的清理核心；全 worker e2e 因无 ML backend mock 暂不覆盖）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BatchStatus
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.async_job import AsyncJob
from app.services.batch import BatchService
from tests.conftest import create_registry_with_pool


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_explicit_preannotation_retries_reuse_job_and_reject_changed_scope(
    httpx_client, super_admin, db_session, monkeypatch
):
    from app.workers.tasks import batch_predict

    owner, token = super_admin
    project, backend, _batch, tasks = await _seed(db_session, owner.id)
    dispatch = Mock(
        side_effect=lambda **kwargs: SimpleNamespace(
            id=kwargs.get("task_id", str(uuid.uuid4()))
        )
    )
    monkeypatch.setattr(batch_predict, "apply_async", dispatch)
    headers = {**_bearer(token), "Idempotency-Key": f"predict-{uuid.uuid4()}"}
    body = {"ml_backend_id": str(backend.id), "task_ids": [str(tasks[0].id)]}
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    first = await httpx_client.post(endpoint, headers=headers, json=body)
    assert first.status_code == 200, first.text
    job_id = uuid.UUID(first.json()["job_id"])
    job = await db_session.get(AsyncJob, job_id)
    assert job.payload["scope"] == {"task_ids": [str(tasks[0].id)]}
    assert dispatch.call_args.kwargs["args"][2] == [str(tasks[0].id)]
    assert dispatch.call_args.kwargs["kwargs"]["async_job_id"] == str(job_id)
    assert dispatch.call_args.kwargs["task_id"] == str(job_id)
    retried = await httpx_client.post(endpoint, headers=headers, json=body)
    assert retried.json()["job_id"] == str(job_id)
    assert dispatch.call_count == 2
    job.status = "running"
    await db_session.commit()
    running = await httpx_client.post(endpoint, headers=headers, json=body)
    assert running.json()["job_id"] == str(job_id)
    assert dispatch.call_count == 2
    conflict = await httpx_client.post(
        endpoint, headers=headers, json={**body, "task_ids": [str(tasks[1].id)]}
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_explicit_preannotation_rechecks_task_and_batch_scope_before_dispatch(
    httpx_client, super_admin, db_session, monkeypatch
):
    from app.workers.tasks import batch_predict

    owner, token = super_admin
    project, backend, batch, tasks = await _seed(db_session, owner.id)
    foreign_project, _other_backend, _other_batch, foreign_tasks = await _seed(
        db_session, owner.id
    )
    assert foreign_project.id != project.id
    dispatch = Mock()
    monkeypatch.setattr(batch_predict, "apply_async", dispatch)
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    body = {"ml_backend_id": str(backend.id), "task_ids": [str(tasks[0].id)]}
    foreign = await httpx_client.post(
        endpoint,
        headers=_bearer(token),
        json={**body, "task_ids": [str(foreign_tasks[0].id)]},
    )
    assert foreign.status_code == 422
    for ids in ([], [str(uuid.uuid4()) for _ in range(201)]):
        invalid_selection = await httpx_client.post(
            endpoint, headers=_bearer(token), json={**body, "task_ids": ids}
        )
        assert invalid_selection.status_code == 422, invalid_selection.text
    for state in ["review", "completed"]:
        tasks[0].status = state
        await db_session.commit()
        response = await httpx_client.post(endpoint, headers=_bearer(token), json=body)
        assert response.status_code == 409, response.text
    tasks[0].status = "pending"
    batch.admin_locked = True
    await db_session.commit()
    locked = await httpx_client.post(endpoint, headers=_bearer(token), json=body)
    assert locked.status_code == 409, locked.text
    dispatch.assert_not_called()


async def _seed(db: AsyncSession, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-{suffix}",
        name=f"idem-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
        ai_enabled=True,
    )
    db.add(proj)
    await db.flush()

    backend, pool = await create_registry_with_pool(
        db,
        name="g-sam2",
        url=f"http://test-{suffix}/",
        is_interactive=True,
        state="connected",
    )
    proj.ml_backend_pool_id = pool.id
    # v0.19.0 ADR-0044 · 预标校验项目「已启用」, 为该 backend 建启用关联
    db.add(ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True))
    await db.flush()

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id=f"B-{suffix}",
        name="b1",
        status=BatchStatus.ACTIVE,
    )
    db.add(batch)
    await db.flush()

    tasks = []
    for i in range(3):
        t = Task(
            id=uuid.uuid4(),
            project_id=proj.id,
            batch_id=batch.id,
            display_id=f"T-{suffix}-{i}",
            file_name=f"img{i}.jpg",
            file_path=f"items/{suffix}-{i}.jpg",
            file_type="image",
            status="pending",
        )
        db.add(t)
        tasks.append(t)
    await db.commit()
    return proj, backend, batch, tasks


@pytest.fixture
def _mock_celery(monkeypatch):
    captured: dict = {}

    class _FakeJob:
        id = "fake-job-uuid"

    # v0.19.5 · 派发改用 apply_async(args=, kwargs=, queue=)。
    def _fake_apply_async(args=None, kwargs=None, queue=None, **_extra):
        captured["args"] = tuple(args or ())
        captured["kwargs"] = kwargs or {}
        captured["queue"] = queue
        return _FakeJob()

    from app.workers import tasks as worker_tasks

    monkeypatch.setattr(worker_tasks.batch_predict, "apply_async", _fake_apply_async)
    return captured


@pytest.mark.asyncio
async def test_predict_mode_defaults_skip_predicted(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch, _ = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={"ml_backend_id": str(backend.id), "batch_id": str(batch.id)},
    )
    assert resp.status_code == 200, resp.text
    assert _mock_celery["kwargs"]["predict_mode"] == "skip_predicted"


@pytest.mark.asyncio
async def test_predict_mode_overwrite_forwarded(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch, _ = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "predict_mode": "overwrite",
        },
    )
    assert resp.status_code == 200, resp.text
    assert _mock_celery["kwargs"]["predict_mode"] == "overwrite"


@pytest.mark.asyncio
async def test_skip_hint_excludes_predicted(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch, tasks = await _seed(db_session, owner.id)
    # 一个 task 标记已预标
    tasks[0].total_predictions = 1
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={"ml_backend_id": str(backend.id), "batch_id": str(batch.id)},
    )
    assert resp.status_code == 200, resp.text
    # skip 模式：3 个 pending 里排除 1 个已预标 → hint=2
    assert resp.json()["total_tasks"] == 2


@pytest.mark.asyncio
async def test_clean_task_predictions_removes_ai_keeps_manual(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    proj, backend, batch, tasks = await _seed(db_session, owner.id)
    t = tasks[0]
    t.total_predictions = 1
    db_session.add_all(
        [
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=proj.id,
                source="manual",
                class_name="cat",
                geometry={"x": 1, "y": 1, "w": 2, "h": 2},
                is_active=True,
            ),
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=proj.id,
                source="prediction_based",
                class_name="dog",
                geometry={"x": 3, "y": 3, "w": 4, "h": 4},
                is_active=True,
            ),
            Prediction(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=proj.id,
                result={"shapes": []},
                source="ml_backend",
            ),
        ]
    )
    await db_session.flush()

    counts = await BatchService(db_session).clean_task_predictions([t.id])
    assert counts["predictions"] == 1
    assert counts["ai_annotations_deactivated"] == 1

    # 人工标注保留 active，AI 标注软删
    manual_active = (
        await db_session.execute(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.task_id == t.id,
                Annotation.source == "manual",
                Annotation.is_active.is_(True),
            )
        )
    ).scalar()
    ai_active = (
        await db_session.execute(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.task_id == t.id,
                Annotation.source == "prediction_based",
                Annotation.is_active.is_(True),
            )
        )
    ).scalar()
    assert manual_active == 1
    assert ai_active == 0

    pred_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.task_id == t.id)
        )
    ).scalar()
    assert pred_count == 0

    # total_predictions 归 0；仍有存活人工标注 → is_labeled True
    row = (
        await db_session.execute(
            select(Task.total_predictions, Task.is_labeled).where(Task.id == t.id)
        )
    ).one()
    assert row.total_predictions == 0
    assert row.is_labeled is True
