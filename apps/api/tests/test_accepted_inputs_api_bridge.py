"""Parse real native receipts through the production API transaction adapter."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from aap_assets_inputs import contract as c
from app.db.models.annotation import Annotation
from app.services.accepted_inputs import (
    SqlAlchemyInputDatabase,
    attach_annotation_input_source,
    copy_annotation_input_sources,
    lock_input_source,
)
from tests.test_assets_accepted_inputs_native import _seed_workbench_input

pytestmark = pytest.mark.asyncio


async def test_native_receipts_parse_and_annotation_pins_share_caller_rollback(
    db_session,
):
    (
        actor_id,
        project,
        _dataset,
        _item,
        task,
        job_id,
        source,
    ) = await _seed_workbench_input(db_session)
    native = SqlAlchemyInputDatabase(
        db_session, commit=False, allowed_roles={"control", "worker"}
    )
    request = c.FindInputRequest(job_id=job_id, task_id=task.id, actor_id=actor_id)
    assert (await native.call("find_accepted_image_input", request)).input is None
    accepted = await native.call(
        "reserve_accepted_image_input",
        c.ReserveInputRequest(
            **request.model_dump(),
            source_snapshot=source["source_snapshot"],
            object_identity=source["object_identity"],
        ),
    )
    identity = c.InputRequest(
        input_id=accepted.input_id, job_id=job_id, task_id=task.id
    )
    assert (await native.call("read_accepted_image_input", identity)) == accepted
    claimed = await native.call(
        "claim_accepted_image_input",
        c.ClaimInputRequest(worker_id=uuid4(), input_id=accepted.input_id),
    )
    claim = claimed.claim
    assert claim is not None
    assert claim.source.object_identity == c.ObjectIdentity.model_validate(
        source["object_identity"]
    )
    await native.call(
        "heartbeat_accepted_image_input",
        c.HeartbeatInputRequest(
            input_id=accepted.input_id,
            generation=claim.generation,
            lease_token=claim.lease_token,
        ),
    )
    ready = await native.call(
        "finish_accepted_image_input",
        c.FinishInputRequest(
            input_id=accepted.input_id,
            generation=claim.generation,
            lease_token=claim.lease_token,
            blob=c.InputBlob(
                key=claim.blob_key,
                version_id="native-bridge-version",
                etag="native-bridge-etag",
                byte_size=claim.source.object_identity.byte_size,
                sha256="a" * 64,
            ),
        ),
    )
    work = await native.call("read_accepted_image_input_work", identity)
    assert ready.status == "ready" and work.input == ready
    assert work.blob.version_id == "native-bridge-version"
    # Workflow changes are not source changes; the current Labeling scope is
    # independently checked by the application before this native source fence.
    task.version += 1
    await db_session.flush()
    assert (
        await lock_input_source(
            db_session, input_id=ready.input_id, task_id=task.id, project_id=project.id
        )
    ).sha256 == ready.sha256

    async with db_session.begin_nested() as transaction:
        annotations = [
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor_id,
                class_name="car",
                geometry={"type": "bbox", "x": index, "y": 0, "width": 2, "height": 2},
            )
            for index in range(2)
        ]
        db_session.add_all(annotations)
        await db_session.flush()
        annotation_ids = [row.id for row in annotations]
        assert (
            await attach_annotation_input_source(
                db_session,
                annotation_id=annotation_ids[0],
                input_id=ready.input_id,
                sha256=ready.sha256,
            )
            is True
        )
        assert (
            await attach_annotation_input_source(
                db_session,
                annotation_id=annotation_ids[0],
                input_id=ready.input_id,
                sha256=ready.sha256,
            )
            is False
        )
        assert (
            await copy_annotation_input_sources(
                db_session,
                source_annotation_id=annotation_ids[0],
                target_annotation_id=annotation_ids[1],
            )
            == 1
        )
        assert (
            await copy_annotation_input_sources(
                db_session,
                source_annotation_id=annotation_ids[0],
                target_annotation_id=annotation_ids[1],
            )
            == 0
        )
        assert (
            await db_session.scalar(
                text(
                    "SELECT count(*) FROM public.annotation_input_sources WHERE input_id=:input"
                ),
                {"input": ready.input_id},
            )
            == 2
        )
        await transaction.rollback()

    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.annotation_input_sources WHERE input_id=:input"
            ),
            {"input": ready.input_id},
        )
        == 0
    )
    assert (await native.call("read_accepted_image_input", identity)).status == "ready"
