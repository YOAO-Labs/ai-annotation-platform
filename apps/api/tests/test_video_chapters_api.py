"""视频章节端点级测试。

回归 (v0.21.14): PATCH 章节起止帧后, updated_at 因 onupdate=func.now() 在 flush 后被
expire; 若响应序列化前不 refresh, 读取 updated_at 会触发异步上下文外的惰性加载
(MissingGreenlet) → 500。时间轴上拖章节条边界 resize 走的正是这条 PATCH。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_chapter import VideoChapter
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


async def test_delete_chapter_keeps_item_scope_and_response_contract(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    item = await _video_item_with_task(db_session, user.id)
    other_item = await _video_item_with_task(db_session, user.id)

    created = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json={"start_frame": 0, "end_frame": 10, "title": "seg"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]

    wrong_item = await httpx_client.delete(
        f"/api/v1/videos/{other_item.id}/chapters/{chapter_id}",
        headers=headers,
    )
    assert wrong_item.status_code == 404

    deleted = await httpx_client.delete(
        f"/api/v1/videos/{item.id}/chapters/{chapter_id}",
        headers=headers,
    )
    assert deleted.status_code == 204
    listed = await httpx_client.get(
        f"/api/v1/videos/{item.id}/chapters",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json() == {"chapters": []}


async def test_chapter_mutation_uses_actual_project_authority_after_stale_task_read(
    db_session, httpx_client, project_admin, super_admin
):
    actor, token = project_admin
    other_owner, _ = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    item = await _video_item_with_task(db_session, actor.id)
    stale_task = (
        await db_session.execute(select(Task).where(Task.dataset_item_id == item.id))
    ).scalar_one()
    actual_project = Project(
        display_id=f"P-CH-{uuid.uuid4().hex[:6]}",
        name="Actual Chapter Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=other_owner.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(actual_project)
    await db_session.flush()
    await db_session.execute(
        update(Task)
        .where(Task.id == stale_task.id)
        .values(project_id=actual_project.id)
        .execution_options(synchronize_session=False)
    )

    response = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json={"start_frame": 0, "end_frame": 10, "title": "seg"},
        headers=headers,
    )

    assert response.status_code == 404
    rows = (
        (
            await db_session.execute(
                select(VideoChapter).where(VideoChapter.dataset_item_id == item.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_chapter_mutation_rechecks_task_item_binding_after_lock(
    db_session, httpx_client, super_admin, monkeypatch
):
    from app.application import video_chapters

    actor, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    item = await _video_item_with_task(db_session, actor.id)
    other_item = await _video_item_with_task(db_session, actor.id)
    original_lock = video_chapters.lock_current_task_target

    async def retarget_before_lock(db, *, task_id, actor_id, expected_access):
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(dataset_item_id=other_item.id)
            .execution_options(synchronize_session=False)
        )
        return await original_lock(
            db,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=expected_access,
        )

    monkeypatch.setattr(
        video_chapters, "lock_current_task_target", retarget_before_lock
    )
    response = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json={"start_frame": 0, "end_frame": 10, "title": "seg"},
        headers=headers,
    )

    assert response.status_code == 404
    rows = (
        (
            await db_session.execute(
                select(VideoChapter).where(
                    VideoChapter.dataset_item_id.in_([item.id, other_item.id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_chapter_update_refreshes_existing_row_before_range_validation(
    db_session, httpx_client, super_admin
):
    actor, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    item = await _video_item_with_task(db_session, actor.id)
    created = await httpx_client.post(
        f"/api/v1/videos/{item.id}/chapters",
        json={"start_frame": 0, "end_frame": 10, "title": "seg"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    chapter_id = uuid.UUID(created.json()["id"])
    stale_chapter = await db_session.get(VideoChapter, chapter_id)
    assert stale_chapter is not None
    await db_session.execute(
        update(VideoChapter)
        .where(VideoChapter.id == chapter_id)
        .values(start_frame=5, end_frame=20)
        .execution_options(synchronize_session=False)
    )

    response = await httpx_client.patch(
        f"/api/v1/videos/{item.id}/chapters/{chapter_id}",
        json={"end_frame": 3},
        headers=headers,
    )

    assert response.status_code == 422
    await db_session.refresh(stale_chapter)
    assert (stale_chapter.start_frame, stale_chapter.end_frame) == (5, 20)
