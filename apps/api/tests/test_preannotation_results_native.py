"""Database regressions for restartable ordinary preannotation snapshots."""

from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction, PredictionMeta
from app.services.labeling_write_scope import capture_labeling_write_scope
from tests.factory import create_project, create_task

pytestmark = pytest.mark.asyncio

SCHEMA = "aap-preannotation-results/v1"


async def _native(db, schema: str, operation: str, payload: dict):
    result = await db.execute(
        text(f"SELECT {schema}.{operation}(CAST(:payload AS jsonb))"),
        {"payload": json.dumps(payload, separators=(",", ":"), default=str)},
    )
    reply = result.scalar_one()
    return json.loads(reply) if isinstance(reply, str) else reply


def _assert_ok(reply: dict) -> dict:
    assert reply["schema"] == SCHEMA, reply
    assert reply["ok"] is True, reply
    return reply["data"]


def _scope_json(scope) -> dict:
    return {
        key: str(value) if isinstance(value, uuid.UUID) else value
        for key, value in asdict(scope).items()
    }


def _lease_scope(lease: dict) -> dict:
    return {
        "job_id": lease["job_id"],
        "actor_id": lease["actor_id"],
        "project_id": lease["project_id"],
        "request_digest": lease["request_digest"],
        "generation": lease["generation"],
        "lease_token": lease["lease_token"],
    }


def _bundle_digest(
    job_id: str, ordinal: int, request_digest: str, stage: str, bodies: list[bytes]
):
    head = (
        SCHEMA.encode()
        + b"\x00"
        + stage.encode()
        + b"\x00"
        + uuid.UUID(job_id).bytes
        + ordinal.to_bytes(8, "big", signed=True)
        + len(bodies).to_bytes(8, "big", signed=True)
        + bytes.fromhex(request_digest)
    )
    state = hashlib.sha256(head).digest()
    digests = [hashlib.sha256(body).digest() for body in bodies]
    for index, digest in enumerate(digests):
        state = hashlib.sha256(
            state + index.to_bytes(8, "big", signed=True) + digest
        ).digest()
    return state.hex(), [digest.hex() for digest in digests]


def _request_body(
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    write_scope: dict,
    result: list[dict],
    pipeline_extra=None,
):
    return {
        "task_id": str(task_id),
        "project_id": str(project_id),
        "ml_backend_id": None,
        "ml_backend_pool_id": None,
        "model_version": None,
        "score": None,
        "result": result,
        "write_scope": write_scope,
        "inference_time_ms": None,
        "token_meta": None,
        "source": "ml_backend",
        "pipeline_extra": pipeline_extra,
        "model_provenance": None,
        "accepted_input_job_id": None,
        "accepted_video_input": None,
    }


async def _world(db, actor, *, member_count: int = 1, predict_mode: str = "append"):
    project = await create_project(db, owner_id=actor.id)
    tasks = [await create_task(db, project_id=project.id) for _ in range(member_count)]
    job_id = uuid.uuid4()
    request_digest = "a" * 64
    db.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor.id,
            status="pending",
            celery_task_id=str(job_id),
            payload={
                "execution_scope": "bulk",
                "predict_mode": predict_mode,
                "cancel_requested": False,
            },
            result={},
        )
    )
    await db.flush()
    base_scope = {
        "job_id": str(job_id),
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "request_digest": request_digest,
    }
    claimed = _assert_ok(
        await _native(
            db,
            "aap_jobs_v1",
            "claim_batch_result_execution",
            base_scope
            | {
                "worker_id": str(uuid.uuid4()),
                "execution_id": None,
                "lease_seconds": 120,
            },
        )
    )
    lease = claimed["lease"]
    lease_scope = _lease_scope(lease)
    members = []
    scopes = []
    for index, task in enumerate(tasks, start=1):
        write_scope = _scope_json(
            capture_labeling_write_scope(
                task,
                actor_id=actor.id,
                actor_known=True,
                operation="preannotation",
                execution_scope="bulk",
                execution_scope_known=True,
                job_id=job_id,
                job_id_known=True,
            )
        )
        scopes.append(write_scope)
        members.append(
            {
                "ordinal": index,
                "task_id": str(task.id),
                "write_scope": write_scope,
                "manifest_member": None,
            }
        )
    staged = None
    for after in range(0, len(members), 100):
        page = members[after : after + 100]
        complete = after + len(page) == len(members) and len(page) < 100
        staged = _assert_ok(
            await _native(
                db,
                "aap_jobs_v1",
                "stage_batch_result_members",
                lease_scope
                | {
                    "after_ordinal": after,
                    "members": page,
                    "complete": complete,
                },
            )
        )
    if len(members) % 100 == 0:
        staged = _assert_ok(
            await _native(
                db,
                "aap_jobs_v1",
                "stage_batch_result_members",
                lease_scope
                | {
                    "after_ordinal": len(members),
                    "members": [],
                    "complete": True,
                },
            )
        )
    assert staged is not None
    assert staged["progress"]["selection_cursor"] == member_count
    started = _assert_ok(
        await _native(db, "aap_jobs_v1", "start_batch_result_execution", lease_scope)
    )
    assert started["progress"]["next_ordinal"] == 1
    return project, tasks, job_id, base_scope, lease_scope, scopes


