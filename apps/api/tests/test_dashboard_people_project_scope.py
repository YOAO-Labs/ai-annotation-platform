"""v0.12.6 (A3) · 成员绩效项目级范围 + RBAC。

核心保证:project 给定时聚合**按项目切分**(非全局);project_admin 强制其项目范围。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID, name: str) -> Project:
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-SC-{uuid.uuid4().hex[:6]}",
        name=name,
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    return p


async def _seed_annotations(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, n: int
) -> None:
    now = datetime.now(timezone.utc)
    for _ in range(n):
        t = Task(
            id=uuid.uuid4(),
            project_id=project_id,
            display_id=f"T-SC-{uuid.uuid4().hex[:6]}",
            file_name="x.jpg",
            file_path="/tmp/x.jpg",
            file_type="image",
            tags=[],
            status="completed",
        )
        db.add(t)
        await db.flush()
        db.add(
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=project_id,
                user_id=user_id,
                class_name="car",
                geometry={"x": 0, "y": 0, "w": 1, "h": 1},
                created_at=now,
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_detail_aggregation_sliced_by_project(
    httpx_client, db_session, super_admin, annotator
):
    """对账:全局 throughput = 各项目之和;project 过滤后 = 单项目数字。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    p1 = await _seed_project(db_session, admin_user.id, "P1")
    p2 = await _seed_project(db_session, admin_user.id, "P2")
    await _seed_annotations(db_session, p1.id, ann_user.id, 3)
    await _seed_annotations(db_session, p2.id, ann_user.id, 2)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    # 全局
    g = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}", headers=headers
    )
    assert g.status_code == 200
    assert g.json()["throughput"] == 5
    # 项目 P1
    s1 = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}?project={p1.id}",
        headers=headers,
    )
    assert s1.status_code == 200
    assert s1.json()["throughput"] == 3
    # 项目 P2
    s2 = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}?project={p2.id}",
        headers=headers,
    )
    assert s2.json()["throughput"] == 2


@pytest.mark.asyncio
async def test_list_main_metric_sliced_by_project(
    httpx_client, db_session, super_admin, annotator
):
    """list 端点:project 过滤后 main_metric 反映单项目(非全局 5)。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    p1 = await _seed_project(db_session, admin_user.id, "P1")
    p2 = await _seed_project(db_session, admin_user.id, "P2")
    await _seed_annotations(db_session, p1.id, ann_user.id, 3)
    await _seed_annotations(db_session, p2.id, ann_user.id, 2)
    # annotator 必须是 P1 成员才会出现在 list
    db_session.add(
        ProjectMember(
            id=uuid.uuid4(), project_id=p1.id, user_id=ann_user.id, role="annotator"
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {admin_token}"}
    r = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={p1.id}&period=4w", headers=headers
    )
    assert r.status_code == 200
    items = {it["user_id"]: it for it in r.json()["items"]}
    assert str(ann_user.id) in items
    assert items[str(ann_user.id)]["main_metric"] == 3  # 仅 P1,非全局 5


@pytest.mark.asyncio
async def test_super_admin_global_unchanged(
    httpx_client, db_session, super_admin, annotator
):
    """回归:super_admin 不带 project → 全局数字(5)不变。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    p1 = await _seed_project(db_session, admin_user.id, "P1")
    p2 = await _seed_project(db_session, admin_user.id, "P2")
    await _seed_annotations(db_session, p1.id, ann_user.id, 3)
    await _seed_annotations(db_session, p2.id, ann_user.id, 2)
    await db_session.commit()

    r = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.json()["throughput"] == 5


@pytest.mark.asyncio
async def test_project_admin_scoped_to_own_project(
    httpx_client, db_session, super_admin, project_admin, annotator
):
    """project_admin:仅自有项目可见 + 强制项目范围。"""
    admin_user, _ = super_admin
    pm_user, pm_token = project_admin
    ann_user, _ = annotator
    own = await _seed_project(db_session, pm_user.id, "Own")  # project_admin 拥有
    other = await _seed_project(db_session, admin_user.id, "Other")  # super_admin 拥有
    await _seed_annotations(db_session, own.id, ann_user.id, 3)
    db_session.add(
        ProjectMember(
            id=uuid.uuid4(), project_id=own.id, user_id=ann_user.id, role="annotator"
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {pm_token}"}
    # 自有项目 → 200
    ok = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={own.id}&period=4w", headers=headers
    )
    assert ok.status_code == 200
    # 不带 project → 403(必须指定范围)
    no_proj = await httpx_client.get("/api/v1/dashboard/admin/people", headers=headers)
    assert no_proj.status_code == 403
    # 越权他人项目 → 404(隐藏存在性)
    forbidden = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={other.id}", headers=headers
    )
    assert forbidden.status_code == 404


@pytest.mark.asyncio
async def test_project_admin_detail_blocks_non_member(
    httpx_client, db_session, super_admin, project_admin, annotator
):
    """安全:project_admin 查非本项目成员的详情 → 404(防 IDOR / 跨项目枚举)。"""
    admin_user, _ = super_admin
    pm_user, pm_token = project_admin
    ann_user, _ = annotator
    own = await _seed_project(db_session, pm_user.id, "Own")
    other = await _seed_project(db_session, admin_user.id, "Other")
    # ann_user 在 other(非 pm 的项目)有数据,但不是 own 的成员
    await _seed_annotations(db_session, other.id, ann_user.id, 2)
    await db_session.commit()

    # pm 用自己的项目 own + 他人 user_id → 非成员 → 404
    resp = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{ann_user.id}?project={own.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_project_admin_member_of_others_project_blocked(
    httpx_client, db_session, super_admin, project_admin
):
    """安全:project_admin 是他人项目的 ProjectMember 但非 owner 时,仍 → 404。

    _resolve_people_scope 严格按 owner 校验,**不复用** assert_project_visible
    的 ProjectMember fallback,防止「owner ∪ member」的 IDOR 漏窗。
    """
    admin_user, _ = super_admin
    pm_user, pm_token = project_admin
    # admin 拥有的项目,把 pm_user 加为 ProjectMember(但 owner 仍是 admin)
    other = await _seed_project(db_session, admin_user.id, "Other")
    db_session.add(
        ProjectMember(
            id=uuid.uuid4(),
            project_id=other.id,
            user_id=pm_user.id,
            role="annotator",
        )
    )
    await db_session.commit()

    # pm 拿 other.id 访问 → 严格 owner 校验失败 → 404(不是 200,不是 403)
    r_list = await httpx_client.get(
        f"/api/v1/dashboard/admin/people?project={other.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert r_list.status_code == 404
    r_export = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/export?project={other.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert r_export.status_code == 404


@pytest.mark.asyncio
async def test_annotator_still_denied(httpx_client, annotator):
    """普通 annotator 无权访问成员绩效。"""
    _, token = annotator
    r = await httpx_client.get(
        "/api/v1/dashboard/admin/people",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
