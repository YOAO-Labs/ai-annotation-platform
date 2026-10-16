from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.batch import BatchSplitRequest
from app.services.batch import BatchService
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow


@pytest.mark.asyncio
async def test_split_by_scene_creates_one_batch_per_scene_in_frame_order(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-BYS-{suffix}",
        name="by scene dataset",
        data_type="image",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()

    scenes = []
    for scene_name in ("scene-b", "scene-a"):
        scene = Scene(
            id=uuid.uuid4(),
            display_id=f"S-BYS-{scene_name}-{suffix}",
            dataset_id=ds.id,
            name=scene_name,
        )
        db_session.add(scene)
        scenes.append(scene)
    await db_session.flush()

    for scene in scenes:
        for frame_index in (2, 0, 1):
            db_session.add(
                DatasetItem(
                    id=uuid.uuid4(),
                    dataset_id=ds.id,
                    file_name=f"{scene.name}-{frame_index}.jpg",
                    file_path=f"/tmp/{scene.name}-{frame_index}.jpg",
                    file_type="image",
                    scene_id=scene.id,
                    frame_index=frame_index,
                )
            )
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-BYS-{suffix}",
        name="by scene project",
        type_label="图像",
        type_key="image-det",
        data_type="image",
        scene_mode=True,
        owner_id=user.id,
        total_tasks=0,
    )
    db_session.add(project)
    await db_session.flush()

    await DatasetTaskCreationWorkflow(db_session).link_project(ds.id, project.id)
    batches = await BatchService(db_session).split(
        project.id,
        BatchSplitRequest(strategy="by_scene", name_prefix="Scene"),
        user.id,
    )

    assert len(batches) == 2
    assert {batch.total_tasks for batch in batches} == {3}

    for batch in batches:
        rows = (
            await db_session.execute(
                select(Task.sequence_order, DatasetItem.scene_id)
                .join(DatasetItem, Task.dataset_item_id == DatasetItem.id)
                .where(Task.batch_id == batch.id)
                .order_by(Task.sequence_order)
            )
        ).all()
        assert [row[0] for row in rows] == [0, 1, 2]
        assert len({row[1] for row in rows}) == 1


@pytest.mark.asyncio
async def test_split_by_scene_resolves_primary_lidar_link_path(
    db_session: AsyncSession, super_admin
):
    """3D 点云 task 经 TaskDatasetItemLink(role=primary_lidar) 关联主点云,
    没有直链 dataset_item_id;by_scene 必须经 link 路径反查到 scene/frame。"""
    user, _ = super_admin
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-PL-{suffix}",
        name="primary lidar dataset",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()

    scene = Scene(
        id=uuid.uuid4(),
        display_id=f"S-PL-{suffix}",
        dataset_id=ds.id,
        name="lidar-scene",
    )
    db_session.add(scene)
    await db_session.flush()

    items: list[DatasetItem] = []
    for frame_index in (2, 0, 1):
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=ds.id,
            file_name=f"lidar-{frame_index}.pcd",
            file_path=f"/tmp/lidar-{frame_index}.pcd",
            file_type="point_cloud",
            scene_id=scene.id,
            frame_index=frame_index,
        )
        db_session.add(item)
        items.append(item)
    await db_session.flush()

    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-PL-{suffix}",
        name="primary lidar project",
        type_label="点云",
        type_key="lidar",
        data_type="lidar",
        scene_mode=True,
        owner_id=user.id,
        total_tasks=0,
    )
    db_session.add(project)
    await db_session.flush()

    # 仅经 primary_lidar link 关联,task.dataset_item_id 保持 None。
    for item in items:
        task = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=None,
            display_id=f"T-PL-{item.frame_index}-{suffix}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=task.id,
                dataset_item_id=item.id,
                role="primary_lidar",
            )
        )
    await db_session.flush()

    batches = await BatchService(db_session).split(
        project.id,
        BatchSplitRequest(strategy="by_scene", name_prefix="Scene"),
        user.id,
    )

    assert len(batches) == 1
    assert batches[0].total_tasks == 3

    # 经 link 路径反查:每个 task 的 sequence_order 必须 == 其 frame_index。
    rows = (
        await db_session.execute(
            select(Task.sequence_order, DatasetItem.frame_index)
            .join(TaskDatasetItemLink, TaskDatasetItemLink.task_id == Task.id)
            .join(DatasetItem, TaskDatasetItemLink.dataset_item_id == DatasetItem.id)
            .where(Task.batch_id == batches[0].id)
        )
    ).all()
    assert sorted(seq for seq, _ in rows) == [0, 1, 2]
    assert all(seq == frame for seq, frame in rows)


@pytest.mark.asyncio
async def test_split_by_scene_groups_scene_less_tasks_into_own_batch(
    db_session: AsyncSession, super_admin
):
    """无 scene_id 的 task 落入独立「无 scene」batch,不与 scene task 混批。"""
    user, _ = super_admin
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-NS-{suffix}",
        name="no scene dataset",
        data_type="image",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()

    scene = Scene(
        id=uuid.uuid4(),
        display_id=f"S-NS-{suffix}",
        dataset_id=ds.id,
        name="has-scene",
    )
    db_session.add(scene)
    await db_session.flush()

    for frame_index in (0, 1):
        db_session.add(
            DatasetItem(
                id=uuid.uuid4(),
                dataset_id=ds.id,
                file_name=f"s-{frame_index}.jpg",
                file_path=f"/tmp/s-{frame_index}.jpg",
                file_type="image",
                scene_id=scene.id,
                frame_index=frame_index,
            )
        )
    for n in range(2):
        db_session.add(
            DatasetItem(
                id=uuid.uuid4(),
                dataset_id=ds.id,
                file_name=f"plain-{n}.jpg",
                file_path=f"/tmp/plain-{n}.jpg",
                file_type="image",
                scene_id=None,
                frame_index=None,
            )
        )
    await db_session.flush()

    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-NS-{suffix}",
        name="no scene project",
        type_label="图像",
        type_key="image-det",
        data_type="image",
        scene_mode=True,
        owner_id=user.id,
        total_tasks=0,
    )
    db_session.add(project)
    await db_session.flush()

    await DatasetTaskCreationWorkflow(db_session).link_project(ds.id, project.id)
    batches = await BatchService(db_session).split(
        project.id,
        BatchSplitRequest(strategy="by_scene", name_prefix="Scene"),
        user.id,
    )

    assert len(batches) == 2
    by_name = {batch.name: batch for batch in batches}
    no_scene_batch = next(b for n, b in by_name.items() if "无 scene" in n)
    scene_batch = next(b for n, b in by_name.items() if "无 scene" not in n)
    assert no_scene_batch.total_tasks == 2
    assert scene_batch.total_tasks == 2
