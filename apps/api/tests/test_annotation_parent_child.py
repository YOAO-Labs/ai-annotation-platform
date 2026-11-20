"""父子标注 service 层覆盖 (Q2).

- create 带 parent_annotation_id: 正常建子框
- 一层深度约束: 给已有 parent 的框再当 parent → 400
- 父框跨 task / 不存在 / 已软删 → 400
- 级联软删: 删父框时其 active 子框一并软删, task 计数正确
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.services.annotation import AnnotationService
from tests.factory import create_project, create_task


async def _mk_ann(db, task_id, user_id, parent_annotation_id=None, **kw):
    svc = AnnotationService(db)
    return await svc.create(
        task_id=task_id,
        user_id=user_id,
        annotation_type="bbox",
        class_name=kw.get("class_name", "__unknown"),
        geometry=kw.get("geometry", {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}),
        tool_unit_id=kw.get("tool_unit_id", "bbox"),
        parent_annotation_id=parent_annotation_id,
    )


@pytest.mark.asyncio
async def test_create_child_annotation(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    parent = await _mk_ann(db_session, task.id, user.id)
    child = await _mk_ann(db_session, task.id, user.id, parent_annotation_id=parent.id)
    await db_session.flush()

    assert child.parent_annotation_id == parent.id
    assert parent.parent_annotation_id is None


@pytest.mark.asyncio
async def test_one_level_depth_rejected(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    parent = await _mk_ann(db_session, task.id, user.id)
    child = await _mk_ann(db_session, task.id, user.id, parent_annotation_id=parent.id)
    await db_session.flush()

    # 给 child (已有 parent) 再挂子框 → 一层约束拒绝
    with pytest.raises(HTTPException) as exc:
        await _mk_ann(db_session, task.id, user.id, parent_annotation_id=child.id)
    assert exc.value.status_code == 400
    assert "one level" in exc.value.detail


@pytest.mark.asyncio
async def test_parent_not_found_rejected(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await _mk_ann(db_session, task.id, user.id, parent_annotation_id=uuid.uuid4())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_parent_cross_task_rejected(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task_a = await create_task(db_session, project_id=proj.id)
    task_b = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    parent = await _mk_ann(db_session, task_a.id, user.id)
    await db_session.flush()

    # 父框在 task_a, 子框在 task_b → 父子限帧内, 拒绝
    with pytest.raises(HTTPException) as exc:
        await _mk_ann(db_session, task_b.id, user.id, parent_annotation_id=parent.id)
    assert exc.value.status_code == 400
    assert "same task" in exc.value.detail


@pytest.mark.asyncio
async def test_parent_inactive_rejected(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    parent = await _mk_ann(db_session, task.id, user.id)
    parent.is_active = False
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await _mk_ann(db_session, task.id, user.id, parent_annotation_id=parent.id)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_cascade_soft_delete_children(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    parent = await _mk_ann(db_session, task.id, user.id)
    child1 = await _mk_ann(db_session, task.id, user.id, parent_annotation_id=parent.id)
    child2 = await _mk_ann(db_session, task.id, user.id, parent_annotation_id=parent.id)
    sibling = await _mk_ann(db_session, task.id, user.id)  # 无父, 不应被级联删
    await db_session.flush()

    svc = AnnotationService(db_session)
    ok = await svc.delete(parent.id)
    assert ok

    for a in (parent, child1, child2):
        await db_session.refresh(a)
        assert a.is_active is False
    await db_session.refresh(sibling)
    assert sibling.is_active is True

    # task 计数只剩 sibling 一条 active
    await db_session.refresh(task)
    assert task.total_annotations == 1
