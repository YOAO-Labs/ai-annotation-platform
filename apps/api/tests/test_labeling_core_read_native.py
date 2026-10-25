"""Saved role-boundary proof for Labeling core-row reads and locks."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob
from tests.factory import create_project, create_task, create_user
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified Labeling core-read role",
    ),
]

_ROLE = "labeling_core_read_client"
_PRIVATE_TABLES = (
    "tasks",
    "task_batches",
    "annotations",
    "predictions",
    "prediction_metas",
    "failed_predictions",
    "task_locks",
    "task_dataset_item_links",
    "annotation_drafts",
    "scene_tracks",
    "scene_track_intervals",
    "video_segments",
    "video_tracker_jobs",
)
_PUBLIC_RELATIONS = (
    "task_rows",
    "batch_rows",
    "annotation_rows",
    "prediction_rows",
    "prediction_meta_rows",
    "failed_prediction_rows",
    "task_lock_rows",
    "task_dataset_item_link_rows",
    "scene_track_rows",
    "scene_track_interval_rows",
    "video_segment_rows",
    "video_tracker_job_rows",
    "annotation_draft_rows",
)


async def test_core_rows_are_readable_only_through_fixed_relations(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = project_id = dataset_id = item_id = None
    ordered_task_id = None
    task_id = batch_id = annotation_id = lock_id = link_id = draft_id = None
    segment_id = tracker_job_id = None
    connection = None
    holder = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"core-read-native-{uuid.uuid4().hex}@example.test",
                "Labeling core read native owner",
            )
            project = await create_project(db, owner_id=actor.id)
            task = await create_task(db, project_id=project.id, status="pending")
            ordered_task = await create_task(
                db, project_id=project.id, status="pending"
            )
            ordered_task_id = ordered_task.id
            batch = TaskBatch(
                project_id=project.id,
                display_id=f"B-{uuid.uuid4().hex[:10]}",
                name="Core read native",
                status="active",
            )
            dataset = Dataset(
                display_id=f"DS-{uuid.uuid4().hex[:10]}",
                name="Core read native",
                data_type="pointcloud",
                created_by=actor.id,
            )
            db.add_all([batch, dataset])
            await db.flush()
            task.batch_id = batch.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="frame.pcd",
                file_path="core-read/frame.pcd",
                file_type="pointcloud",
            )
            annotation = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                class_name="car",
                geometry={"type": "bbox", "x": 0.1},
                source="manual",
            )
            task_lock = TaskLock(
                task_id=task.id,
                user_id=actor.id,
                expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            draft = AnnotationDraft(task_id=task.id, user_id=actor.id, result={})
            db.add_all([item, annotation, task_lock, draft])
            await db.flush()
            link = TaskDatasetItemLink(
                task_id=task.id,
                dataset_item_id=item.id,
                role="primary_lidar",
            )
            segment = VideoSegment(
                dataset_item_id=item.id,
                segment_index=0,
                start_frame=0,
                end_frame=1,
            )
            db.add_all([link, segment])
            await db.flush()
            tracker_job = VideoTrackerJob(
                task_id=task.id,
                dataset_item_id=item.id,
                segment_id=segment.id,
                created_by=actor.id,
                model_key="core-read-test",
                direction="forward",
                from_frame=0,
                to_frame=1,
                event_channel=f"core-read-{uuid.uuid4().hex}",
            )
            db.add(tracker_job)
            await db.flush()
            actor_id, project_id, dataset_id, item_id = (
                actor.id,
                project.id,
                dataset.id,
                item.id,
            )
            task_id, batch_id, annotation_id, lock_id, link_id = (
                task.id,
                batch.id,
                annotation.id,
                task_lock.id,
                link.id,
            )
            draft_id = draft.id
            segment_id, tracker_job_id = segment.id, tracker_job.id

        dsn = _dsn(test_engine, "AAP_MODULE_DATA_LABELING_CORE_READ_URL", _ROLE)
        connection = await asyncpg.connect(
            dsn,
            timeout=5,
            command_timeout=10,
        )
        holder = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert await connection.fetchval("SELECT current_user") == _ROLE
        for relation in _PUBLIC_RELATIONS:
            assert (
                await connection.fetchval(
                    f"SELECT pg_catalog.count(*) FROM aap_labeling_v1.{relation}"
                )
                is not None
            )
        for table_name in _PRIVATE_TABLES:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch(f"SELECT id FROM public.{table_name} LIMIT 1")
        for relation, column, row_id in (
            ("task_rows", "status", task_id),
            ("batch_rows", "name", batch_id),
            ("annotation_rows", "class_name", annotation_id),
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.transaction():
                    await connection.execute(
                        f"UPDATE aap_labeling_v1.{relation} "
                        f"SET {column} = {column} WHERE id = $1",
                        row_id,
                    )

        task_row = await connection.fetchrow(
            "SELECT project_id, batch_id FROM aap_labeling_v1.task_rows WHERE id = $1",
            task_id,
        )
        assert (task_row["project_id"], task_row["batch_id"]) == (project_id, batch_id)
        batch_row = await connection.fetchrow(
            "SELECT project_id, status FROM aap_labeling_v1.batch_rows WHERE id = $1",
            batch_id,
        )
        assert (batch_row["project_id"], batch_row["status"]) == (project_id, "active")
        annotation_row = await connection.fetchrow(
            "SELECT task_id, source, version, geometry "
            "FROM aap_labeling_v1.annotation_rows WHERE id = $1",
            annotation_id,
        )
        assert (annotation_row["task_id"], annotation_row["source"]) == (
            task_id,
            "manual",
        )
        assert annotation_row["version"] == 1
        assert json.loads(annotation_row["geometry"])["x"] == 0.1
        assert (
            await connection.fetchval(
                "SELECT user_id FROM aap_labeling_v1.task_lock_rows WHERE id = $1",
                lock_id,
            )
            == actor_id
        )
        assert (
            await connection.fetchval(
                "SELECT dataset_item_id FROM aap_labeling_v1.task_dataset_item_link_rows "
                "WHERE id = $1",
                link_id,
            )
            == item_id
        )
        draft_row = await connection.fetchrow(
            "SELECT id, task_id FROM aap_labeling_v1.annotation_draft_rows WHERE id = $1",
            draft_id,
        )
        assert (draft_row["id"], draft_row["task_id"]) == (draft_id, task_id)
        segment_row = await connection.fetchrow(
            "SELECT dataset_item_id, start_frame, end_frame "
            "FROM aap_labeling_v1.video_segment_rows WHERE id = $1",
            segment_id,
        )
        assert (
            segment_row["dataset_item_id"],
            segment_row["start_frame"],
            segment_row["end_frame"],
        ) == (item_id, 0, 1)
        tracker_job_row = await connection.fetchrow(
            "SELECT task_id, segment_id, model_key "
            "FROM aap_labeling_v1.video_tracker_job_rows WHERE id = $1",
            tracker_job_id,
        )
        assert (
            tracker_job_row["task_id"],
            tracker_job_row["segment_id"],
            tracker_job_row["model_key"],
        ) == (task_id, segment_id, "core-read-test")
        ordered_task_ids = sorted((task_id, ordered_task_id), reverse=True)
        ordered_task_rows = await connection.fetch(
            "SELECT id FROM aap_labeling_v1.lock_task_rows_for_core_read($1)",
            ordered_task_ids,
        )
        assert [row["id"] for row in ordered_task_rows] == sorted(ordered_task_ids)

        async with connection.transaction():

            async def assert_nowait_conflicts(hold_sql, hold_args, contenders) -> None:
                transaction = holder.transaction()
                await transaction.start()
                try:
                    assert await holder.fetchrow(hold_sql, *hold_args) is not None
                    for statement, arguments in contenders:
                        with pytest.raises(asyncpg.PostgresError) as conflict:
                            async with connection.transaction():
                                await connection.fetchrow(statement, *arguments)
                        assert conflict.value.sqlstate == "55P03"
                finally:
                    await transaction.rollback()

            await assert_nowait_conflicts(
                "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'update', false)",
                (task_id,),
                [
                    (
                        "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'update', true)",
                        (task_id,),
                    ),
                    (
                        "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'share', true)",
                        (task_id,),
                    ),
                    (
                        "SELECT id FROM aap_labeling_v1.lock_task_rows_for_core_read($1)",
                        (ordered_task_ids,),
                    ),
                ],
            )
            await assert_nowait_conflicts(
                "SELECT id FROM aap_labeling_v1.lock_batch_rows_for_core_read($1, $2, 'update', false)",
                ([batch_id], project_id),
                [
                    (
                        "SELECT id FROM aap_labeling_v1.lock_batch_rows_for_core_read($1, $2, 'update', true)",
                        ([batch_id], project_id),
                    ),
                    (
                        "SELECT id FROM aap_labeling_v1.lock_batch_rows_for_core_read($1, $2, 'share', true)",
                        ([batch_id], project_id),
                    ),
                ],
            )
            await assert_nowait_conflicts(
                "SELECT id FROM aap_labeling_v1.lock_annotation_rows_for_core_read($1, $2, $3, 'update', false)",
                ([annotation_id], task_id, project_id),
                [
                    (
                        "SELECT id FROM aap_labeling_v1.lock_annotation_rows_for_core_read($1, $2, $3, 'update', true)",
                        ([annotation_id], task_id, project_id),
                    ),
                    (
                        "SELECT id FROM aap_labeling_v1.lock_annotation_rows_for_core_read($1, $2, $3, 'share', true)",
                        ([annotation_id], task_id, project_id),
                    ),
                ],
            )
            await assert_nowait_conflicts(
                "SELECT id FROM aap_labeling_v1.lock_active_learning_feedback_task_rows($1, $2)",
                (project_id, [task_id]),
                [
                    (
                        "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'update', true)",
                        (task_id,),
                    )
                ],
            )
            await assert_nowait_conflicts(
                "SELECT id FROM aap_labeling_v1.lock_active_learning_feedback_annotation_rows($1)",
                ([task_id],),
                [
                    (
                        "SELECT id FROM aap_labeling_v1.lock_annotation_rows_for_core_read($1, $2, $3, 'update', true)",
                        ([annotation_id], task_id, project_id),
                    )
                ],
            )

            segment_holder_transaction = holder.transaction()
            await segment_holder_transaction.start()
            locked_segment = await holder.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_video_segment_for_core_read($1, $2)",
                segment_id,
                item_id,
            )
            assert locked_segment is not None
            await connection.execute("SET LOCAL lock_timeout = '5s'")
            contender_pid = await connection.fetchval(
                "SELECT pg_catalog.pg_backend_pid()"
            )
            waiting_segment = asyncio.create_task(
                connection.fetchrow(
                    "SELECT id FROM aap_labeling_v1.lock_video_segment_for_core_read($1, $2)",
                    segment_id,
                    item_id,
                )
            )
            try:
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline:
                    await holder.fetchval("SELECT pg_catalog.pg_stat_clear_snapshot()")
                    wait_event_type = await holder.fetchval(
                        "SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid = $1",
                        contender_pid,
                    )
                    if wait_event_type == "Lock":
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("the competing Segment UPDATE did not wait")
                assert not waiting_segment.done()
                await segment_holder_transaction.commit()
                segment_holder_transaction = None
                released_segment = await asyncio.wait_for(waiting_segment, timeout=5)
                assert released_segment["id"] == segment_id
            finally:
                if segment_holder_transaction is not None:
                    await segment_holder_transaction.rollback()
                if not waiting_segment.done():
                    waiting_segment.cancel()
                    await asyncio.gather(waiting_segment, return_exceptions=True)

            locked_task = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'share', true)",
                task_id,
            )
            project_scoped_task = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'share', true, $2)",
                task_id,
                project_id,
            )
            foreign_project_task = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'share', true, $2)",
                task_id,
                uuid.uuid4(),
            )
            locked_id_only_tasks = await connection.fetch(
                "SELECT id FROM aap_labeling_v1.lock_task_rows_for_core_read($1)",
                [task_id],
            )
            locked_batch_tasks = await connection.fetch(
                "SELECT id FROM aap_labeling_v1.lock_task_rows_for_core_read($1, $2, $3)",
                [task_id],
                project_id,
                batch_id,
            )
            foreign_project_batch_tasks = await connection.fetch(
                "SELECT id FROM aap_labeling_v1.lock_task_rows_for_core_read($1, $2, $3)",
                [task_id],
                uuid.uuid4(),
                batch_id,
            )
            foreign_batch_tasks = await connection.fetch(
                "SELECT id FROM aap_labeling_v1.lock_task_rows_for_core_read($1, $2, $3)",
                [task_id],
                project_id,
                uuid.uuid4(),
            )
            locked_batch = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_batch_rows_for_core_read($1, $2, 'update', true)",
                [batch_id],
                project_id,
            )
            locked_annotation = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_annotation_rows_for_core_read($1, $2, $3, 'update', true)",
                [annotation_id],
                task_id,
                project_id,
            )
            locked_segment = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_video_segment_for_core_read($1, $2)",
                segment_id,
                item_id,
            )
            foreign_item_segment = await connection.fetchrow(
                "SELECT id FROM aap_labeling_v1.lock_video_segment_for_core_read($1, $2)",
                segment_id,
                uuid.uuid4(),
            )
            assert locked_task["id"] == task_id
            assert project_scoped_task["id"] == task_id
            assert foreign_project_task is None
            assert [row["id"] for row in locked_id_only_tasks] == [task_id]
            assert [row["id"] for row in locked_batch_tasks] == [task_id]
            assert foreign_project_batch_tasks == []
            assert foreign_batch_tasks == []
            assert locked_batch["id"] == batch_id
            assert locked_annotation["id"] == annotation_id
            assert locked_segment["id"] == segment_id
            assert foreign_item_segment is None
            feedback_tasks = await connection.fetch(
                "SELECT id FROM aap_labeling_v1.lock_active_learning_feedback_task_rows($1, $2)",
                project_id,
                [task_id],
            )
            feedback_annotations = await connection.fetch(
                "SELECT id FROM aap_labeling_v1.lock_active_learning_feedback_annotation_rows($1)",
                [task_id],
            )
            assert [row["id"] for row in feedback_tasks] == [task_id]
            assert [row["id"] for row in feedback_annotations] == [annotation_id]
            with pytest.raises(asyncpg.PostgresError) as invalid_mode:
                async with connection.transaction():
                    await connection.fetchrow(
                        "SELECT id FROM aap_labeling_v1.lock_task_for_core_read($1, 'delete', false)",
                        task_id,
                    )
            assert invalid_mode.value.sqlstate == "22023"
    finally:
        if connection is not None:
            await connection.close()
        if holder is not None:
            await holder.close()
        async with maker.begin() as db:
            if task_id is not None:
                await db.execute(
                    delete(Annotation).where(Annotation.task_id == task_id)
                )
            if lock_id is not None:
                await db.execute(delete(TaskLock).where(TaskLock.id == lock_id))
            if draft_id is not None:
                await db.execute(
                    delete(AnnotationDraft).where(AnnotationDraft.id == draft_id)
                )
            if link_id is not None:
                await db.execute(
                    delete(TaskDatasetItemLink).where(TaskDatasetItemLink.id == link_id)
                )
            cleanup_task_ids = [
                value for value in (task_id, ordered_task_id) if value is not None
            ]
            if cleanup_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(cleanup_task_ids)))
            if batch_id is not None:
                await db.execute(delete(TaskBatch).where(TaskBatch.id == batch_id))
            if item_id is not None:
                await db.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
            if dataset_id is not None:
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if project_id is not None:
                await db.execute(delete(Project).where(Project.id == project_id))
            if actor_id is not None:
                await db.execute(delete(User).where(User.id == actor_id))
