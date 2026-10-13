"""v0.10.5 M4-β · annotation shape 状态位字段级 PATCH (I15).

覆盖 PATCH /tasks/{tid}/annotations/{aid} 写入 z_order / is_locked /
is_hidden 后:
  - DB 字段持久化
  - AnnotationOut 透出新字段
  - 默认值（旧记录）回落 0 / false
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(db_session, ann_user):
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-MD-{suffix}",
        name="shape metadata",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=ann_user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    # Literal employee: annotator authority comes from this explicit membership.
    await create_membership(
        db_session, project_id=project.id, user_id=ann_user.id, role="annotator"
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-MD-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        status="in_progress",
        assignee_id=ann_user.id,
    )
    db_session.add(task)
    await db_session.flush()
    ann = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=ann_user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        confidence=1.0,
        is_active=True,
        attributes={},
    )
    db_session.add(ann)
    await db_session.flush()
    return project, task, ann


@pytest.mark.asyncio
async def test_shape_metadata_defaults_are_zero_false(
    httpx_client, db_session, annotator
):
    ann_user, ann_token = annotator
    _, task, ann = await _seed(db_session, ann_user)

    r = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=_bearer(ann_token),
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    a = rows[0]
    assert a["z_order"] == 0
    assert a["is_locked"] is False
    assert a["is_hidden"] is False


@pytest.mark.asyncio
async def test_patch_shape_metadata_persists(httpx_client, db_session, annotator):
    ann_user, ann_token = annotator
    _, task, ann = await _seed(db_session, ann_user)

    r = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{ann.id}",
        json={"z_order": 3, "is_locked": True, "is_hidden": True},
        headers=_bearer(ann_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["z_order"] == 3
    assert body["is_locked"] is True
    assert body["is_hidden"] is True

    # DB 持久化
    await db_session.refresh(ann)
    assert ann.z_order == 3
    assert ann.is_locked is True
    assert ann.is_hidden is True
    # version 应递增（乐观并发）
    assert ann.version >= 2


@pytest.mark.asyncio
async def test_patch_shape_metadata_partial(httpx_client, db_session, annotator):
    ann_user, ann_token = annotator
    _, task, ann = await _seed(db_session, ann_user)

    # 单独切 is_locked，其它字段保持默认。
    r = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{ann.id}",
        json={"is_locked": True},
        headers=_bearer(ann_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_locked"] is True
    assert body["z_order"] == 0  # 未指定字段保持原值
    assert body["is_hidden"] is False