async def _start_member(db, lease_scope, ordinal: int):
    return _assert_ok(
        await _native(
            db,
            "aap_jobs_v1",
            "start_batch_result_member",
            lease_scope | {"ordinal": ordinal},
        )
    )


async def _begin_and_put_raw(
    db, job_id, request_digest, lease_scope, task, write_scope, bodies
):
    bundle_digest, hashes = _bundle_digest(
        str(job_id), 1, request_digest, "raw", bodies
    )
    context = {
        "pipeline_stages": {},
        "gpu_arbiter_failures": [],
        "total_cost": "0",
    }
    opened = _assert_ok(
        await _native(
            db,
            "aap_labeling_v1",
            "begin_batch_result",
            lease_scope
            | {
                "ordinal": 1,
                "task_id": str(task.id),
                "result_count": len(bodies),
                "bundle_digest": bundle_digest,
                "stage": "raw",
                "context": context,
                "restart_incomplete": False,
            },
        )
    )
    for request_index, (body, body_hash) in enumerate(zip(bodies, hashes, strict=True)):
        parts = [
            body[offset : offset + 512 * 1024]
            for offset in range(0, len(body), 512 * 1024)
        ]
        for part_index, part in enumerate(parts):
            reply = await _native(
                db,
                "aap_labeling_v1",
                "put_batch_result_part",
                lease_scope
                | {
                    "ordinal": 1,
                    "stage": "raw",
                    "request_index": request_index,
                    "sha256": body_hash,
                    "byte_size": len(body),
                    "part_count": len(parts),
                    "part_index": part_index,
                    "content": base64.b64encode(part).decode("ascii"),
                    "tool_unit_id": None,
                },
            )
            data = _assert_ok(reply)
    assert data["result"]["result_id"] == opened["result"]["result_id"]
    return data["result"]


