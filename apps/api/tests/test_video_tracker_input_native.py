"""Native admission and current-source fences for full-video tracker inputs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as e
from aap_assets_video_inputs import tracker as c
from app.db.models.video_tracker_job import (
    VideoTrackerJob,
    VideoTrackerJobStatus,
)
from tests.test_video_input_authorities_native import (
    _seed_video_job,
    _seed_video_task,
)
from tests.test_video_input_selection_native import _selection

pytestmark = pytest.mark.asyncio

_CAPTURE = text(
    "SELECT aap_labeling_v1.capture_video_tracker_input("
    "CAST(:actor AS uuid),CAST(:job AS uuid),CAST(:grid_step AS integer))"
)
_READ = text(
    "SELECT aap_assets_v1.read_video_tracker_input("
    "CAST(:actor AS uuid),CAST(:job AS uuid),CAST(:task AS uuid))"
)


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _error(value, code: str, schema: str = c.LABELING_SCHEMA) -> None:
    assert _reply(value) == {
        "schema": schema,
        "ok": False,
        "error": {"code": code},
    }


async def _seed_tracker_job(db, *, status="queued", started_at=None, prompt=None):
    actor, project, _, item, task = await _seed_video_task(db)
    job = VideoTrackerJob(
        task_id=task,
        dataset_item_id=item,
        created_by=actor,
        status=status,
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=5,
        prompt=prompt
        if prompt is not None
        else {
            "text": "person",
            "seeds": [],
            "correction": {"routing": {"execution_backend_id": "local-a"}},
        },
        event_channel=f"tracker-input-test:{uuid.uuid4()}",
        started_at=started_at,
    )
    db.add(job)
    await db.flush()
    return actor, project, item, task, job


async def _capture(db, actor, job, *, grid_step=2):
    return await db.scalar(
        _CAPTURE,
        {"actor": actor, "job": job, "grid_step": grid_step},
    )


async def _read(db, actor, job, task):
    return await db.scalar(_READ, {"actor": actor, "job": job, "task": task})


async def _native_call(db, operation, request):
    definition = c.TRACKER_OPERATIONS[operation]
    arguments = ",".join(
        f"CAST(:{name} AS {sql_type})"
        for name, sql_type in zip(
            definition.arguments, definition.sql_types, strict=True
        )
    )
    params = request.model_dump(mode="json", by_alias=True)
    for name, sql_type in zip(definition.arguments, definition.sql_types, strict=True):
        if sql_type == "jsonb" and params[name] is not None:
            params[name] = json.dumps(params[name], ensure_ascii=False)
    raw = await db.scalar(
        text(f"SELECT {definition.namespace}.{operation}({arguments})"), params
    )
    return c.parse_native(operation, request, raw)


async def test_tracker_capture_is_idempotent_and_binds_stable_request_facts(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    request = c.TrackerVideoInputRequest(
        actor_id=actor, tracker_job_id=job.id, grid_step=2
    )
    first = c.parse_labeling_native(
        "capture_video_tracker_input",
        request,
        await _capture(db_session, actor, job.id),
    )
    assert isinstance(first, c.TrackerVideoInputRecord)
    assert first.status == "pending"
    assert first.scope.execution.prompt_snapshot["text"] == "person"
    assert first.scope.source_facts.object_identity is None

    # The execution backend is a mutable routing choice; the accepted prompt,
    # seed, range, source and grid remain the immutable admission facts.
    job.prompt = {
        **job.prompt,
        "correction": {"routing": {"execution_backend_id": "local-b"}},
    }
    await db_session.flush()
    replay = c.parse_labeling_native(
        "capture_video_tracker_input",
        request,
        await _capture(db_session, actor, job.id),
    )
    assert replay.input_id == first.input_id
    assert replay.scope.request_digest == first.scope.request_digest

    job.prompt = {**job.prompt, "text": "car"}
    await db_session.flush()
    _error(await _capture(db_session, actor, job.id), "input_scope_mismatch")
    _error(
        await _capture(db_session, actor, job.id, grid_step=3), "input_scope_mismatch"
    )


async def test_tracker_capture_requires_the_canonical_origin_and_unstarted_queued_job(
    db_session,
):
    actor, project, _, _, job = await _seed_tracker_job(db_session)
    _error(await _capture(db_session, uuid.uuid4(), job.id), "permission_denied")
    _error(await _capture(db_session, actor, uuid.uuid4()), "input_missing")
    batch_job = await _seed_video_job(
        db_session,
        actor_id=actor,
        project_id=project.id,
        selection=_selection(),
    )
    _error(await _capture(db_session, actor, batch_job), "input_missing")

    running_actor, _, _, _, running = await _seed_tracker_job(
        db_session,
        status=VideoTrackerJobStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc),
    )
    _error(await _capture(db_session, running_actor, running.id), "job_not_active")
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.video_tracker_input_scopes "
                "WHERE tracker_job_id=:job"
            ),
            {"job": running.id},
        )
        == 0
    )


async def test_tracker_read_ignores_workflow_version_but_fences_task_aba_and_timetable_changes(
    db_session,
):
    actor, _, item, task, job = await _seed_tracker_job(db_session)
    capture_request = c.TrackerVideoInputRequest(
        actor_id=actor, tracker_job_id=job.id, grid_step=2
    )
    captured = c.parse_labeling_native(
        "capture_video_tracker_input",
        capture_request,
        await _capture(db_session, actor, job.id),
    )

    await db_session.execute(
        text("UPDATE public.tasks SET version=version+1 WHERE id=:task"),
        {"task": task},
    )
    read_request = c.TrackerInputReadRequest(
        actor_id=actor, tracker_job_id=job.id, task_id=task
    )
    read = c.parse_native(
        "read_video_tracker_input",
        read_request,
        await _read(db_session, actor, job.id, task),
    )
    assert isinstance(read, c.TrackerVideoInputWork)
    assert read.record.input_id == captured.input_id

    task_facts = (
        await db_session.execute(
            text(
                "SELECT file_path,media_source_generation FROM public.tasks "
                "WHERE id=:task"
            ),
            {"task": task},
        )
    ).one()
    await db_session.execute(
        text(
            "UPDATE public.tasks SET file_path=:path,"
            "media_source_generation=media_source_generation+1,version=version+1 "
            "WHERE id=:task"
        ),
        {"path": "source/temporary-aba.mp4", "task": task},
    )
    _error(
        await _read(db_session, actor, job.id, task),
        "source_changed",
        c.TRACKER_SCHEMA,
    )

    await db_session.execute(
        text(
            "UPDATE public.tasks SET file_path=:path,"
            "media_source_generation=:generation,version=version+1 WHERE id=:task"
        ),
        {
            "path": task_facts.file_path,
            "generation": task_facts.media_source_generation,
            "task": task,
        },
    )
    read = c.parse_native(
        "read_video_tracker_input",
        read_request,
        await _read(db_session, actor, job.id, task),
    )
    assert read.record.input_id == captured.input_id

    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_timetable_revision="
            "media_timetable_revision+1 WHERE id=:item"
        ),
        {"item": item},
    )
    _error(
        await _read(db_session, actor, job.id, task),
        "source_changed",
        c.TRACKER_SCHEMA,
    )


async def test_tracker_claim_uses_only_its_canonical_scope(db_session):
    actor, _, _, task, job = await _seed_tracker_job(db_session)
    captured = c.parse_labeling_native(
        "capture_video_tracker_input",
        c.TrackerVideoInputRequest(actor_id=actor, tracker_job_id=job.id, grid_step=2),
        await _capture(db_session, actor, job.id),
    )
    prepared = await _native_call(
        db_session,
        "prepare_video_tracker_input",
        c.PrepareTrackerInputRequest(
            actor_id=actor,
            tracker_job_id=job.id,
            task_id=task,
            input_id=captured.input_id,
            request_digest=captured.scope.request_digest,
            source_snapshot=captured.scope.source_facts.source_snapshot,
            timetable_revision=captured.scope.source_facts.timetable_revision,
            object_identity=c.ObjectIdentity(
                etag="native-tracker-etag",
                byte_size=128,
                version_id="native-tracker-source-v1",
            ),
            terminal_error=None,
        ),
    )
    assert isinstance(prepared, c.TrackerVideoInputRecord)
    assert prepared.status == "pending"

    # The legacy batch claimant cannot consume a tracker-owned row even when
    # given the exact input ID; the dedicated tracker claim can take it next.
    legacy = await db_session.scalar(
        text(
            "SELECT aap_assets_v1.claim_accepted_video_input("
            "CAST(:worker AS uuid),CAST(:input AS uuid),CAST(:lease AS integer))"
        ),
        {"worker": uuid.uuid4(), "input": captured.input_id, "lease": 2100},
    )
    assert _reply(legacy) == {
        "schema": "aap-assets-video-input-execution/v1",
        "ok": False,
        "error": {"code": "input_scope_mismatch"},
    }
    claimed = await _native_call(
        db_session,
        "claim_video_tracker_input",
        e.ClaimVideoInputRequest(worker_id=uuid.uuid4(), input_id=captured.input_id),
    )
    assert claimed.claim is not None
    assert claimed.claim.input.job_id == job.id
    assert claimed.claim.input.actor_id == actor
    assert claimed.claim.input.task.task_id == task
    assert claimed.claim.input.sampling.step == captured.scope.execution.grid_step
