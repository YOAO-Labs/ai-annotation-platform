"""v0.12.5 · GET /dashboard/admin/people/export CSV 导出（A2）。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task


async def _seed_one_annotation(
    db: AsyncSession, owner_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-EXP-{uuid.uuid4().hex[:6]}",
        name="export-test",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    t = Task(
        id=uuid.uuid4(),
        project_id=p.id,
        display_id=f"T-EXP-{uuid.uuid4().hex[:6]}",
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
            project_id=p.id,
            user_id=user_id,
            class_name="car",
            geometry={"x": 0, "y": 0, "w": 1, "h": 1},
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_admin_people_export_csv(
    httpx_client, db_session, super_admin, annotator
):
    """super_admin 导出 CSV：带 BOM、含表头、含成员行。"""
    admin_user, admin_token = super_admin
    ann_user, _ = annotator
    await _seed_one_annotation(db_session, admin_user.id, ann_user.id)
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/admin/people/export?period=4w",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers.get("content-disposition", "")
    body = resp.text
    # Excel UTF-8 BOM
    assert body.startswith("﻿")
    # 表头 + 成员行
    assert "user_id" in body
    assert "quality_score" in body
    assert ann_user.email in body


@pytest.mark.asyncio
async def test_admin_people_export_requires_admin_role(
    httpx_client, db_session, project_admin, annotator
):
    """RBAC 角色门(v0.12.6 起放行 project_admin,annotator 仍拒)。

    覆盖三条边界:
    - annotator → 403(无 admin 角色)
    - project_admin 不带 project → 403(必须指定其管理的项目范围)
    - project_admin 带自有项目 → 200(委托 admin_people_list 强制项目级聚合)
    """
    _, ann_token = annotator
    r_ann = await httpx_client.get(
        "/api/v1/dashboard/admin/people/export",
        headers={"Authorization": f"Bearer {ann_token}"},
    )
    assert r_ann.status_code == 403

    pm_user, pm_token = project_admin
    r_pm_no_proj = await httpx_client.get(
        "/api/v1/dashboard/admin/people/export",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert r_pm_no_proj.status_code == 403

    # 自有项目:owner 是 pm_user → 严格 owner 校验通过 → 200。
    own = Project(
        id=uuid.uuid4(),
        display_id=f"P-EXP-{uuid.uuid4().hex[:6]}",
        name="pm-own",
        type_label="image-det",
        type_key="image-det",
        owner_id=pm_user.id,
    )
    db_session.add(own)
    await db_session.commit()
    r_pm_own = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/export?project={own.id}",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert r_pm_own.status_code == 200
    assert "text/csv" in r_pm_own.headers["content-type"]