async def test_selection_replay_lease_takeover_and_cooperative_cancel(
    db_session, super_admin
):
    actor, _ = super_admin
    await db_session.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
    project = await create_project(db_session, owner_id=actor.id)
    tasks = [await create_task(db_session, project_id=project.id) for _ in range(2)]
    job_id = uuid.uuid4()
    digest = "b" * 64
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor.id,
            status="pending",
            payload={
                "execution_scope": "bulk",
                "predict_mode": "append",
                "cancel_requested": False,
            },
            result={},
        )
    )
    await db_session.flush()
    scope = {
        "job_id": str(job_id),
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "request_digest": digest,
    }
    first = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "claim_batch_result_execution",
            scope
            | {
                "worker_id": str(uuid.uuid4()),
                "execution_id": None,
                "lease_seconds": 30,
            },
        )
    )
    lease_expiry = datetime.fromisoformat(
        first["lease"]["lease_expires_at"].replace("Z", "+00:00")
    )
    assert lease_expiry.utcoffset() is not None
    assert lease_expiry.utcoffset().total_seconds() == 0
    with pytest.raises(DBAPIError) as active_delete:
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM public.async_jobs WHERE id=:job"), {"job": job_id}
            )
    assert getattr(active_delete.value.orig, "sqlstate", None) == "P0001"
    old_lease = _lease_scope(first["lease"])
    assert (
        await db_session.scalar(
            text(
                "SELECT payload ->> 'preannotation_request_digest' FROM async_jobs WHERE id=:job"
            ),
            {"job": job_id},
        )
        == digest
    )
    member_values = []
    for ordinal, task in enumerate(tasks, start=1):
        write_scope = _scope_json(
            capture_labeling_write_scope(
                task,
                actor_id=actor.id,
                actor_known=True,
                operation="preannotation",
                execution_scope="bulk",
                execution_scope_known=True,
                job_id=job_id,
                job_id_known=True,
            )
        )
        member_values.append(
            {
                "ordinal": ordinal,
                "task_id": str(task.id),
                "write_scope": write_scope,
                "manifest_member": None,
            }
        )
    page = old_lease | {"after_ordinal": 0, "members": member_values, "complete": True}
    accepted = _assert_ok(
        await _native(db_session, "aap_jobs_v1", "stage_batch_result_members", page)
    )
    replay = _assert_ok(
        await _native(db_session, "aap_jobs_v1", "stage_batch_result_members", page)
    )
    assert replay["progress"] == accepted["progress"]

    await db_session.execute(
        text(
            "UPDATE public.batch_result_executions SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
        ),
        {"job": job_id},
    )
    expired_replay = _assert_ok(
        await _native(db_session, "aap_jobs_v1", "stage_batch_result_members", page)
    )
    assert expired_replay["progress"] == accepted["progress"]
    second = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "claim_batch_result_execution",
            scope
            | {
                "worker_id": str(uuid.uuid4()),
                "execution_id": None,
                "lease_seconds": 30,
            },
        )
    )
    new_lease = _lease_scope(second["lease"])
    assert new_lease["generation"] == old_lease["generation"] + 1
    stale = await _native(
        db_session,
        "aap_jobs_v1",
        "heartbeat_batch_result_execution",
        old_lease | {"lease_seconds": 30},
    )
    assert stale["ok"] is False and stale["error"]["code"] == "lease_lost"

    _assert_ok(
        await _native(
            db_session, "aap_jobs_v1", "start_batch_result_execution", new_lease
        )
    )
    started_member = await _start_member(db_session, new_lease, 1)
    assert started_member["member"]["state"] == "prepared"
    await db_session.execute(
        text(
            "UPDATE public.async_jobs SET payload=payload || jsonb_build_object('cancel_requested',true) WHERE id=:job"
        ),
        {"job": job_id},
    )
    current_replay = await _start_member(db_session, new_lease, 1)
    assert current_replay["member"]["state"] == "prepared"
    skipped = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "skip_batch_result_member",
            new_lease | {"ordinal": 1, "reason": "cancelled_before_capture"},
        )
    )
    assert skipped["progress"]["next_ordinal"] == 2
    blocked = await _native(
        db_session,
        "aap_jobs_v1",
        "start_batch_result_member",
        new_lease | {"ordinal": 2},
    )
    assert blocked["ok"] is False and blocked["error"]["code"] == "job_not_writable"
    finished = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "finish_batch_result_execution",
            new_lease
            | {
                "status": "cancelled",
                "error_code": None,
                "result_extra": {},
            },
        )
    )
    assert finished["progress"]["state"] == "cancelled"
    assert finished["progress"]["skipped_count"] == 1


async def test_empty_selection_is_a_valid_completed_execution(db_session, super_admin):
    actor, _ = super_admin
    _, _, _, _, lease_scope, _ = await _world(db_session, actor, member_count=0)
    started = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "read_batch_result_execution",
            {
                "job_id": lease_scope["job_id"],
                "actor_id": lease_scope["actor_id"],
                "project_id": lease_scope["project_id"],
                "request_digest": lease_scope["request_digest"],
                "after_ordinal": 0,
                "limit": 100,
            },
        )
    )
    assert started["progress"]["selection_complete"] is True
    assert started["progress"]["total"] == 0
    assert started["progress"]["next_ordinal"] == 1
    finished = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "finish_batch_result_execution",
            lease_scope
            | {"status": "completed", "error_code": None, "result_extra": {}},
        )
    )
    assert finished["progress"]["state"] == "completed"


async def test_hundred_member_selection_accepts_empty_final_page(
    db_session, super_admin
):
    actor, _ = super_admin
    _, _, _, _, lease_scope, _ = await _world(db_session, actor, member_count=100)
    read = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "read_batch_result_execution",
            {
                "job_id": lease_scope["job_id"],
                "actor_id": lease_scope["actor_id"],
                "project_id": lease_scope["project_id"],
                "request_digest": lease_scope["request_digest"],
                "after_ordinal": 0,
                "limit": 100,
            },
        )
    )
    assert read["progress"]["selection_complete"] is True
    assert read["progress"]["selection_cursor"] == 100
    assert read["progress"]["total"] == 100
    assert len(read["members"]) == 100


