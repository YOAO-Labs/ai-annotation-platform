from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset
from app.db.models.task import Task
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from tests.factory import create_project


@pytest.mark.asyncio
async def test_create_scene_mode_project_enables_scene_continuation(
    httpx_client, super_admin
):
    _, token = super_admin
    res = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "scene mode",
            "type_label": "图像",
            "data_type": "image",
            "scene_mode": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scene_mode"] is True
    assert body["prefer_same_scene_continuation"] is True


@pytest.mark.asyncio
async def test_create_video_scene_mode_rejected(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.post(
        "/api/v1/projects",
        json={
            "name": "video scene mode",
            "type_label": "视频",
            "data_type": "video",
            "scene_mode": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_patch_scene_mode_rejected_after_tasks(
    httpx_client, db_session: AsyncSession, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="image-det")
    project.data_type = "image"
    project.scene_mode = False
    project.total_tasks = 1
    db_session.add(
        Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=None,
            display_id=f"T-SMODE-{uuid.uuid4().hex[:6]}",
            file_name="image.jpg",
            file_path="/tmp/image.jpg",
            file_type="image",
        )
    )
    await db_session.commit()

    res = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"scene_mode": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422, res.text
    assert "不可切换 scene 模式" in res.text


@pytest.mark.asyncio
async def test_patch_scene_mode_off_rejected_after_tasks(
    httpx_client, db_session: AsyncSession, super_admin
):
    """flip-lock 对称:scene_mode True→False 在有 task 后同样拒绝。"""
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="image-det")
    project.data_type = "image"
    project.scene_mode = True
    project.total_tasks = 1
    db_session.add(
        Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=None,
            display_id=f"T-SMODE-{uuid.uuid4().hex[:6]}",
            file_name="image.jpg",
            file_path="/tmp/image.jpg",
            file_type="image",
        )
    )
    await db_session.commit()

    res = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"scene_mode": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422, res.text
    assert "不可切换 scene 模式" in res.text


@pytest.mark.asyncio
async def test_patch_scene_mode_allowed_without_tasks(
    httpx_client, db_session: AsyncSession, super_admin
):
    """total_tasks==0 边界:无 task 时 scene_mode 可翻转(条件是 > 0)。"""
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="image-det")
    project.data_type = "image"
    project.scene_mode = False
    project.total_tasks = 0
    await db_session.commit()

    res = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"scene_mode": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["scene_mode"] is True


@pytest.mark.asyncio
async def test_patch_scene_mode_rejected_when_linked_dataset_mismatches(
    httpx_client, db_session: AsyncSession, super_admin
):
    """无 task 但已 link 不匹配数据集时,翻转 scene_mode 要求先解绑。"""
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="image-det")
    project.data_type = "image"
    project.scene_mode = False
    project.total_tasks = 0
    suffix = uuid.uuid4().hex[:6]
    plain_ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-LNK-{suffix}",
        name="plain image",
        data_type="image",
        created_by=user.id,
    )
    db_session.add(plain_ds)
    await db_session.commit()

    # 普通项目 link 普通数据集:匹配,放行。
    await DatasetTaskCreationWorkflow(db_session).link_project(plain_ds.id, project.id)
    await db_session.commit()

    # 翻 scene_mode=True 后,已 link 的非 scene 数据集 kind 不再匹配 → 422 要求解绑。
    res = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"scene_mode": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422, res.text
    assert "解绑" in res.text
