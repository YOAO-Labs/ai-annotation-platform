"""Owned-role acceptance proof for atomic candidate Annotation and Job writes."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.video_control_contract import (
    ApplyReview,
    ClaimVideoSegment,
    HeartbeatVideoSegment,
    LockWorkerJob,
    PrepareVideoSegments,
    ReadWorkerJob,
    ReadWorkerSegment,
    ReleaseVideoSegment,
    ReopenVideoSegment,
    StartWorkerJob,
    SubmitVideoSegment,
    UnassignVideoSegment,
    SourceAnnotationWrite,
)
from aap_labeling.video_control_data import VideoControlDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.dataset import VideoSegment
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from tests.factory import create_user
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
            functions={
                ("video_control_command", "jsonb"),
                ("video_segment_lifecycle_command", "jsonb"),
            },
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


async def test_worker_video_controls_keep_locks_and_caller_commit(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    job_id = uuid.uuid4()
    actor_id = None
    project_id = None
    dataset_id = None
    item_id = None
    task_id = None
    connection = None
    second_connection = None
    segment_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    later_start = started_at + timedelta(minutes=1)
    try:
        async with maker.begin() as db:
            actor_id, project, dataset_id, item_id, task_id = await _seed_video_task(
                db, status="in_progress"
            )
            project_id = project.id
            db.add_all(
                [
                    VideoSegment(
                        id=segment_id,
                        dataset_item_id=item_id,
                        segment_index=0,
                        start_frame=0,
                        end_frame=9,
                        status="locked",
                        locked_by=actor_id,
                        locked_at=started_at,
                        lock_expires_at=later_start,
                    ),
                    VideoTrackerJob(
                        id=job_id,
                        task_id=task_id,
                        dataset_item_id=item_id,
                        segment_id=segment_id,
                        created_by=actor_id,
                        status="queued",
                        model_key="mock_bbox",
                        direction="forward",
                        from_frame=0,
                        to_frame=9,
                        prompt={},
                        event_channel=f"video-control-worker:{job_id}",
                    ),
                ]
            )
            await db.flush()

        dsn = _dsn(test_engine, "AAP_MODULE_DATA_LABELING_VIDEO_CONTROL_URL", _ROLE)
        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        second_connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={
                ("video_control_command", "jsonb"),
                ("video_segment_lifecycle_command", "jsonb"),
            },
            role=_ROLE,
        )
        for table in ("public.video_tracker_jobs", "public.video_segments"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetchval(f"SELECT id FROM {table} LIMIT 1")

        async def client_for(conn):
            async def execute(payload):
                return await conn.fetchval(
                    "SELECT aap_labeling_v1.video_control_command($1::jsonb)",
                    payload,
                )

            return VideoControlDataClient(execute)

        client = await client_for(connection)
        second_client = await client_for(second_connection)
        missing = await client.command(
            ReadWorkerJob(
                schema="aap-labeling-video-control/v1",
                operation="read_worker_job",
                job_id=uuid.uuid4(),
            )
        )
        assert missing is None

        lock = connection.transaction()
        await lock.start()
        row = await client.command(
            LockWorkerJob(
                schema="aap-labeling-video-control/v1",
                operation="lock_worker_job",
                job_id=job_id,
            )
        )
        assert row["status"] == "queued"
        with pytest.raises(asyncpg.PostgresError, match="aap_video_control_error"):
            await second_client.command(
                LockWorkerJob(
                    schema="aap-labeling-video-control/v1",
                    operation="lock_worker_job",
                    job_id=job_id,
                )
            )
        await lock.rollback()

        transaction = connection.transaction()
        await transaction.start()
        first_start = await client.command(
            StartWorkerJob(
                schema="aap-labeling-video-control/v1",
                operation="start_worker_job",
                job_id=job_id,
                celery_task_id="celery-first",
                started_at=started_at,
            )
        )
        assert first_start["status"] == "running"
        await transaction.rollback()
        async with maker() as db:
            rolled_back = await db.get(VideoTrackerJob, job_id)
            assert rolled_back.status == "queued"
            assert rolled_back.celery_task_id is None
            assert rolled_back.started_at is None

        await client.command(
            StartWorkerJob(
                schema="aap-labeling-video-control/v1",
                operation="start_worker_job",
                job_id=job_id,
                celery_task_id="celery-first",
                started_at=started_at,
            )
        )
        unchanged_start = await client.command(
            StartWorkerJob(
                schema="aap-labeling-video-control/v1",
                operation="start_worker_job",
                job_id=job_id,
                celery_task_id="celery-second",
                started_at=later_start,
            )
        )
        assert unchanged_start["celery_task_id"] == "celery-first"
        assert unchanged_start["started_at"] == started_at

        async with connection.transaction():
            snapshot = await client.command(
                ReadWorkerSegment(
                    schema="aap-labeling-video-control/v1",
                    operation="read_worker_segment",
                    job_id=job_id,
                )
            )
            assert snapshot["segment"]["id"] == segment_id
            assert snapshot["segment_count"] == 1
            async with maker.begin() as db:
                await db.execute(
                    text("UPDATE public.video_segments SET status='open' WHERE id=:id"),
                    {"id": segment_id},
                )
    finally:
        if second_connection is not None:
            await second_connection.close()
        if connection is not None:
            await connection.close()
        async with test_engine.begin() as cleanup:
            await cleanup.execute(
                delete(VideoTrackerJob).where(VideoTrackerJob.id == job_id)
            )
            await cleanup.execute(
                delete(VideoSegment).where(VideoSegment.id == segment_id)
            )
            if task_id is not None:
                await cleanup.execute(delete(Task).where(Task.id == task_id))
            if item_id is not None:
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


async def test_video_segment_lifecycle_uses_owner_and_preserves_outer_transaction(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    project_id = None
    dataset_id = None
    item_id = None
    task_id = None
    connection = None
    annotator_id = None
    viewer_id = None
    try:
        async with maker.begin() as db:
            actor_id, project, dataset_id, item_id, task_id = await _seed_video_task(
                db, status="pending"
            )
            project_id = project.id
            project.video_collaboration = {"enabled": True}
            viewer = await create_user(
                db,
                "employee",
                f"video-segment-viewer-{uuid.uuid4().hex[:10]}@example.test",
                "Video segment viewer",
            )
            viewer_id = viewer.id
            db.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=viewer_id,
                    role="viewer",
                    assigned_by=actor_id,
                )
            )
            annotator = await create_user(
                db,
                "employee",
                f"video-segment-native-{uuid.uuid4().hex[:10]}@example.test",
                "Video segment annotator",
            )
            annotator_id = annotator.id
            db.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=annotator_id,
                    role="annotator",
                    assigned_by=actor_id,
                )
            )
            task = await db.get(Task, task_id)
            task.status = "in_progress"
            task.assignee_id = annotator_id
            await db.flush()
            task_scope = {
                "task_id": task.id,
                "project_id": task.project_id,
                "expected_batch_id": task.batch_id,
                "expected_task_status": task.status,
                "expected_review_round_id": task.review_round_id,
                "dataset_item_id": item_id,
            }
            preparation_scope = {
                "task_id": task.id,
                "project_id": task.project_id,
                "actor_id": viewer_id,
                "dataset_item_id": item_id,
            }

        connection = await asyncpg.connect(
            _dsn(test_engine, "AAP_MODULE_DATA_LABELING_VIDEO_CONTROL_URL", _ROLE),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={
                ("video_control_command", "jsonb"),
                ("video_segment_lifecycle_command", "jsonb"),
            },
            role=_ROLE,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval("SELECT id FROM public.video_segments LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "UPDATE public.video_segments SET status='open' WHERE dataset_item_id=$1",
                item_id,
            )

        async def execute(payload):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.video_segment_lifecycle_command($1::jsonb)",
                payload,
            )

        client = VideoControlDataClient(execute)
        preparation = PrepareVideoSegments(
            schema="aap-labeling-video-control/v1",
            operation="prepare_segments",
            **preparation_scope,
            frame_count=360,
            segment_size_frames=180,
            now=datetime.now(timezone.utc),
        )
        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await client.command(preparation)
        assert rolled_back["segment_count"] == 2
        await transaction.rollback()
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.video_segments WHERE dataset_item_id=:id"
                    ),
                    {"id": item_id},
                )
                == 0
            )

        prepared = await client.command(preparation)
        assert prepared["segment_count"] == 2
        assert [row["segment_index"] for row in prepared["segments"]] == [0, 1]
        segment_id = prepared["segments"][0]["id"]
        async with test_engine.begin() as seed_assignment:
            await seed_assignment.execute(
                text(
                    "UPDATE public.video_segments SET assignee_id=:actor,status='assigned' "
                    "WHERE id=:segment"
                ),
                {"actor": annotator_id, "segment": segment_id},
            )
        now = datetime.now(timezone.utc)
        target = task_scope | {"actor_id": annotator_id, "segment_id": segment_id}
        manager_target = task_scope | {"actor_id": actor_id, "segment_id": segment_id}

        with pytest.raises(asyncpg.PostgresError) as missing_lease:
            await client.command(
                SubmitVideoSegment(
                    schema="aap-labeling-video-control/v1",
                    operation="submit_segment",
                    privileged=False,
                    **target,
                    now=now,
                )
            )
        assert missing_lease.value.message == "aap_video_control_error"
        assert json.loads(missing_lease.value.detail)["detail"] == {
            "reason": "video_segment_lease_required"
        }
        with pytest.raises(asyncpg.PostgresError) as unassign_forbidden:
            await client.command(
                UnassignVideoSegment(
                    schema="aap-labeling-video-control/v1",
                    operation="unassign_segment",
                    **target,
                )
            )
        assert unassign_forbidden.value.message == "aap_video_control_error"
        assert json.loads(unassign_forbidden.value.detail)["detail"] == (
            "reviewer privilege required"
        )
        async with maker() as db:
            row = await db.get(VideoSegment, segment_id)
            assert row.status == "assigned"
            assert row.assignee_id == annotator_id

        claim = ClaimVideoSegment(
            schema="aap-labeling-video-control/v1",
            operation="claim_segment",
            privileged=False,
            **target,
            now=now,
            lock_ttl_seconds=300,
        )
        transaction = connection.transaction()
        await transaction.start()
        rolled_back_claim = await client.command(claim)
        assert rolled_back_claim["segment"]["status"] == "locked"
        await transaction.rollback()
        async with maker() as db:
            row = await db.get(VideoSegment, segment_id)
            assert row.status == "assigned"
            assert row.assignee_id == annotator_id
            assert row.locked_by is None

        claimed = await client.command(claim)
        assert claimed["changed"] is True
        assert claimed["segment"]["assignee_id"] == annotator_id
        assert claimed["segment"]["locked_by"] == annotator_id
        heartbeat = await client.command(
            HeartbeatVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="heartbeat_segment",
                privileged=False,
                **target,
                now=now + timedelta(seconds=1),
                lock_ttl_seconds=300,
            )
        )
        assert heartbeat["segment"]["locked_at"] == claimed["segment"]["locked_at"]
        released = await client.command(
            ReleaseVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="release_segment",
                privileged=False,
                **target,
                now=now + timedelta(seconds=2),
            )
        )
        assert released["segment"]["status"] == "assigned"
        assert released["segment"]["locked_by"] is None

        claimed = await client.command(
            ClaimVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="claim_segment",
                privileged=False,
                **target,
                now=now + timedelta(seconds=3),
                lock_ttl_seconds=300,
            )
        )
        assert claimed["segment"]["status"] == "locked"
        submitted = await client.command(
            SubmitVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="submit_segment",
                privileged=False,
                **target,
                now=now + timedelta(seconds=4),
            )
        )
        assert submitted["changed"] is True
        assert submitted["segment"]["status"] == "completed"
        duplicate = await client.command(
            SubmitVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="submit_segment",
                privileged=False,
                **target,
                now=now + timedelta(seconds=5),
            )
        )
        assert duplicate["changed"] is False

        reopened = await client.command(
            ReopenVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="reopen_segment",
                **manager_target,
            )
        )
        assert reopened["segment"]["status"] == "assigned"
        unassigned = await client.command(
            UnassignVideoSegment(
                schema="aap-labeling-video-control/v1",
                operation="unassign_segment",
                **manager_target,
            )
        )
        assert unassigned["segment"]["status"] == "open"
        assert unassigned["segment"]["assignee_id"] is None
    finally:
        if connection is not None:
            await connection.close()
        async with test_engine.begin() as cleanup:
            if item_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.video_segments WHERE dataset_item_id=:id"),
                    {"id": item_id},
                )
            if task_id is not None:
                await cleanup.execute(delete(Task).where(Task.id == task_id))
            if project_id is not None and annotator_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.project_members WHERE project_id=:id"),
                    {"id": project_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.users WHERE id=:id"),
                    {"id": annotator_id},
                )
            if viewer_id is not None:
                await cleanup.execute(
                    text("DELETE FROM public.users WHERE id=:id"),
                    {"id": viewer_id},
                )
            if item_id is not None:
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
