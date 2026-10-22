"""Native atomic finalization tests for the claimed video worker boundary."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text

from tests.test_video_execution_native import _step
from tests.test_video_tracker_input_native import _capture, _seed_tracker_job

pytestmark = pytest.mark.asyncio

_WRITER_STEP = text("SELECT aap_labeling_v1.video_writer_step(:action, :arguments)")


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _proof(job_id, claim):
    return {
        "job_id": str(job_id),
        "run_id": claim["run_id"],
        "worker_id": claim["worker_id"],
        "generation": claim["generation"],
        "lease_token": claim["lease_token"],
    }


async def _writer_step(db, action: str, arguments: dict):
    raw = await db.scalar(
        _WRITER_STEP,
        {
            "action": action,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    )
    return _decode(raw)


async def _claimed_running_job(db):
    actor, _, _, _, job = await _seed_tracker_job(db)
    await _capture(db, actor, job.id)
    claimed = await _step(
        db,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert claimed["status"] == "ok"
    job.status = "running"
    await db.flush()
    return job, _proof(job.id, claimed["execution"])


def _failed_apply(job_id, proof, *, operation_id=None, request_digest="a" * 64):
    return {
        "operation_id": str(operation_id or uuid.uuid4()),
        "execution": proof,
        "outcome": "failed",
        "request_digest": request_digest,
        "expected_input": None,
        "error_message": "worker failed",
        "staged_result_json": None,
        "staged_sha256": None,
        "staged_byte_size": 0,
    }


async def test_failure_publication_is_atomic_and_receipt_replay_is_immutable(
    db_session,
):
    job, proof = await _claimed_running_job(db_session)
    request = _failed_apply(job.id, proof)

    first = await _writer_step(db_session, "apply", request)
    assert first["schema"] == "aap-labeling-video-writer-data/v1"
    assert first["ok"] is True
    receipt = first["data"]["receipt"]
    assert set(receipt) == {
        "operation_id",
        "job_id",
        "run_id",
        "request_digest",
        "outcome",
        "status",
        "completed_at",
        "staged_sha256",
        "staged_byte_size",
    }
    assert receipt["outcome"] == "failed"
    assert receipt["status"] == "failed"
    assert receipt["staged_sha256"] is None
    assert receipt["staged_byte_size"] == 0
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.staged_result is None

    replay = await _writer_step(db_session, "apply", request)
    assert replay["ok"] is True
    assert replay["data"]["receipt"] == receipt

    conflict = await _writer_step(
        db_session,
        "apply",
        {
            **request,
            "request_digest": "b" * 64,
        },
    )
    assert conflict["ok"] is False
    assert conflict["error"] == {"code": "operation_conflict"}

    stale = await _writer_step(
        db_session,
        "status",
        {
            "operation_id": request["operation_id"],
            "execution": {**proof, "lease_token": str(uuid.uuid4())},
            "request_digest": request["request_digest"],
        },
    )
    assert stale["ok"] is False
    assert stale["error"] == {"code": "lease_lost"}


async def test_invalid_staged_digest_leaves_running_job_and_no_receipt(db_session):
    job, proof = await _claimed_running_job(db_session)
    request = {
        **_failed_apply(job.id, proof),
        "staged_result_json": "{}",
        "staged_sha256": "f" * 64,
        "staged_byte_size": 2,
    }

    response = await _writer_step(db_session, "apply", request)
    assert response["ok"] is False
    assert response["error"] == {"code": "candidate_invalid"}
    await db_session.refresh(job)
    assert job.status == "running"
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_labeling_v1.video_writer_receipts "
                "WHERE job_id=:job"
            ),
            {"job": job.id},
        )
        == 0
    )


async def test_failure_after_cancellation_preserves_partial_candidates(db_session):
    job, proof = await _claimed_running_job(db_session)
    preserved = {"results": [{"frame_index": 2}], "legacy": True}
    job.status = "cancelled"
    job.cancel_requested_at = await db_session.scalar(
        text("SELECT pg_catalog.clock_timestamp()")
    )
    job.staged_result = preserved
    await db_session.flush()

    response = await _writer_step(
        db_session,
        "apply",
        _failed_apply(job.id, proof),
    )
    assert response["ok"] is True, response
    receipt = response["data"]["receipt"]
    assert receipt["outcome"] == "cancelled"
    assert receipt["status"] == "cancelled"
    assert receipt["staged_sha256"] is None
    assert receipt["staged_byte_size"] == 0
    await db_session.refresh(job)
    assert job.status == "cancelled"
    assert job.staged_result == preserved


async def test_native_source_mapping_matches_frozen_legacy_observations(db_session):
    path = (
        Path(__file__).resolve().parents[2]
        / "labeling/tests/fixtures/video-writer-legacy.json"
    )
    corpus = json.loads(path.read_text())
    for case in corpus["cases"]:
        expected = case.get("expected_staged_result")
        if expected is None and case["name"] == "candidate-size-refusal":
            continue
        actual = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1._video_writer_source_map("
                "CAST(:annotation AS uuid), CAST(:execution AS jsonb), CAST(:results AS jsonb))"
            ),
            {
                "annotation": case["job_annotation_id"],
                "execution": json.dumps(case["execution"]),
                "results": json.dumps(case["frame_results"]),
            },
        )
        assert actual == (
            expected["source_instance_map"] if expected is not None else None
        ), case["name"]


async def test_publication_lookup_requires_current_proof_and_is_read_only(db_session):
    from aap_labeling.video_writer_contract import video_writer_operation_id

    job, proof = await _claimed_running_job(db_session)
    operation_id = video_writer_operation_id(uuid.UUID(proof["run_id"]))
    request = _failed_apply(job.id, proof, operation_id=operation_id)
    first = await _writer_step(db_session, "apply", request)
    assert first["ok"] is True
    lookup = await _writer_step(db_session, "read_publication", {"execution": proof})
    assert lookup["data"]["receipt"] == first["data"]["receipt"]
    stale = await _writer_step(
        db_session,
        "read_publication",
        {"execution": {**proof, "lease_token": str(uuid.uuid4())}},
    )
    assert stale["error"] == {"code": "lease_lost"}
    exact = await _writer_step(
        db_session,
        "status",
        {
            "execution": proof,
            "operation_id": str(operation_id),
            "request_digest": "b" * 64,
        },
    )
    assert exact["error"] == {"code": "operation_conflict"}


async def test_cancel_requires_actual_cancel_marker(db_session):
    job, proof = await _claimed_running_job(db_session)
    request = {
        **_failed_apply(job.id, proof),
        "outcome": "cancelled",
        "error_message": None,
    }
    response = await _writer_step(db_session, "apply", request)
    assert response["error"] == {"code": "job_cancelled"}
    await db_session.refresh(job)
    assert job.status == "running"


async def test_cancel_without_candidates_does_not_require_creator_authority(db_session):
    job, proof = await _claimed_running_job(db_session)
    job.status = "cancelled"
    job.created_by = None
    await db_session.flush()
    request = {
        **_failed_apply(job.id, proof),
        "outcome": "cancelled",
        "error_message": None,
    }
    response = await _writer_step(db_session, "apply", request)
    assert response["ok"] is True, response
    assert response["data"]["receipt"]["outcome"] == "cancelled"
    await db_session.refresh(job)
    assert job.staged_result is None


async def test_falsy_prompt_seed_defaults_match_legacy_mapping(db_session):
    corpus = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "labeling/tests/fixtures/video-writer-legacy.json"
        ).read_text()
    )
    case = next(
        case
        for case in corpus["cases"]
        if case.get("expected_staged_result")
        and not case["execution"]["prompt_snapshot"].get("seeds")
    )
    for seeds in (None, False, 0, "", {}, []):
        execution = {
            **case["execution"],
            "prompt_snapshot": {**case["execution"]["prompt_snapshot"], "seeds": seeds},
        }
        actual = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1._video_writer_source_map("
                "CAST(:annotation AS uuid),CAST(:execution AS jsonb),CAST(:results AS jsonb))"
            ),
            {
                "annotation": case["job_annotation_id"],
                "execution": json.dumps(execution),
                "results": json.dumps(case["frame_results"]),
            },
        )
        assert actual == case["expected_staged_result"]["source_instance_map"]


async def test_late_jobs_finish_rejection_rolls_back_job_publication(db_session):
    job, proof = await _claimed_running_job(db_session)
    # Refuse only the downstream finish; the other real authority checks remain.
    definition = await db_session.scalar(
        text(
            "SELECT pg_catalog.pg_get_functiondef("
            "'aap_jobs_v1.video_writer_authority(text,jsonb)'::regprocedure)"
        )
    )
    connection = await db_session.connection()
    await connection.exec_driver_sql(
        definition.replace(
            "aap_jobs_v1.video_writer_authority(",
            "aap_jobs_v1._test_original_writer_authority(",
            1,
        )
    )
    await connection.exec_driver_sql(
        "ALTER FUNCTION aap_jobs_v1._test_original_writer_authority(text,jsonb) OWNER TO jobs_code"
    )
    await connection.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION aap_jobs_v1.video_writer_authority(
            p_action text,p_arguments jsonb
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $test$
        BEGIN
            IF p_action='finish_failed' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'finish_conflict');
            END IF;
            RETURN aap_jobs_v1._test_original_writer_authority(p_action,p_arguments);
        END
        $test$
    """)
    response = await _writer_step(db_session, "apply", _failed_apply(job.id, proof))
    assert response["error"] == {"code": "finish_conflict"}
    await db_session.refresh(job)
    assert job.status == "running"
    assert job.error_message is None
    assert job.staged_result is None
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_labeling_v1.video_writer_receipts WHERE job_id=:job"
            ),
            {"job": job.id},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_execution_journals "
                "WHERE job_id=:job"
            ),
            {"job": job.id},
        )
        == 0
    )
