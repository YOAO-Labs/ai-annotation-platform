"""Born Dataset scope for native prediction-retry Jobs.

The ordinary admission path is exercised end to end: a real failed prediction,
Task, Project and actor, the native attempt/delivery/execution/member rows, the
parent-owned born receipt, and the deferred family guard. No test adopts a
historical attempt or rewrites a frozen source.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.prediction import FailedPrediction
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import fixture_run_ownership as ownership
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_project, create_user

pytestmark = [pytest.mark.asyncio]

ADMIT = text("SELECT aap_labeling_v1.admit_prediction_retry(CAST(:payload AS jsonb))")
SET_CONSTRAINTS = text("SET CONSTRAINTS aap_retry_born_dataset_scope_guard IMMEDIATE")
SCOPE = text(
    "SELECT aap_jobs_v1.fixture_job_dataset_scope("
    "id,kind,payload,project_id,user_id) "
    "FROM public.async_jobs WHERE id=:id"
)
RECEIPT = text(
    "SELECT dataset_ids FROM aap_jobs_v1.fixture_worker_job_datasets WHERE job_id=:id"
)


def _sqlstate(error: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if value:
            return value
        current = getattr(current, "orig", None) or current.__cause__
    return None


async def _world(
    db,
    *,
    file_type="image",
    linked=False,
    dataset=True,
    item_path=None,
    project=None,
    actor=None,
):
    suffix = uuid.uuid4().hex[:12]
    actor = actor or await create_user(
        db,
        "project_admin",
        f"retry-scope-{suffix}@example.test",
        f"Retry scope {suffix}",
    )
    project = project or await create_project(
        db,
        owner_id=actor.id,
        name=f"Retry scope {suffix}",
        type_key="ocr" if file_type == "text" else "image-det",
    )
    dataset_row = item = task = None
    if dataset:
        dataset_row = Dataset(
            display_id=f"D-{suffix}",
            name=f"Retry scope {suffix}",
            data_type="text" if file_type == "text" else "image",
            created_by=actor.id,
        )
        db.add(dataset_row)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset_row.id,
            file_name=f"retry-{suffix}.{'txt' if file_type == 'text' else 'png'}",
            file_path=item_path or f"retry-scope/{suffix}",
            file_type=file_type,
            width=32 if file_type != "text" else None,
            height=32 if file_type != "text" else None,
        )
        db.add(item)
        await db.flush()
    if file_type == "text":
        path = item_path or f"projects/{project.id}/guide/{uuid.uuid4()}.txt"
    else:
        path = item.file_path
    task = Task(
        project_id=project.id,
        dataset_item_id=(item.id if item is not None and not linked else None),
        display_id=f"T-{uuid.uuid4().hex[:15]}",
        file_name=item.file_name if item is not None else f"retry-{suffix}.txt",
        file_path=path,
        file_type=file_type,
        status="pending",
    )
    db.add(task)
    await db.flush()
    if linked and item is not None:
        db.add(
            TaskDatasetItemLink(
                task_id=task.id,
                dataset_item_id=item.id,
                role="primary_lidar",
            )
        )
        await db.flush()
    failed_id, job_id = uuid.uuid4(), uuid.uuid4()
    extra = (
        {"request_context": {"type": "text", "text": "retry the supplied text"}}
        if file_type == "text"
        else {}
    )
    db.add(
        FailedPrediction(
            id=failed_id,
            project_id=project.id,
            task_id=task.id,
            ml_backend_id=None,
            ml_backend_pool_id=None,
            retry_count=0,
            error_type="TIMEOUT",
            message="ordinary retry source fixture",
            extra=extra,
        )
    )
    await db.flush()
    return SimpleNamespace(
        actor=actor,
        project=project,
        dataset=dataset_row,
        item=item,
        task=task,
        failed_id=failed_id,
        job_id=job_id,
        extra=extra,
    )


async def _admit(db, world, *, job_id=None):
    job_id = job_id or uuid.uuid4()
    admission = await db.scalar(
        ADMIT,
        {
            "payload": json.dumps(
                {
                    "job_id": str(job_id),
                    "failed_prediction_id": str(world.failed_id),
                    "actor_id": str(world.actor.id),
                    "execution_queue": "ml.cpu",
                    "legacy_delivery": False,
                }
            )
        },
    )
    assert isinstance(admission, dict), admission
    assert admission.get("ok") is True, admission
    return job_id


async def _force_guard(db):
    await db.execute(SET_CONSTRAINTS)


async def _scope(db, job_id):
    return await db.scalar(SCOPE, {"id": job_id})


async def _receipt(db, job_id):
    return await db.scalar(RECEIPT, {"id": job_id})


async def _assert_blocked(db, dataset_ids):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as blocked:
        async with db.begin_nested():
            await ownership.guard_dataset_jobs(db, dataset_ids)
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"


async def _assert_unrelated(db, dataset_ids):
    await ownership.guard_dataset_jobs(db, dataset_ids)


async def _snapshot(db, project_id, *, unit="image"):
    payload = {
        "execution_scope": "workbench",
        "execution_unit": unit,
        "stages": [],
    }
    snapshot = PredictionExecutionSnapshot(
        project_id=project_id,
        schema_version=1,
        digest=content_digest(payload),
        payload=payload,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


def _authority(world, snapshot, *, video=False, task_id=None, dataset_id=None):
    top_item = world.task.dataset_item_id
    original = {
        "schema": 1,
        "owner_kind": "dataset_asset",
        "content_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "size_bytes": 16,
        "item_id": str(world.item.id),
        "object_key": world.task.file_path,
        "facts_digest": "b" * 64,
        "facts": {
            "owner_kind": "dataset_asset",
            "item_id": str(world.item.id),
            "relation": {
                "task_id": str(world.task.id),
                "project_id": str(world.project.id),
                "file_path": world.task.file_path,
                "file_type": world.task.file_type,
            },
            "item_facts": {
                "id": str(world.item.id),
                "dataset_id": str(dataset_id or world.dataset.id),
                "file_path": world.task.file_path,
                "file_type": world.task.file_type,
            },
        },
    }
    binding = {
        "task_id": str(task_id or world.task.id),
        "batch_id": None,
        "item_id": str(top_item) if top_item is not None else None,
        "file_path": world.task.file_path,
        "file_name": world.task.file_name,
        "version": world.task.version,
        "original": original,
    }
    proof = {
        "schema": 2,
        "job_kind": "prediction_retry",
        "actor_id": str(world.actor.id),
        "project_id": str(world.project.id),
        "batch_id": None,
        "execution_scope": "workbench",
        "predict_mode": "append",
        "execution_unit": "video" if video else "image",
        "api_key_id": None,
        "key_proof": None,
        "credentials": {
            "schema": 1,
            "actor_id": str(world.actor.id),
            "api_key_id": None,
            "key_fingerprint": None,
            "key_expires_at": None,
            "jwt": None,
        },
        "snapshot_id": str(snapshot.id),
        "snapshot_digest": snapshot.digest,
        "tasks": [binding],
    }
    if video:
        proof["media_type"] = "video"
    return proof


async def _bind_snapshot(db, job_id, snapshot, *, authority=None):
    await db.execute(
        text(
            "UPDATE public.async_jobs SET execution_snapshot_id=:snapshot WHERE id=:id"
        ),
        {"snapshot": snapshot.id, "id": job_id},
    )
    if authority is None:
        return
    await db.execute(
        text(
            "UPDATE public.async_jobs SET payload = payload || "
            "CAST(:authority AS jsonb) WHERE id=:id"
        ),
        {"authority": json.dumps(authority), "id": job_id},
    )


async def test_ordinary_image_retry_binds_task_dataset_and_rejects_mutation(
    db_session,
):
    first = await _world(db_session, file_type="image")
    second = await _world(
        db_session,
        file_type="image",
        project=first.project,
        actor=first.actor,
    )
    job = await _admit(db_session, first)
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == [first.dataset.id]
    assert await _scope(db_session, job) == {
        "kind": "datasets",
        "dataset_ids": [str(first.dataset.id)],
    }
    await _assert_blocked(db_session, [first.dataset.id])
    await _assert_unrelated(db_session, [second.dataset.id])

    # The born receipt is authoritative. A Task source change after birth
    # cannot add its new Dataset to this Job's proven scope.
    first.task.dataset_item_id = second.item.id
    first.task.file_path = second.item.file_path
    first.task.file_type = second.item.file_type
    await db_session.flush()
    assert await _receipt(db_session, job) == [first.dataset.id]
    assert await _scope(db_session, job) == {
        "kind": "datasets",
        "dataset_ids": [str(first.dataset.id)],
    }
    await _assert_unrelated(db_session, [second.dataset.id])


async def test_ordinary_linked_task_binds_primary_link_dataset(db_session):
    world = await _world(db_session, file_type="lidar", linked=True)
    job = await _admit(db_session, world)
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == [world.dataset.id]
    assert await _scope(db_session, job) == {
        "kind": "datasets",
        "dataset_ids": [str(world.dataset.id)],
    }
    await _assert_blocked(db_session, [world.dataset.id])


async def test_itemless_text_retry_is_dataset_independent_and_ignores_metadata(
    db_session,
):
    world = await _world(db_session, file_type="text", dataset=False)
    job = await _admit(db_session, world)
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == []
    assert await _scope(db_session, job) == {"kind": "datasets", "dataset_ids": []}

    # Production display metadata and cooperative cancellation do not turn the
    # proven dataset-independent execution into an unknown or Dataset scope.
    await db_session.execute(
        text(
            "UPDATE public.async_jobs SET payload = payload || "
            "CAST(:extra AS jsonb) WHERE id=:id"
        ),
        {
            "extra": json.dumps(
                {"cancel_requested": True, "display": {"label": "retry progress"}}
            ),
            "id": job,
        },
    )
    await db_session.refresh(world.task)
    assert (
        await db_session.scalar(
            text(
                "SELECT aap_jobs_v1.retry_born_dataset_scope("
                "id,project_id,user_id,payload) FROM public.async_jobs WHERE id=:id"
            ),
            {"id": job},
        )
        == []
    )
    assert await _scope(db_session, job) == {"kind": "datasets", "dataset_ids": []}


async def test_missing_delivery_execution_and_member_mismatch_roll_back(
    db_session,
):
    first = await _world(db_session, file_type="image")
    job = await _admit(db_session, first)

    with pytest.raises(DBAPIError) as missing_delivery:
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM public.prediction_retry_deliveries WHERE job_id=:id"),
                {"id": job},
            )
            await _force_guard(db_session)
    assert _sqlstate(missing_delivery.value) == "23514"

    with pytest.raises(DBAPIError) as missing_execution:
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM public.batch_result_executions WHERE job_id=:id"),
                {"id": job},
            )
            await _force_guard(db_session)
    assert _sqlstate(missing_execution.value) == "23514"

    with pytest.raises(DBAPIError) as changed_member:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.batch_result_members "
                    "SET write_scope='{}'::jsonb WHERE job_id=:id"
                ),
                {"id": job},
            )
            await _force_guard(db_session)
    assert _sqlstate(changed_member.value) == "23514"


async def test_terminal_delivery_and_completed_writer_member_do_not_change_scope(
    db_session,
):
    world = await _world(db_session, file_type="image")
    job = await _admit(db_session, world)

    # A legitimate writer may already have removed the member in this
    # transaction; a terminal delivery phase is not a source fact.
    await db_session.execute(
        text("DELETE FROM public.batch_result_members WHERE job_id=:id"),
        {"id": job},
    )
    await db_session.execute(
        text(
            "UPDATE public.prediction_retry_deliveries "
            "SET state='terminal', next_attempt_at=pg_catalog.clock_timestamp() "
            "WHERE job_id=:id"
        ),
        {"id": job},
    )
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == [world.dataset.id]
    assert await _scope(db_session, job) == {
        "kind": "datasets",
        "dataset_ids": [str(world.dataset.id)],
    }


async def test_attempt_birth_and_source_are_immutable(db_session):
    world = await _world(db_session, file_type="image")
    job = await _admit(db_session, world)
    await _force_guard(db_session)

    with pytest.raises(DBAPIError, match="attempt birth is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.prediction_retry_attempts "
                    "SET birth_xid = birth_xid + 1 WHERE job_id=:id"
                ),
                {"id": job},
            )

    with pytest.raises(DBAPIError, match="attempt scope is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.prediction_retry_attempts "
                    "SET write_scope = '{}'::jsonb WHERE job_id=:id"
                ),
                {"id": job},
            )

    with pytest.raises(DBAPIError, match="attempt birth is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM public.prediction_retry_attempts WHERE job_id=:id"),
                {"id": job},
            )


async def test_historical_attempt_without_same_transaction_birth_is_not_adopted(
    db_session,
):
    world = await _world(db_session, file_type="image")
    historical_job = uuid.uuid4()
    historical_failed = uuid.uuid4()
    digest = hashlib.sha256(b"historical retry").hexdigest()
    async with db_session.begin_nested() as savepoint:
        # Simulate a pre-migration attempt row: explicitly NULL birth_xid and
        # no same-transaction insert trigger (the production migration adds the
        # column without backfilling).
        await db_session.execute(
            text(
                "ALTER TABLE public.prediction_retry_attempts "
                "DISABLE TRIGGER trg_prediction_retry_attempt_bind_birth"
            )
        )
        await db_session.execute(
            text(
                "INSERT INTO public.prediction_retry_attempts "
                "(job_id,failed_prediction_id,actor_id,project_id,task_id,"
                "source_extra,retry_count_at_admission,write_scope,"
                "request_digest,execution_queue,legacy_delivery,state,birth_xid) "
                "VALUES (:job,:failed,:actor,:project,:task,'{}'::jsonb,0,"
                "CAST(:scope AS jsonb),:digest,'ml.cpu',false,'accepted',NULL)"
            ),
            {
                "job": historical_job,
                "failed": historical_failed,
                "actor": world.actor.id,
                "project": world.project.id,
                "task": world.task.id,
                "scope": json.dumps(
                    {
                        "task_id": str(world.task.id),
                        "project_id": str(world.project.id),
                        "actor_id": str(world.actor.id),
                        "actor_known": True,
                        "operation": "prediction_retry",
                        "execution_scope": None,
                        "execution_scope_known": True,
                        "task_version": world.task.version,
                        "task_status": world.task.status,
                        "assignee_id": None,
                        "review_round_id": None,
                        "source_annotation_id": None,
                        "source_annotation_version": None,
                        "expected_batch_id": None,
                        "expected_batch_id_known": True,
                        "job_id": str(historical_job),
                        "job_id_known": True,
                        "source_binding_known": True,
                        "task_source_generation": max(
                            int(world.task.media_source_generation or 1), 1
                        ),
                        "source_dataset_item_id": str(world.item.id),
                        "source_file_path": world.task.file_path,
                        "source_file_type": world.task.file_type,
                        "accepted_input_id": None,
                    }
                ),
                "digest": digest,
            },
        )
        await db_session.execute(
            text(
                "ALTER TABLE public.prediction_retry_attempts "
                "ENABLE TRIGGER trg_prediction_retry_attempt_bind_birth"
            )
        )
        await db_session.execute(
            text(
                "INSERT INTO public.async_jobs "
                "(id,kind,project_id,user_id,status,progress_pct,payload,result) "
                "VALUES (:id,'prediction_retry',:project,:actor,'pending',0,"
                "CAST(:payload AS jsonb),CAST('{}'::jsonb))"
            ),
            {
                "id": historical_job,
                "project": world.project.id,
                "actor": world.actor.id,
                "payload": json.dumps(
                    {
                        "failed_prediction_id": str(historical_failed),
                        "task_id": str(world.task.id),
                        "prediction_retry_request_digest": digest,
                        "execution_queue": "ml.cpu",
                    }
                ),
            },
        )
        with pytest.raises(DBAPIError) as not_current:
            async with db_session.begin_nested():
                await _force_guard(db_session)
        assert _sqlstate(not_current.value) == "23514"
        await savepoint.rollback()


async def test_birth_xid_supplied_by_ordinary_input_is_overwritten(db_session):
    world = await _world(db_session, file_type="image")
    attempt_job = uuid.uuid4()
    digest = hashlib.sha256(b"supplied birth xid").hexdigest()
    await db_session.execute(
        text(
            "INSERT INTO public.prediction_retry_attempts "
            "(job_id,failed_prediction_id,actor_id,project_id,task_id,"
            "source_extra,retry_count_at_admission,write_scope,"
            "request_digest,execution_queue,legacy_delivery,state,birth_xid) "
            "VALUES (:job,:failed,:actor,:project,:task,'{}'::jsonb,0,NULL,"
            ":digest,'ml.cpu',false,'accepted',-1)"
        ),
        {
            "job": attempt_job,
            "failed": uuid.uuid4(),
            "actor": world.actor.id,
            "project": world.project.id,
            "task": world.task.id,
            "digest": digest,
        },
    )
    assert await db_session.scalar(
        text("SELECT birth_xid FROM public.prediction_retry_attempts WHERE job_id=:id"),
        {"id": attempt_job},
    ) == await db_session.scalar(text("SELECT pg_catalog.txid_current()"))


async def test_second_admission_does_not_reinsert_or_rebind_an_attempt(db_session):
    world = await _world(db_session, file_type="image")
    job = await _admit(db_session, world)
    first_birth = await db_session.scalar(
        text("SELECT birth_xid FROM public.prediction_retry_attempts WHERE job_id=:id"),
        {"id": job},
    )
    second = await db_session.scalar(
        ADMIT,
        {
            "payload": json.dumps(
                {
                    "job_id": str(job),
                    "failed_prediction_id": str(world.failed_id),
                    "actor_id": str(world.actor.id),
                    "execution_queue": "ml.cpu",
                    "legacy_delivery": False,
                }
            )
        },
    )
    assert second is not None and second.get("ok") is True, second
    assert second.get("data", {}).get("created") is False
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.prediction_retry_attempts WHERE job_id=:id"
            ),
            {"id": job},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT birth_xid FROM public.prediction_retry_attempts "
                "WHERE job_id=:id"
            ),
            {"id": job},
        )
        == first_birth
    )
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == [world.dataset.id]


async def test_retry_snapshot_and_same_source_authority_are_accepted(db_session):
    world = await _world(db_session, file_type="image")
    job = await _admit(db_session, world)
    snapshot = await _snapshot(db_session, world.project.id)
    await _bind_snapshot(
        db_session,
        job,
        snapshot,
        authority={"prediction_authority": _authority(world, snapshot)},
    )
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == [world.dataset.id]
    assert await _scope(db_session, job) == {
        "kind": "datasets",
        "dataset_ids": [str(world.dataset.id)],
    }

    with pytest.raises(DBAPIError, match="authority is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.async_jobs SET payload = payload || "
                    "CAST(:authority AS jsonb) WHERE id=:id"
                ),
                {
                    "authority": json.dumps(
                        {"prediction_authority": _authority(world, snapshot)}
                    ),
                    "id": job,
                },
            )

    other_snapshot = await _snapshot(db_session, world.project.id)
    with pytest.raises(DBAPIError, match="snapshot is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.async_jobs SET execution_snapshot_id=:snapshot "
                    "WHERE id=:id"
                ),
                {"snapshot": other_snapshot.id, "id": job},
            )


async def test_retry_video_authority_preserves_video_source(db_session):
    world = await _world(db_session, file_type="video")
    job = await _admit(db_session, world)
    snapshot = await _snapshot(db_session, world.project.id, unit="video")
    await _bind_snapshot(
        db_session,
        job,
        snapshot,
        authority={
            "video_prediction_authority": _authority(world, snapshot, video=True)
        },
    )
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == [world.dataset.id]
    assert await _scope(db_session, job) == {
        "kind": "datasets",
        "dataset_ids": [str(world.dataset.id)],
    }


async def test_retry_authority_cannot_change_the_born_dataset(db_session):
    world = await _world(db_session, file_type="image")
    other = await _world(
        db_session,
        file_type="image",
        project=world.project,
        actor=world.actor,
    )
    job = await _admit(db_session, world)
    snapshot = await _snapshot(db_session, world.project.id)
    await _bind_snapshot(db_session, job, snapshot)
    widened = _authority(world, snapshot, dataset_id=other.dataset.id)
    with pytest.raises(DBAPIError, match="authority changed the Task source"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.async_jobs SET payload = payload || "
                    "CAST(:authority AS jsonb) WHERE id=:id"
                ),
                {
                    "authority": json.dumps({"prediction_authority": widened}),
                    "id": job,
                },
            )


async def test_itemless_text_accepts_snapshot_but_never_authority(db_session):
    world = await _world(db_session, file_type="text", dataset=False)
    job = await _admit(db_session, world)
    snapshot = await _snapshot(db_session, world.project.id, unit="image")
    await _bind_snapshot(db_session, job, snapshot)
    await _force_guard(db_session)
    assert await _receipt(db_session, job) == []
    assert await _scope(db_session, job) == {"kind": "datasets", "dataset_ids": []}

    with pytest.raises(DBAPIError, match="authority changed the Task source"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.async_jobs SET payload = payload || "
                    "CAST(:authority AS jsonb) WHERE id=:id"
                ),
                {
                    "authority": json.dumps(
                        {
                            "prediction_authority": {
                                "schema": 2,
                                "job_kind": "prediction_retry",
                                "actor_id": str(world.actor.id),
                                "project_id": str(world.project.id),
                                "batch_id": None,
                                "execution_scope": "workbench",
                                "execution_unit": "image",
                                "predict_mode": "append",
                                "snapshot_id": str(snapshot.id),
                                "snapshot_digest": snapshot.digest,
                                "tasks": [],
                            }
                        }
                    ),
                    "id": job,
                },
            )


async def test_snapshot_outside_the_job_project_is_rejected(db_session):
    world = await _world(db_session, file_type="image")
    other_project = await create_project(
        db_session,
        owner_id=world.actor.id,
        name="Retry other project",
    )
    job = await _admit(db_session, world)
    foreign_snapshot = await _snapshot(db_session, other_project.id)
    with pytest.raises(DBAPIError, match="authority changed the Task source"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE public.async_jobs SET execution_snapshot_id=:snapshot "
                    "WHERE id=:id"
                ),
                {"snapshot": foreign_snapshot.id, "id": job},
            )


async def test_marked_but_unbound_attempt_keeps_maintenance_delete(db_session):
    world = await _world(db_session, file_type="image")
    attempt_job = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO public.prediction_retry_attempts "
            "(job_id,failed_prediction_id,actor_id,project_id,task_id,"
            "source_extra,retry_count_at_admission,write_scope,"
            "request_digest,execution_queue,legacy_delivery,state) "
            "VALUES (:job,:failed,:actor,:project,:task,'{}'::jsonb,0,NULL,"
            ":digest,'ml.cpu',false,'accepted')"
        ),
        {
            "job": attempt_job,
            "failed": uuid.uuid4(),
            "actor": world.actor.id,
            "project": world.project.id,
            "task": world.task.id,
            "digest": hashlib.sha256(b"unbound retry").hexdigest(),
        },
    )
    await db_session.execute(
        text("DELETE FROM public.prediction_retry_attempts WHERE job_id=:id"),
        {"id": attempt_job},
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.prediction_retry_attempts WHERE job_id=:id"
            ),
            {"id": attempt_job},
        )
        == 0
    )
