"""v0.8.8 · GET /annotations/{id}/comments/page keyset 分页单测。

覆盖：
- limit + cursor 串联拉取，items 顺序 DESC(created_at, id)
- 末页 next_cursor=None
- invalid_cursor → 400
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.project import Project
from app.db.models.task import Task

pytestmark = pytest.mark.asyncio


async def _seed_with_comments(
    db: AsyncSession, owner_id: uuid.UUID, n: int
) -> tuple[Annotation, list[AnnotationComment]]:
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-CMT-{suffix}",
        name=f"cmt-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(project)
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-CMT-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status="pending",
    )
    db.add(task)
    await db.flush()

    ann = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        annotation_type="bbox",
        class_name="object",
        geometry={"type": "rect", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={},
        is_active=True,
    )
    db.add(ann)
    await db.flush()

    # 测试环境用 SAVEPOINT 隔离 → 单个外 tx 下 func.now() 恒等，无法靠真实时间拉开 created_at。
    # 显式给每条 comment 写入 created_at = base + Δi，保证 DESC 排序确定。
    base_ts = datetime.now(timezone.utc)
    comments: list[AnnotationComment] = []
    for i in range(n):
        c = AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=ann.id,
            project_id=project.id,
            author_id=owner_id,
            body=f"comment #{i}",
            mentions=[],
            attachments=[],
            is_active=True,
            created_at=base_ts + timedelta(seconds=i),
        )
        db.add(c)
        await db.flush()
        comments.append(c)
    await db.commit()
    return ann, comments


async def test_paged_returns_chunks_in_desc_order(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    ann, comments = await _seed_with_comments(db_session, user.id, n=12)
    headers = {"Authorization": f"Bearer {token}"}

    # 第一页：5 条
    r1 = await httpx_client.get(
        f"/api/v1/annotations/{ann.id}/comments/page?limit=5",
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    page1 = r1.json()
    assert len(page1["items"]) == 5
    assert page1["next_cursor"]

    # 时间倒序：第 0 条应是 comments[-1]
    assert page1["items"][0]["id"] == str(comments[-1].id)

    # 第二页（cursor 是 base64-urlsafe，无需额外 encode）
    r2 = await httpx_client.get(
        f"/api/v1/annotations/{ann.id}/comments/page?limit=5&cursor={page1['next_cursor']}",
        headers=headers,
    )
    assert r2.status_code == 200
    page2 = r2.json()
    assert len(page2["items"]) == 5
    assert page2["items"][0]["id"] == str(comments[-6].id)

    # 第三页：2 条 + next_cursor=None
    r3 = await httpx_client.get(
        f"/api/v1/annotations/{ann.id}/comments/page?limit=5&cursor={page2['next_cursor']}",
        headers=headers,
    )
    assert r3.status_code == 200
    page3 = r3.json()
    assert len(page3["items"]) == 2
    assert page3["next_cursor"] is None


async def test_paged_invalid_cursor_returns_400(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    user, token = super_admin
    ann, _ = await _seed_with_comments(db_session, user.id, n=1)

    r = await httpx_client.get(
        f"/api/v1/annotations/{ann.id}/comments/page?cursor=garbage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
