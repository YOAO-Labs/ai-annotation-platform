from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aap_preannotation_results import contract as result_contract
from aap_preannotation_results import retry_contract as c
from aap_preannotation_results_runtime.retry_database import (
    AsyncpgPredictionRetryDatabase,
    EXECUTOR_OPERATIONS,
    WRITER_OPERATIONS,
    prepare_native_call,
)


JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
FAILED_ID = UUID("22222222-2222-4222-8222-222222222222")
ACTOR_ID = UUID("33333333-3333-4333-8333-333333333333")
PROJECT_ID = UUID("44444444-4444-4444-8444-444444444444")
TASK_ID = UUID("55555555-5555-4555-8555-555555555555")
TOKEN = UUID("66666666-6666-4666-8666-666666666666")
WORKER_ID = UUID("77777777-7777-4777-8777-777777777777")
REQUEST_DIGEST = "a" * 64
PREPARED_DIGEST = "b" * 64


def retry_write_scope(**overrides):
    values = {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "actor_id": ACTOR_ID,
        "actor_known": True,
        "operation": "prediction_retry",
        "execution_scope": "retry:original-failure",
        "execution_scope_known": True,
        "task_version": 4,
        "task_status": "pending",
        "assignee_id": None,
        "review_round_id": None,
        "source_annotation_id": None,
        "source_annotation_version": None,
        "expected_batch_id": None,
        "expected_batch_id_known": True,
        "job_id": JOB_ID,
        "job_id_known": True,
        "source_binding_known": True,
        "task_source_generation": None,
        "source_dataset_item_id": None,
        "source_file_path": None,
        "source_file_type": None,
        "accepted_input_id": None,
    }
    values.update(overrides)
    return values


def attempt(**overrides):
    values = {
        "job_id": JOB_ID,
        "failed_prediction_id": FAILED_ID,
        "actor_id": ACTOR_ID,
        "project_id": PROJECT_ID,
        "task_id": TASK_ID,
        "ml_backend_id": None,
        "ml_backend_pool_id": None,
        "source_extra": {"source": "previous attempt"},
        "retry_count_at_admission": 2,
        "write_scope": retry_write_scope(),
        "request_digest": REQUEST_DIGEST,
        "execution_queue": None,
        "state": "accepted",
        "outcome": None,
    }
    values.update(overrides)
    return c.RetryAttempt.model_validate(values)


def progress(state="running", **overrides):
    values = {
        "job_id": JOB_ID,
        "state": state,
        "total": 1,
        "selection_cursor": 1,
        "selection_complete": True,
        "next_ordinal": 1,
        "bulk_overwrite_done": False,
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "total_cost": "0",
        "pipeline_stages": {},
        "gpu_arbiter_failures": [],
    }
    values.update(overrides)
    return result_contract.Progress.model_validate(values)


def lease_values():
    return {
        "job_id": JOB_ID,
        "actor_id": ACTOR_ID,
        "project_id": PROJECT_ID,
        "request_digest": REQUEST_DIGEST,
        "generation": 2,
        "lease_token": TOKEN,
    }


def test_retry_registry_is_separate_and_assigns_exact_database_roles():
    assert set(c.OPERATION) == {
        "admit_prediction_retry",
        "read_prediction_retry",
        "finish_prediction_retry_write",
        "finish_prediction_retry_failure",
        "claim_prediction_retry_deliveries",
        "finish_prediction_retry_delivery",
    }
    assert len(result_contract.OPERATION) == 17
    assert EXECUTOR_OPERATIONS == {
        "read_prediction_retry",
        "claim_prediction_retry_deliveries",
        "finish_prediction_retry_delivery",
    }
    assert WRITER_OPERATIONS == {
        "admit_prediction_retry",
        "finish_prediction_retry_write",
        "finish_prediction_retry_failure",
    }
    assert c.OPERATION["admit_prediction_retry"].namespace == "aap_labeling_v1"
    assert c.OPERATION["claim_prediction_retry_deliveries"].namespace == "aap_jobs_v1"


