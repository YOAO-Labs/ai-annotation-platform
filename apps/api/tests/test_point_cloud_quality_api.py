from __future__ import annotations

from types import SimpleNamespace
import uuid

import numpy as np
from sqlalchemy import select, update

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.point_cloud_quality import PointCloudQualityIssue
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.schemas._jsonb_types import SensorCalibration
from app.schemas.point_cloud_quality import (
    PointCloudQualityConfig,
    PointCloudQualityRunRequest,
)
from app.services import async_job as async_job_svc
from app.services.point_cloud_quality.service import (
    create_quality_run,
    list_issues,
    refresh_issue_staleness,
)
from app.services.sensor_calibration import calibration_digest
from app.services.task_dataset_link import link_items
from app.workers.point_cloud_quality import (
    _cancel_requested,
    _normalize_points,
    execute_scan,
)
from tests.factory import create_membership, create_project


def _box(center_x: float = 0) -> dict:
    return {
        "type": "box_3d",
        "center": [center_x, 0.0, 1.2],
        "size": [4.0, 2.0, 2.0],
        "rotation": [0.0, 0.0, 0.0],
        "convention_at_create": "iso_8855",
    }


async def _seed_quality_scene(db, *, owner_id):
    project = await create_project(db, owner_id=owner_id, type_key="lidar")
    project.data_type = "lidar"
    dataset = Dataset(
        display_id=f"DS-Q3-{uuid.uuid4().hex[:6]}",
        name=f"quality-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
        metadata_={"axis_convention": "iso_8855"},
    )
    db.add(dataset)
    await db.flush()
    db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    scene = Scene(
        display_id=f"SCN-Q3-{uuid.uuid4().hex[:6]}",
        dataset_id=dataset.id,
        name=f"scene-{uuid.uuid4().hex[:6]}",
    )
    db.add(scene)
    await db.flush()
    tasks: dict[int, Task] = {}
    for frame in range(3):
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=f"{frame:06d}.pcd",
            file_path=f"quality/{frame:06d}.pcd",
            file_type="point_cloud",
            content_hash=f"hash-{frame}",
            scene_id=scene.id,
            frame_index=frame,
        )
        db.add(item)
        await db.flush()
        task = Task(
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-Q3-{uuid.uuid4().hex[:7]}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        tasks[frame] = task
    track = SceneTrack(
        project_id=project.id,
        scene_id=scene.id,
        track_id="car-1",
        class_name="car",
        presence_mode="explicit",
        revision=2,
        created_by=owner_id,
    )
    db.add(track)
    await db.flush()
    db.add(
        SceneTrackInterval(
            scene_track_id=track.id,
            start_frame=0,
            end_frame=2,
            source="manual",
            version=2,
            created_by=owner_id,
        )
    )
    annotations = []
    for frame, center_x in ((0, 0.0), (2, 20.0)):
        row = Annotation(
            task_id=tasks[frame].id,
            project_id=project.id,
            user_id=owner_id,
            source="manual",
            annotation_type="box_3d",
            tool_unit_id="lidar_box_3d",
            class_name="car",
            geometry=_box(center_x),
            track_id="car-1",
            scene_track_id=track.id,
            temporal_role="keyframe",
        )
        db.add(row)
        await db.flush()
        annotations.append(row)
    return project, scene, tasks, track, annotations


async def test_projection_residual_scan_and_staleness(
    db_session, super_admin, monkeypatch
):
    user, _token = super_admin
    project, scene, tasks, track, annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    task = tasks[0]
    source = annotations[0]
    source.geometry = _box(10.0)
    primary = await db_session.get(DatasetItem, task.dataset_item_id)
    assert primary is not None
    calibration = SensorCalibration.model_validate(
        {
            "extrinsic": [
                0,
                -1,
                0,
                0,
                0,
                0,
                -1,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
                1,
            ],
            "intrinsic": [100, 0, 100, 0, 100, 60, 0, 0, 1],
        }
    )
    digest = calibration_digest(calibration)
    camera = DatasetItem(
        dataset_id=primary.dataset_id,
        file_name="front.jpg",
        file_path="quality/front.jpg",
        file_type="image",
        width=200,
        height=120,
        scene_id=scene.id,
        frame_index=0,
        metadata_={"calibration": calibration.model_dump(mode="json")},
    )
    db_session.add(camera)
    await db_session.flush()
    await link_items(
        db_session,
        task.id,
        [(camera.id, "camera_front", "CAM_FRONT")],
    )
    member = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry={"type": "bbox", "x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1},
        track_id=source.track_id,
        scene_track_id=track.id,
        temporal_role="sample",
        sensor_dataset_item_id=camera.id,
        sensor_role="camera_front",
        sensor_visibility="visible",
        calibration_revision=1,
        calibration_digest=digest,
    )
    db_session.add(member)
    track.revision += 1
    await db_session.flush()

    run, job, created = await create_quality_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=PointCloudQualityRunRequest(scope="task_ids", task_ids=[task.id]),
    )
    assert created is True and job is not None
    monkeypatch.setattr(
        "app.workers.point_cloud_quality._read_pointcloud",
        lambda _key: np.asarray([(100.0, 0.0, 0.0)], dtype=np.float32),
    )
    run.status = "running"
    await async_job_svc.mark_running(db_session, job.id)
    await execute_scan(db_session, run)

    issue = (
        await db_session.execute(
            select(PointCloudQualityIssue).where(
                PointCloudQualityIssue.project_id == project.id,
                PointCloudQualityIssue.code == "projection_residual",
            )
        )
    ).scalar_one()
    assert issue.locator["camera"] == "camera_front"
    assert issue.evidence["camera_calibration_digest"] == digest
    assert str(member.id) in issue.source_versions
    assert await refresh_issue_staleness(db_session, issue) is False

    member.version += 1
    await db_session.flush()
    assert await refresh_issue_staleness(db_session, issue) is True


