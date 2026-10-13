"""v0.12.3 · GET /dashboard/me/performance 单测（取经合集 §4.1 个人页）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID) -> Project:
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-MP-{uuid.uuid4().hex[:6]}",
        name="my-perf-test",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    return p


async def _seed_task(db: AsyncSession, project_id: uuid.UUID) -> Task:
    t = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"T-MP-{uuid.uuid4().hex[:6]}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status="completed",
    )
    db.add(t)
    await db.flush()
    return t


async def _seed_annotation(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    db.add(
        Annotation(
            id=uuid.uuid4(),
            task_id=task_id,
            project_id=project_id,
            user_id=user_id,
            class_name="car",
            geometry={"x": 0, "y": 0, "w": 1, "h": 1},
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_my_performance_zeros_when_no_data(httpx_client, annotator):
    """无数据时任意已认证用户拿到全 0 结构。"""
    _, token = annotator
    resp = await httpx_client.get(
        "/api/v1/dashboard/me/performance",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["throughput"] == 0
    assert len(data["trend_throughput"]) == 4
    assert len(data["team_trend_throughput"]) == 4
    assert data["duration_histogram"] == []
    # v0.12.4 质量归因(A1):无数据时空 + None
    assert data["reject_reason_breakdown"] == []
    assert data["class_distribution"] == []
    assert data["first_pass_yield"] is None


@pytest.mark.asyncio
async def test_my_performance_counts_own_throughput(
    httpx_client, db_session, super_admin, annotator
):
    """统计本人本周标注产出，trend 本周桶反映出来。"""
    admin_user, _ = super_admin
    ann_user, ann_token = annotator
    proj = await _seed_project(db_session, admin_user.id)
    task = await _seed_task(db_session, proj.id)
    for _ in range(3):
        await _seed_annotation(
            db_session, task_id=task.id, project_id=proj.id, user_id=ann_user.id
        )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/me/performance",
        headers={"Authorization": f"Bearer {ann_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["throughput"] == 3
    # 本周是趋势最后一个点
    assert data["trend_throughput"][-1] == 3
    assert data["user_id"] == str(ann_user.id)


@pytest.mark.asyncio
async def test_my_performance_is_self_scoped(
    httpx_client, db_session, super_admin, annotator
):
    """强制 self：annotator 的标注不计入 super_admin 自己的 /me。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    proj = await _seed_project(db_session, admin_user.id)
    task = await _seed_task(db_session, proj.id)
    await _seed_annotation(
        db_session, task_id=task.id, project_id=proj.id, user_id=ann_user.id
    )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/me/performance",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    # admin 自己没有标注产出
    assert resp.json()["throughput"] == 0


@pytest.mark.asyncio
async def test_my_performance_quality_attribution(
    httpx_client, db_session, super_admin, annotator
):
    """v0.12.4 · 类别覆盖 / reject 原因细分 / 首过率(A1)。"""
    admin_user, _ = super_admin
    ann_user, ann_token = annotator
    proj = await _seed_project(db_session, admin_user.id)
    now = datetime.now(timezone.utc)

    # 类别分布:car×2, person×1
    for cls in ("car", "car", "person"):
        task = await _seed_task(db_session, proj.id)
        db_session.add(
            Annotation(
                id=uuid.uuid4(),
                task_id=task.id,
                project_id=proj.id,
                user_id=ann_user.id,
                class_name=cls,
                geometry={"x": 0, "y": 0, "w": 1, "h": 1},
                created_at=now,
            )
        )
    # 首过率:3 提交(2 干净 + 1 reopen=1) → 2/3;其中 reopen 那条带 reject 原因
    for reopened, reason in [(0, None), (0, None), (1, "missing")]:
        t = await _seed_task(db_session, proj.id)
        t.assignee_id = ann_user.id
        t.submitted_at = now
        t.reopened_count = reopened
        t.reject_reason_type = reason
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/me/performance",
        headers={"Authorization": f"Bearer {ann_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    classes = {c["class_name"]: c["count"] for c in data["class_distribution"]}
    assert classes == {"car": 2, "person": 1}
    assert data["first_pass_yield"] == 0.667  # 2/3 round 3
    reasons = {r["reason_type"]: r["count"] for r in data["reject_reason_breakdown"]}
    assert reasons == {"missing": 1}


@pytest.mark.asyncio
async def test_my_performance_requires_auth(httpx_client):
    """未认证返回 401/403。"""
    resp = await httpx_client.get("/api/v1/dashboard/me/performance")
    assert resp.status_code in (401, 403)