def test_retry_attempt_is_closed_strict_and_all_nullable_fields_are_required():
    valid = attempt()
    assert valid.write_scope.operation == "prediction_retry"
    assert valid.ml_backend_id is None
    with pytest.raises(ValidationError):
        c.RetryAttempt.model_validate({**valid.model_dump(), "extra": "no"})

    for required_nullable in (
        "task_id",
        "ml_backend_id",
        "ml_backend_pool_id",
        "write_scope",
        "execution_queue",
        "outcome",
    ):
        values = valid.model_dump()
        values.pop(required_nullable)
        with pytest.raises(ValidationError):
            c.RetryAttempt.model_validate(values)

    with pytest.raises(ValidationError):
        c.RetryAttempt.model_validate(
            {
                **valid.model_dump(),
                "write_scope": retry_write_scope(operation="preannotation"),
            }
        )
    with pytest.raises(ValidationError):
        c.RetryAttempt.model_validate({**valid.model_dump(), "source_extra": []})
    with pytest.raises(ValidationError):
        c.RetryAttempt.model_validate(
            {**valid.model_dump(), "retry_count_at_admission": True}
        )


def test_attempt_can_represent_a_missing_task_without_fabricating_scope():
    missing_task = attempt(task_id=None, write_scope=None)
    assert missing_task.task_id is None
    assert missing_task.write_scope is None
    with pytest.raises(ValidationError):
        c.RetryAttempt.model_validate(
            {**missing_task.model_dump(), "write_scope": retry_write_scope()}
        )


def test_requests_apply_defaults_budgets_and_required_nullable_fields():
    claim = c.ClaimPredictionRetryDeliveriesRequest(worker_id=WORKER_ID)
    assert (claim.job_id, claim.limit, claim.lease_seconds) == (None, 100, 30)
    with pytest.raises(ValidationError):
        c.ClaimPredictionRetryDeliveriesRequest(worker_id=WORKER_ID, limit=0)
    with pytest.raises(ValidationError):
        c.ClaimPredictionRetryDeliveriesRequest(worker_id=WORKER_ID, lease_seconds=301)
    with pytest.raises(ValidationError):
        c.FinishPredictionRetryDeliveryRequest(
            job_id=JOB_ID,
            generation=1,
            lease_token=TOKEN,
            submitted=True,
        )
    with pytest.raises(ValidationError):
        c.FinishPredictionRetryFailureRequest.model_validate(
            {
                **lease_values(),
                "error_code": "e" * 257,
                "reason": "",
                "gpu_arbiter_error": None,
                "context": {},
            }
        )
    request = c.FinishPredictionRetryFailureRequest.model_validate(
        {
            **lease_values(),
            "error_code": "model_failed",
            "reason": "r" * 200,
            "gpu_arbiter_error": None,
            "context": {},
        }
    )
    assert len(request.reason) == 200


def test_admit_and_read_validate_exact_identity_and_queue_echoes():
    admit = c.AdmitPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
        execution_queue=None,
    )
    accepted = c.AdmitPredictionRetryResult(attempt=attempt(), created=True)
    assert (
        c.decode_reply("admit_prediction_retry", admit, c.success_envelope(accepted))
        == accepted
    )
    wrong = accepted.model_copy(
        update={
            "attempt": accepted.attempt.model_copy(
                update={"failed_prediction_id": uuid4()}
            )
        }
    )
    with pytest.raises(c.PredictionRetryFailure, match="invalid_response"):
        c.decode_reply("admit_prediction_retry", admit, c.success_envelope(wrong))

    read = c.ReadPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
    )
    read_result = c.ReadPredictionRetryResult(attempt=attempt())
    assert (
        c.decode_reply("read_prediction_retry", read, c.success_envelope(read_result))
        == read_result
    )
    wrong_actor = read_result.model_copy(
        update={"attempt": read_result.attempt.model_copy(update={"actor_id": uuid4()})}
    )
    with pytest.raises(c.PredictionRetryFailure) as error:
        c.decode_reply("read_prediction_retry", read, c.success_envelope(wrong_actor))
    assert error.value.code == "invalid_response"


