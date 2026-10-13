"""Issue #121 · 工作台「当前题 AI」单题执行范围校验.

工作台复用 POST /projects/{id}/preannotate, 但通过 execution_scope=workbench 声明
交互式单题语义: 允许 in_progress 任务与 draft 批次, 仍保留管理员锁 / 他人编辑锁 /
终态任务保护; 数据管理批量路径 (缺省 bulk) 的 pending + active 批次限制不退化。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BatchStatus
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from tests.conftest import create_registry_with_pool


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    task_status: str = "in_progress",
    batch_status: str = BatchStatus.DRAFT,
    admin_locked: bool = False,
):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-{suffix}",
        name=f"wb-preanno-{suffix}",
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
    db.add(ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True))
    await db.flush()

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id=f"B-{suffix}",
        name="wb-batch",
        status=batch_status,
        admin_locked=admin_locked,
    )
    db.add(batch)
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        batch_id=batch.id,
        display_id=f"T-{suffix}-0",
        file_name="img.jpg",
        file_path=f"items/{suffix}.jpg",
        file_type="image",
        status=task_status,
    )
    db.add(task)
    await db.commit()
    return proj, backend, batch, task


def _own_lock(db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
    db.add(
        TaskLock(
            task_id=task_id,
            user_id=user_id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )


@pytest.fixture
def _mock_celery(monkeypatch):
    captured: dict = {}

    class _FakeJob:
        id = "fake-job-uuid"

    def _fake_apply_async(args=None, kwargs=None, queue=None, **extra):
        captured["args"] = tuple(args or ())
        captured["kwargs"] = kwargs or {}
        captured["queue"] = queue
        captured["extra"] = extra
        return _FakeJob()

    from app.workers import tasks as worker_tasks

    monkeypatch.setattr(worker_tasks.batch_predict, "apply_async", _fake_apply_async)
    return captured


@pytest.mark.asyncio
async def test_workbench_scope_allows_in_progress_draft_with_own_lock(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, _, task = await _seed(db_session, owner.id)
    _own_lock(db_session, task.id, owner.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "execution_scope": "workbench",
            "prompt": "Drivable Area",
            "predict_mode": "overwrite",
        },
    )
    assert resp.status_code == 200, resp.text
    assert _mock_celery["kwargs"]["execution_scope"] == "workbench"
    assert _mock_celery["args"][2] == [str(task.id)]
    assert _mock_celery["extra"]["task_id"] == resp.json()["job_id"]

    # issue #121 review · 工作台单题请求预建持久作业, 使 worker 早期校验失败也有终态记录。
    job_id = uuid.UUID(resp.json()["job_id"])
    job = await db_session.get(AsyncJob, job_id)
    assert job is not None
    assert job.kind == "batch_predict"
    assert job.user_id == owner.id
    assert job.project_id == proj.id
    assert job.celery_task_id == str(job_id)
    assert job.payload["scope"] == {"task_ids": [str(task.id)]}
    assert job.payload["total_tasks"] == 1


@pytest.mark.asyncio
async def test_workbench_scope_rejects_another_members_lock(
    httpx_client, super_admin, annotator, db_session, _mock_celery
):
    owner, token = super_admin
    other, _ = annotator
    proj, backend, _, task = await _seed(db_session, owner.id)
    _own_lock(db_session, task.id, other.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 409
    assert "其他成员" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_workbench_scope_rejects_admin_locked_batch(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, _, task = await _seed(db_session, owner.id, admin_locked=True)

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 409
    assert "管理员锁定" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_workbench_scope_rejects_terminal_task(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, _, task = await _seed(db_session, owner.id, task_status="completed")

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 409
    assert "撤回" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_workbench_scope_rejects_multiple_tasks(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, _, task = await _seed(db_session, owner.id)

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id), str(uuid.uuid4())],
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 422
    assert "一个任务" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_bulk_scope_still_requires_pending(
    httpx_client, super_admin, db_session, _mock_celery
):
    owner, token = super_admin
    proj, backend, _, task = await _seed(db_session, owner.id)

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "prompt": "x",
        },
    )
    assert resp.status_code == 409
    assert "pending" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_workbench_scope_rejects_batch_id(
    httpx_client, super_admin, db_session, _mock_celery
):
    """review #121 · 工作台单题不接受 batch_id, 避免落入批量 active 校验。"""
    owner, token = super_admin
    proj, backend, batch, task = await _seed(db_session, owner.id)

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "batch_id": str(batch.id),
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 422
    assert "批次" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploading", "review", "completed"])
async def test_workbench_scope_rejects_non_editable_status(
    status, httpx_client, super_admin, db_session, _mock_celery
):
    """review #121 · 白名单放行 pending/in_progress/rejected, 其余状态 (含 uploading) 拒绝。"""
    owner, token = super_admin
    proj, backend, _, task = await _seed(db_session, owner.id, task_status=status)

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 409
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_workbench_scope_rejects_foreign_lock_hidden_behind_own_lock(
    httpx_client, super_admin, annotator, db_session, _mock_celery
):
    """review #121 · 同 task 多行残留锁时, 他人锁不能被本人更新的一行掩盖。"""
    owner, token = super_admin
    other, _ = annotator
    proj, backend, _, task = await _seed(db_session, owner.id)
    # 本人锁更晚过期 (active_lock 会先返回它), 但他人锁仍然存在。
    _own_lock(db_session, task.id, other.id)
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=owner.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/preannotate",
        headers=_bearer(token),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "execution_scope": "workbench",
            "prompt": "x",
        },
    )
    assert resp.status_code == 409
    assert "其他成员" in resp.json()["detail"]
    assert _mock_celery == {}


@pytest.mark.asyncio
async def test_workbench_validator_allows_own_lock_then_rejects_other_lock(
    db_session, super_admin, annotator
):
    """锁归属直接契约: 本人锁放行, 换成他人锁后拒绝。"""
    from app.api.v1.projects import _validate_workbench_task_scope

    owner, _ = super_admin
    other, _ = annotator
    proj, _, _, task = await _seed(db_session, owner.id)

    # 本人锁 + in_progress + draft → 允许
    _own_lock(db_session, task.id, owner.id)
    await db_session.commit()
    assert await _validate_workbench_task_scope(
        db_session, project=proj, task_ids=[task.id], actor=owner
    ) == [task.id]

    # 他人锁 → 409
    task_lock = (
        await db_session.execute(select(TaskLock).where(TaskLock.task_id == task.id))
    ).scalar_one()
    task_lock.user_id = other.id
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await _validate_workbench_task_scope(
            db_session, project=proj, task_ids=[task.id], actor=owner
        )
    assert exc.value.status_code == 409