async def test_setup_failure_can_finish_before_selection_is_complete(
    db_session, super_admin
):
    actor, _ = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    job_id = uuid.uuid4()
    request_digest = "c" * 64
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor.id,
            status="pending",
            celery_task_id=str(job_id),
            payload={
                "execution_scope": "bulk",
                "predict_mode": "append",
                "cancel_requested": False,
            },
            result={},
        )
    )
    await db_session.flush()
    scope = {
        "job_id": str(job_id),
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "request_digest": request_digest,
    }
    claimed = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "claim_batch_result_execution",
            scope
            | {
                "worker_id": str(uuid.uuid4()),
                "execution_id": None,
                "lease_seconds": 120,
            },
        )
    )
    failed = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "finish_batch_result_execution",
            _lease_scope(claimed["lease"])
            | {"status": "failed", "error_code": "ValueError", "result_extra": {}},
        )
    )
    assert failed["progress"]["state"] == "failed"
    assert failed["progress"]["selection_complete"] is False
    assert failed["progress"]["total"] is None


async def test_missing_task_manifest_member_can_be_frozen_and_failed(
    db_session, super_admin
):
    actor, _ = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    job_id = uuid.uuid4()
    task_id = uuid.uuid4()
    request_digest = "f" * 64
    selection_digest = "a" * 64
    sealed_digest = "b" * 64
    input_id = uuid.uuid4()
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor.id,
            status="pending",
            celery_task_id=str(job_id),
            payload={
                "execution_scope": "bulk",
                "predict_mode": "append",
                "cancel_requested": False,
                "accepted_input_manifest_protocol": "aap-assets-input-manifest/v1",
                "accepted_input_manifest_id": str(job_id),
                "accepted_input_manifest_digest": selection_digest,
                "accepted_input_manifest_sealed_digest": sealed_digest,
            },
            result={},
        )
    )
    await db_session.flush()
    scope = {
        "job_id": str(job_id),
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "request_digest": request_digest,
    }
    claimed = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "claim_batch_result_execution",
            scope
            | {
                "worker_id": str(uuid.uuid4()),
                "execution_id": None,
                "lease_seconds": 120,
            },
        )
    )
    lease_scope = _lease_scope(claimed["lease"])
    member = {
        "ordinal": 1,
        "task_id": str(task_id),
        "write_scope": None,
        "manifest_member": {
            "manifest_id": str(job_id),
            "selection_digest": selection_digest,
            "sealed_digest": sealed_digest,
            "ordinal": 1,
            "task_id": str(task_id),
            "project_id": str(project.id),
            "task_version": 1,
            "task_source_generation": 1,
            "dataset_item_id": None,
            "task_source_snapshot": "c" * 64,
            "file_path": "images/missing.png",
            "file_type": "image",
            "source": None,
            "state": "ready",
            "input": {
                "input_id": str(input_id),
                "job_id": str(job_id),
                "actor_id": str(actor.id),
                "project_id": str(project.id),
                "task_id": str(task_id),
                "status": "ready",
                "task_version": 1,
                "task_source_generation": 1,
                "dataset_item_id": None,
                "task_source_snapshot": "c" * 64,
                "byte_size": 128,
                "sha256": "d" * 64,
            },
            "error": None,
        },
    }
    _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "stage_batch_result_members",
            lease_scope
            | {
                "after_ordinal": 0,
                "members": [member],
                "complete": True,
            },
        )
    )
    _assert_ok(
        await _native(
            db_session, "aap_jobs_v1", "start_batch_result_execution", lease_scope
        )
    )
    failed_member = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "finish_batch_result_failure",
            lease_scope
            | {
                "ordinal": 1,
                "failed_prediction_id": None,
                "error_code": "task_missing",
                "context": {},
            },
        )
    )
    assert failed_member["progress"]["failed_count"] == 1
    finished = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "finish_batch_result_execution",
            lease_scope
            | {"status": "failed", "error_code": "task_missing", "result_extra": {}},
        )
    )
    assert finished["progress"]["state"] == "failed"