def test_write_and_failure_replies_require_terminal_progress_for_same_job():
    write = c.FinishPredictionRetryWriteRequest(
        **lease_values(), result_id=uuid4(), prepared_digest=PREPARED_DIGEST
    )
    receipt = result_contract.Receipt(
        id=uuid4(),
        created_at="2026-10-20T00:00:00Z",
        tool_unit_id="bbox",
    )
    outcome = {
        "status": "succeeded",
        "failed_id": str(FAILED_ID),
        "prediction_id": str(receipt.id),
    }
    success = c.FinishPredictionRetryWriteResult(
        outcome=outcome,
        receipt=receipt,
        progress=progress("completed", success_count=1),
    )
    assert (
        c.decode_reply(
            "finish_prediction_retry_write", write, c.success_envelope(success)
        )
        == success
    )

    wrong_job = success.model_copy(
        update={"progress": progress("completed", job_id=uuid4())}
    )
    with pytest.raises(c.PredictionRetryFailure) as error:
        c.decode_reply(
            "finish_prediction_retry_write", write, c.success_envelope(wrong_job)
        )
    assert error.value.code == "invalid_response"

    failure_request = c.FinishPredictionRetryFailureRequest(
        **lease_values(),
        error_code="inference_failed",
        reason="model returned an invalid payload",
        gpu_arbiter_error=None,
        context={"attempt": 1},
    )
    failed = c.FinishPredictionRetryFailureResult(
        outcome={"status": "failed", "failed_id": str(FAILED_ID), "reason": "failed"},
        progress=progress("failed", failed_count=1),
    )
    assert (
        c.decode_reply(
            "finish_prediction_retry_failure",
            failure_request,
            c.success_envelope(failed),
        )
        == failed
    )
    wrong_state = failed.model_copy(update={"progress": progress("running")})
    with pytest.raises(c.PredictionRetryFailure):
        c.decode_reply(
            "finish_prediction_retry_failure",
            failure_request,
            c.success_envelope(wrong_state),
        )


def test_delivery_claim_page_and_finish_echoes_are_checked():
    request = c.ClaimPredictionRetryDeliveriesRequest(
        worker_id=WORKER_ID, job_id=JOB_ID, limit=1
    )
    delivery = c.RetryDelivery(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
        execution_queue=None,
        generation=1,
        lease_token=TOKEN,
        lease_expires_at="2026-10-20T00:00:00Z",
    )
    claimed = c.ClaimPredictionRetryDeliveriesResult(items=[delivery])
    assert (
        c.decode_reply(
            "claim_prediction_retry_deliveries", request, c.success_envelope(claimed)
        )
        == claimed
    )

    duplicate = c.ClaimPredictionRetryDeliveriesResult(items=[delivery, delivery])
    with pytest.raises(c.PredictionRetryFailure) as error:
        c.decode_reply(
            "claim_prediction_retry_deliveries", request, c.success_envelope(duplicate)
        )
    assert error.value.code == "invalid_response"

    unfiltered = c.ClaimPredictionRetryDeliveriesRequest(worker_id=WORKER_ID, limit=2)
    duplicate_jobs = c.ClaimPredictionRetryDeliveriesResult(
        items=[delivery, delivery.model_copy(update={"failed_prediction_id": uuid4()})]
    )
    with pytest.raises(c.PredictionRetryFailure):
        c.decode_reply(
            "claim_prediction_retry_deliveries",
            unfiltered,
            c.success_envelope(duplicate_jobs),
        )

    finish = c.FinishPredictionRetryDeliveryRequest(
        job_id=JOB_ID,
        generation=1,
        lease_token=TOKEN,
        submitted=False,
        error_code="publisher_unavailable",
    )
    completed = c.FinishPredictionRetryDeliveryResult(job_id=JOB_ID, submitted=False)
    assert (
        c.decode_reply(
            "finish_prediction_retry_delivery", finish, c.success_envelope(completed)
        )
        == completed
    )
    mismatch = completed.model_copy(update={"submitted": True})
    with pytest.raises(c.PredictionRetryFailure):
        c.decode_reply(
            "finish_prediction_retry_delivery", finish, c.success_envelope(mismatch)
        )


def test_retry_errors_are_closed_and_provider_details_are_hidden():
    request = c.ReadPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
    )
    with pytest.raises(c.PredictionRetryFailure) as controlled:
        c.decode_reply(
            "read_prediction_retry",
            request,
            {"schema": c.SCHEMA, "ok": False, "error": {"code": "content_deleting"}},
        )
    assert controlled.value.code == "content_deleting"

    with pytest.raises(c.PredictionRetryFailure) as response:
        c.decode_reply(
            "read_prediction_retry",
            request,
            {
                "schema": c.SCHEMA,
                "ok": False,
                "error": {
                    "code": "storage_unavailable",
                    "detail": "secret database value",
                },
            },
        )
    assert response.value.code == "invalid_response"
    assert "secret" not in str(response.value)


