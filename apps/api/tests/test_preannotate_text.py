"""v0.9.5 · POST /projects/{id}/preannotate 文本批量预标触发端点。

覆盖参数校验四条主路径：
1. backend 不存在 → 404
2. batch 不存在 / 跨项目 → 404
3. batch 状态非 active / 未分派 draft → 400/409（issue #124 放行未分派 draft）
4. happy path → 202 风格响应携带 channel + total_tasks

不跑 Celery（mock batch_predict.delay）。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BatchStatus
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from tests.conftest import create_registry_with_pool


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    batch_status: str = BatchStatus.ACTIVE,
    annotator_id: uuid.UUID | None = None,
    reviewer_id: uuid.UUID | None = None,
):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-{suffix}",
        name=f"preanno-{suffix}",
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
        status=batch_status,
        annotator_id=annotator_id,
        reviewer_id=reviewer_id,
    )
    db.add(batch)
    await db.flush()

    for i in range(3):
        db.add(
            Task(
                id=uuid.uuid4(),
                project_id=proj.id,
                batch_id=batch.id,
                display_id=f"T-{suffix}-{i}",
                file_name=f"img{i}.jpg",
                file_path=f"items/{suffix}-{i}.jpg",
                file_type="image",
                status="pending",
            )
        )
    await db.commit()
    return proj, backend, batch


@pytest.fixture
def _mock_celery(monkeypatch):
    """把 batch_predict.delay 替换成假 job 对象，避免真触发 Celery。"""
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
async def test_preannotate_backend_not_found(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, _, _ = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={"ml_backend_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_preannotate_batch_not_found(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, _ = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 404
    assert "batch" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_preannotate_batch_wrong_status(
    httpx_client, super_admin, db_session, _mock_celery
):
    """已进入人工流程的批次 (非 active / 非 draft) 仍按状态拒绝。"""
    owner, token = super_admin
    proj, backend, batch = await _seed(
        db_session, owner.id, batch_status=BatchStatus.ANNOTATING
    )
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "person",
        },
    )
    assert resp.status_code == 400
    assert "active" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_preannotate_unassigned_draft_batch_allowed(
    httpx_client, super_admin, db_session, _mock_celery
):
    """issue #124 · 未分派标注员与质检员的 draft 批次可直接批量预标。

    管理员无需为了跑 AI 先分派人员或提前激活批次; worker 复校验同源放行。
    """
    owner, token = super_admin
    proj, backend, batch = await _seed(
        db_session, owner.id, batch_status=BatchStatus.DRAFT
    )
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "person",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["total_tasks"] == 3
    assert _mock_celery["kwargs"]["batch_id"] == str(batch.id)


@pytest.mark.asyncio
async def test_preannotate_assigned_draft_batch_rejected(
    httpx_client, super_admin, annotator, db_session, _mock_celery
):
    """issue #124 · 已分派人员 (标注员或质检员) 的 draft 批次拒绝批量预标。"""
    owner, token = super_admin
    anno_user, _ = annotator
    proj, backend, batch = await _seed(
        db_session,
        owner.id,
        batch_status=BatchStatus.DRAFT,
        annotator_id=anno_user.id,
    )
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "person",
        },
    )
    assert resp.status_code == 409
    assert "assigned" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_preannotate_assigned_draft_reviewer_rejected(
    httpx_client, super_admin, reviewer, db_session, _mock_celery
):
    """issue #124 · 仅分派了质检员的 draft 批次同样拒绝。"""
    owner, token = super_admin
    reviewer_user, _ = reviewer
    proj, backend, batch = await _seed(
        db_session,
        owner.id,
        batch_status=BatchStatus.DRAFT,
        reviewer_id=reviewer_user.id,
    )
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "person",
        },
    )
    assert resp.status_code == 409
    assert "assigned" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_preannotate_explicit_tasks_in_unassigned_draft_allowed(
    httpx_client, super_admin, db_session, _mock_celery
):
    """issue #124 · 显式选择未分派 draft 批次内的 pending 任务可预标 (数据管理入口)。"""
    owner, token = super_admin
    proj, backend, batch = await _seed(
        db_session, owner.id, batch_status=BatchStatus.DRAFT
    )
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch.id)))
        .scalars()
        .all()
    )
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(t.id) for t in tasks],
            "prompt": "person",
        },
    )
    assert resp.status_code == 200, resp.text
    assert _mock_celery["args"][2] == sorted((str(t.id) for t in tasks))


@pytest.mark.asyncio
async def test_preannotate_explicit_tasks_in_assigned_draft_rejected(
    httpx_client, super_admin, annotator, db_session, _mock_celery
):
    """issue #124 · 显式选择已分派 draft 批次的任务仍被拒 (批量路径)。"""
    owner, token = super_admin
    anno_user, _ = annotator
    proj, backend, batch = await _seed(
        db_session,
        owner.id,
        batch_status=BatchStatus.DRAFT,
        annotator_id=anno_user.id,
    )
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch.id)))
        .scalars()
        .all()
    )
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(t.id) for t in tasks],
            "prompt": "person",
        },
    )
    assert resp.status_code == 409
    assert "active or unassigned draft" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_preannotate_happy_path_text_box_mode(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "ripe apples",
            "output_mode": "box",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["job_id"] == "fake-job-uuid"
    assert data["total_tasks"] == 3
    assert data["channel"] == f"project:{proj.id}:preannotate"

    # delay 收到的 kwargs 透传 prompt + output_mode + batch_id
    assert _mock_celery["kwargs"]["prompt"] == "ripe apples"
    assert _mock_celery["kwargs"]["output_mode"] == "box"
    assert _mock_celery["kwargs"]["batch_id"] == str(batch.id)
    assert _mock_celery["kwargs"]["user_id"] == str(owner.id)


@pytest.mark.asyncio
async def test_preannotate_forwards_model_id_and_task_type(
    httpx_client, super_admin, db_session, _mock_celery
):
    """v0.14.9 · 协议 v2: model_id / task_type 透传到 batch_predict.delay kwargs。"""
    owner, token = super_admin
    proj, backend, batch = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "task_type": "ocr",
            "model_id": "pp-ocrv4",
        },
    )
    assert resp.status_code == 200, resp.text
    assert _mock_celery["kwargs"]["task_type"] == "ocr"
    assert _mock_celery["kwargs"]["model_id"] == "pp-ocrv4"


@pytest.mark.asyncio
async def test_preannotate_invalid_output_mode_rejected(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "output_mode": "invalid",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_preannotate_rejects_oversized_retry_context(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "x" * (8 * 1024),
        },
    )
    assert resp.status_code == 422
    assert "8 KiB" in resp.text


@pytest.mark.asyncio
async def test_preannotate_sizes_normalized_retry_context(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, batch = await _seed(db_session, owner.id)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "batch_id": str(batch.id),
            "prompt": "bus",
            "params": {f"unset_{index}": None for index in range(1_000)},
        },
    )
    assert resp.status_code == 200, resp.text


def test_preannotate_rejects_child_stage_sorted_before_root():
    from app.api.v1.projects import PreannotateRequest

    backend_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="必须小于子阶段序号"):
        PreannotateRequest(
            pipeline_stages=[
                {
                    "stage": 5,
                    "ml_backend_id": backend_id,
                    "parent_stage": None,
                    "params": {"root": "ok"},
                },
                {
                    "stage": 0,
                    "ml_backend_id": backend_id,
                    "parent_stage": 5,
                    "params": {"blob": "x" * 20_000},
                },
            ]
        )