async def test_complete_raw_bytes_survive_mask_root_fence_at_seal(
    db_session, super_admin
):
    actor, _ = super_admin
    _, tasks, job_id, _, lease_scope, write_scopes = await _world(db_session, actor)
    await _start_member(db_session, lease_scope, 1)
    task = tasks[0]
    body = _request_body(
        task_id=task.id,
        project_id=task.project_id,
        write_scope=write_scopes[0],
        result=[{"geometry": {"type": "raster_mask", "object_key": ""}}],
    )
    key_digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    key = f"raster-masks/sha256/{key_digest[:2]}/{key_digest[2:4]}/{key_digest}.json"
    body["result"][0]["geometry"]["object_key"] = key
    body_bytes = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    raw_digest, _ = _bundle_digest(str(job_id), 1, "a" * 64, "raw", [body_bytes])
    _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "begin_batch_result",
            lease_scope
            | {
                "ordinal": 1,
                "task_id": str(task.id),
                "result_count": 1,
                "bundle_digest": raw_digest,
                "stage": "raw",
                "context": {
                    "pipeline_stages": {},
                    "gpu_arbiter_failures": [],
                    "total_cost": "0",
                },
                "restart_incomplete": False,
            },
        )
    )
    operation_id = uuid.uuid4()
    sweep_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO public.mask_gc_intents(operation_id,object_key,etag,last_modified,byte_size,epoch,sweep_id,grace_cutoff,state,outcome,next_check_at) "
            "VALUES (:operation,:key,'gc-etag',clock_timestamp(),100,1,:sweep,clock_timestamp(),'pending',NULL,clock_timestamp())"
        ),
        {"operation": operation_id, "key": key, "sweep": sweep_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.mask_gc_key_epochs(object_key,epoch,active_operation_id) "
            "VALUES (:key,1,:operation)"
        ),
        {"key": key, "operation": operation_id},
    )
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    put = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "put_batch_result_part",
            lease_scope
            | {
                "ordinal": 1,
                "stage": "raw",
                "request_index": 0,
                "sha256": body_hash,
                "byte_size": len(body_bytes),
                "part_count": 1,
                "part_index": 0,
                "content": base64.b64encode(body_bytes).decode("ascii"),
                "tool_unit_id": None,
            },
        )
    )
    assert put["result"]["raw_complete"] is True
    assert put["payload"]["complete"] is True
    assert put["payload"]["prediction_id"]
    sealed = await _native(
        db_session,
        "aap_labeling_v1",
        "seal_batch_result",
        lease_scope | {"ordinal": 1, "stage": "raw"},
    )
    assert sealed["ok"] is False and sealed["error"]["code"] == "content_deleting"
    snapshot = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "read_batch_result",
            {
                "job_id": str(job_id),
                "actor_id": str(actor.id),
                "project_id": str(task.project_id),
                "request_digest": "a" * 64,
                "ordinal": 1,
                "stage": "raw",
                "request_index": 0,
            },
        )
    )
    assert snapshot["result"]["raw_complete"] is True
    assert snapshot["payload"]["complete"] is True
    assert snapshot["payload"]["receipt"] is None
    assert snapshot["content"] is None
    assert snapshot["payload"]["payload_id"] == put["payload"]["payload_id"]

    await db_session.execute(
        text(
            "UPDATE public.mask_gc_key_epochs SET epoch=0,active_operation_id=NULL "
            "WHERE object_key=:key"
        ),
        {"key": key},
    )
    captured = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "seal_batch_result",
            lease_scope | {"ordinal": 1, "stage": "raw"},
        )
    )
    assert captured["result"]["state"] == "captured"
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.mask_gc_roots WHERE object_key=:key"),
            {"key": key},
        )
        == 1
    )

    failed = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "finish_batch_result_execution",
            lease_scope
            | {
                "status": "failed",
                "error_code": "ValueError",
                "result_extra": {},
            },
        )
    )
    assert failed["progress"]["state"] == "failed"
    abandoned = await db_session.execute(
        text(
            "SELECT state,raw_complete FROM public.batch_result_bundles "
            "WHERE job_id=:job AND ordinal=1"
        ),
        {"job": job_id},
    )
    assert abandoned.one() == ("abandoned", True)
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.batch_result_payloads WHERE job_id=:job"),
            {"job": job_id},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.batch_result_parts WHERE payload_id=:payload"
            ),
            {"payload": put["payload"]["payload_id"]},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.mask_gc_roots WHERE object_key=:key"),
            {"key": key},
        )
        == 0
    )


