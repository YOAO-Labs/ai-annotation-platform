"""Owned-role acceptance proof for atomic candidate Annotation and Job writes."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.video_control_contract import (
    ApplyReview,
    SourceAnnotationWrite,
)
from aap_labeling.video_control_data import VideoControlDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from tests.test_video_input_authorities_native import _seed_video_task
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling video-control role",
    ),
]

_ROLE = "labeling_video_control_client"


async def test_candidate_accept_uses_owner_and_preserves_outer_rollback(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    job_id = uuid.uuid4()
    annotation_id = uuid.uuid4()
    actor_id = None
    project_id = None
    dataset_id = None
    item_id = None
    task_id = None
    connection = None
    try:
        async with maker.begin() as db:
            actor_id, project, dataset_id, item_id, task_id = await _seed_video_task(
                db, status="in_progress"
            )
            project_id = project.id
            annotation = Annotation(
                id=annotation_id,
                task_id=task_id,
                project_id=project_id,
                user_id=actor_id,
                source="manual",
                annotation_type="video_track_bbox",
                tool_unit_id="bbox",
                class_name="car",
                geometry={
                    "type": "video_track_bbox",
                    "track_id": "trk-video-control-native",
                    "keyframes": [],
                    "outside": [],
                },
                track_id="trk-video-control-native",
                version=1,
            )
            db.add_all(
                [
                    annotation,
                    VideoTrackerJob(
                        id=job_id,
                        task_id=task_id,
                        dataset_item_id=item_id,
                        annotation_id=annotation_id,
                        created_by=actor_id,
                        status="pending_review",
                        model_key="mock_bbox",
                        direction="forward",
                        from_frame=0,
                        to_frame=1,
                        prompt={"expected_source_versions": {str(annotation_id): 1}},
                        staged_result={"results": [{"frame_index": 0}]},
                        event_channel=f"video-control-native:{job_id}",
                    ),
                ]
            )
            await db.flush()

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_VIDEO_CONTROL_URL",
                _ROLE,
            ),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("video_control_command", "jsonb")},
            role=_ROLE,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(
                "SELECT id FROM public.video_tracker_jobs LIMIT 1"
            )

        async def execute(payload):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.video_control_command($1::jsonb)", payload
            )

        client = VideoControlDataClient(execute)
        request = ApplyReview(
            schema="aap-labeling-video-control/v1",
            operation="apply_review",
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            kind="accept",
            expected_revision=1,
            expected_status="pending_review",
            prompt={"expected_source_versions": {str(annotation_id): 2}},
            staged_result=None,
            status="accepted",
            source_annotations=(
                SourceAnnotationWrite(
                    annotation_id=annotation_id,
                    expected_version=1,
                    geometry={
                        "type": "video_track_bbox",
                        "track_id": "trk-video-control-native",
                        "keyframes": [
                            {
                                "frame_index": 0,
                                "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                                "source": "prediction",
                                "occluded": False,
                            }
                        ],
                        "outside": [],
                    },
                    track_id="trk-video-control-native",
                    annotation_type="video_track_bbox",
                    tool_unit_id="bbox",
                ),
            ),
            new_tracks=(),
        )

        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await client.command(request)
        assert rolled_back["status"] == "accepted"
        await transaction.rollback()
        async with maker() as db:
            annotation = await db.get(Annotation, annotation_id)
            job = await db.get(VideoTrackerJob, job_id)
            assert annotation.version == 1
            assert job.status == "pending_review"

        transaction = connection.transaction()
        await transaction.start()
        committed = await client.command(request)
        await transaction.commit()
        assert committed["status"] == "accepted"

        async with maker() as db:
            annotation = await db.get(Annotation, annotation_id)
            job = await db.get(VideoTrackerJob, job_id)
            assert annotation.version == 2
            assert annotation.geometry["keyframes"][0]["frame_index"] == 0
            assert job.status == "accepted"
            assert job.staged_result is None
    finally:
        if connection is not None:
            await connection.close()
        async with test_engine.begin() as cleanup:
            if job_id is not None:
                await cleanup.execute(
                    delete(VideoTrackerJob).where(VideoTrackerJob.id == job_id)
                )
            if annotation_id is not None:
                await cleanup.execute(
                    delete(Annotation).where(Annotation.id == annotation_id)
                )
            if task_id is not None:
                await cleanup.execute(delete(Task).where(Task.id == task_id))
            if item_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.video_segments WHERE dataset_item_id=:id"),
                    {"id": item_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.dataset_items WHERE id=:id"),
                    {"id": item_id},
                )
            if dataset_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.datasets WHERE id=:id"),
                    {"id": dataset_id},
                )
            if project_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.projects WHERE id=:id"),
                    {"id": project_id},
                )
            if actor_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.users WHERE id=:id"),
                    {"id": actor_id},
                )
