"""Cross-resource scope regressions for task and point-cloud SDK endpoints."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.prediction import Prediction
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from tests.factory import create_membership, create_project, create_task


async def _create_scene_with_tasks(
    db,
    *,
    project,
    owner_id,
    name: str,
    frame_indexes: list[int],
    assignee_id=None,
):
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-SDK-{suffix}",
        name=f"sdk-scene-{name}-{suffix}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    scene = Scene(
        display_id=f"SCN-SDK-{suffix}",
        dataset_id=dataset.id,
        name=f"sdk-scene-{name}-{suffix}",
    )
    db.add(scene)
    await db.flush()

    tasks = {}
    for frame_index in frame_indexes:
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=f"{frame_index:06d}.pcd",
            file_path=f"sdk-scene/{suffix}/{frame_index:06d}.pcd",
            file_type="point_cloud",
            scene_id=scene.id,
            frame_index=frame_index,
        )
        db.add(item)
        await db.flush()
        task = Task(
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-SDK-{uuid.uuid4().hex[:8]}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
            status="in_progress",
            assignee_id=assignee_id,
        )
        db.add(task)
        await db.flush()
        tasks[frame_index] = task
    return dataset, scene, tasks


async def test_camera_members_reject_track_from_another_scene_in_same_project(
    db_session, httpx_client, super_admin
):
    owner, token = super_admin
    project = await create_project(
        db_session, owner_id=owner.id, type_key="lidar", type_label="Point cloud"
    )
    project.data_type = "lidar"
    _, task_scene, tasks = await _create_scene_with_tasks(
        db_session,
        project=project,
        owner_id=owner.id,
        name="task",
        frame_indexes=[0],
    )
    _, foreign_scene, _ = await _create_scene_with_tasks(
        db_session,
        project=project,
        owner_id=owner.id,
        name="foreign",
        frame_indexes=[0],
    )
    foreign_track = SceneTrack(
        project_id=project.id,
        scene_id=foreign_scene.id,
        track_id=f"trk_{uuid.uuid4().hex[:12]}",
        class_name="car",
        presence_mode="explicit",
        revision=7,
        created_by=owner.id,
    )
    db_session.add(foreign_track)
    await db_session.flush()
    assert task_scene.id != foreign_track.scene_id

    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/point-cloud/camera-members",
        params={"scene_track_id": str(foreign_track.id)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404, response.text


async def test_prediction_accept_rejects_foreign_task_before_write_or_heartbeat(
    db_session, httpx_client, super_admin, monkeypatch
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task_a = await create_task(db_session, project_id=project.id, status="pending")
    task_b = await create_task(db_session, project_id=project.id, status="pending")

    def _prediction(task):
        return Prediction(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            ml_backend_id=None,
            model_version="sdk-contract-test",
            score=0.9,
            result=[
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 1,
                        "y": 2,
                        "width": 20,
                        "height": 30,
                        "rectanglelabels": ["car"],
                    },
                    "score": 0.9,
                }
            ],
        )

    foreign_prediction = _prediction(task_b)
    local_prediction = _prediction(task_a)
    db_session.add_all([foreign_prediction, local_prediction])
    await db_session.flush()

    from app.api.v1.tasks import predictions as predictions_api

    heartbeat_calls = []

    async def _record_heartbeat(_db, task, user_id):
        heartbeat_calls.append((task.id, user_id))

    monkeypatch.setattr(
        predictions_api, "heartbeat_task_lock_for_legacy_video", _record_heartbeat
    )
    headers = {"Authorization": f"Bearer {token}"}
    wrong_parent = await httpx_client.post(
        f"/api/v1/tasks/{task_a.id}/predictions/{foreign_prediction.id}/accept?shape_index=0",
        headers=headers,
    )
    wrong_task_annotations = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task_b.id)
    )
    assert wrong_parent.status_code == 404, wrong_parent.text
    assert wrong_task_annotations == 0
    assert heartbeat_calls == []

    correct_parent = await httpx_client.post(
        f"/api/v1/tasks/{task_a.id}/predictions/{local_prediction.id}/accept?shape_index=0",
        headers=headers,
    )
    task_a_annotations = await db_session.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.task_id == task_a.id)
    )
    assert correct_parent.status_code == 200, correct_parent.text
    assert len(correct_parent.json()) == 1
    assert task_a_annotations == 1
    assert heartbeat_calls == [(task_a.id, owner.id)]


async def test_task_predictions_requires_api_key_annotation_read_scope(
    db_session, httpx_client, super_admin
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="sdk-contract-test",
        score=0.8,
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 2,
                    "y": 3,
                    "width": 10,
                    "height": 11,
                    "rectanglelabels": ["car"],
                },
                "score": 0.8,
            }
        ],
    )
    db_session.add(prediction)
    await db_session.flush()

    async def _api_key(scopes):
        response = await httpx_client.post(
            "/api/v1/me/api-keys",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": f"sdk-contract-{uuid.uuid4().hex[:8]}", "scopes": scopes},
        )
        assert response.status_code == 201, response.text
        return response.json()["plaintext"]

    no_read_key = await _api_key([])
    read_key = await _api_key(["annotations:read"])

    denied = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions",
        headers={"Authorization": f"Bearer {no_read_key}"},
    )
    allowed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/predictions",
        headers={"Authorization": f"Bearer {read_key}"},
    )

    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 200, allowed.text
    assert len(allowed.json()) == 1


async def test_scene_timeline_hides_selected_interval_on_unavailable_task(
    db_session, httpx_client, super_admin, annotator
):
    owner, _owner_token = super_admin
    worker, worker_token = annotator
    project = await create_project(
        db_session, owner_id=owner.id, type_key="lidar", type_label="Point cloud"
    )
    project.data_type = "lidar"
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=worker.id,
        role="annotator",
        assigned_by=owner.id,
    )
    _, scene, tasks = await _create_scene_with_tasks(
        db_session,
        project=project,
        owner_id=owner.id,
        name="timeline",
        frame_indexes=[0, 1],
        assignee_id=worker.id,
    )
    # This same-project frame exists in the scene but is not assigned to this user.
    tasks[1].assignee_id = None
    track_id = f"trk_{uuid.uuid4().hex[:12]}"
    track = SceneTrack(
        project_id=project.id,
        scene_id=scene.id,
        track_id=track_id,
        class_name="car",
        presence_mode="explicit",
        revision=2,
        created_by=owner.id,
    )
    db_session.add(track)
    await db_session.flush()
    interval = SceneTrackInterval(
        scene_track_id=track.id,
        start_frame=0,
        end_frame=1,
        source="manual",
        version=2,
        created_by=owner.id,
    )
    db_session.add(interval)
    await db_session.flush()

    response = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/scene-timeline",
        params={"start_frame": 0, "end_frame": 1, "track_id": track_id},
        headers={"Authorization": f"Bearer {worker_token}"},
    )

    assert response.status_code == 200, response.text
    frames = response.json()["frames"]
    assert frames[0]["state"] == "available"
    assert frames[0]["selected_track_present"] is True
    assert frames[1]["state"] == "unavailable"
    assert frames[1]["selected_track_present"] is None
