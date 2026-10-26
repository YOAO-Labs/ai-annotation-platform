from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import VideoSegment
from app.db.models.project import Project
from app.db.models.video_track_quality import VideoTrackQualityRun
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services.video_context import VideoContext
from app.schemas.task import VideoMetadata
from app.services.video_track_quality import (
    VideoTrackQualityError,
    _annotations,
    create_quality_run,
    dispatch_quality_run,
)
from tests.factory import create_project, create_task
from tests.media_owner_fixtures import media_owner as media_owner
from tests.test_video_frame_service import _make_video_task


async def _boundary(db, owner_id):
    task, item = await _make_video_task(db, owner_id)
    project = await db.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 10}
    segments = [
        VideoSegment(
            dataset_item_id=item.id,
            segment_index=index,
            start_frame=index * 30,
            end_frame=index * 30 + 29,
            status="completed",
        )
        for index in range(2)
    ]
    db.add_all(segments)
    await db.flush()
    return task, item, segments


def _bind_fixture_metadata_reader(monkeypatch, item):
    from app.services import video_track_quality

    # These rows belong to a rollback-only SAVEPOINT, not the independent owner.
    metadata = VideoMetadata.model_validate((item.metadata_ or {}).get("video") or {})

    class Reader:
        def __init__(self, db):
            pass

        async def from_task(self, **kwargs):
            return SimpleNamespace(metadata=metadata)

    monkeypatch.setattr(video_track_quality, "AssetsVideoContextReader", Reader)


async def test_boundary_annotations_exclude_foreign_task_on_shared_segments(
    db_session, super_admin, media_owner
):
    owner, _ = super_admin
    task, item, segments = await _boundary(db_session, owner.id)
    other_project = await create_project(db_session, owner_id=owner.id)
    other_task = await create_task(db_session, project_id=other_project.id)
    other_task.dataset_item_id = item.id
    other_task.file_type = "video"
    annotations = [
        Annotation(
            task_id=target.id,
            project_id=target.project_id,
            user_id=owner.id,
            video_segment_id=segments[0].id,
            annotation_type="bbox",
            class_name="car",
            geometry={"type": "bbox", "x": 0, "y": 0, "w": 0.1, "h": 0.1},
            source="manual",
            is_active=True,
            was_cancelled=False,
        )
        for target in (task, other_task)
    ]
    db_session.add_all(annotations)
    await db_session.flush()

    selected = await _annotations(
        db_session, task.id, [segment.id for segment in segments]
    )

    assert {annotation.id for annotation in selected} == {annotations[0].id}


async def test_dispatch_failure_finishes_both_quality_run_and_async_job(
    db_session, super_admin, monkeypatch, media_owner
):
    owner, _ = super_admin
    task, item, segments = await _boundary(db_session, owner.id)
    _bind_fixture_metadata_reader(monkeypatch, item)
    run, job, created = await create_quality_run(
        db_session,
        task=task,
        left_segment_id=segments[0].id,
        right_segment_id=segments[1].id,
        actor_id=owner.id,
    )
    assert created and job is not None
    run_id, job_id = run.id, job.id
    await db_session.commit()
    dispatcher = Mock(spec=QualityJobDispatcher)
    dispatcher.dispatch_run.side_effect = OSError("broker unavailable")
    lease = SimpleNamespace(job_id=job_id, execution_id=job.celery_task_id)

    from app.services import video_track_quality

    async def publish_prepared_dispatch(_db, _lease, *, publish):
        return publish()

    monkeypatch.setattr(
        video_track_quality, "publish_prepared_dispatch", publish_prepared_dispatch
    )
    with pytest.raises(VideoTrackQualityError) as error:
        await dispatch_quality_run(
            db_session,
            run_id=run_id,
            async_job_id=job_id,
            lease=lease,
            dispatcher=dispatcher,
        )

    assert error.value.status_code == 503
    assert error.value.detail["reason"] == "video_track_quality_dispatch_failed"
    persisted_run = await db_session.get(
        VideoTrackQualityRun, run_id, populate_existing=True
    )
    persisted_job = await db_session.get(AsyncJob, job_id, populate_existing=True)
    assert persisted_run.status == "failed"
    assert persisted_run.completed_at is not None
    assert persisted_job.status == "failed"
    assert persisted_job.completed_at is not None


