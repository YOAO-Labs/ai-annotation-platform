"""Real SQL receipts use the application's original Job/Labeling transactions."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs.evidence import ready_evidence
from app.db.models.annotation import Annotation
from app.services.accepted_video_inputs import (
    SqlAlchemyVideoInputDatabase,
    attach_annotation_video_input_source,
    copy_annotation_video_input_sources,
    get_video_input_manifest_capture,
    lock_video_frame_source,
    lock_video_input_pin,
    lock_video_input_source,
)
from tests.test_video_input_authorities_native import _seed_video_job, _seed_video_task
from tests.test_video_input_ledger_lifecycle import (
    _frame_claim,
    _frame_request,
    _publish_frame,
    _ready_plan,
)
from tests.test_video_input_selection_native import _selection

pytestmark = pytest.mark.asyncio


async def test_video_capture_and_original_job_roll_back_together(db_session):
    actor, project, _dataset, _item, _task = await _seed_video_task(db_session)
    selection = _selection()
    async with db_session.begin_nested() as transaction:
        job_id = await _seed_video_job(
            db_session, actor_id=actor, project_id=project.id, selection=selection
        )
        header = await get_video_input_manifest_capture()(db_session).capture(
            c.CaptureVideoManifestRequest(
                job_id=job_id,
                actor_id=actor,
                project_id=project.id,
                selection=selection,
            )
        )
        await db_session.execute(
            text(
                "UPDATE public.async_jobs SET payload=payload || "
                "pg_catalog.jsonb_build_object('accepted_video_selection_digest',CAST(:digest AS text)) "
                "WHERE id=:job"
            ),
            {"digest": header.selection_digest, "job": job_id},
        )
        found = await SqlAlchemyVideoInputDatabase(db_session, commit=False).call(
            "read_accepted_video_manifest",
            c.VideoManifestScope(job_id=job_id, actor_id=actor, project_id=project.id),
        )
        assert found == header
        await transaction.rollback()
    assert not await db_session.scalar(
        text("SELECT EXISTS(SELECT 1 FROM public.async_jobs WHERE id=:job)"),
        {"job": job_id},
    )
    assert not await db_session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM public.asset_accepted_video_manifests WHERE job_id=:job)"
        ),
        {"job": job_id},
    )


async def test_video_final_fences_and_annotation_pins_join_labeling_rollback(
    db_session,
):
    actor, project_id, plan = await _ready_plan(db_session)
    request = _frame_request(actor, plan, 1)
    native = SqlAlchemyVideoInputDatabase(db_session, commit=False)
    frame = await native.call("require_accepted_video_frame", request)
    frame = await _publish_frame(db_session, await _frame_claim(db_session, frame))
    proof = ready_evidence(plan, frame)
    assert await lock_video_input_source(
        db_session,
        c.LockVideoInputRequest(**request.model_dump(exclude={"ordinal"})),
    ) == await native.call(
        "read_accepted_video_input",
        c.ReadVideoInputRequest(
            **request.model_dump(exclude={"ordinal", "plan_digest"})
        ),
    )
    assert (
        await lock_video_frame_source(
            db_session,
            c.LockVideoFrameRequest(
                **request.model_dump(),
                expected_entry=frame.entry,
                expected_blob=frame.blob,
            ),
        )
        == frame
    )
    assert (
        await lock_video_input_pin(
            db_session,
            task_id=proof.task_id,
            project_id=project_id,
            evidence=proof,
            actor_id=actor,
        )
    ).frame == frame
    async with db_session.begin_nested() as transaction:
        annotations = [
            Annotation(
                id=uuid4(),
                task_id=proof.task_id,
                project_id=project_id,
                user_id=actor,
                class_name="car",
                geometry={
                    "type": "video_bbox",
                    "frame_index": 0,
                    "x": 0,
                    "y": 0,
                    "w": 0.1,
                    "h": 0.1,
                },
            )
            for _ in range(2)
        ]
        db_session.add_all(annotations)
        await db_session.flush()
        source_id, target_id = (row.id for row in annotations)
        assert await attach_annotation_video_input_source(
            db_session, annotation_id=source_id, actor_id=actor, evidence=proof
        )
        assert not await attach_annotation_video_input_source(
            db_session, annotation_id=source_id, actor_id=actor, evidence=proof
        )
        assert (
            await copy_annotation_video_input_sources(
                db_session,
                source_annotation_id=source_id,
                target_annotation_id=target_id,
            )
            == 1
        )
        assert (
            await copy_annotation_video_input_sources(
                db_session,
                source_annotation_id=source_id,
                target_annotation_id=target_id,
            )
            == 0
        )
        assert (
            await db_session.scalar(
                text(
                    "SELECT count(*) FROM public.annotation_video_input_sources WHERE input_id=:input"
                ),
                {"input": proof.input_id},
            )
            == 2
        )
        await transaction.rollback()
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.annotation_video_input_sources WHERE input_id=:input"
            ),
            {"input": proof.input_id},
        )
        == 0
    )
