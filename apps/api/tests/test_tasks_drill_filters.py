"""v0.12.6 (A3) · GET /tasks 的 reject_reason_type / class_name 下钻过滤。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task


async def _seed(db: AsyncSession, owner_id: uuid.UUID):
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-DF-{uuid.uuid4().hex[:6]}",
        name="drill-filter-test",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()

    async def _task(reason, cls):
        t = Task(
            id=uuid.uuid4(),
            project_id=p.id,
            display_id=f"T-DF-{uuid.uuid4().hex[:6]}",
            file_name="x.jpg",
            file_path="/tmp/x.jpg",
            file_type="image",
            tags=[],
            status="completed",
            reject_reason_type=reason,
        )
        db.add(t)
        await db.flush()
        db.add(
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=p.id,
                user_id=owner_id,
                class_name=cls,
                geometry={"x": 0, "y": 0, "w": 1, "h": 1},
            )
        )
        await db.flush()
        return t

    a = await _task("missing", "car")
    b = await _task("extra", "person")
    return p, a, b


@pytest.mark.asyncio
async def test_filter_by_reject_reason_type(httpx_client, db_session, super_admin):
    admin_user, token = super_admin
    p, a, b = await _seed(db_session, admin_user.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&reject_reason_type=missing",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    ids = {it["id"] for it in resp.json()["items"]}
    assert ids == {str(a.id)}


@pytest.mark.asyncio
async def test_filter_by_class_name(httpx_client, db_session, super_admin):
    admin_user, token = super_admin
    p, a, b = await _seed(db_session, admin_user.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks?project_id={p.id}&class_name=person",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    ids = {it["id"] for it in resp.json()["items"]}
    assert ids == {str(b.id)}
