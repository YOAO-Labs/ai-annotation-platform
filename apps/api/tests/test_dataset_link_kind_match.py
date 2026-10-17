from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, Scene
from app.db.models.project import Project
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow


async def _dataset(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    data_type: str,
    has_scene: bool,
) -> Dataset:
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-KIND-{suffix}",
        name=f"kind {data_type}",
        data_type=data_type,
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    if has_scene:
        db.add(
            Scene(
                id=uuid.uuid4(),
                display_id=f"S-KIND-{suffix}",
                dataset_id=ds.id,
                name=f"scene-{suffix}",
            )
        )
        await db.flush()
    return ds


async def _project(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    data_type: str,
    scene_mode: bool,
) -> Project:
    suffix = uuid.uuid4().hex[:6]
    type_key = "lidar" if data_type == "lidar" else "image-det"
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-KIND-{suffix}",
        name="kind target",
        type_label="kind",
        type_key=type_key,
        data_type=data_type,
        scene_mode=scene_mode,
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add(project)
    await db.flush()
    return project


@pytest.mark.asyncio
async def test_link_project_enforces_data_type_and_scene_mode(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    workflow = DatasetTaskCreationWorkflow(db_session)

    plain_image = await _dataset(
        db_session, user.id, data_type="image", has_scene=False
    )
    scene_image = await _dataset(db_session, user.id, data_type="image", has_scene=True)
    scene_lidar = await _dataset(
        db_session, user.id, data_type="point_cloud", has_scene=True
    )
    normal_project = await _project(
        db_session, user.id, data_type="image", scene_mode=False
    )
    scene_project = await _project(
        db_session, user.id, data_type="image", scene_mode=True
    )
    lidar_scene_project = await _project(
        db_session, user.id, data_type="lidar", scene_mode=True
    )

    assert (await workflow.link_project(plain_image.id, normal_project.id)).link_id
    assert (await workflow.link_project(scene_image.id, scene_project.id)).link_id
    assert (await workflow.link_project(scene_lidar.id, lidar_scene_project.id)).link_id

    with pytest.raises(HTTPException) as scene_exc:
        await workflow.link_project(plain_image.id, scene_project.id)
    assert scene_exc.value.status_code == 422
    assert "scene" in scene_exc.value.detail

    with pytest.raises(HTTPException) as type_exc:
        await workflow.link_project(scene_lidar.id, scene_project.id)
    assert type_exc.value.status_code == 422
    assert "data_type" in type_exc.value.detail

    # 反向 scene 门:普通项目拒绝 has_scenes 数据集(对称强制)。
    with pytest.raises(HTTPException) as inv_scene_exc:
        await workflow.link_project(scene_image.id, normal_project.id)
    assert inv_scene_exc.value.status_code == 422
    assert "scene" in inv_scene_exc.value.detail

    # 反向 data_type 门:lidar 项目拒绝 image 数据集。
    with pytest.raises(HTTPException) as inv_type_exc:
        await workflow.link_project(scene_image.id, lidar_scene_project.id)
    assert inv_type_exc.value.status_code == 422
    assert "data_type" in inv_type_exc.value.detail