async def test_existing_quality_run_reuses_supplied_video_facts(
    db_session, test_engine, super_admin, monkeypatch, media_owner
):
    from sqlalchemy import event

    owner, _ = super_admin
    task, item, segments = await _boundary(db_session, owner.id)
    _bind_fixture_metadata_reader(monkeypatch, item)
    arguments = {
        "task": task,
        "left_segment_id": segments[0].id,
        "right_segment_id": segments[1].id,
        "actor_id": owner.id,
    }
    run, _, created = await create_quality_run(db_session, **arguments)
    assert created
    ctx = VideoContext(
        metadata=VideoMetadata.model_validate(
            (item.metadata_ or {}).get("video") or {}
        ),
        dataset_item_id=item.id,
        source_file_path=item.file_path,
        thumbnail_path=item.thumbnail_path,
        source_width=item.width,
        source_height=item.height,
        task_id=task.id,
        project_id=task.project_id,
        actor_id=owner.id,
    )
    project = await db_session.get(Project, task.project_id)
    statements = []

    def count_statement(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        await create_quality_run(db_session, **arguments)
        loaded_row_statements = tuple(statements)
        db_session.expunge(item)
        statements.clear()
        repeated, job, created = await create_quality_run(
            db_session, **arguments, video_metadata=ctx.metadata
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count_statement)

    assert repeated.id == run.id and job is None and not created
    assert project.id == task.project_id
    assert tuple(statements) == loaded_row_statements


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("unready_metadata", [{}, {"frame_count": 90}])
async def test_quality_run_rejects_unready_source_before_sampling(
    db_session, super_admin, monkeypatch, media_owner, supplied, unready_metadata
):
    from sqlalchemy import func, select
    from app.services import video_track_quality

    owner, _ = super_admin
    task, _, segments = await _boundary(db_session, owner.id)
    project = await db_session.get(Project, task.project_id)
    project.video_collaboration = {"enabled": True, "overlap_frames": 10}
    project.video_sampling = {"mode": "fps", "target_fps": 10}

    class Reader:
        def __init__(self, db):
            pass

        async def from_task(self, **kwargs):
            return SimpleNamespace(
                metadata=VideoMetadata.model_validate(unready_metadata)
            )

    monkeypatch.setattr(video_track_quality, "AssetsVideoContextReader", Reader)
    with pytest.raises(VideoTrackQualityError) as error:
        await create_quality_run(
            db_session,
            task=task,
            left_segment_id=segments[0].id,
            right_segment_id=segments[1].id,
            actor_id=owner.id,
            video_metadata=VideoMetadata.model_validate(unready_metadata)
            if supplied
            else None,
        )
    assert error.value.status_code == 503
    assert error.value.detail == {"reason": "video_metadata_not_ready"}
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(VideoTrackQualityRun)
            .where(VideoTrackQualityRun.task_id == task.id)
        )
        == 0
    )


async def test_overlap_bounds_reject_empty_supplied_video_metadata(
    db_session, super_admin, media_owner
):
    from fastapi import HTTPException
    from app.services.video_collaboration import segment_work_bounds_for_task

    owner, _ = super_admin
    task, _, segments = await _boundary(db_session, owner.id)
    with pytest.raises(HTTPException) as error:
        await segment_work_bounds_for_task(
            db_session, task=task, segment=segments[0], video_metadata=VideoMetadata()
        )
    assert error.value.status_code == 503
    assert error.value.detail == {"reason": "video_metadata_not_ready"}
