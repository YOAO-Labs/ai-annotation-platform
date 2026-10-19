"""Native lineage pins, delayed retirement and terminal frame cleanup."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from aap_assets_video_inputs.evidence import (
    AttachAnnotationVideoInputSourceRequest,
    CopyAnnotationVideoInputSourcesRequest,
    parse_labeling_native,
    ready_evidence,
)
from app.db.models.annotation import Annotation
from tests.test_video_input_ledger_control import _call
from tests.test_video_input_ledger_execution import _seed_prepared_video_input
from tests.test_video_input_selection_native import _add_task

pytestmark = pytest.mark.asyncio


async def _ready_plan(db, count=4):
    (
        actor,
        project,
        job,
        selection,
        header,
        input_id,
        raw,
    ) = await _seed_prepared_video_input(db)
    claim = c.VideoInputClaim.model_validate(raw)
    record = claim.input
    source = v.FrozenVideoInput(
        input_id=record.input_id,
        job_id=record.job_id,
        actor_id=record.actor_id,
        task=record.task,
        source=record.source,
        blob={
            "key": claim.blob_key,
            "version_id": "original-retained-v1",
            "etag": "retained-etag",
            "byte_size": 128,
            "sha256": "a" * 64,
        },
        video={
            "frame_count": count,
            "width": 640,
            "height": 360,
            "duration_ms": 1000,
            "video_stream": 0,
        },
        created_at=record.created_at,
    )
    entries = tuple(
        v.FrameTimeEntry(
            ordinal=i, frame_index=i - 1, pts_ms=(i - 1) * 10, is_keyframe=i == 1
        )
        for i in range(1, count + 1)
    )
    plan = v.FrozenFramePlan.from_entries(source, record.sampling, entries)
    lease = {
        "input_id": UUID(input_id),
        "generation": claim.generation,
        "lease_token": claim.lease_token,
    }
    await _call(
        db,
        "stage_accepted_video_times",
        c.StageVideoTimesRequest(**lease, cursor=0, entries=entries),
    )
    await _call(
        db, "finish_accepted_video_input", c.FinishVideoInputRequest(**lease, plan=plan)
    )
    await _call(
        db,
        "seal_accepted_video_manifest",
        c.SealVideoManifestRequest(
            job_id=job,
            actor_id=actor,
            project_id=project.id,
            selection_digest=header["selection_digest"],
        ),
    )
    return actor, project.id, plan


def _frame_request(actor, plan, ordinal):
    return c.VideoFrameRequest(
        actor_id=actor,
        job_id=plan.input.job_id,
        task_id=plan.input.task.task_id,
        input_id=plan.input.input_id,
        plan_digest=plan.plan_digest,
        ordinal=ordinal,
    )


async def _frame_claim(db, frame):
    result = await _call(
        db,
        "claim_accepted_video_frame",
        c.ClaimVideoFrameRequest(
            worker_id=uuid4(), frame_id=frame.frame_id, lease_seconds=600
        ),
    )
    assert result.claim is not None
    return result.claim


async def _publish_frame(db, claim):
    return await _call(
        db,
        "finish_accepted_video_frame",
        c.FinishVideoFrameRequest(
            frame_id=claim.frame.frame_id,
            generation=claim.generation,
            lease_token=claim.lease_token,
            blob={
                "key": claim.blob_key,
                "version_id": "frame-retained-v1",
                "etag": "frame-etag",
                "byte_size": 17,
                "sha256": "b" * 64,
            },
        ),
    )


async def _evidence_call(db, operation, request):
    if operation == "attach_annotation_video_input_source":
        raw = await db.scalar(
            text(
                "SELECT aap_labeling_v1.attach_annotation_video_input_source("
                ":annotation,:actor,CAST(:evidence AS jsonb))"
            ),
            {
                "annotation": request.annotation_id,
                "actor": request.actor_id,
                "evidence": request.evidence.model_dump_json(by_alias=True),
            },
        )
    else:
        raw = await db.scalar(
            text(
                "SELECT aap_labeling_v1.copy_annotation_video_input_sources(:source,:target)"
            ),
            {
                "source": request.source_annotation_id,
                "target": request.target_annotation_id,
            },
        )
    return parse_labeling_native(operation, request, raw)


async def _sweep(db):
    return await _call(db, "sweep_accepted_video_inputs", c.SweepVideoInputsRequest())


async def test_video_lineage_keeps_original_task_and_blocks_retirement_until_unreferenced(
    db_session,
):
    actor, project, plan = await _ready_plan(db_session)
    requested = await _call(
        db_session, "require_accepted_video_frame", _frame_request(actor, plan, 1)
    )
    frame = await _publish_frame(db_session, await _frame_claim(db_session, requested))
    evidence = ready_evidence(plan, frame)
    task2, _ = await _add_task(
        db_session, project_id=project, dataset_id=plan.input.source.dataset_id
    )
    task3, _ = await _add_task(
        db_session, project_id=project, dataset_id=plan.input.source.dataset_id
    )

    def annotation(task, *, parent=None):
        return Annotation(
            task_id=task,
            project_id=project,
            user_id=actor,
            parent_annotation_id=parent,
            class_name="car",
            geometry={"type": "bbox", "x": 1, "y": 2, "width": 5, "height": 6},
        )

    parent = annotation(plan.input.task.task_id)
    db_session.add(parent)
    await db_session.flush()
    child = annotation(task2, parent=parent.id)
    db_session.add(child)
    await db_session.flush()
    attached = await _evidence_call(
        db_session,
        "attach_annotation_video_input_source",
        AttachAnnotationVideoInputSourceRequest(
            annotation_id=parent.id, actor_id=actor, evidence=evidence
        ),
    )
    assert attached.attached
    grandchild = annotation(task3, parent=child.id)
    target = annotation(task3)
    db_session.add_all([grandchild, target])
    await db_session.flush()
    copied = await _evidence_call(
        db_session,
        "copy_annotation_video_input_sources",
        CopyAnnotationVideoInputSourcesRequest(
            source_annotation_id=child.id, target_annotation_id=target.id
        ),
    )
    assert copied.copied == 1
    rows = (
        await db_session.execute(
            text(
                "SELECT task_id,evidence FROM public.annotation_video_input_sources "
                "WHERE input_id=:input ORDER BY annotation_id"
            ),
            {"input": plan.input.input_id},
        )
    ).all()
    assert len(rows) == 4
    assert all(
        task == plan.input.task.task_id
        and raw == evidence.model_dump(mode="json", by_alias=True)
        for task, raw in rows
    )
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job"),
        {"job": plan.input.job_id},
    )
    assert (await _sweep(db_session)).expired == 0
    assert (
        await db_session.scalar(
            text(
                "SELECT retire_after FROM public.asset_accepted_video_inputs WHERE input_id=:input"
            ),
            {"input": plan.input.input_id},
        )
        is None
    )
    await db_session.execute(
        text("UPDATE public.annotations SET parent_annotation_id=NULL WHERE id=:id"),
        {"id": child.id},
    )
    await db_session.execute(
        text("DELETE FROM public.annotations WHERE id=:id"), {"id": parent.id}
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT pg_catalog.count(*) FROM public.annotation_video_input_sources WHERE input_id=:input"
            ),
            {"input": plan.input.input_id},
        )
        == 3
    )
    await db_session.execute(
        text("DELETE FROM public.annotations WHERE id=ANY(CAST(:ids AS uuid[]))"),
        {"ids": [child.id, grandchild.id, target.id]},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs SET retention_checked_at=NULL WHERE input_id=:input"
        ),
        {"input": plan.input.input_id},
    )
    assert (await _sweep(db_session)).expired == 0
    assert await db_session.scalar(
        text(
            "SELECT retire_after > pg_catalog.clock_timestamp()+interval '29 days' "
            "FROM public.asset_accepted_video_inputs WHERE input_id=:input"
        ),
        {"input": plan.input.input_id},
    )


async def test_video_retirement_preserves_ready_receipts_and_terminalizes_every_other_frame(
    db_session,
):
    actor, _, plan = await _ready_plan(db_session)
    frames = [
        await _call(
            db_session, "require_accepted_video_frame", _frame_request(actor, plan, i)
        )
        for i in range(1, 5)
    ]
    ready = await _publish_frame(db_session, await _frame_claim(db_session, frames[0]))
    stale = await _frame_claim(db_session, frames[2])
    failed = await _frame_claim(db_session, frames[3])
    await _call(
        db_session,
        "fail_accepted_video_frame",
        c.FailVideoFrameRequest(
            frame_id=failed.frame.frame_id,
            generation=failed.generation,
            lease_token=failed.lease_token,
            error_code="processing_failed",
        ),
    )
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job"),
        {"job": plan.input.job_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs SET retire_after=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE input_id=:input"
        ),
        {"input": plan.input.input_id},
    )
    assert (await _sweep(db_session)).expired == 0
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_frames SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE frame_id=:frame"
        ),
        {"frame": stale.frame.frame_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_frame_attempts SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE frame_id=:frame"
        ),
        {"frame": stale.frame.frame_id},
    )
    assert (await _sweep(db_session)).expired == 1
    rows = (
        await db_session.execute(
            text(
                "SELECT status,error_code,lease_token,blob FROM public.asset_accepted_video_frames "
                "WHERE input_id=:input ORDER BY ordinal"
            ),
            {"input": plan.input.input_id},
        )
    ).all()
    assert rows[0] == ("expired", None, None, ready.blob.model_dump(mode="json"))
    assert all(
        tuple(row) == ("failed", "input_expired", None, None) for row in rows[1:]
    )
    record = await _call(
        db_session,
        "read_accepted_video_input",
        c.ReadVideoInputRequest(
            input_id=plan.input.input_id,
            job_id=plan.input.job_id,
            task_id=plan.input.task.task_id,
            actor_id=actor,
        ),
    )
    assert record.status == "expired" and record.plan == plan
    prefixes = (
        await db_session.execute(
            text(
                "SELECT garbage.prefix,garbage.not_before > pg_catalog.clock_timestamp()+interval '23 hours' "
                "FROM public.asset_accepted_input_garbage AS garbage ORDER BY garbage.prefix"
            )
        )
    ).all()
    assert len(prefixes) == 4
    assert all(
        prefix.startswith(f"accepted-video-inputs/{plan.input.input_id}/") and delayed
        for prefix, delayed in prefixes
    )
    with pytest.raises(c.VideoInputFailure, match="input_expired"):
        await _call(
            db_session, "require_accepted_video_frame", _frame_request(actor, plan, 2)
        )
