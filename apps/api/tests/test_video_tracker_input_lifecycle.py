"""Native lifecycle fences for retained video tracker inputs."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as e
from aap_assets_video_inputs import tracker as c
from aap_assets_video_inputs import values as v
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJobStatus
from tests.factory import create_membership
from tests.test_assets_catalog_native import _seed_actor
from tests.test_video_input_ledger_execution import _seed_prepared_video_input
from tests.test_video_tracker_input_native import (
    _capture as _capture_raw,
    _native_call,
    _reply,
    _seed_tracker_job,
)

pytestmark = pytest.mark.asyncio


async def _capture_input(db, actor, job, *, grid_step=2):
    request = c.TrackerVideoInputRequest(
        actor_id=actor, tracker_job_id=job.id, grid_step=grid_step
    )
    record = c.parse_labeling_native(
        "capture_video_tracker_input",
        request,
        await _capture_raw(db, actor, job.id, grid_step=grid_step),
    )
    assert isinstance(record, c.TrackerVideoInputRecord)
    return request, record


async def _prepare_and_claim(db, actor, task_id, captured):
    request = c.PrepareTrackerInputRequest(
        actor_id=actor,
        tracker_job_id=captured.scope.tracker_job_id,
        task_id=task_id,
        input_id=captured.input_id,
        request_digest=captured.scope.request_digest,
        source_snapshot=captured.scope.source_facts.source_snapshot,
        timetable_revision=captured.scope.source_facts.timetable_revision,
        object_identity=c.ObjectIdentity(
            etag="tracker-source-etag",
            byte_size=128,
            version_id="tracker-source-v1",
        ),
        terminal_error=None,
    )
    prepared = await _native_call(db, "prepare_video_tracker_input", request)
    assert isinstance(prepared, c.TrackerVideoInputRecord)
    assert prepared.status == "pending"

    claimed = await _native_call(
        db,
        "claim_video_tracker_input",
        e.ClaimVideoInputRequest(
            worker_id=uuid.uuid4(), input_id=captured.input_id, lease_seconds=60
        ),
    )
    assert isinstance(claimed, e.ClaimedVideoInput)
    assert claimed.claim is not None
    return claimed.claim


def _plan_for_claim(claim, *, frame_count: int):
    source_identity = claim.input.source.object_identity
    video = v.FrozenVideoInput(
        input_id=claim.input.input_id,
        job_id=claim.input.job_id,
        actor_id=claim.input.actor_id,
        task=claim.input.task,
        source=claim.input.source,
        blob=v.VideoBlob(
            key=claim.blob_key,
            version_id=f"tracker-copy-v{claim.generation}",
            etag="tracker-copy-etag",
            byte_size=source_identity.byte_size,
            sha256="b" * 64,
        ),
        video=v.VideoFacts(
            frame_count=frame_count,
            width=640,
            height=360,
            duration_ms=frame_count * 40,
            video_stream=0,
        ),
        created_at=claim.input.created_at,
    )
    sampling = claim.input.sampling
    entries = tuple(
        v.FrameTimeEntry(
            ordinal=ordinal,
            frame_index=(ordinal - 1) * sampling.step,
            pts_ms=(ordinal - 1) * sampling.step * 40,
            is_keyframe=ordinal == 1,
        )
        for ordinal in range(1, sampling.count(frame_count) + 1)
    )
    return v.FrozenFramePlan.from_entries(video, sampling, entries), entries


async def _stage_times(db, claim, entries, *, cursor=0):
    request = e.StageVideoTimesRequest(
        input_id=claim.input.input_id,
        generation=claim.generation,
        lease_token=claim.lease_token,
        cursor=cursor,
        entries=tuple(entries),
    )
    result = await _native_call(db, "stage_video_tracker_times", request)
    assert isinstance(result, e.StagedVideoTimes)
    return request, result


async def _expect_error(db, operation, request, code):
    with pytest.raises(c.TrackerFailure) as captured:
        await _native_call(db, operation, request)
    assert captured.value.code == code


def _finish_request(claim, plan):
    return e.FinishVideoInputRequest(
        input_id=claim.input.input_id,
        generation=claim.generation,
        lease_token=claim.lease_token,
        plan=plan,
    )


async def test_tracker_lease_takeover_preserves_staged_replay_and_fences_old_token(
    db_session,
):
    actor, _, _, task_id, job = await _seed_tracker_job(db_session)
    _, captured = await _capture_input(db_session, actor, job)
    first_claim = await _prepare_and_claim(db_session, actor, task_id, captured)
    first_plan, first_entries = _plan_for_claim(first_claim, frame_count=6)

    heartbeat = await _native_call(
        db_session,
        "heartbeat_video_tracker_input",
        e.HeartbeatVideoInputRequest(
            input_id=first_claim.input.input_id,
            generation=first_claim.generation,
            lease_token=first_claim.lease_token,
            lease_seconds=60,
        ),
    )
    assert heartbeat.lease_token == first_claim.lease_token
    assert heartbeat.generation == first_claim.generation

    staged_request, staged = await _stage_times(db_session, first_claim, first_entries)
    assert staged.next_cursor == len(first_entries)
    replayed_stage = await _native_call(
        db_session, "stage_video_tracker_times", staged_request
    )
    assert replayed_stage.next_cursor == staged.next_cursor

    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=:input AND generation=:generation AND lease_token=:token"
        ),
        {
            "input": first_claim.input.input_id,
            "generation": first_claim.generation,
            "token": first_claim.lease_token,
        },
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=:input AND generation=:generation AND lease_token=:token"
        ),
        {
            "input": first_claim.input.input_id,
            "generation": first_claim.generation,
            "token": first_claim.lease_token,
        },
    )

    takeover = await _native_call(
        db_session,
        "claim_video_tracker_input",
        e.ClaimVideoInputRequest(
            worker_id=uuid.uuid4(),
            input_id=first_claim.input.input_id,
            lease_seconds=60,
        ),
    )
    second_claim = takeover.claim
    assert second_claim is not None
    assert second_claim.generation == first_claim.generation + 1
    assert second_claim.lease_token != first_claim.lease_token
    await _expect_error(
        db_session, "stage_video_tracker_times", staged_request, "lease_lost"
    )
    await _expect_error(
        db_session,
        "finish_video_tracker_input",
        _finish_request(first_claim, first_plan),
        "lease_lost",
    )

    second_plan, second_entries = _plan_for_claim(second_claim, frame_count=6)
    _, staged = await _stage_times(db_session, second_claim, second_entries)
    assert staged.next_cursor == second_plan.selected_count
    finish_request = _finish_request(second_claim, second_plan)
    ready = await _native_call(db_session, "finish_video_tracker_input", finish_request)
    replayed_finish = await _native_call(
        db_session, "finish_video_tracker_input", finish_request
    )
    assert ready.status == replayed_finish.status == "ready"
    assert ready.plan.plan_digest == replayed_finish.plan.plan_digest
    assert ready.plan.plan_digest == second_plan.plan_digest


async def test_finish_rejects_measured_video_shorter_than_admitted_tracker_range(
    db_session,
):
    actor, _, _, task_id, job = await _seed_tracker_job(db_session)
    _, captured = await _capture_input(db_session, actor, job)
    claim = await _prepare_and_claim(db_session, actor, task_id, captured)
    # Sampling can produce three valid decoded rows (0, 2, 4) from five frames,
    # but the admitted Job still requires frame 5.
    plan, entries = _plan_for_claim(claim, frame_count=5)
    _, staged = await _stage_times(db_session, claim, entries)
    assert staged.next_cursor == plan.selected_count == 3

    await _expect_error(
        db_session,
        "finish_video_tracker_input",
        _finish_request(claim, plan),
        "invalid_frame_range",
    )
    read = await _native_call(
        db_session,
        "read_video_tracker_input",
        c.TrackerInputReadRequest(
            actor_id=actor, tracker_job_id=job.id, task_id=task_id
        ),
    )
    assert read.record.status == "materializing"
    assert read.record.plan is None


def _mask_reference(digest: str) -> dict:
    return {
        "encoding": "coco_rle_ref",
        "size": [2, 2],
        "object_key": f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json",
        "sha256": digest,
        "runs": 2,
        "bytes": 20,
    }


def _stage_lock_request(actor, job, captured, plan):
    return c.LockTrackerVideoInputRequest(
        actor_id=actor,
        tracker_job_id=job.id,
        task_id=job.task_id,
        input_id=captured.input_id,
        request_digest=captured.scope.request_digest,
        plan_digest=plan.plan_digest,
    )


async def test_correction_input_fences_source_mask_and_keeps_runtime_routing_mutable(
    db_session,
):
    actor, project, _, task_id, job = await _seed_tracker_job(db_session)
    digest = "a" * 64
    reference = _mask_reference(digest)
    annotation = Annotation(
        task_id=task_id,
        project_id=project.id,
        user_id=actor,
        source="manual",
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="person",
        track_id="mask-track-1",
        version=1,
        geometry={
            "type": "video_track_mask",
            "track_id": "mask-track-1",
            "keyframes": [
                {
                    "frame_index": 2,
                    "mask": reference,
                    "source": "manual",
                    "occluded": False,
                }
            ],
            "outside": [],
        },
    )
    db_session.add(annotation)
    await db_session.flush()
    job.job_kind = "correction"
    job.annotation_id = annotation.id
    job.track_id_snapshot = "mask-track-1"
    job.correction_frame = 2
    job.prompt = {
        "text": "repair the mask",
        "expected_source_versions": {str(annotation.id): annotation.version},
        "touched_annotation_ids": [],
        "correction": {
            "source_annotation_id": str(annotation.id),
            "corrected_digest": digest,
            "routing": {"execution_backend_id": "backend-a"},
        },
    }
    await db_session.flush()

    _, captured = await _capture_input(db_session, actor, job)
    assert captured.scope.execution.correction_mask.mask_digest == digest
    claim = await _prepare_and_claim(db_session, actor, task_id, captured)
    plan, entries = _plan_for_claim(claim, frame_count=6)
    await _stage_times(db_session, claim, entries)
    await _native_call(
        db_session,
        "finish_video_tracker_input",
        _finish_request(claim, plan),
    )

    job.status = VideoTrackerJobStatus.RUNNING.value
    job.started_at = datetime.now(timezone.utc)
    job.prompt = {
        **job.prompt,
        "touched_annotation_ids": [str(uuid.uuid4())],
        "correction": {
            **job.prompt["correction"],
            "routing": {"execution_backend_id": "backend-b"},
        },
    }
    await db_session.flush()
    lock_request = _stage_lock_request(actor, job, captured, plan)
    accepted = await _native_call(
        db_session, "lock_video_tracker_input_for_stage", lock_request
    )
    assert accepted.input.plan.plan_digest == plan.plan_digest
    assert accepted.evidence.prompt_digest == captured.scope.prompt_digest

    job.prompt = {
        **job.prompt,
        "correction": {
            **job.prompt["correction"],
            "corrected_digest": "f" * 64,
        },
    }
    await db_session.flush()
    await _expect_error(
        db_session,
        "lock_video_tracker_input_for_stage",
        lock_request,
        "input_scope_mismatch",
    )

    job.prompt = {
        **job.prompt,
        "correction": {
            **job.prompt["correction"],
            "corrected_digest": digest,
        },
    }
    changed_reference = _mask_reference("b" * 64)
    annotation.geometry = {
        **annotation.geometry,
        "keyframes": [
            {**annotation.geometry["keyframes"][0], "mask": changed_reference}
        ],
    }
    await db_session.flush()
    await _expect_error(
        db_session,
        "lock_video_tracker_input_for_stage",
        lock_request,
        "source_changed",
    )


async def test_partial_review_uses_current_reviewer_without_rebinding_tracker_plan(
    db_session,
):
    actor, project, _, task_id, job = await _seed_tracker_job(db_session)
    annotation = Annotation(
        task_id=task_id,
        project_id=project.id,
        user_id=actor,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="person",
        version=1,
        geometry={"type": "bbox", "x": 1.0, "y": 2.0, "w": 5.0, "h": 6.0},
    )
    db_session.add(annotation)
    await db_session.flush()
    job.annotation_id = annotation.id
    job.prompt = {
        "text": "person",
        "seeds": [],
        "expected_source_versions": {str(annotation.id): annotation.version},
        "touched_annotation_ids": [],
        "correction": {"routing": {"execution_backend_id": "backend-a"}},
    }
    await db_session.flush()
    _, captured = await _capture_input(db_session, actor, job)
    claim = await _prepare_and_claim(db_session, actor, task_id, captured)
    plan, entries = _plan_for_claim(claim, frame_count=6)
    await _stage_times(db_session, claim, entries)
    await _native_call(
        db_session,
        "finish_video_tracker_input",
        _finish_request(claim, plan),
    )

    reviewer = await _seed_actor(db_session, role="employee")
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=reviewer,
        role="reviewer",
        assigned_by=actor,
    )
    task = await db_session.get(Task, task_id)
    task.reviewer_id = reviewer
    annotation.version += 1
    annotation.geometry = {
        "type": "bbox",
        "x": 2.0,
        "y": 2.0,
        "w": 5.0,
        "h": 6.0,
    }
    job.status = VideoTrackerJobStatus.PARTIALLY_REVIEWED.value
    job.staged_result = {
        "results": [],
        "grid_step": captured.scope.execution.grid_step,
        "output_geometry": "bbox",
    }
    job.prompt = {
        **job.prompt,
        "expected_source_versions": {str(annotation.id): annotation.version},
        "touched_annotation_ids": [str(annotation.id)],
        "correction": {"routing": {"execution_backend_id": "backend-b"}},
    }
    await db_session.flush()

    lock_request = _stage_lock_request(reviewer, job, captured, plan)
    first = await _native_call(
        db_session, "lock_video_tracker_input_for_accept", lock_request
    )
    assert first.input.plan.plan_digest == plan.plan_digest
    assert first.evidence.prompt_digest == captured.scope.prompt_digest
    assert first.evidence.input_id == captured.input_id

    job.prompt = {
        **job.prompt,
        "touched_annotation_ids": [str(uuid.uuid4())],
        "correction": {"routing": {"execution_backend_id": "backend-c"}},
    }
    await db_session.flush()
    replay = await _native_call(
        db_session, "lock_video_tracker_input_for_accept", lock_request
    )
    assert replay.evidence == first.evidence
    assert replay.input.plan.plan_digest == first.input.plan.plan_digest


async def test_manifest_and_tracker_claimers_progress_only_their_owner_lane(
    db_session,
):
    manifest = await _seed_prepared_video_input(db_session)
    manifest_job_id = manifest[2]
    manifest_input_id = manifest[5]
    manifest_claim = manifest[6]
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=:input AND generation=:generation AND lease_token=:token"
        ),
        {
            "input": manifest_input_id,
            "generation": manifest_claim["generation"],
            "token": manifest_claim["lease_token"],
        },
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=:input AND generation=:generation AND lease_token=:token"
        ),
        {
            "input": manifest_input_id,
            "generation": manifest_claim["generation"],
            "token": manifest_claim["lease_token"],
        },
    )

    actor, _, _, task_id, tracker_job = await _seed_tracker_job(db_session)
    _, captured = await _capture_input(db_session, actor, tracker_job)
    prepared = await _native_call(
        db_session,
        "prepare_video_tracker_input",
        c.PrepareTrackerInputRequest(
            actor_id=actor,
            tracker_job_id=tracker_job.id,
            task_id=task_id,
            input_id=captured.input_id,
            request_digest=captured.scope.request_digest,
            source_snapshot=captured.scope.source_facts.source_snapshot,
            timetable_revision=captured.scope.source_facts.timetable_revision,
            object_identity=c.ObjectIdentity(
                etag="tracker-source-etag",
                byte_size=128,
                version_id="tracker-source-v1",
            ),
            terminal_error=None,
        ),
    )
    assert prepared.status == "pending"
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:job"),
            {"job": tracker_job.id},
        )
        == 0
    )

    legacy = _reply(
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.claim_accepted_video_input("
                "CAST(:worker AS uuid),NULL,CAST(:lease AS integer))"
            ),
            {"worker": uuid.uuid4(), "lease": 60},
        )
    )
    assert legacy["ok"] is True
    assert legacy["data"]["claim"]["input"]["input_id"] == str(manifest_input_id)
    manifest_takeover = legacy["data"]["claim"]

    tracker_claim = await _native_call(
        db_session,
        "claim_video_tracker_input",
        e.ClaimVideoInputRequest(worker_id=uuid.uuid4(), lease_seconds=60),
    )
    assert tracker_claim.claim is not None
    assert tracker_claim.claim.input.input_id == captured.input_id

    # Both independent GC sweeps must progress their own terminal owner row.
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='failed' WHERE id=:job"),
        {"job": manifest_job_id},
    )
    tracker_job.status = VideoTrackerJobStatus.FAILED.value
    await db_session.flush()
    for input_id, claim in (
        (
            manifest_input_id,
            {
                "generation": manifest_takeover["generation"],
                "lease_token": manifest_takeover["lease_token"],
            },
        ),
        (
            captured.input_id,
            {
                "generation": tracker_claim.claim.generation,
                "lease_token": tracker_claim.claim.lease_token,
            },
        ),
    ):
        await db_session.execute(
            text(
                "UPDATE public.asset_accepted_video_inputs "
                "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input AND generation=:generation AND lease_token=:token"
            ),
            {"input": input_id, **claim},
        )
        await db_session.execute(
            text(
                "UPDATE public.asset_accepted_video_input_attempts "
                "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                "WHERE input_id=:input AND generation=:generation AND lease_token=:token"
            ),
            {"input": input_id, **claim},
        )

    manifest_sweep = _reply(
        await db_session.scalar(
            text("SELECT aap_assets_v1.sweep_accepted_video_inputs(10)")
        )
    )
    assert manifest_sweep["ok"] is True
    states = (
        await db_session.execute(
            text(
                "SELECT input_id::text AS input_id,input_owner,status "
                "FROM public.asset_accepted_video_inputs "
                "WHERE input_id IN (:manifest,:tracker)"
            ),
            {"manifest": manifest_input_id, "tracker": captured.input_id},
        )
    ).all()
    by_id = {row.input_id: row for row in states}
    assert by_id[str(manifest_input_id)].input_owner == "manifest"
    assert by_id[str(manifest_input_id)].status == "failed"
    assert by_id[str(captured.input_id)].input_owner == "tracker"
    assert by_id[str(captured.input_id)].status == "materializing"

    tracker_sweep = await _native_call(
        db_session,
        "sweep_video_tracker_inputs",
        e.SweepVideoInputsRequest(limit=10),
    )
    assert tracker_sweep.expired == 0
    states = (
        await db_session.execute(
            text(
                "SELECT input_id::text AS input_id,input_owner,status "
                "FROM public.asset_accepted_video_inputs "
                "WHERE input_id IN (:manifest,:tracker)"
            ),
            {"manifest": manifest_input_id, "tracker": captured.input_id},
        )
    ).all()
    by_id = {row.input_id: row for row in states}
    assert by_id[str(manifest_input_id)].status == "failed"
    assert by_id[str(captured.input_id)].input_owner == "tracker"
    assert by_id[str(captured.input_id)].status == "failed"
