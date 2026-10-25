"""Exercise spatial quality persistence through its function-only LOGIN."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.spatial_quality_contract import (
    spatial_quality_request,
    spatial_quality_request_json,
)
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene, VideoSegment
from app.db.models.point_cloud_quality import (
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_track_quality import (
    VideoTrackQualityIssue,
    VideoTrackQualityRun,
)
from tests.factory import create_project, create_user
from tests.test_track_operations import _add_box, _seed_scene
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling spatial quality role",
    ),
]

_ROLE = "labeling_spatial_quality_client"


async def test_spatial_quality_owner_reads_and_writes_inside_one_uow(test_engine):
    dsn = _dsn(test_engine, "AAP_MODULE_DATA_LABELING_SPATIAL_QUALITY_URL", _ROLE)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    scene_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    annotation_ids: list[uuid.UUID] = []
    track_ids: list[uuid.UUID] = []
    run_ids: list[uuid.UUID] = []
    issue_ids: list[uuid.UUID] = []
    connection = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"spatial-quality-native-{uuid.uuid4().hex}@example.test",
                "Labeling spatial quality native owner",
            )
            actor_ids.append(actor.id)
            project, scene, tasks = await _seed_scene(
                db, owner_id=actor.id, frame_count=2
            )
            project_ids.append(project.id)
            scene_ids.append(scene.id)
            dataset_ids.append(scene.dataset_id)
            task_ids.extend(task.id for task in tasks.values())
            annotation = await _add_box(
                db,
                task=tasks[0],
                project=project,
                user_id=actor.id,
                track_id=f"trk-quality-{uuid.uuid4().hex}",
            )
            annotation_ids.append(annotation.id)
            track = (
                await db.execute(
                    select(SceneTrack).where(SceneTrack.scene_id == scene.id)
                )
            ).scalar_one()
            track_ids.append(track.id)
            run = PointCloudQualityRun(
                project_id=project.id,
                requested_by_id=actor.id,
                scope_json={"scope": "project", "task_ids": [], "scene_ids": []},
                config_revision=1,
                config_digest="a" * 64,
                config_snapshot={},
                source_snapshot=[],
                source_snapshot_digest="b" * 64,
                singleflight_key=uuid.uuid4().hex * 2,
                summary={},
            )
            db.add(run)
            await db.flush()
            run_ids.append(run.id)

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("spatial_quality_command", "jsonb")},
            role=_ROLE,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                "SELECT id FROM public.point_cloud_quality_runs LIMIT 1"
            )

        async def call(action: str, **values: object) -> dict:
            request = spatial_quality_request(action, **values)
            payload = await connection.fetchval(
                "SELECT aap_labeling_v1.spatial_quality_command($1::jsonb)",
                spatial_quality_request_json(request),
            )
            if isinstance(payload, (bytes, str)):
                payload = json.loads(payload)
            return payload["data"]

        source = await call("read_point_source", project_id=project_ids[0])
        assert {uuid.UUID(row["id"]) for row in source["tasks"]} == set(task_ids)
        assert scene_ids[0] in {uuid.UUID(value) for value in source["track_scene_ids"]}

        details = await call(
            "read_point_details",
            project_id=project_ids[0],
            task_ids=task_ids,
            scene_ids=scene_ids,
            track_ids=track_ids,
            annotation_ids=[],
            annotation_scope="all",
        )
        assert [
            uuid.UUID(row["id"]) for row in details["annotations"]
        ] == annotation_ids
        assert [uuid.UUID(row["id"]) for row in details["tracks"]] == track_ids

        issue_id = uuid.uuid4()
        issue_ids.append(issue_id)
        command = {
            "new_issue_id": issue_id,
            "run_id": run_ids[0],
            "project_id": project_ids[0],
            "scene_id": scene_ids[0],
            "task_id": task_ids[0],
            "annotation_id": annotation_ids[0],
            "annotation_version": 1,
            "scene_track_id": track_ids[0],
            "track_revision": 1,
            "related_annotation_ids": annotation_ids,
            "source_versions": {str(annotation_ids[0]): 1},
            "class_name": "car",
            "code": "low_point_count",
            "rule_version": 1,
            "severity": "warning",
            "severity_rank": 1,
            "frame_start": 0,
            "frame_end": 0,
            "metric": {"count": 1},
            "threshold": {"minimum": 2},
            "evidence": {},
            "locator": {"scene_id": str(scene_ids[0])},
            "suggested_command": None,
            "dedupe_key": "d" * 64,
        }
        inserted = await call("upsert_point_issue", **command)
        assert uuid.UUID(inserted["row"]["id"]) == issue_id
        assert inserted["row"]["status"] == "open"

        rollback = connection.transaction()
        await rollback.start()
        await call(
            "decide_point_issue",
            issue_id=issue_id,
            actor_id=actor_ids[0],
            review_status="resolved",
            expected_task_id=task_ids[0],
            resolution_reason="checked",
            review_verdict="confirmed",
            review_note=None,
            resolved_at=datetime.now(timezone.utc).isoformat(),
            reviewed_at=datetime.now(timezone.utc).isoformat(),
        )
        await rollback.rollback()
        current = await call("get_point_issue", issue_id=issue_id)
        assert current["row"]["status"] == "open"

        for malformed in (
            {"schema": "aap-labeling-spatial-quality/v1", "action": "get_point_run"},
            {
                "schema": "aap-labeling-spatial-quality/v1",
                "action": "get_point_run",
                "run_id": str(run_ids[0]),
                "unexpected": True,
            },
        ):
            with pytest.raises(asyncpg.PostgresError) as invalid_request:
                await connection.fetchval(
                    "SELECT aap_labeling_v1.spatial_quality_command($1::jsonb)",
                    json.dumps(malformed),
                )
            assert invalid_request.value.sqlstate == "22023"
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if issue_ids:
                await db.execute(
                    delete(PointCloudQualityIssue).where(
                        PointCloudQualityIssue.id.in_(issue_ids)
                    )
                )
            if run_ids:
                await db.execute(
                    delete(PointCloudQualityRun).where(
                        PointCloudQualityRun.id.in_(run_ids)
                    )
                )
            if annotation_ids:
                await db.execute(
                    delete(Annotation).where(Annotation.id.in_(annotation_ids))
                )
            if task_ids:
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            if track_ids:
                await db.execute(delete(SceneTrack).where(SceneTrack.id.in_(track_ids)))
            if scene_ids:
                await db.execute(delete(Scene).where(Scene.id.in_(scene_ids)))
            if dataset_ids:
                await db.execute(
                    delete(DatasetItem).where(DatasetItem.dataset_id.in_(dataset_ids))
                )
                await db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
            if project_ids:
                await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            if actor_ids:
                await db.execute(delete(User).where(User.id.in_(actor_ids)))


async def test_video_quality_owner_runs_and_issues_share_the_callers_uow(test_engine):
    dsn = _dsn(test_engine, "AAP_MODULE_DATA_LABELING_SPATIAL_QUALITY_URL", _ROLE)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    dataset_item_id = uuid.uuid4()
    task_id = uuid.uuid4()
    segment_ids = [uuid.uuid4(), uuid.uuid4()]
    run_id = uuid.uuid4()
    issue_id = uuid.uuid4()
    connection = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"video-quality-native-{uuid.uuid4().hex}@example.test",
                "Video quality native owner",
            )
            actor_id = actor.id
            project = await create_project(
                db, owner_id=actor_id, type_key="video-track"
            )
            project.data_type = "video"
            project_id = project.id
            dataset = Dataset(
                display_id=f"DS-VQ-{uuid.uuid4().hex[:8]}",
                name=f"video-quality-{uuid.uuid4().hex[:8]}",
                data_type="video",
                created_by=actor_id,
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset_id,
                file_name="clip.mp4",
                file_path=f"video-quality/{uuid.uuid4().hex}/clip.mp4",
                file_type="video",
                width=64,
                height=64,
            )
            db.add(item)
            await db.flush()
            dataset_item_id = item.id
            task = Task(
                project_id=project_id,
                dataset_item_id=dataset_item_id,
                display_id=f"T-VQ-{uuid.uuid4().hex[:8]}",
                file_name=item.file_name,
                file_path=item.file_path,
                file_type="video",
                status="in_progress",
            )
            db.add(task)
            await db.flush()
            task_id = task.id
            segments = [
                VideoSegment(
                    id=segment_ids[index],
                    dataset_item_id=dataset_item_id,
                    segment_index=index,
                    start_frame=index * 10,
                    end_frame=(index + 1) * 10 - 1,
                    status="completed",
                )
                for index in range(2)
            ]
            db.add_all(segments)
            run = VideoTrackQualityRun(
                id=run_id,
                task_id=task_id,
                left_segment_id=segment_ids[0],
                right_segment_id=segment_ids[1],
                input_snapshot=[],
                input_digest="e" * 64,
                sampling_digest="f" * 64,
                metrics={"sampling_frames": [0, 1]},
                pairs=[],
            )
            db.add(run)

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("spatial_quality_command", "jsonb")},
            role=_ROLE,
        )

        async def call(action: str, **values: object) -> dict:
            request = spatial_quality_request(action, **values)
            payload = await connection.fetchval(
                "SELECT aap_labeling_v1.spatial_quality_command($1::jsonb)",
                spatial_quality_request_json(request),
            )
            if isinstance(payload, (bytes, str)):
                payload = json.loads(payload)
            return payload["data"]

        boundary = await call(
            "read_video_boundary",
            task_id=task_id,
            dataset_item_id=dataset_item_id,
            left_segment_id=segment_ids[0],
            right_segment_id=segment_ids[1],
        )
        assert boundary["segment_count"] == 2
        assert [row["segment_index"] for row in boundary["segments"]] == [0, 1]
        annotations = await call(
            "read_video_annotations", task_id=task_id, segment_ids=segment_ids
        )
        assert annotations["annotations"] == []

        await call(
            "replace_video_issues",
            run_id=run_id,
            issue_rows=[
                {
                    "id": issue_id,
                    "left_annotation_id": None,
                    "right_annotation_id": None,
                    "code": "false_positive",
                    "frame_start": 0,
                    "frame_end": 1,
                    "metric": {"min_iou": 0.0},
                }
            ],
        )
        await call(
            "update_video_run",
            run_id=run_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            progress_pct=5,
        )
        await call(
            "update_video_run",
            run_id=run_id,
            status="completed",
            progress_pct=100,
            metrics={"aggregates": {}},
            pairs=[],
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        issues = await call("list_video_issues", run_id=run_id)
        assert [uuid.UUID(row["id"]) for row in issues["rows"]] == [issue_id]

        rollback = connection.transaction()
        await rollback.start()
        await call(
            "update_video_run",
            run_id=run_id,
            status="stale",
            stale_at=datetime.now(timezone.utc).isoformat(),
        )
        await rollback.rollback()
        run = await call("get_video_run", run_id=run_id)
        assert run["row"]["status"] == "completed"

        await call("mark_video_runs_stale", segment_ids=[segment_ids[0]])
        run = await call("get_video_run", run_id=run_id)
        assert run["row"]["status"] == "stale"
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            await db.execute(
                delete(VideoTrackQualityIssue).where(
                    VideoTrackQualityIssue.id == issue_id
                )
            )
            await db.execute(
                delete(VideoTrackQualityRun).where(VideoTrackQualityRun.id == run_id)
            )
            await db.execute(
                delete(VideoSegment).where(VideoSegment.id.in_(segment_ids))
            )
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(
                delete(DatasetItem).where(DatasetItem.id == dataset_item_id)
            )
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.id == actor_id))