async def test_issue_status_filter_refreshes_stale_rows_before_pagination(
    db_session, super_admin
):
    user, _token = super_admin
    project, scene, tasks, _track, annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    source = annotations[0]
    common = {
        "project_id": project.id,
        "scene_id": scene.id,
        "task_id": tasks[0].id,
        "annotation_id": source.id,
        "annotation_version": source.version,
        "related_annotation_ids": [source.id],
        "code": "low_point_count",
        "frame_start": 0,
        "frame_end": 0,
        "metric": {"point_count": 0},
        "threshold": {"minimum_points": 5},
        "evidence": {},
        "locator": {"task_id": str(tasks[0].id)},
    }
    stale_first = PointCloudQualityIssue(
        **common,
        source_versions={str(source.id): source.version + 1},
        severity="blocker",
        severity_rank=0,
        dedupe_key="b" * 64,
    )
    valid_second = PointCloudQualityIssue(
        **common,
        source_versions={str(source.id): source.version},
        severity="warning",
        severity_rank=1,
        dedupe_key="c" * 64,
    )
    db_session.add_all([stale_first, valid_second])
    await db_session.flush()

    rows, total = await list_issues(
        db_session,
        project_id=project.id,
        status="open",
        limit=1,
        offset=0,
    )

    assert [row.id for row in rows] == [valid_second.id]
    assert total == 1
    assert stale_first.status == "stale"


async def test_quality_run_singleflight_and_worker_track_findings(
    db_session, super_admin, monkeypatch
):
    user, _token = super_admin
    project, scene, tasks, track, _annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    request = PointCloudQualityRunRequest(scope="scene_ids", scene_ids=[scene.id])
    first, job, created = await create_quality_run(
        db_session, project=project, actor_id=user.id, request=request
    )
    second, second_job, second_created = await create_quality_run(
        db_session, project=project, actor_id=user.id, request=request
    )
    assert created is True and job is not None
    assert second_created is False and second_job is None and second.id == first.id
    pointcloud_record = next(
        row
        for row in first.source_snapshot
        if row["kind"] == "task" and row["pointcloud"] is not None
    )
    assert pointcloud_record["pointcloud"]["axis_convention"] == "iso_8855"

    ground = np.asarray(
        [(100 + x, y, 0.0) for x in np.linspace(-3, 3, 12) for y in (-1.0, 0.0, 1.0)],
        dtype=np.float32,
    )
    monkeypatch.setattr(
        "app.workers.point_cloud_quality._read_pointcloud", lambda _key: ground
    )
    first.status = "running"
    await async_job_svc.mark_running(db_session, job.id)
    summary = await execute_scan(db_session, first)
    codes = set(
        (
            await db_session.execute(
                select(PointCloudQualityIssue.code).where(
                    PointCloudQualityIssue.project_id == project.id
                )
            )
        ).scalars()
    )
    assert {"track_gap", "temporal_jump"}.issubset(codes)
    assert summary["issues_by_code"]["track_gap"] == 1
    gap = (
        await db_session.execute(
            select(PointCloudQualityIssue).where(
                PointCloudQualityIssue.scene_track_id == track.id,
                PointCloudQualityIssue.code == "track_gap",
            )
        )
    ).scalar_one()
    assert gap.locator["scene_id"] == str(scene.id)
    assert gap.locator["frame_index"] == 1
    point_issue = (
        (
            await db_session.execute(
                select(PointCloudQualityIssue).where(
                    PointCloudQualityIssue.project_id == project.id,
                    PointCloudQualityIssue.code == "low_point_count",
                )
            )
        )
        .scalars()
        .first()
    )
    assert point_issue is not None
    assert point_issue.severity == "blocker"
    assert point_issue.evidence["axis_convention"] == "iso_8855"
    item = await db_session.get(DatasetItem, tasks[0].dataset_item_id)
    assert item is not None
    item.content_hash = "changed-content"
    await db_session.flush()
    assert await refresh_issue_staleness(db_session, point_issue) is True

    next_config = PointCloudQualityConfig().model_dump(mode="json")
    next_config["config_revision"] = 2
    next_config["thresholds"]["minimum_points"] = 99
    project.point_cloud_quality_config = next_config
    await db_session.flush()
    assert await refresh_issue_staleness(db_session, gap) is True


