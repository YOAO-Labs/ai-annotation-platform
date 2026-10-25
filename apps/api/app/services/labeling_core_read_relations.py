"""API bindings to Labeling's fixed public core-row relations."""

from __future__ import annotations

import uuid

from sqlalchemy import column, func, inspect, select, table
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import identity_key

from aap_labeling.core_read_contract import CORE_READ_COLUMNS
from app.db.models.annotation import Annotation
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_lock import TaskLock
from app.db.models.dataset import VideoSegment
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.video_tracker_job import VideoTrackerJob


def _row_relation(relation: str, model):
    expected = CORE_READ_COLUMNS[relation]
    actual = tuple(model.__table__.columns.keys())
    if actual != expected:
        raise RuntimeError(f"{relation} no longer matches its public core-read schema")
    selectable = table(
        relation,
        *(column(name, model.__table__.c[name].type) for name in expected),
        schema="aap_labeling_v1",
    )
    return aliased(model, selectable, adapt_on_names=True)


task_row = _row_relation("task_rows", Task)
batch_row = _row_relation("batch_rows", TaskBatch)
annotation_row = _row_relation("annotation_rows", Annotation)
prediction_row = _row_relation("prediction_rows", Prediction)
prediction_meta_row = _row_relation("prediction_meta_rows", PredictionMeta)
failed_prediction_row = _row_relation("failed_prediction_rows", FailedPrediction)
task_lock_row = _row_relation("task_lock_rows", TaskLock)
task_dataset_item_link_row = _row_relation(
    "task_dataset_item_link_rows", TaskDatasetItemLink
)
scene_track_row = _row_relation("scene_track_rows", SceneTrack)
scene_track_interval_row = _row_relation(
    "scene_track_interval_rows", SceneTrackInterval
)
video_segment_row = _row_relation("video_segment_rows", VideoSegment)
video_tracker_job_row = _row_relation("video_tracker_job_rows", VideoTrackerJob)


async def execute_core_read(db: AsyncSession, statement):
    """Preserve ORM SELECT autoflush, including caller no-autoflush scopes."""
    if db.autoflush:
        await db.flush()
    return await db.execute(statement)


async def get_core_row(
    db: AsyncSession,
    row_model,
    row_id: uuid.UUID,
    *,
    populate_existing: bool = False,
):
    if not populate_existing:
        entity = inspect(row_model).mapper.class_
        cached = db.identity_map.get(identity_key(entity, ident=row_id))
        if cached is not None and not inspect(cached).expired_attributes:
            return cached
    statement = select(row_model).where(row_model.id == row_id)
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    return (await execute_core_read(db, statement)).scalar_one_or_none()


async def lock_task_for_core_read(
    db: AsyncSession,
    task_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
    lock_mode: str = "update",
    nowait: bool = False,
    populate_existing: bool = False,
):
    locked = func.aap_labeling_v1.lock_task_for_core_read(
        task_id, lock_mode, nowait, project_id
    ).table_valued(
        *(
            column(name, Task.__table__.c[name].type)
            for name in CORE_READ_COLUMNS["task_rows"]
        )
    )
    statement = select(aliased(Task, locked, adapt_on_names=True))
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    return (await execute_core_read(db, statement)).scalar_one_or_none()


async def lock_batch_rows_for_core_read(
    db: AsyncSession,
    batch_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None = None,
    lock_mode: str = "update",
    nowait: bool = False,
    populate_existing: bool = False,
):
    if not batch_ids:
        return []
    locked = func.aap_labeling_v1.lock_batch_rows_for_core_read(
        batch_ids, project_id, lock_mode, nowait
    ).table_valued(
        *(
            column(name, TaskBatch.__table__.c[name].type)
            for name in CORE_READ_COLUMNS["batch_rows"]
        )
    )
    row = aliased(TaskBatch, locked, adapt_on_names=True)
    statement = select(row).order_by(row.id)
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    return (await execute_core_read(db, statement)).scalars().all()


async def lock_task_rows_for_core_read(
    db: AsyncSession,
    task_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    populate_existing: bool = False,
):
    locked = func.aap_labeling_v1.lock_task_rows_for_core_read(
        task_ids, project_id, batch_id
    ).table_valued(
        *(
            column(name, Task.__table__.c[name].type)
            for name in CORE_READ_COLUMNS["task_rows"]
        )
    )
    row = aliased(Task, locked, adapt_on_names=True)
    statement = select(row).order_by(row.id)
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    return (await execute_core_read(db, statement)).scalars().all()


async def lock_batch_task_rows_for_core_read(
    db: AsyncSession,
    task_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    populate_existing: bool = False,
):
    return await lock_task_rows_for_core_read(
        db,
        task_ids,
        project_id=project_id,
        batch_id=batch_id,
        populate_existing=populate_existing,
    )


async def lock_annotation_rows_for_core_read(
    db: AsyncSession,
    annotation_ids: list[uuid.UUID],
    *,
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    lock_mode: str = "update",
    nowait: bool = False,
    populate_existing: bool = False,
):
    if not annotation_ids:
        return []
    locked = func.aap_labeling_v1.lock_annotation_rows_for_core_read(
        annotation_ids, task_id, project_id, lock_mode, nowait
    ).table_valued(
        *(
            column(name, Annotation.__table__.c[name].type)
            for name in CORE_READ_COLUMNS["annotation_rows"]
        )
    )
    row = aliased(Annotation, locked, adapt_on_names=True)
    statement = select(row).order_by(row.id)
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    return (await execute_core_read(db, statement)).scalars().all()


async def lock_video_segment_for_core_read(
    db: AsyncSession,
    segment_id: uuid.UUID,
    dataset_item_id: uuid.UUID,
    *,
    populate_existing: bool = False,
):
    locked = func.aap_labeling_v1.lock_video_segment_for_core_read(
        segment_id, dataset_item_id
    ).table_valued(
        *(
            column(name, VideoSegment.__table__.c[name].type)
            for name in CORE_READ_COLUMNS["video_segment_rows"]
        )
    )
    statement = select(aliased(VideoSegment, locked, adapt_on_names=True))
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    return (await execute_core_read(db, statement)).scalar_one_or_none()


__all__ = [
    "annotation_row",
    "batch_row",
    "execute_core_read",
    "failed_prediction_row",
    "task_dataset_item_link_row",
    "get_core_row",
    "lock_batch_rows_for_core_read",
    "lock_batch_task_rows_for_core_read",
    "lock_annotation_rows_for_core_read",
    "lock_task_for_core_read",
    "lock_task_rows_for_core_read",
    "prediction_meta_row",
    "prediction_row",
    "scene_track_interval_row",
    "scene_track_row",
    "task_row",
    "task_lock_row",
    "video_segment_row",
    "video_tracker_job_row",
    "lock_video_segment_for_core_read",
]