async def test_per_request_chunking_has_no_sixteen_mebibyte_task_bundle_cap(
    db_session, super_admin
):
    actor, _ = super_admin
    _, tasks, job_id, _, lease_scope, write_scopes = await _world(db_session, actor)
    await _start_member(db_session, lease_scope, 1)
    bodies = []
    for index in range(9):
        body = _request_body(
            task_id=tasks[0].id,
            project_id=tasks[0].project_id,
            write_scope=write_scopes[0],
            result=[{"type": "rectangle", "index": index}],
            pipeline_extra={
                "padding": "x" * (16 * 1024 * 1024 + 1024 if index == 0 else 1_000)
            },
        )
        body_bytes = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        bodies.append(body_bytes)
    result = await _begin_and_put_raw(
        db_session, job_id, "a" * 64, lease_scope, tasks[0], write_scopes[0], bodies
    )
    assert sum(map(len, bodies)) > 16 * 1024 * 1024
    assert len(bodies[0]) > 16 * 1024 * 1024
    assert result["raw_complete"] is True
    assert result["result_count"] == len(bodies)
    stored = await db_session.execute(
        text(
            "SELECT count(*) AS payload_count, sum(byte_size) AS total_bytes, "
            "bool_and(complete) AS all_complete "
            "FROM public.batch_result_payloads WHERE result_id=:result AND stage='raw'"
        ),
        {"result": result["result_id"]},
    )
    payload_count, total_bytes, all_complete = stored.one()
    assert payload_count == 9
    assert total_bytes == sum(map(len, bodies))
    assert all_complete is True