async def test_quality_api_lists_locates_and_requires_wont_fix_reason(
    db_session, httpx_client, super_admin, annotator, monkeypatch
):
    user, token = super_admin
    other_user, other_token = annotator
    project, scene, tasks, track, annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    dispatched: list[str] = []

    def fake_apply_async(*, args, task_id):
        dispatched.append(task_id)
        return SimpleNamespace(id=task_id, args=args)

    monkeypatch.setattr(
        "app.workers.point_cloud_quality.run_point_cloud_quality.apply_async",
        fake_apply_async,
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    config = PointCloudQualityConfig().model_dump(mode="json")
    config["thresholds"]["minimum_points"] = 7
    response = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"point_cloud_quality_config": config},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["point_cloud_quality_config"]["config_revision"] == 2
    response = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"point_cloud_quality_config": config},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == (
        "point_cloud_quality_config_revision_conflict"
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/runs",
        json={"scope": "scene_ids", "scene_ids": [str(scene.id)]},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    assert dispatched

    issue = PointCloudQualityIssue(
        run_id=uuid.UUID(response.json()["id"]),
        last_seen_run_id=uuid.UUID(response.json()["id"]),
        project_id=project.id,
        scene_id=scene.id,
        task_id=tasks[0].id,
        annotation_id=annotations[0].id,
        annotation_version=annotations[0].version,
        scene_track_id=track.id,
        track_revision=track.revision,
        related_annotation_ids=[annotations[0].id],
        source_versions={str(annotations[0].id): annotations[0].version},
        code="low_point_count",
        severity="warning",
        severity_rank=1,
        frame_start=0,
        frame_end=0,
        metric={"point_count": 0},
        threshold={"minimum_points": 5},
        evidence={},
        locator={
            "scene_id": str(scene.id),
            "frame_index": 0,
            "task_id": str(tasks[0].id),
            "annotation_id": str(annotations[0].id),
            "scene_track_id": str(track.id),
            "camera": None,
            "auxiliary_layers": [],
        },
        suggested_command="inspect_box_or_mark_absent",
        dedupe_key="a" * 64,
    )
    db_session.add(issue)
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/issues",
        params={"scene_id": str(scene.id), "frame": 0},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["locator"]["annotation_id"] == str(
        annotations[0].id
    )

    response = await httpx_client.patch(
        f"/api/v1/point-cloud-quality/issues/{issue.id}",
        json={"status": "wont_fix"},
        headers=headers,
    )
    assert response.status_code == 422
    response = await httpx_client.patch(
        f"/api/v1/point-cloud-quality/issues/{issue.id}",
        json={"status": "wont_fix", "reason": "known sparse return"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "wont_fix"
    assert response.json()["review_verdict"] is None
    response = await httpx_client.patch(
        f"/api/v1/point-cloud-quality/issues/{issue.id}",
        json={"status": "open"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.patch(
        f"/api/v1/point-cloud-quality/issues/{issue.id}",
        json={
            "status": "resolved",
            "review_verdict": "confirmed",
            "review_note": "box is genuinely empty",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_verdict"] == "confirmed"
    assert response.json()["reviewed_by_id"] == str(user.id)

    feedback_payload = {
        "kind": "comment",
        "anchor_type": "point_cloud",
        "project_id": str(project.id),
        "task_id": str(tasks[0].id),
        "annotation_id": str(annotations[0].id),
        "anchor_position": {
            "frame": 0,
            "point_cloud_quality_issue_id": str(issue.id),
            "scene_id": str(scene.id),
            "scene_track_id": str(track.id),
            "auxiliary_layers": [],
        },
        "body": "复核后确认稀疏回波",
    }
    await create_membership(
        db_session, project_id=project.id, user_id=other_user.id, role="annotator"
    )
    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/feedbacks",
        json=feedback_payload,
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 404

    response = await httpx_client.post(
        "/api/v1/feedbacks",
        json=feedback_payload,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["anchor_type"] == "point_cloud"
    assert response.json()["anchor_position"] == feedback_payload["anchor_position"]
    response = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": str(project.id), "anchor_type": "point_cloud"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []

    annotations[0].version += 1
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/point-cloud-quality/issues/{issue.id}", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    response = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "point_cloud",
            "project_id": str(project.id),
            "task_id": str(tasks[0].id),
            "annotation_id": str(annotations[0].id),
            "anchor_position": {
                "frame": 0,
                "point_cloud_quality_issue_id": str(issue.id),
                "scene_id": str(scene.id),
                "scene_track_id": str(track.id),
                "auxiliary_layers": [],
            },
            "body": "过期问题不应追加评论",
        },
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "point_cloud_quality_issue_stale"


def test_quality_worker_normalizes_source_points_to_iso() -> None:
    points = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)
    np.testing.assert_allclose(
        _normalize_points(points, "apollo"),
        [[2.0, -1.0, 3.0]],
    )


async def test_quality_run_can_be_cancelled_through_async_jobs(
    db_session, httpx_client, super_admin, monkeypatch
) -> None:
    user, token = super_admin
    project, scene, _tasks, _track, _annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    dispatched: list[str] = []
    revoked: list[str] = []

    def fake_apply_async(*, args, task_id):
        dispatched.append(task_id)
        return SimpleNamespace(id=task_id, args=args)

    monkeypatch.setattr(
        "app.workers.point_cloud_quality.run_point_cloud_quality.apply_async",
        fake_apply_async,
    )
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.control.revoke",
        lambda task_id, terminate=False: revoked.append(task_id),
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/runs",
        json={"scope": "scene_ids", "scene_ids": [str(scene.id)]},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{body['async_job_id']}/cancel",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert revoked == dispatched
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/runs/{body['id']}",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_task_scope_skips_incomplete_track_rules_without_staling_scene_issues(
    db_session, super_admin, monkeypatch
) -> None:
    user, _token = super_admin
    project, scene, tasks, _track, _annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    points = np.zeros((32, 3), dtype=np.float32)
    monkeypatch.setattr(
        "app.workers.point_cloud_quality._read_pointcloud", lambda _key: points
    )

    scene_run, _scene_job, _created = await create_quality_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=PointCloudQualityRunRequest(scope="scene_ids", scene_ids=[scene.id]),
    )
    await execute_scan(db_session, scene_run)
    gap = (
        await db_session.execute(
            select(PointCloudQualityIssue).where(
                PointCloudQualityIssue.project_id == project.id,
                PointCloudQualityIssue.code == "track_gap",
            )
        )
    ).scalar_one()

    task_run, _task_job, _created = await create_quality_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=PointCloudQualityRunRequest(scope="task_ids", task_ids=[tasks[0].id]),
    )
    summary = await execute_scan(db_session, task_run)
    await db_session.refresh(gap)
    assert summary["skips"]["track_rules:scope_incomplete"] == 1
    assert gap.last_seen_run_id == scene_run.id
    assert gap.status == "open"


async def test_cancel_probe_reads_database_instead_of_stale_identity_map(
    db_session, super_admin
) -> None:
    user, _token = super_admin
    project, scene, _tasks, _track, _annotations = await _seed_quality_scene(
        db_session, owner_id=user.id
    )
    run, job, _created = await create_quality_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=PointCloudQualityRunRequest(scope="scene_ids", scene_ids=[scene.id]),
    )
    assert job is not None
    job.status = "running"
    await db_session.flush()
    await db_session.execute(
        update(AsyncJob)
        .where(AsyncJob.id == job.id)
        .values(status="cancelled")
        .execution_options(synchronize_session=False)
    )
    assert job.status == "running"
    assert await _cancel_requested(db_session, run) is True
