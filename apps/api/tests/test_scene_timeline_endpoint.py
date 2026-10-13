"""GET /tasks/{id}/scene-timeline 的窗口、权限与聚合合同。"""

from __future__ import annotations

import uuid

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from tests.factory import create_membership, create_batch, create_project, create_user


def _box3d():
    return {
        "type": "box_3d",
        "center": [1.0, 2.0, 3.0],
        "size": [4.0, 5.0, 6.0],
        "rotation": [0.0, 0.0, 0.0],
        "convention_at_create": "iso_8855",
    }


async def _seed_scene(db, *, owner_id, frame_indexes, linked=False):
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="点云"
    )
    project.data_type = "lidar"
    dataset = Dataset(
        display_id=f"DS-TL-{uuid.uuid4().hex[:6]}",
        name=f"timeline-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    scene = Scene(
        display_id=f"SCN-TL-{uuid.uuid4().hex[:6]}",
        dataset_id=dataset.id,
        name=f"scene-{uuid.uuid4().hex[:6]}",
    )
    db.add(scene)
    await db.flush()

    tasks = {}
    for frame_index in frame_indexes:
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=f"{frame_index:06d}.pcd",
            file_path=f"{dataset.name}/lidar/{frame_index:06d}.pcd",
            file_type="point_cloud",
            scene_id=scene.id,
            frame_index=frame_index,
        )
        db.add(item)
        await db.flush()
        task = Task(
            project_id=project.id,
            dataset_item_id=None if linked else item.id,
            display_id=f"T-TL-{uuid.uuid4().hex[:8]}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        if linked:
            db.add(
                TaskDatasetItemLink(
                    task_id=task.id,
                    dataset_item_id=item.id,
                    role="primary_lidar",
                    sensor_name="LIDAR_TOP",
                )
            )
            await db.flush()
        tasks[frame_index] = task
    return project, dataset, scene, tasks


async def _add_annotation(
    db,
    *,
    task,
    project,
    user_id,
    annotation_type="box_3d",
    track_id=None,
    active=True,
    cancelled=False,
):
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type=annotation_type,
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry=_box3d(),
        track_id=track_id,
        is_active=active,
        was_cancelled=cancelled,
    )
    db.add(annotation)
    await db.flush()
    return annotation


