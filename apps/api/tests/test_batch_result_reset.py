"""Batch reset must retire durable execution ownership before deleting history."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import text

from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction, PredictionMeta
from app.services.async_job import delete_batch_predict_jobs

pytestmark = pytest.mark.asyncio


async def test_delete_batch_predict_jobs_rolls_back_and_retains_applied_receipt(
    db_session, super_admin
):
    from tests.test_preannotation_results_native import (
        _assert_ok,
        _bundle_digest,
        _native,
        _request_body,
        _start_member,
        _world,
    )

    actor, _ = super_admin
    project, tasks, job_id, base_scope, lease_scope, scopes = await _world(
        db_session, actor
    )
    task = tasks[0]
    batch_id = uuid.uuid4()
    job = await db_session.get(AsyncJob, job_id)
    assert job is not None
    job.payload = {**job.payload, "batch_id": str(batch_id)}

    await _start_member(db_session, lease_scope, 1)
    body = _request_body(
        task_id=task.id,
        project_id=project.id,
        write_scope=scopes[0],
        result=[{"type": "choices", "value": {"choices": ["accepted"]}}],
    )
    body_bytes = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    context = {"pipeline_stages": {}, "gpu_arbiter_failures": [], "total_cost": "0"}

    async def capture(stage: str) -> tuple[dict, dict, str]:
        digest, hashes = _bundle_digest(
            str(job_id), 1, base_scope["request_digest"], stage, [body_bytes]
        )
        if stage == "raw":
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
                        "bundle_digest": digest,
                        "stage": stage,
                        "context": context,
                        "restart_incomplete": False,
                    },
                )
            )
        else:
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
                        "bundle_digest": digest,
                        "stage": stage,
                        "context": context,
                        "restart_incomplete": False,
                    },
                )
            )
        put = _assert_ok(
            await _native(
                db_session,
                "aap_labeling_v1",
                "put_batch_result_part",
                lease_scope
                | {
                    "ordinal": 1,
                    "stage": stage,
                    "request_index": 0,
                    "sha256": hashes[0],
                    "byte_size": len(body_bytes),
                    "part_count": 1,
                    "part_index": 0,
                    "content": base64.b64encode(body_bytes).decode("ascii"),
                    "tool_unit_id": None,
                },
            )
        )
        sealed = _assert_ok(
            await _native(
                db_session,
                "aap_labeling_v1",
                "seal_batch_result",
                lease_scope | {"ordinal": 1, "stage": stage},
            )
        )
        return (
            put["result"],
            put["payload"],
            sealed["result"]["prepared_digest"] if stage == "prepared" else digest,
        )

    raw_result, raw_payload, _ = await capture("raw")
    result_id = raw_result["result_id"]
    prepared_result, _, prepared_digest = await capture("prepared")
    assert prepared_result["result_id"] == result_id
    _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "lock_batch_result_write",
            lease_scope
            | {
                "ordinal": 1,
                "result_id": result_id,
                "prepared_digest": prepared_digest,
            },
        )
    )
    created_at = datetime.fromisoformat(
        raw_payload["prediction_created_at"].replace("Z", "+00:00")
    )
    prediction_id = uuid.UUID(raw_payload["prediction_id"])
    db_session.add(
        Prediction(
            id=prediction_id,
            created_at=created_at,
            task_id=task.id,
            project_id=project.id,
            ml_backend_id=None,
            ml_backend_pool_id=None,
            model_version=None,
            score=None,
            tool_unit_id=None,
            result=body["result"],
            source="ml_backend",
        )
    )
    db_session.add(
        PredictionMeta(
            id=uuid.uuid4(),
            prediction_id=prediction_id,
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
    applied = _assert_ok(
        await _native(
            db_session,
            "aap_labeling_v1",
            "finish_batch_result_write",
            lease_scope
            | {
                "ordinal": 1,
                "result_id": result_id,
                "prepared_digest": prepared_digest,
            },
        )
    )
    assert applied["result"]["receipt_count"] == 1

    completed_job_id = uuid.uuid4()
    other_batch_job_id = uuid.uuid4()
    other_kind_job_id = uuid.uuid4()
    db_session.add_all(
        [
            AsyncJob(
                id=completed_job_id,
                kind="batch_predict",
                status="completed",
                completed_at=datetime.now(timezone.utc),
                payload={"batch_id": str(batch_id)},
            ),
            AsyncJob(
                id=other_batch_job_id,
                kind="batch_predict",
                status="running",
                payload={"batch_id": str(uuid.uuid4())},
            ),
            AsyncJob(
                id=other_kind_job_id,
                kind="video_tracker",
                status="running",
                payload={"batch_id": str(batch_id)},
            ),
        ]
    )
    await db_session.flush()

    savepoint = await db_session.begin_nested()
    deleted = await delete_batch_predict_jobs(db_session, batch_id)
    assert deleted == 2
    assert (
        await db_session.scalar(
            text("SELECT status FROM public.async_jobs WHERE id=:job"),
            {"job": job_id},
        )
        is None
    )
    retired = await db_session.execute(
        text(
            "SELECT state, worker_id, lease_token, lease_expires_at "
            "FROM public.batch_result_executions WHERE job_id=:job"
        ),
        {"job": job_id},
    )
    assert retired.one() == ("cancelled", None, None, None)
    await savepoint.rollback()

    assert (
        await db_session.scalar(
            text("SELECT status FROM public.async_jobs WHERE id=:job"),
            {"job": job_id},
        )
        == "running"
    )
    restored = await db_session.execute(
        text(
            "SELECT state, lease_token FROM public.batch_result_executions WHERE job_id=:job"
        ),
        {"job": job_id},
    )
    restored_state, restored_token = restored.one()
    assert restored_state == "running"
    assert str(restored_token) == lease_scope["lease_token"]

    assert await delete_batch_predict_jobs(db_session, batch_id) == 2
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:job"),
            {"job": job_id},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:job"),
            {"job": completed_job_id},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:job"),
            {"job": other_batch_job_id},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:job"),
            {"job": other_kind_job_id},
        )
        == 1
    )
    final_state = await db_session.execute(
        text(
            "SELECT state, worker_id, lease_token, lease_expires_at "
            "FROM public.batch_result_executions WHERE job_id=:job"
        ),
        {"job": job_id},
    )
    assert final_state.one() == ("cancelled", None, None, None)
    receipt = _assert_ok(
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
    assert receipt["payload"]["receipt"]["id"] == str(prediction_id)
