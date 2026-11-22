"""批量编辑 (bulk_update) service 层覆盖.

覆盖以下行为：
- bulk_update partial fail (锁 / 已软删 / 不存在) 整体回滚
- bulk_update 属性批量应用到全部 ids
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.annotation import AnnotationService
from tests.factory import create_project, create_task


async def _mk_ann(db, task_id, user_id, **kw):
    svc = AnnotationService(db)
    return await svc.create(
        task_id=task_id,
        user_id=user_id,
        annotation_type="bbox",
        class_name=kw.get("class_name", "__unknown"),
        geometry=kw.get("geometry", {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}),
        tool_unit_id=kw.get("tool_unit_id", "bbox"),
    )


@pytest.mark.asyncio
async def test_bulk_update_locked_raises(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    a1 = await _mk_ann(db_session, task.id, user.id)
    a2 = await _mk_ann(db_session, task.id, user.id)
    a2.is_locked = True
    await db_session.flush()

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc.bulk_update([a1.id, a2.id], class_name="person")
    assert exc.value.status_code == 422
    await db_session.refresh(a1)
    assert a1.class_name == "__unknown"


@pytest.mark.asyncio
async def test_bulk_update_attributes_applies_to_all(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    a1 = await _mk_ann(db_session, task.id, user.id)
    a2 = await _mk_ann(db_session, task.id, user.id)
    a3 = await _mk_ann(db_session, task.id, user.id)
    await db_session.flush()

    svc = AnnotationService(db_session)
    updated = await svc.bulk_update([a1.id, a2.id, a3.id], is_hidden=True)
    assert len(updated) == 3
    assert all(r.is_hidden for r in updated)
    assert all(r.version == 2 for r in updated)
