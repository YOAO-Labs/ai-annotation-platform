"""Database regressions for Workbench input receipts, leases and Labeling pins."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import ProjectDataset
from tests.factory import create_project, create_task
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_images_execution import _seed_image

pytestmark = pytest.mark.asyncio

_READ_SOURCE = text(
    "SELECT aap_assets_v1.read_image_source(CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), NULL)"
)
_FIND = text(
    "SELECT aap_assets_v1.find_accepted_image_input(CAST(:job_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:actor_id AS uuid))"
)
_RESERVE = text(
    "SELECT aap_assets_v1.reserve_accepted_image_input(CAST(:job_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:actor_id AS uuid), CAST(:source_snapshot AS text), "
    "CAST(:object_identity AS jsonb))"
)
_READ = text(
    "SELECT aap_assets_v1.read_accepted_image_input(CAST(:input_id AS uuid), "
    "CAST(:job_id AS uuid), CAST(:task_id AS uuid))"
)
_READ_WORK = text(
    "SELECT aap_assets_v1.read_accepted_image_input_work(CAST(:input_id AS uuid), "
    "CAST(:job_id AS uuid), CAST(:task_id AS uuid))"
)
_CLAIM = text(
    "SELECT aap_assets_v1.claim_accepted_image_input(CAST(:worker_id AS uuid), "
    "CAST(:input_id AS uuid), :lease_seconds)"
)
_HEARTBEAT = text(
    "SELECT aap_assets_v1.heartbeat_accepted_image_input(CAST(:input_id AS uuid), "
    "CAST(:generation AS bigint), CAST(:lease_token AS uuid), :lease_seconds)"
)
_FINISH = text(
    "SELECT aap_assets_v1.finish_accepted_image_input(CAST(:input_id AS uuid), "
    "CAST(:generation AS bigint), CAST(:lease_token AS uuid), CAST(:blob AS jsonb))"
)
_FAIL = text(
    "SELECT aap_assets_v1.fail_accepted_image_input(CAST(:input_id AS uuid), "
    "CAST(:generation AS bigint), CAST(:lease_token AS uuid), :error_code)"
)
_LOCK = text(
    "SELECT aap_assets_v1.lock_accepted_image_input(CAST(:input_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:project_id AS uuid))"
)
_ATTACH = text(
    "SELECT aap_labeling_v1.attach_annotation_input_source(CAST(:annotation_id AS uuid), "
    "CAST(:input_id AS uuid), CAST(:sha256 AS text))"
)
_COPY_PINS = text(
    "SELECT aap_labeling_v1.copy_annotation_input_sources("
    "CAST(:source_annotation_id AS uuid), CAST(:target_annotation_id AS uuid))"
)
_REFERENCED = text(
    "SELECT aap_labeling_v1.accepted_image_input_referenced(CAST(:input_id AS uuid))"
)
_SWEEP = text("SELECT aap_assets_v1.sweep_accepted_image_inputs(:page_size)")
_CLAIM_GARBAGE = text(
    "SELECT aap_assets_v1.claim_accepted_input_garbage(CAST(:worker_id AS uuid), "
    ":lease_seconds, :page_size)"
)
_COMPLETE_GARBAGE = text(
    "SELECT aap_assets_v1.complete_accepted_input_garbage(CAST(:garbage_id AS uuid), "
    "CAST(:lease_token AS uuid))"
)


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _data(reply: dict) -> dict:
    assert reply["schema"] == "aap-assets-inputs/v1", reply
    assert reply["ok"] is True, reply.get("error")
    assert set(reply) == {"schema", "ok", "data"}
    return reply["data"]


def _image_data(reply: dict) -> dict:
    assert reply["schema"] == "aap-assets-images/v1", reply
    assert reply["ok"] is True, reply.get("error")
    assert set(reply) == {"schema", "ok", "data"}
    return reply["data"]


def _error(reply: dict, code: str) -> None:
    assert reply == {
        "schema": "aap-assets-inputs/v1",
        "ok": False,
        "error": {"code": code},
    }


async def _seed_workbench_input(db_session):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    db_session.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
    item_id = await _seed_image(
        db_session, actor_id=actor_id, dataset_id=dataset_id, width=16, height=8
    )
    task = await create_task(db_session, project_id=project.id)
    task.dataset_item_id = item_id
    await db_session.flush()
    task_ids = [str(task.id)]
    job_id = uuid.uuid4()
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor_id,
            status="pending",
            celery_task_id=str(job_id),
            payload={
                "execution_scope": "workbench",
                "task_ids": task_ids,
                "scope": {"task_ids": task_ids},
            },
            result={},
        )
    )
    await db_session.flush()
    source = _image_data(
        _reply(
            await db_session.scalar(
                _READ_SOURCE, {"actor_id": actor_id, "task_id": task.id}
            )
        )
    )
    return actor_id, project, dataset_id, item_id, task, job_id, source


async def _reserve(db_session, actor_id, task, job_id, source):
    return _data(
        _reply(
            await db_session.scalar(
                _RESERVE,
                {
                    "job_id": job_id,
                    "task_id": task.id,
                    "actor_id": actor_id,
                    "source_snapshot": source["source_snapshot"],
                    "object_identity": json.dumps(source["object_identity"]),
                },
            )
        )
    )


async def _claim(db_session, input_id):
    result = _data(
        _reply(
            await db_session.scalar(
                _CLAIM,
                {"worker_id": uuid.uuid4(), "input_id": input_id, "lease_seconds": 300},
            )
        )
    )
    return result["claim"]


async def _finish(db_session, claim, *, size=128, sha256=None):
    blob = {
        "key": claim["blob_key"],
        "version_id": "minio-version-1",
        "etag": "accepted-blob-etag",
        "byte_size": size,
        "sha256": sha256 or "a" * 64,
    }
    return _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "input_id": claim["input"]["input_id"],
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "blob": json.dumps(blob),
                },
            )
        )
    )


async def test_reservation_replay_keeps_original_source_and_scope(db_session):
    (
        actor_id,
        project,
        _dataset_id,
        item_id,
        task,
        job_id,
        source,
    ) = await _seed_workbench_input(db_session)
    accepted = await _reserve(db_session, actor_id, task, job_id, source)
    found = _data(
        _reply(
            await db_session.scalar(
                _FIND,
                {"job_id": job_id, "task_id": task.id, "actor_id": actor_id},
            )
        )
    )
    assert found["input"]["input_id"] == accepted["input_id"]
    assert accepted["source_owner"] == {
        "kind": "dataset_item",
        "id": str(item_id),
    }
    assert "file_path" not in accepted and "source_identity" not in accepted

    # Replays return the accepted receipt before looking at changed source facts.
    await db_session.execute(
        text("UPDATE public.dataset_items SET file_path=:path WHERE id=:item_id"),
        {"path": f"changed/{item_id}.png", "item_id": item_id},
    )
    replay = await _reserve(db_session, actor_id, task, job_id, source)
    assert replay["input_id"] == accepted["input_id"]
    assert replay["source_snapshot"] == accepted["source_snapshot"]
    assert replay["source_generation"] == accepted["source_generation"]

    # The worker refuses to copy if the captured source no longer resolves.
    assert await _claim(db_session, accepted["input_id"]) is None
    current = _data(
        _reply(
            await db_session.scalar(
                _READ,
                {
                    "input_id": accepted["input_id"],
                    "job_id": job_id,
                    "task_id": task.id,
                },
            )
        )
    )
    assert current["status"] == "failed"
    assert current["error"] == "source_changed"
    _error(
        _reply(
            await db_session.scalar(
                _READ_WORK,
                {
                    "input_id": accepted["input_id"],
                    "job_id": job_id,
                    "task_id": task.id,
                },
            )
        ),
        "source_changed",
    )

    # A Job cannot use a batch/bulk scope or a different actor as an input grant.
    wrong_job = uuid.uuid4()
    db_session.add(
        AsyncJob(
            id=wrong_job,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor_id,
            status="pending",
            celery_task_id=str(wrong_job),
            payload={"execution_scope": "bulk", "task_ids": [str(task.id)]},
            result={},
        )
    )
    await db_session.flush()
    invalid = await db_session.scalar(
        _RESERVE,
        {
            "job_id": wrong_job,
            "task_id": task.id,
            "actor_id": actor_id,
            "source_snapshot": source["source_snapshot"],
            "object_identity": json.dumps(source["object_identity"]),
        },
    )
    _error(_reply(invalid), "input_scope_mismatch")


async def test_input_lease_finish_source_fence_and_task_workflow_version(db_session):
    (
        actor_id,
        project,
        _dataset_id,
        _item_id,
        task,
        job_id,
        source,
    ) = await _seed_workbench_input(db_session)
    accepted = await _reserve(db_session, actor_id, task, job_id, source)
    claim = await _claim(db_session, accepted["input_id"])
    assert claim["input"]["status"] == "materializing"
    assert claim["blob_key"] == (
        f"accepted-inputs/{accepted['input_id']}/g{claim['generation']}/"
        f"a{claim['lease_token']}/source"
    )
    heartbeat = _data(
        _reply(
            await db_session.scalar(
                _HEARTBEAT,
                {
                    "input_id": accepted["input_id"],
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "lease_seconds": 300,
                },
            )
        )
    )
    assert heartbeat["lease_expires_at"] > claim["lease_expires_at"]

    ready = await _finish(db_session, claim)
    assert ready["status"] == "ready"
    assert ready["sha256"] == "a" * 64
    assert ready["byte_size"] == 128
    replay = await _finish(db_session, claim)
    assert replay["input_id"] == ready["input_id"]
    _error(
        _reply(
            await db_session.scalar(
                _FAIL,
                {
                    "input_id": ready["input_id"],
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "error_code": "storage_unavailable",
                },
            )
        ),
        "lease_lost",
    )
    read_work = _data(
        _reply(
            await db_session.scalar(
                _READ_WORK,
                {
                    "input_id": ready["input_id"],
                    "job_id": job_id,
                    "task_id": task.id,
                },
            )
        )
    )
    assert read_work["blob"]["version_id"] == "minio-version-1"
    assert read_work["blob"]["key"] == claim["blob_key"]
    assert read_work["source"]["source_snapshot"] == source["source_snapshot"]

    # A completed origin Job can still back an authorized candidate/retry write.
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job_id"),
        {"job_id": job_id},
    )

    # Workflow-only version and assignment changes do not change source identity.
    task.version = task.version + 1
    task.assignee_id = actor_id
    await db_session.flush()
    locked = _data(
        _reply(
            await db_session.scalar(
                _LOCK,
                {
                    "input_id": ready["input_id"],
                    "task_id": task.id,
                    "project_id": project.id,
                },
            )
        )
    )
    assert locked["input_id"] == ready["input_id"]
    assert locked["task_version"] != task.version

    # A real Task source-binding change is still fenced at the final write.
    task.file_path = f"relinked/{task.id}.png"
    await db_session.flush()
    _error(
        _reply(
            await db_session.scalar(
                _LOCK,
                {
                    "input_id": ready["input_id"],
                    "task_id": task.id,
                    "project_id": project.id,
                },
            )
        ),
        "source_changed",
    )


async def test_annotation_input_pins_survive_parent_clones_and_drive_gc(db_session):
    (
        actor_id,
        project,
        _dataset_id,
        _item_id,
        task,
        job_id,
        source,
    ) = await _seed_workbench_input(db_session)
    accepted = await _reserve(db_session, actor_id, task, job_id, source)
    claim = await _claim(db_session, accepted["input_id"])
    ready = await _finish(db_session, claim)
    assert ready["status"] == "ready"
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job_id"),
        {"job_id": job_id},
    )

    parent = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=actor_id,
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "width": 5, "height": 6},
    )
    db_session.add(parent)
    await db_session.flush()
    attached = _reply(
        await db_session.scalar(
            _ATTACH,
            {
                "annotation_id": parent.id,
                "input_id": ready["input_id"],
                "sha256": ready["sha256"],
            },
        )
    )
    assert attached["schema"] == "aap-labeling-input-sources/v1"
    assert attached["ok"] is True
    assert attached["data"] == {
        "annotation_id": str(parent.id),
        "input_id": str(ready["input_id"]),
        "sha256": ready["sha256"],
        "attached": True,
    }
    attached_replay = _reply(
        await db_session.scalar(
            _ATTACH,
            {
                "annotation_id": parent.id,
                "input_id": ready["input_id"],
                "sha256": ready["sha256"],
            },
        )
    )
    assert attached_replay["ok"] is True
    assert attached_replay["data"]["attached"] is False
    child_task = await create_task(db_session, project_id=project.id)
    child = Annotation(
        task_id=child_task.id,
        project_id=project.id,
        user_id=actor_id,
        parent_annotation_id=parent.id,
        class_name="car",
        geometry={"type": "bbox", "x": 2, "y": 3, "width": 5, "height": 6},
    )
    db_session.add(child)
    await db_session.flush()
    wrong_task_attach = _reply(
        await db_session.scalar(
            _ATTACH,
            {
                "annotation_id": child.id,
                "input_id": ready["input_id"],
                "sha256": ready["sha256"],
            },
        )
    )
    assert wrong_task_attach == {
        "schema": "aap-labeling-input-sources/v1",
        "ok": False,
        "error": {"code": "input_scope_mismatch"},
    }
    copy_target = Annotation(
        task_id=child_task.id,
        project_id=project.id,
        user_id=actor_id,
        class_name="car",
        geometry={"type": "bbox", "x": 3, "y": 4, "width": 5, "height": 6},
    )
    db_session.add(copy_target)
    await db_session.flush()
    copied = _reply(
        await db_session.scalar(
            _COPY_PINS,
            {
                "source_annotation_id": parent.id,
                "target_annotation_id": copy_target.id,
            },
        )
    )
    assert copied["schema"] == "aap-labeling-input-sources/v1"
    assert copied["ok"] is True
    assert copied["data"] == {
        "source_annotation_id": str(parent.id),
        "target_annotation_id": str(copy_target.id),
        "copied": 1,
    }
    copied_replay = _reply(
        await db_session.scalar(
            _COPY_PINS,
            {
                "source_annotation_id": parent.id,
                "target_annotation_id": copy_target.id,
            },
        )
    )
    assert copied_replay["ok"] is True
    assert copied_replay["data"]["copied"] == 0
    references = await db_session.scalar(
        text(
            "SELECT count(*) FROM public.annotation_input_sources "
            "WHERE input_id=:input_id AND sha256=:sha256"
        ),
        {"input_id": ready["input_id"], "sha256": ready["sha256"]},
    )
    assert references == 3
    retained = _reply(
        await db_session.scalar(_REFERENCED, {"input_id": ready["input_id"]})
    )
    assert retained["ok"] is True
    assert retained["data"]["referenced"] is True

    # Deleting Job history cannot invalidate bytes retained by Annotation pins.
    await db_session.execute(
        text("DELETE FROM public.async_jobs WHERE id=:job_id"), {"job_id": job_id}
    )
    metadata = _data(
        _reply(
            await db_session.scalar(
                _READ,
                {
                    "input_id": ready["input_id"],
                    "job_id": job_id,
                    "task_id": task.id,
                },
            )
        )
    )
    assert metadata["input_id"] == ready["input_id"]
    retained_work = _data(
        _reply(
            await db_session.scalar(
                _READ_WORK,
                {
                    "input_id": ready["input_id"],
                    "job_id": job_id,
                    "task_id": task.id,
                },
            )
        )
    )
    assert retained_work["blob"]["version_id"] == "minio-version-1"
    locked = _data(
        _reply(
            await db_session.scalar(
                _LOCK,
                {
                    "input_id": ready["input_id"],
                    "task_id": task.id,
                    "project_id": project.id,
                },
            )
        )
    )
    assert locked["input_id"] == ready["input_id"]

    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_image_inputs "
            "SET retire_after=clock_timestamp()-interval '1 second', "
            "retention_checked_at=NULL WHERE input_id=:input_id"
        ),
        {"input_id": ready["input_id"]},
    )
    swept = _data(_reply(await db_session.scalar(_SWEEP, {"page_size": 100})))
    assert swept["retired"] == 0
    still_ready = await db_session.scalar(
        text(
            "SELECT status FROM public.asset_accepted_image_inputs WHERE input_id=:id"
        ),
        {"id": ready["input_id"]},
    )
    assert still_ready == "ready"

    await db_session.delete(child)
    await db_session.flush()
    await db_session.delete(copy_target)
    await db_session.flush()
    await db_session.delete(parent)
    await db_session.flush()
    retained = _reply(
        await db_session.scalar(_REFERENCED, {"input_id": ready["input_id"]})
    )
    assert retained["ok"] is True
    assert retained["data"]["referenced"] is False
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_image_inputs "
            "SET retire_after=clock_timestamp()-interval '1 second', "
            "retention_checked_at=NULL WHERE input_id=:input_id"
        ),
        {"input_id": ready["input_id"]},
    )
    swept = _data(_reply(await db_session.scalar(_SWEEP, {"page_size": 100})))
    assert swept["retired"] == 1
    expired = await db_session.scalar(
        text(
            "SELECT status FROM public.asset_accepted_image_inputs WHERE input_id=:id"
        ),
        {"id": ready["input_id"]},
    )
    assert expired == "expired"
    _error(
        _reply(
            await db_session.scalar(
                _READ_WORK,
                {
                    "input_id": ready["input_id"],
                    "job_id": job_id,
                    "task_id": task.id,
                },
            )
        ),
        "input_expired",
    )

    batch = _data(
        _reply(
            await db_session.scalar(
                _CLAIM_GARBAGE,
                {"worker_id": uuid.uuid4(), "lease_seconds": 300, "page_size": 10},
            )
        )
    )
    assert len(batch["items"]) == 1
    garbage = batch["items"][0]
    assert garbage["prefix"] == claim["blob_key"].removesuffix("source")
    ack = _data(
        _reply(
            await db_session.scalar(
                _COMPLETE_GARBAGE,
                {
                    "garbage_id": garbage["garbage_id"],
                    "lease_token": garbage["lease_token"],
                },
            )
        )
    )
    assert ack == {"garbage_id": garbage["garbage_id"], "completed": True}
    replay_ack = _data(
        _reply(
            await db_session.scalar(
                _COMPLETE_GARBAGE,
                {
                    "garbage_id": garbage["garbage_id"],
                    "lease_token": garbage["lease_token"],
                },
            )
        )
    )
    assert replay_ack == ack
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_input_garbage "
            "SET not_before=clock_timestamp()-interval '1 second' WHERE garbage_id=:id"
        ),
        {"id": garbage["garbage_id"]},
    )
    rescan = _data(
        _reply(
            await db_session.scalar(
                _CLAIM_GARBAGE,
                {"worker_id": uuid.uuid4(), "lease_seconds": 300, "page_size": 10},
            )
        )
    )
    assert len(rescan["items"]) == 1
    assert rescan["items"][0]["prefix"] == garbage["prefix"]


async def test_independent_connections_cannot_claim_one_input_twice(test_engine):
    # Commit a private test world so two independent PostgreSQL sessions can race.
    async with test_engine.connect() as setup_connection:
        async with AsyncSession(setup_connection, expire_on_commit=False) as setup:
            (
                actor_id,
                _project,
                _dataset_id,
                _item_id,
                task,
                job_id,
                source,
            ) = await _seed_workbench_input(setup)
            accepted = await _reserve(setup, actor_id, task, job_id, source)
            await setup.commit()

    async with test_engine.connect() as first, test_engine.connect() as second:
        first_tx = await first.begin()
        second_tx = await second.begin()
        try:
            first_claim = _data(
                _reply(
                    await first.scalar(
                        _CLAIM,
                        {
                            "worker_id": uuid.uuid4(),
                            "input_id": accepted["input_id"],
                            "lease_seconds": 300,
                        },
                    )
                )
            )["claim"]
            assert first_claim is not None
            # The first transaction still owns the receipt lock; SKIP LOCKED
            # makes the concurrent worker return no claim without waiting.
            second_claim = _data(
                _reply(
                    await second.scalar(
                        _CLAIM,
                        {
                            "worker_id": uuid.uuid4(),
                            "input_id": accepted["input_id"],
                            "lease_seconds": 300,
                        },
                    )
                )
            )["claim"]
            assert second_claim is None
            await second_tx.commit()
            await first_tx.commit()
        finally:
            if second_tx.is_active:
                await second_tx.rollback()
            if first_tx.is_active:
                await first_tx.rollback()

    # Leave the committed disposable fixture terminal so later polling tests
    # cannot mistake its receipt for live accepted work.
    async with test_engine.begin() as cleanup:
        await cleanup.execute(
            text("UPDATE public.async_jobs SET status='completed' WHERE id=:id"),
            {"id": job_id},
        )
        await cleanup.execute(
            text(
                "UPDATE public.asset_accepted_image_input_attempts "
                "SET status='failed', completed_at=clock_timestamp() "
                "WHERE input_id=:id AND generation=:generation"
            ),
            {"id": accepted["input_id"], "generation": first_claim["generation"]},
        )
        await cleanup.execute(
            text(
                "UPDATE public.asset_accepted_image_inputs "
                "SET status='failed', error_code='job_not_active', "
                "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
                "last_lease_token=:token, last_outcome='failed_terminal' "
                "WHERE input_id=:id"
            ),
            {"token": first_claim["lease_token"], "id": accepted["input_id"]},
        )


async def test_input_pin_copy_serializes_with_retirement_sweep(test_engine):
    async with test_engine.connect() as setup_connection:
        async with AsyncSession(setup_connection, expire_on_commit=False) as setup:
            (
                actor_id,
                project,
                _dataset_id,
                _item_id,
                task,
                job_id,
                source,
            ) = await _seed_workbench_input(setup)
            accepted = await _reserve(setup, actor_id, task, job_id, source)
            claim = await _claim(setup, accepted["input_id"])
            ready = await _finish(setup, claim)
            parent = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor_id,
                class_name="car",
                geometry={"type": "bbox", "x": 1, "y": 2, "width": 5, "height": 6},
            )
            target_task = await create_task(setup, project_id=project.id)
            target = Annotation(
                task_id=target_task.id,
                project_id=project.id,
                user_id=actor_id,
                class_name="car",
                geometry={"type": "bbox", "x": 2, "y": 3, "width": 5, "height": 6},
            )
            setup.add_all((parent, target))
            await setup.flush()
            await setup.scalar(
                _ATTACH,
                {
                    "annotation_id": parent.id,
                    "input_id": ready["input_id"],
                    "sha256": ready["sha256"],
                },
            )
            await setup.execute(
                text("UPDATE public.async_jobs SET status='completed' WHERE id=:id"),
                {"id": job_id},
            )
            await setup.execute(
                text(
                    "UPDATE public.asset_accepted_image_inputs "
                    "SET retire_after=clock_timestamp()-interval '1 second', "
                    "retention_checked_at=NULL WHERE input_id=:id"
                ),
                {"id": ready["input_id"]},
            )
            await setup.commit()
            source_annotation_id = parent.id
            target_annotation_id = target.id
            input_id = ready["input_id"]

    async with (
        test_engine.connect() as copy_connection,
        test_engine.connect() as sweep_connection,
    ):
        copy_tx = await copy_connection.begin()
        sweep_tx = await sweep_connection.begin()
        try:
            copied = _reply(
                await copy_connection.scalar(
                    _COPY_PINS,
                    {
                        "source_annotation_id": source_annotation_id,
                        "target_annotation_id": target_annotation_id,
                    },
                )
            )
            assert copied["ok"] is True
            assert copied["data"]["copied"] == 1
            swept_while_copy_holds_receipt = _data(
                _reply(await sweep_connection.scalar(_SWEEP, {"page_size": 100}))
            )
            assert swept_while_copy_holds_receipt["retired"] == 0
            await sweep_tx.commit()
            await copy_tx.commit()
        finally:
            if sweep_tx.is_active:
                await sweep_tx.rollback()
            if copy_tx.is_active:
                await copy_tx.rollback()

    async with test_engine.begin() as verify:
        assert (
            await verify.scalar(
                text(
                    "SELECT status FROM public.asset_accepted_image_inputs "
                    "WHERE input_id=:id"
                ),
                {"id": input_id},
            )
            == "ready"
        )
        retained = _reply(await verify.scalar(_REFERENCED, {"input_id": input_id}))
        assert retained["ok"] is True
        assert retained["data"]["referenced"] is True
        swept_after_copy = _data(
            _reply(await verify.scalar(_SWEEP, {"page_size": 100}))
        )
        assert swept_after_copy["retired"] == 0
        assert await verify.scalar(
            text(
                "SELECT retire_after IS NULL FROM public.asset_accepted_image_inputs "
                "WHERE input_id=:id"
            ),
            {"id": input_id},
        )
