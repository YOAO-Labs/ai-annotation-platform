"""v0.11.25 · 删批次保护：默认拒删含进行中成果/已预标的批次，需显式 force。"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.batch import BatchSplitRequest
from app.services.dataset import DatasetService
from app.services.batch import BatchService


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_batch(db: AsyncSession, owner_id: uuid.UUID, n_items: int):
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-FRC-{suffix}",
        name="force test",
        data_type="image",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    for i in range(n_items):
        db.add(
            DatasetItem(
                id=uuid.uuid4(),
                dataset_id=ds.id,
                file_name=f"img-{i}.jpg",
                file_path=f"/tmp/{suffix}-{i}.jpg",
                file_type="image",
            )
        )
    await db.flush()
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-FRC-{suffix}",
        name="force target",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add(project)
    await db.flush()
    await DatasetService(db).link_project(ds.id, project.id)
    batches = await BatchService(db).split(
        project.id,
        BatchSplitRequest(strategy="random", n_batches=1, name_prefix="B"),
        owner_id,
    )
    return project, batches[0].id


@pytest.mark.asyncio
async def test_delete_pure_pending_no_force_ok(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 3)
    # 全 pending、无预测 → 非 force 直接删
    assert await BatchService(db_session).delete(batch_id) is True


@pytest.mark.asyncio
async def test_delete_nonpending_requires_force(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 3)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    tasks[0].status = "review"
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await BatchService(db_session).delete(batch_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["requires_force"] is True
    assert exc.value.detail["non_pending"] == 1
    # 批次与 task 未被改动
    still = await db_session.get(Task, tasks[0].id)
    assert still.batch_id == batch_id and still.status == "review"


@pytest.mark.asyncio
async def test_delete_predicted_requires_force(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 2)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    # 仍 pending 但已预标 → 也算成果
    tasks[0].total_predictions = 1
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await BatchService(db_session).delete(batch_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["predicted"] == 1


@pytest.mark.asyncio
async def test_delete_force_resets_and_deletes(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 3)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    tasks[0].status = "completed"
    await db_session.flush()

    assert await BatchService(db_session).delete(batch_id, force=True) is True
    rows = (
        await db_session.execute(
            select(Task.status, Task.batch_id).where(Task.project_id == project.id)
        )
    ).all()
    assert all(r.status == "pending" and r.batch_id is None for r in rows)
    # 无 B-DEFAULT 的新项目也必须同步 project 物化列，避免停在删前快照
    await db_session.refresh(project)
    assert project.completed_tasks == 0
    assert project.review_tasks == 0
    assert project.in_progress_tasks == 0


@pytest.mark.asyncio
async def test_count_protected_dedups_affected(db_session: AsyncSession, super_admin):
    """non_pending 与 predicted 重叠时 affected 去重计数。"""
    user, _ = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 2)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    # 同一 task 既 review 又已预标 → non_pending=1, predicted=1, 但实际只 1 个 task 受影响
    tasks[0].status = "review"
    tasks[0].total_predictions = 1
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await BatchService(db_session).delete(batch_id)
    assert exc.value.detail["non_pending"] == 1
    assert exc.value.detail["predicted"] == 1
    assert exc.value.detail["affected_tasks"] == 1


@pytest.mark.asyncio
async def test_bulk_delete_force_guard(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    project, protected_id = await _seed_batch(db_session, user.id, 2)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == protected_id)))
        .scalars()
        .all()
    )
    tasks[0].status = "review"
    await db_session.flush()

    svc = BatchService(db_session)
    res = await svc.bulk_delete(project.id, [protected_id])
    # 非 force：受保护批次进 failed，不被删
    assert protected_id not in res["succeeded"]
    assert any(
        f["batch_id"] == protected_id and f["reason"] == "requires_force"
        for f in res["failed"]
    )
    remaining = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.batch_id == protected_id)
        )
    ).scalar()
    assert remaining == 2

    # force=True：删除并重置
    res2 = await svc.bulk_delete(project.id, [protected_id], force=True)
    assert protected_id in res2["succeeded"]


@pytest.mark.asyncio
async def test_delete_route_force_param_and_audit(
    db_session: AsyncSession, super_admin, httpx_client
):
    """HTTP 层：非 force → 409(affected_tasks)；force=true → 204 + 审计 forced=True。"""
    user, token = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 2)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    tasks[0].status = "review"
    await db_session.flush()

    r409 = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/batches/{batch_id}",
        headers=_bearer(token),
    )
    assert r409.status_code == 409
    detail = r409.json()["detail"]
    assert detail["requires_force"] is True
    assert detail["affected_tasks"] == 1

    r = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/batches/{batch_id}?force=true",
        headers=_bearer(token),
    )
    assert r.status_code == 204
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == str(batch_id))
            )
        )
        .scalars()
        .all()
    )
    assert any((a.detail_json or {}).get("forced") is True for a in audits)


@pytest.mark.asyncio
async def test_bulk_delete_route_force_audit(
    db_session: AsyncSession, super_admin, httpx_client
):
    """HTTP 层 bulk-delete：force=true 删除成功且审计 detail 写入 forced 标记。"""
    user, token = super_admin
    project, batch_id = await _seed_batch(db_session, user.id, 2)
    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    tasks[0].status = "completed"
    await db_session.flush()

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/bulk-delete?force=true",
        headers=_bearer(token),
        json={"batch_ids": [str(batch_id)]},
    )
    assert r.status_code == 200, r.text
    assert str(batch_id) in r.json()["succeeded"]
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == str(project.id))
            )
        )
        .scalars()
        .all()
    )
    assert any((a.detail_json or {}).get("forced") is True for a in audits)
