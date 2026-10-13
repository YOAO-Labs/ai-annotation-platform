"""视频章节端点级测试。

回归 (v0.21.14): PATCH 章节起止帧后, updated_at 因 onupdate=func.now() 在 flush 后被
expire; 若响应序列化前不 refresh, 读取 updated_at 会触发异步上下文外的惰性加载
(MissingGreenlet) → 500。时间轴上拖章节条边界 resize 走的正是这条 PATCH。
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from tests.factory import build_tool_bindings

pytestmark = pytest.mark.asyncio


async def _video_item_with_task(db_session, owner_id):
    project = Project(
        display_id=f"P-CH-{uuid.uuid4().hex[:6]}",
        name="Chapter Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    dataset = Dataset(
        display_id=f"D-CH-{uuid.uuid4().hex[:6]}",
        name="videos",
        data_type="video",
        created_by=owner_id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        width=640,
        height=360,
        metadata_={
            "video": {
                "duration_ms": 1000,
                "fps": 25,
                "frame_count": 40,
                "width": 640,
                "height": 360,
                "codec": "h264",
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-CH-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    return item


async def test_update_chapter_frames_returns_200(db_session, httpx_client, super_admin):
    """拖章节条 resize 的 PATCH 只带 start/end_frame, 必须 200 且落新值 (不再 500)。"""
    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    item = await _video_item_with_task(db_session, user.id)

    created = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json={"start_frame": 0, "end_frame": 10, "title": "seg"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]

    resized = await httpx_client.patch(
        f"/api/v1/videos/{item.id}/chapters/{chapter_id}",
        json={"start_frame": 3, "end_frame": 30},
        headers=headers,
    )
    assert resized.status_code == 200, resized.text
    body = resized.json()
    assert body["start_frame"] == 3
    assert body["end_frame"] == 30
    assert body["updated_at"] is not None