async def test_final_writer_proves_multi_result_prediction_rows_and_receipts(
    db_session, super_admin
):
    actor, _ = super_admin
    await db_session.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
    _, tasks, job_id, base_scope, lease_scope, write_scopes = await _world(
        db_session, actor
    )
    await _start_member(db_session, lease_scope, 1)
    task = tasks[0]
    raw_bodies = []
    for index in range(2):
        body = _request_body(
            task_id=task.id,
            project_id=task.project_id,
            write_scope=write_scopes[0],
            result=[{"type": "rectangle", "index": index}],
            pipeline_extra={"accepted_input": {"owner_kind": "task_original"}},
        )
        body["model_provenance"] = {"model_id": str(uuid.uuid4())} if index == 0 else {}
        body["token_meta"] = {
            "inference_runtime": {"provider": "libreyolo"},
            "segmentation": {"format": "polygons"},
        }
        raw_bodies.append(
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        )
    raw_digest, _ = _bundle_digest(str(job_id), 1, "a" * 64, "raw", raw_bodies)
    context = {"pipeline_stages": {}, "gpu_arbiter_failures": [], "total_cost": "0"}
    begun = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "begin_batch_result",
            lease_scope
            | {
                "ordinal": 1,
                "task_id": str(task.id),
                "result_count": 2,
                "bundle_digest": raw_digest,
                "stage": "raw",
                "context": context,
                "restart_incomplete": False,
            },
        )
    )
    raw_meta = []
    for index, body_bytes in enumerate(raw_bodies):
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        put = _assert_ok(
            await _native(
                db_session,
                "aap_labeling_v1",
                "put_batch_result_part",
                lease_scope
                | {
                    "ordinal": 1,
                    "stage": "raw",
                    "request_index": index,
                    "sha256": body_hash,
                    "byte_size": len(body_bytes),
                    "part_count": 1,
                    "part_index": 0,
                    "content": base64.b64encode(body_bytes).decode("ascii"),
                    "tool_unit_id": None,
                },
            )
        )
        raw_meta.append(put["payload"])
    raw_sealed = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "seal_batch_result",
            lease_scope | {"ordinal": 1, "stage": "raw"},
        )
    )
    assert raw_sealed["result"]["state"] == "captured"
    from app.application.preannotation_results import restore_write_scope
    from app.services.batch_result_masks import normalize_batch_prediction_masks
    from app.services.labeling_prediction_contract import (
        ApplyPredictionResultRequest,
        BatchResultPredictionIdentity,
    )
    from app.services.prediction import PredictionService

    prepared_bodies = []
    for body_bytes in raw_bodies:
        body = json.loads(body_bytes)
        (normalized,) = await normalize_batch_prediction_masks(
            db_session,
            (ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=task.project_id,
                ml_backend_id=None,
                result=body["result"],
                write_scope=restore_write_scope(body["write_scope"]),
                pipeline_extra=body["pipeline_extra"],
                model_provenance=body["model_provenance"],
                token_meta=body["token_meta"],
            ),),
        )
        body["result"] = normalized.result
        prepared_bodies.append(
            json.dumps(
                body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
        )
    prepared_digest, prepared_hashes = _bundle_digest(
        str(job_id), 1, "a" * 64, "prepared", prepared_bodies
    )
    _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "begin_batch_result",
            lease_scope
            | {
                "ordinal": 1,
                "task_id": str(task.id),
                "result_count": 2,
                "bundle_digest": prepared_digest,
                "stage": "prepared",
                "context": context,
                "restart_incomplete": False,
            },
        )
    )
    for index, (body_bytes, body_hash, source) in enumerate(
        zip(prepared_bodies, prepared_hashes, raw_meta, strict=True)
    ):
        put = _assert_ok(
            await _native(
                db_session,
                "aap_labeling_v1",
                "put_batch_result_part",
                lease_scope
                | {
                    "ordinal": 1,
                    "stage": "prepared",
                    "request_index": index,
                    "sha256": body_hash,
                    "byte_size": len(body_bytes),
                    "part_count": 1,
                    "part_index": 0,
                    "content": base64.b64encode(body_bytes).decode("ascii"),
                    "tool_unit_id": "bbox",
                },
            )
        )
        assert put["payload"]["prediction_id"] == source["prediction_id"]
        assert (
            put["payload"]["prediction_created_at"] == source["prediction_created_at"]
        )
    ready = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "seal_batch_result",
            lease_scope | {"ordinal": 1, "stage": "prepared"},
        )
    )
    assert ready["result"]["state"] == "ready"
    prepared_digest = ready["result"]["prepared_digest"]
    _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "lock_batch_result_write",
            lease_scope
            | {
                "ordinal": 1,
                "result_id": begun["result"]["result_id"],
                "prepared_digest": prepared_digest,
            },
        )
    )

    async def insert_predictions(*, wrong_first=False):
        for index, metadata in enumerate(raw_meta):
            body = json.loads(prepared_bodies[index])
            created_at = datetime.fromisoformat(
                metadata["prediction_created_at"].replace("Z", "+00:00")
            )
            result = body["result"]
            db_session.add(
                Prediction(
                    id=uuid.UUID(metadata["prediction_id"]),
                    created_at=created_at,
                    task_id=task.id,
                    project_id=task.project_id,
                    ml_backend_id=None,
                    ml_backend_pool_id=None,
                    model_version=None,
                    score=None,
                    tool_unit_id="polygon" if wrong_first and index == 0 else "bbox",
                    result=result,
                    source="ml_backend",
                )
            )
            db_session.add(
                PredictionMeta(
                    id=uuid.uuid4(),
                    prediction_id=uuid.UUID(metadata["prediction_id"]),
                    prediction_created_at=created_at,
                    inference_time_ms=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    prompt_cost=None,
                    completion_cost=None,
                    total_cost=None,
                    extra={},
                )
            )
        await db_session.flush()

    with pytest.raises(DBAPIError) as rejected:
        async with db_session.begin_nested():
            await insert_predictions(wrong_first=True)
            await _native(
                db_session,
                "aap_labeling_v1",
                "finish_batch_result_write",
                lease_scope
                | {
                    "ordinal": 1,
                    "result_id": begun["result"]["result_id"],
                    "prepared_digest": prepared_digest,
                },
            )
    assert getattr(rejected.value.orig, "sqlstate", None) == "P0001"
    unchanged = await db_session.scalar(
        text("SELECT state FROM public.batch_result_bundles WHERE result_id=:result"),
        {"result": begun["result"]["result_id"]},
    )
    assert unchanged == "ready"
    assert (
        await db_session.scalar(
            text(
                "SELECT next_ordinal FROM public.batch_result_executions WHERE job_id=:job"
            ),
            {"job": job_id},
        )
        == 1
    )


    writer = PredictionService(db_session)
    for body_bytes, metadata in zip(prepared_bodies, raw_meta, strict=True):
        body = json.loads(body_bytes)
        request = ApplyPredictionResultRequest(
            task_id=task.id,
            project_id=task.project_id,
            ml_backend_id=None,
            result=body["result"],
            write_scope=restore_write_scope(body["write_scope"]),
            pipeline_extra=body["pipeline_extra"],
            model_provenance=body["model_provenance"],
            token_meta=body["token_meta"],
            batch_result_identity=BatchResultPredictionIdentity(
                uuid.UUID(metadata["prediction_id"]),
                datetime.fromisoformat(metadata["prediction_created_at"].replace("Z", "+00:00")),
            ),
        )
        await writer._persist_prediction_result(request, task, request.result)

    with pytest.raises(DBAPIError) as rejected_metadata:
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE public.prediction_metas SET extra = extra - 'inference_runtime' "
                     "WHERE prediction_id = :prediction_id"),
                {"prediction_id": uuid.UUID(raw_meta[0]["prediction_id"])},
            )
            await _native(
                db_session, "aap_labeling_v1", "finish_batch_result_write",
                lease_scope | {
                    "ordinal": 1,
                    "result_id": begun["result"]["result_id"],
                    "prepared_digest": prepared_digest,
                },
            )
    assert getattr(rejected_metadata.value.orig, "sqlstate", None) == "P0001"
    assert "result_conflict" in str(rejected_metadata.value.orig)
    applied = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "finish_batch_result_write",
            lease_scope
            | {
                "ordinal": 1,
                "result_id": begun["result"]["result_id"],
                "prepared_digest": prepared_digest,
            },
        )
    )
    assert applied["result"]["state"] == "applied"
    assert applied["result"]["receipt_count"] == 2
    assert applied["progress"]["success_count"] == 1
    replay = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "finish_batch_result_write",
            lease_scope
            | {
                "ordinal": 1,
                "result_id": begun["result"]["result_id"],
                "prepared_digest": prepared_digest,
            },
        )
    )
    assert replay["result"]["state"] == "applied"
    assert replay["result"]["receipt_count"] == 2
    assert replay["progress"]["success_count"] == 1
    proof = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "batch_result_commit_proof",
            {"result_id": begun["result"]["result_id"]},
        )
    )
    assert proof["applied"] is True
    for index in range(2):
        receipt_reply = _assert_ok(
            await _native(
                db_session,
                "aap_labeling_v1",
                "read_batch_result",
                base_scope
                | {
                    "ordinal": 1,
                    "stage": "prepared",
                    "request_index": index,
                    "part_index": None,
                },
            )
        )
        receipt_time = datetime.fromisoformat(
            receipt_reply["payload"]["receipt"]["created_at"].replace("Z", "+00:00")
        )
        assert receipt_time.utcoffset() is not None
        assert receipt_time.utcoffset().total_seconds() == 0

    finished = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "finish_batch_result_execution",
            lease_scope
            | {
                "status": "completed",
                "error_code": None,
                "result_extra": {
                    "total_cost": "0.0000",
                    "pipeline_stages": [{"stage": 1, "detections": 2}],
                    "gpu_arbiter_failures": [
                        {
                            "error_code": "gpu_busy",
                            "status_code": 503,
                            "retry_after_s": 2,
                            "count": 3,
                        }
                    ],
                },
            },
        )
    )
    assert finished["progress"]["state"] == "completed"
    persisted_result = await db_session.scalar(
        text("SELECT result FROM public.async_jobs WHERE id=:job"), {"job": job_id}
    )
    assert persisted_result["pipeline_stages"] == [{"stage": 1, "detections": 2}]
    assert persisted_result["total_cost"] == "0.0000"
    assert persisted_result["gpu_arbiter_failures"] == [
        {
            "error_code": "gpu_busy",
            "status_code": 503,
            "retry_after_s": 2,
            "count": 3,
        }
    ]
    await db_session.execute(
        text("DELETE FROM public.async_jobs WHERE id=:job"), {"job": job_id}
    )
    history = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "read_batch_result_execution",
            base_scope | {"after_ordinal": 0, "limit": 100},
        )
    )
    assert history["progress"]["state"] == "completed"
    assert history["members"][0]["state"] == "applied"
    reference = _assert_ok(
        await _native(
            db_session,
            "aap_jobs_v1",
            "batch_result_execution_referenced",
            base_scope,
        )
    )
    assert reference == {
        "job_id": str(job_id),
        "referenced": False,
        "terminal": True,
        "job_status": "completed",
    }
    retained_receipt = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "read_batch_result",
            base_scope
            | {
                "ordinal": 1,
                "stage": "prepared",
                "request_index": 0,
                "part_index": None,
            },
        )
    )
    assert retained_receipt["payload"]["receipt"]["id"] == raw_meta[0]["prediction_id"]
