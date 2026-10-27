"""Temporal video scope keeps project export authorization and exact refusal."""

import uuid

import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.exporting.reference_media import authorize_export_job
from app.services.reference_keys import reference_key


async def test_video_scope_ignores_unselected_reference_but_never_the_selected_one(
    db_session, super_admin
):
    actor, _ = super_admin
    project = Project(
        display_id="P-VA-" + uuid.uuid4().hex[:8],
        name="Temporal scope",
        type_key="video-track",
        type_label="Video",
        data_type="video",
        owner_id=actor.id,
    )
    db_session.add(project)
    await db_session.flush()
    managed = Task(
        project_id=project.id,
        display_id="T-VA-" + uuid.uuid4().hex[:8],
        file_name="clip.mp4",
        file_path="registered/clip.mp4",
        file_type="video",
    )
    reference = Task(
        project_id=project.id,
        display_id="T-VA-" + uuid.uuid4().hex[:8],
        file_name="external.mp4",
        file_path=reference_key(uuid.uuid4(), "a" * 64),
        file_type="video",
    )
    db_session.add_all([managed, reference])
    await db_session.flush()
    job = AsyncJob(
        kind="export",
        project_id=project.id,
        user_id=actor.id,
        payload={"video_export_scope": {"task_id": str(managed.id)}},
    )
    db_session.add(job)
    await db_session.flush()
    args = dict(project_id=project.id, task_ids=None, job_uuid=job.id)
    assert await authorize_export_job(db_session, **args) is actor
    job.payload = {"video_export_scope": {"task_id": str(reference.id)}}
    await db_session.flush()
    with pytest.raises(ContentError, match="reference_export_authority_missing"):
        await authorize_export_job(db_session, **args)
    job.payload = {"video_export_scope": {"task_id": str(uuid.uuid4())}}
    await db_session.flush()
    with pytest.raises(ValueError, match="scope is no longer valid"):
        await authorize_export_job(db_session, **args)
    job.payload = {}
    await db_session.flush()
    with pytest.raises(ContentError, match="reference_export_authority_missing"):
        await authorize_export_job(db_session, **args)