async def test_scene_timeline_returns_window_density_and_selected_track(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, scene, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_indexes=range(5), linked=True
    )
    selected = await _add_annotation(
        db_session,
        task=tasks[1],
        project=project,
        user_id=user.id,
        track_id="trk_scene_car",
    )
    await _add_annotation(
        db_session,
        task=tasks[1],
        project=project,
        user_id=user.id,
        annotation_type="point_mask_3d",
    )
    await _add_annotation(
        db_session,
        task=tasks[1],
        project=project,
        user_id=user.id,
        annotation_type="bbox",
    )
    await _add_annotation(
        db_session,
        task=tasks[2],
        project=project,
        user_id=user.id,
        track_id="trk_scene_car",
        active=False,
    )

    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[2].id}/scene-timeline",
        params={"start_frame": 1, "end_frame": 3, "track_id": "trk_scene_car"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary_version"] == 1
    assert body["scene_id"] == str(scene.id)
    assert body["current_frame_index"] == 2
    assert body["scene_start_frame"] == 0
    assert body["scene_end_frame"] == 4
    assert body["populated_frame_count"] == 5
    assert [frame["frame_index"] for frame in body["frames"]] == [1, 2, 3]
    assert body["frames"][0]["annotation_count"] == 2
    assert body["frames"][0]["selected_track"] == {
        "annotation_id": str(selected.id),
        "source": "manual",
        "class_name": "car",
        "temporal_role": "sample",
    }
    assert body["frames"][0]["selected_track_present"] is False
    assert body["frames"][1]["annotation_count"] == 0
    assert body["frames"][1]["selected_track"] is None


async def test_scene_timeline_marks_sparse_frame_as_missing(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    _, _, _, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_indexes=[0, 2]
    )
    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/scene-timeline",
        params={"start_frame": 0, "end_frame": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert [frame["state"] for frame in response.json()["frames"]] == [
        "available",
        "missing",
        "available",
    ]


async def test_scene_timeline_hides_cross_batch_task_details(
    db_session, httpx_client, super_admin, annotator
):
    admin, _ = super_admin
    anno, token = annotator
    project, _, _, tasks = await _seed_scene(
        db_session, owner_id=admin.id, frame_indexes=[0, 1, 2]
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=anno.id,
        role="annotator",
        assigned_by=admin.id,
    )
    other_anno = await create_user(
        db_session, "annotator", f"other-{uuid.uuid4().hex[:6]}@test.local", "Other"
    )
    mine = await create_batch(db_session, project_id=project.id, status="active")
    mine.annotator_id = anno.id
    other = await create_batch(db_session, project_id=project.id, status="active")
    other.annotator_id = other_anno.id
    tasks[0].batch_id = other.id
    tasks[1].batch_id = mine.id
    tasks[2].batch_id = mine.id
    hidden = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=other_anno.id,
        track_id="trk_hidden",
    )
    assert hidden.id is not None
    await db_session.flush()

    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[1].id}/scene-timeline",
        params={"start_frame": 0, "end_frame": 2, "track_id": "trk_hidden"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    hidden_frame = response.json()["frames"][0]
    assert hidden_frame == {
        "frame_index": 0,
        "state": "unavailable",
        "task_id": None,
        "task_status": None,
        "annotation_count": 0,
        "selected_track": None,
        "selected_track_present": False,
    }


async def test_scene_timeline_does_not_include_tasks_from_another_project(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    _, _, _, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_indexes=[0, 1]
    )
    other_project = await create_project(
        db_session, owner_id=user.id, type_key="lidar", type_label="点云"
    )
    tasks[1].project_id = other_project.id
    await db_session.flush()

    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/scene-timeline",
        params={"start_frame": 0, "end_frame": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["frames"][1] == {
        "frame_index": 1,
        "state": "missing",
        "task_id": None,
        "task_status": None,
        "annotation_count": 0,
        "selected_track": None,
        "selected_track_present": None,
    }


async def test_scene_timeline_no_scene_returns_stable_empty_response(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project = await create_project(
        db_session, owner_id=user.id, type_key="image_detection"
    )
    task = Task(
        project_id=project.id,
        display_id=f"T-EMPTY-{uuid.uuid4().hex[:8]}",
        file_name="legacy.jpg",
        file_path="legacy.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/scene-timeline",
        params={"start_frame": 0, "end_frame": 20},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "summary_version": 1,
        "scene_id": None,
        "scene_name": None,
        "current_frame_index": None,
        "scene_start_frame": None,
        "scene_end_frame": None,
        "populated_frame_count": 0,
        "window_start_frame": None,
        "window_end_frame": None,
        "frames": [],
    }


async def test_scene_timeline_validates_window_bounds(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    _, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, frame_indexes=[0])
    url = f"/api/v1/tasks/{tasks[0].id}/scene-timeline"
    headers = {"Authorization": f"Bearer {token}"}

    reversed_response = await httpx_client.get(
        url, params={"start_frame": 2, "end_frame": 1}, headers=headers
    )
    oversized_response = await httpx_client.get(
        url, params={"start_frame": 0, "end_frame": 200}, headers=headers
    )
    negative_response = await httpx_client.get(
        url, params={"start_frame": -1, "end_frame": 1}, headers=headers
    )
    assert reversed_response.status_code == 422
    assert oversized_response.status_code == 422
    assert negative_response.status_code == 422


async def test_scene_timeline_10000_frame_extent_returns_only_requested_window(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    _, _, _, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_indexes=[0, 9999]
    )
    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[9999].id}/scene-timeline",
        params={"start_frame": 9950, "end_frame": 9999},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scene_end_frame"] == 9999
    assert len(body["frames"]) == 50
    assert body["frames"][0]["frame_index"] == 9950
    assert body["frames"][-1]["frame_index"] == 9999