def test_native_call_is_one_jsonb_argument_and_writer_scope_is_separate():
    read = c.ReadPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
    )
    sql, arguments = prepare_native_call("read_prediction_retry", read)
    assert sql == "SELECT aap_labeling_v1.read_prediction_retry($1::jsonb)"
    assert len(arguments) == 1
    assert json.loads(arguments[0]) == read.model_dump(mode="json")

    admit = c.AdmitPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
        execution_queue=None,
    )
    with pytest.raises(PermissionError):
        prepare_native_call("admit_prediction_retry", admit)
    assert prepare_native_call("admit_prediction_retry", admit, writer=True)[
        0
    ].startswith("SELECT aap_labeling_v1.")
    with pytest.raises(PermissionError):
        prepare_native_call("read_prediction_retry", read, writer=True)

    oversized = c.AdmitPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
        execution_queue="x" * (c.MAX_REQUEST_BYTES + 1),
    )
    with pytest.raises(c.PredictionRetryFailure) as error:
        prepare_native_call("admit_prediction_retry", oversized, writer=True)
    assert error.value.code == "invalid_request"


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transactions += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.commits += exc_type is None
        self.connection.rollbacks += exc_type is not None


class FakeConnection:
    def __init__(self, reply):
        self.reply = reply
        self.statements = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return FakeTransaction(self)

    async def execute(self, statement, *args):
        self.statements.append((statement, args))

    async def fetchval(self, statement, *args, **kwargs):
        self.statements.append((statement, args))
        return self.reply


class FakePool:
    def __init__(self, reply):
        self.connection = FakeConnection(reply)

    def acquire(self, *, timeout):
        assert timeout == 5

        class Acquire:
            async def __aenter__(inner_self):
                return self.connection

            async def __aexit__(inner_self, exc_type, exc, traceback):
                return None

        return Acquire()


def test_executor_adapter_commits_only_valid_reply_and_rejects_writer_ops():
    request = c.ClaimPredictionRetryDeliveriesRequest(worker_id=WORKER_ID)
    valid = c.ClaimPredictionRetryDeliveriesResult(items=[])
    pool = FakePool(c.json_bytes(c.success_envelope(valid)))
    result = asyncio.run(
        AsyncpgPredictionRetryDatabase(pool).call(
            "claim_prediction_retry_deliveries", request
        )
    )
    assert result == valid
    assert pool.connection.commits == 1
    assert pool.connection.rollbacks == 0
    assert pool.connection.statements[1][0] == (
        "SELECT aap_jobs_v1.claim_prediction_retry_deliveries($1::jsonb)"
    )

    admit = c.AdmitPredictionRetryRequest(
        job_id=JOB_ID,
        failed_prediction_id=FAILED_ID,
        actor_id=ACTOR_ID,
        execution_queue=None,
    )
    writer_pool = FakePool("unused")
    with pytest.raises(c.PredictionRetryFailure) as error:
        asyncio.run(
            AsyncpgPredictionRetryDatabase(writer_pool).call(
                "admit_prediction_retry", admit
            )
        )
    assert error.value.code == "invalid_request"
    assert writer_pool.connection.statements == []


def test_retry_cli_uses_shared_config_and_returns_sanitized_failure():
    package = Path(__file__).resolve().parents[1] / "src"
    request = c.ClaimPredictionRetryDeliveriesRequest(worker_id=WORKER_ID)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PREANNOTATION_RESULTS_DATABASE_URL")
    }
    environment["PYTHONPATH"] = str(package)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aap_preannotation_results_runtime.retry_main",
            "claim_prediction_retry_deliveries",
        ],
        input=c.json_bytes(request),
        capture_output=True,
        env=environment,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == c.error_envelope("storage_unavailable")
    assert result.stderr == b""


def test_retry_schema_uses_one_of_for_disjoint_closed_wire_shapes():
    document = c.contract_document()
    assert document["oneOf"]
    assert document["$defs"]["_SuccessEnvelope"]["properties"]["data"]["oneOf"]
    request_names = [operation.request.__name__ for operation in c.OPERATION.values()]
    assert len(request_names) == len(set(request_names))
    for name in request_names:
        model_schema = document["$defs"][name]
        assert model_schema["additionalProperties"] is False
        assert model_schema["type"] == "object"
