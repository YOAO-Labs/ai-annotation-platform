"""v0.6.6 · PATCH /tasks/{tid}/annotations/{aid} 改 attributes 时
audit_logs 写入字段级 attribute_change 行（v0.6.3 log_many 后 round-trip 1 行）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from tests.factory import create_membership, build_tool_bindings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_attribute_change_writes_one_audit_per_changed_key(
    httpx_client, db_session, annotator
):
    ann_user, ann_token = annotator
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-AC-{suffix}",
        name="attr audit",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=ann_user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    # Literal employee: explicit annotator membership carries the write authority.
    await create_membership(
        db_session, project_id=project.id, user_id=ann_user.id, role="annotator"
    )

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-AC-{suffix}",
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
        attributes={"color": "red", "occluded": False},
    )
    db_session.add(ann)
    await db_session.flush()

    # PATCH attributes：改 2 个 key（color: red→blue，occluded: false→true），加 1 个新 key（truncated）
    r = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{ann.id}",
        json={"attributes": {"color": "blue", "occluded": True, "truncated": True}},
        headers=_bearer(ann_token),
    )
    assert r.status_code == 200, r.text

    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "annotation.attribute_change")
                .where(AuditLog.target_id == str(ann.id))
            )
        )
        .scalars()
        .all()
    )
    # 3 个 key 变化 → 3 条审计行
    assert len(rows) == 3
    field_keys = {r.detail_json["field_key"] for r in rows}
    assert field_keys == {"color", "occluded", "truncated"}

    # 同一 PATCH 触发的所有 audit 行应共享 request_id（v0.6.6 持久化）
    request_ids = {r.request_id for r in rows}
    assert len(request_ids) == 1, f"expected 1 request_id, got {request_ids}"
    assert next(iter(request_ids)) is not None
