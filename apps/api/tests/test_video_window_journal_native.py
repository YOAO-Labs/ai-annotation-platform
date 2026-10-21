"""Exercise the portable window protocol against real, owner-held Native state."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_preannotation_results import codec
from aap_preannotation_results.video_execution import ExecutionProof
from aap_preannotation_results.video_execution_journal import (
    BeginWindowRequest,
    CaptureWindowRequest,
    FinishRequest,
    FreezePlanRequest,
    PutPartRequest,
    ReadPartRequest,
    ReadRequest,
    ResumePageRequest,
    SealWindowRequest,
    VerifyReplayRequest,
    VideoJournalFailure,
    decode_native_reply,
    request_bytes,
)
from aap_preannotation_results.video_execution_plan import VideoContinuationPostState
from aap_preannotation_results.video_execution_planner import (
    build_video_execution_plan,
    freeze_video_window_settings,
)
from aap_preannotation_results.video_window_checkpoint import (
    make_video_window_checkpoint_payload,
    snapshot_video_window_checkpoint,
)
from app.services.video_tracking.backend_identity import (
    build_video_backend_compatibility,
)
from tests.test_video_execution_native import _proof, _step
from tests.test_video_tracker_input_native import _native_call, _seed_tracker_job
from tests.test_video_tracker_input_lifecycle import (
    _capture_input,
    _finish_request,
    _plan_for_claim,
    _prepare_and_claim,
    _stage_times,
)

pytestmark = pytest.mark.asyncio


async def _call(db, action, request):
    raw = await db.scalar(
        text("SELECT aap_jobs_v1.video_execution_checkpoint_step(:action,:arguments)"),
        {"action": action, "arguments": request_bytes(action, request).decode()},
    )
    return decode_native_reply(action, request, raw)


async def _prepared_execution(db):
    actor, _, _, task_id, job = await _seed_tracker_job(db)
    _, captured = await _capture_input(db, actor, job)
    source_claim = await _prepare_and_claim(db, actor, task_id, captured)
    source_plan, entries = _plan_for_claim(source_claim, frame_count=6)
    await _stage_times(db, source_claim, entries)
    await _native_call(
        db, "finish_video_tracker_input", _finish_request(source_claim, source_plan)
    )
    reply = await _step(
        db, "claim", dict(job_id=str(job.id), worker_id=str(uuid4()), lease_seconds=60)
    )
    assert reply["status"] == "ok"
    proof = ExecutionProof.model_validate_json(
        json.dumps(_proof(job.id, reply["execution"]))
    )
    job.status = "running"
    await db.flush()
    facts = (await _call(db, "input_facts", ReadRequest(**proof.model_dump())))["facts"]
    assert facts.source.source_version_id == source_plan.input.blob.version_id
    assert facts.timebase.sampling_step == source_plan.sampling.step
    assert facts.timebase.selected_frame_count == source_plan.selected_count
    backend = build_video_backend_compatibility(
        model_key="mock_bbox", selected_backend=None
    )
    settings = freeze_video_window_settings(
        requested_model_key="mock_bbox",
        platform_default_frames=2,
        platform_sam3_frames=2,
        backend_compatibility=backend,
    )
    plan = build_video_execution_plan(
        job_id=job.id,
        facts=facts,
        window_settings=settings,
        backend_compatibility=backend,
        output_geometry="bbox",
        initial_seed_source="prompt",
        initial_seed_digest=facts.prompt_digest,
    )
    assert (
        await _call(
            db, "freeze_plan", FreezePlanRequest(**proof.model_dump(), plan=plan)
        )
    )["plan"] == plan
    return job, proof, plan


def _begin(proof, plan, ordinal, previous=None):
    window = plan.windows[ordinal]
    return BeginWindowRequest(
        **proof.model_dump(),
        plan_digest=plan.plan_digest,
        window_ordinal=ordinal,
        window_digest=window.window_digest,
        seed_input_digest=window.seed_digest
        if ordinal == 0
        else previous.continuation_digest
        if previous
        else None,
        predecessor_checkpoint_digest=previous.manifest_digest if previous else None,
    )


def _payload(plan, ordinal, previous=None):
    window = plan.windows[ordinal]
    geometry = dict(type="bbox", x=0.1, y=0.2, w=0.3, h=0.4)
    return make_video_window_checkpoint_payload(
        plan=plan,
        window_ordinal=ordinal,
        raw_response_digest="1" * 64,
        frame_results=[
            dict(
                frame_index=index,
                geometry=geometry,
                confidence=1.0,
                outside=False,
                instance_id=None,
                primary=False,
            )
            for index in range(window.from_frame, window.to_frame + 1)
        ],
        continuation=VideoContinuationPostState(
            last_geometry=geometry,
            last_geom_by_instance={},
            mp_prev_boundary={},
            mp_next_global=1,
        ),
        predecessor_checkpoint=previous,
    )


async def _seal(db, proof, plan, ordinal, previous=None):
    attempt = (await _call(db, "begin_window", _begin(proof, plan, ordinal, previous)))[
        "attempt"
    ]
    with snapshot_video_window_checkpoint(
        plan, _payload(plan, ordinal, previous), predecessor_checkpoint=previous
    ) as snapshot:
        manifest = snapshot.manifest
        await _call(
            db,
            "capture_window",
            CaptureWindowRequest(
                **proof.model_dump(),
                plan_digest=plan.plan_digest,
                window_ordinal=ordinal,
                capture_id=attempt.capture_id,
                manifest=manifest,
            ),
        )
        for part in snapshot.iter_parts():
            await _call(
                db,
                "put_part",
                PutPartRequest(
                    **proof.model_dump(),
                    capture_id=attempt.capture_id,
                    index=part.part_index,
                    sha256=part.part_digest,
                    data_base64=part.data_base64,
                ),
            )
        assert (
            await _call(
                db,
                "seal_window",
                SealWindowRequest(**proof.model_dump(), capture_id=attempt.capture_id),
            )
        )["window"].manifest == manifest
        return manifest


async def test_native_window_parts_are_atomic_immutable_and_claim_bound(db_session):
    db = db_session
    job, proof, plan = await _prepared_execution(db)
    with pytest.raises(VideoJournalFailure, match="window_out_of_order"):
        await _call(db, "begin_window", _begin(proof, plan, 1))
    attempt = (await _call(db, "begin_window", _begin(proof, plan, 0)))["attempt"]
    with snapshot_video_window_checkpoint(plan, _payload(plan, 0)) as snapshot:
        manifest, part = snapshot.manifest, next(snapshot.iter_parts())
    capture = CaptureWindowRequest(
        **proof.model_dump(),
        plan_digest=plan.plan_digest,
        window_ordinal=0,
        capture_id=attempt.capture_id,
        manifest=manifest,
    )
    await _call(db, "capture_window", capture)
    with pytest.raises(VideoJournalFailure, match="part_missing|checkpoint_incomplete"):
        await _call(
            db,
            "seal_window",
            SealWindowRequest(**proof.model_dump(), capture_id=attempt.capture_id),
        )
    bad = codec.decode_part(part.data_base64)[:-1]
    with pytest.raises(VideoJournalFailure, match="part_invalid"):
        await _call(
            db,
            "put_part",
            PutPartRequest(
                **proof.model_dump(),
                capture_id=attempt.capture_id,
                index=0,
                sha256=hashlib.sha256(bad).hexdigest(),
                data_base64=codec.encode_part(bad),
            ),
        )
    put = PutPartRequest(
        **proof.model_dump(),
        capture_id=attempt.capture_id,
        index=0,
        sha256=part.part_digest,
        data_base64=part.data_base64,
    )
    first = await _call(db, "put_part", put)
    assert await _call(db, "put_part", put) == first
    seal = SealWindowRequest(**proof.model_dump(), capture_id=attempt.capture_id)
    assert (await _call(db, "seal_window", seal))["window"].manifest == manifest
    assert (await _call(db, "seal_window", seal))["window"].manifest == manifest
    read = ReadPartRequest(
        **proof.model_dump(), checkpoint_id=manifest.checkpoint_id, index=0
    )
    assert (await _call(db, "read_part", read))["part"] == part
    await db.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' WHERE job_id=:job"
        ),
        {"job": job.id},
    )
    second = await _step(
        db, "claim", dict(job_id=str(job.id), worker_id=str(uuid4()), lease_seconds=60)
    )
    replacement = ExecutionProof.model_validate_json(
        json.dumps(_proof(job.id, second["execution"]))
    )
    with pytest.raises(VideoJournalFailure, match="lease_lost"):
        await _call(db, "put_part", put)
    replay = (await _call(db, "begin_window", _begin(replacement, plan, 0)))["attempt"]
    assert replay.mode == "replay" and replay.checkpoint == manifest
    verify = dict(
        **replacement.model_dump(),
        plan_digest=plan.plan_digest,
        window_ordinal=0,
        capture_id=replay.capture_id,
        checkpoint_id=manifest.checkpoint_id,
        result_digest=manifest.result_digest,
        continuation_digest=manifest.continuation_digest,
        discovery_seeds_digest=manifest.discovery_seeds_digest,
        seed_input_digest=manifest.seed_input_digest,
        predecessor_checkpoint_digest=manifest.predecessor_checkpoint_digest,
        raw_response_digest="2" * 64,
    )
    with pytest.raises(VideoJournalFailure, match="replay_mismatch"):
        await _call(
            db,
            "verify_replay",
            VerifyReplayRequest(**{**verify, "result_digest": "3" * 64}),
        )
    assert await _call(db, "verify_replay", VerifyReplayRequest(**verify)) == {
        "verified": True
    }
    page = (
        await _call(
            db, "read_resume", ResumePageRequest(**replacement.model_dump(), limit=1)
        )
    )["page"]
    assert page.next_ordinal == 0
    assert page.windows[0].checkpoint == manifest
    assert page.windows[0].attempt.state == "replay_verified"
    assert page.closed_spans[0].closed is True


async def test_success_requires_all_windows_but_cancellation_preserves_partial_review(
    db_session,
):
    db = db_session
    job, proof, plan = await _prepared_execution(db)
    await _seal(db, proof, plan, 0)
    with pytest.raises(VideoJournalFailure, match="checkpoint_incomplete"):
        await _call(
            db, "finish", FinishRequest(**proof.model_dump(), outcome="completed")
        )
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE public.video_tracker_jobs SET status='pending_review' WHERE id=:job"
                ),
                {"job": proof.job_id},
            )
    await db.execute(
        text("UPDATE public.video_tracker_jobs SET status='cancelled' WHERE id=:job"),
        {"job": proof.job_id},
    )
    assert (
        await db.scalar(
            text(
                "SELECT writer_terminal FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": proof.job_id},
        )
        is False
    )
    await _call(db, "finish", FinishRequest(**proof.model_dump(), outcome="cancelled"))
    await _call(db, "retire", ReadRequest(**proof.model_dump()))
    # Reviewing accepted partial cancellation must not rewrite the writer outcome.
    await db.execute(
        text("UPDATE public.video_tracker_jobs SET status='accepted' WHERE id=:job"),
        {"job": proof.job_id},
    )
    assert (
        await db.scalar(
            text(
                "SELECT writer_terminal_outcome FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": proof.job_id},
        )
        == "cancelled"
    )


async def test_complete_window_chain_finishes_with_source_retired(db_session):
    db = db_session
    job, proof, plan = await _prepared_execution(db)
    previous = None
    for ordinal in range(len(plan.windows)):
        previous = await _seal(db, proof, plan, ordinal, previous)
    await db.execute(
        text(
            "UPDATE public.video_tracker_jobs SET status='pending_review' WHERE id=:job"
        ),
        {"job": proof.job_id},
    )
    await _call(db, "finish", FinishRequest(**proof.model_dump(), outcome="completed"))
    assert await _call(db, "retire", ReadRequest(**proof.model_dump())) == {
        "retired": True
    }
    flags = (
        await db.execute(
            text(
                "SELECT writer_terminal,execution_retired FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": proof.job_id},
        )
    ).one()
    assert tuple(flags) == (True, True)
