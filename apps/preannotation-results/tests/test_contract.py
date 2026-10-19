from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aap_preannotation_results import contract as c


JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
PROJECT_ID = UUID("33333333-3333-4333-8333-333333333333")
TASK_ID = UUID("44444444-4444-4444-8444-444444444444")
TOKEN = UUID("55555555-5555-4555-8555-555555555555")
WORKER_ID = UUID("66666666-6666-4666-8666-666666666666")
REQUEST_DIGEST = "a" * 64
PAYLOAD_DIGEST = "b" * 64


def scope_values() -> dict:
    return {
        "job_id": JOB_ID,
        "actor_id": ACTOR_ID,
        "project_id": PROJECT_ID,
        "request_digest": REQUEST_DIGEST,
    }


def lease_values() -> dict:
    return {**scope_values(), "generation": 2, "lease_token": TOKEN}


def progress(**overrides):
    values = {
        "job_id": JOB_ID,
        "state": "running",
        "total": 1,
        "selection_cursor": 0,
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
    return c.Progress.model_validate(values)


def result_value(**overrides):
    values = {
        "result_id": uuid4(),
        "job_id": JOB_ID,
        "ordinal": 1,
        "task_id": TASK_ID,
        "result_count": 1,
        "raw_digest": PAYLOAD_DIGEST,
        "prepared_digest": None,
        "raw_complete": False,
        "prepared_complete": False,
        "state": "capturing",
        "context": {},
        "receipt_count": 0,
    }
    values.update(overrides)
    return c.Result.model_validate(values)


def test_request_dtos_are_closed_strict_frozen_and_nullable_fields_are_required():
    request = c.ReadBatchResultExecutionRequest.model_validate(scope_values())
    assert request.after_ordinal == 0
    assert request.limit == 100

    with pytest.raises(ValidationError):
        c.ReadBatchResultExecutionRequest.model_validate(
            {**scope_values(), "extra": True}
        )
    with pytest.raises(ValidationError):
        c.Payload.model_validate(
            {
                "payload_id": uuid4(),
                "request_index": 0,
                "prediction_id": uuid4(),
                "prediction_created_at": "2026-10-20T00:00:00Z",
                "stage": "raw",
                "sha256": PAYLOAD_DIGEST,
                "byte_size": 0,
                "part_count": 1,
                "complete": False,
                "tool_unit_id": None,
            }
        )
    with pytest.raises(ValidationError):
        c.Member.model_validate(
            {
                "ordinal": 1,
                "task_id": TASK_ID,
                "manifest_member": None,
                "failure_id": uuid4(),
                "overwrite_done": False,
                "state": "planned",
                "result_id": None,
                "failed_prediction_id": None,
                "error_code": None,
            }
        )

    member = c.Member.model_validate(
        {
            "ordinal": 1,
            "task_id": TASK_ID,
            "write_scope": None,
            "manifest_member": None,
            "failure_id": uuid4(),
            "overwrite_done": False,
            "state": "planned",
            "result_id": None,
            "failed_prediction_id": None,
            "error_code": None,
        }
    )
    with pytest.raises(ValidationError):
        member.ordinal = 2


def test_labeling_scope_is_exact_and_matches_member_execution_scope():
    frozen_scope = {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "actor_id": ACTOR_ID,
        "actor_known": True,
        "operation": "preannotation",
        "execution_scope": "batch:run-1",
        "execution_scope_known": True,
        "task_version": 9,
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
        "task_source_generation": 1,
        "source_dataset_item_id": None,
        "source_file_path": "sample.png",
        "source_file_type": "image/png",
        "accepted_input_id": None,
    }
    request = c.StageBatchResultMembersRequest.model_validate(
        {
            **lease_values(),
            "after_ordinal": 0,
            "members": [
                {
                    "ordinal": 1,
                    "task_id": TASK_ID,
                    "write_scope": frozen_scope,
                    "manifest_member": {"source": {"sha256": PAYLOAD_DIGEST}},
                }
            ],
            "complete": True,
        }
    )
    assert request.members[0].write_scope.operation == "preannotation"

    with pytest.raises(ValidationError):
        c.StageBatchResultMembersRequest.model_validate(
            {
                **lease_values(),
                "after_ordinal": 0,
                "members": [
                    {
                        "ordinal": 1,
                        "task_id": TASK_ID,
                        "write_scope": {
                            **frozen_scope,
                            "operation": "prediction_retry",
                        },
                        "manifest_member": None,
                    }
                ],
                "complete": True,
            }
        )

    with pytest.raises(ValidationError):
        c.StageBatchResultMembersRequest.model_validate(
            {
                **lease_values(),
                "after_ordinal": 0,
                "members": [
                    {
                        "ordinal": 1,
                        "task_id": TASK_ID,
                        "write_scope": {**frozen_scope, "actor_id": uuid4()},
                        "manifest_member": None,
                    }
                ],
                "complete": True,
            }
        )


def test_put_part_checks_decoded_limit_before_base64_transport():
    body = b"\x00" * c.MAX_PART_BYTES
    request = c.PutBatchResultPartRequest.model_validate(
        {
            **lease_values(),
            "ordinal": 1,
            "stage": "raw",
            "request_index": 0,
            "sha256": PAYLOAD_DIGEST,
            "byte_size": c.MAX_PART_BYTES,
            "part_count": 1,
            "part_index": 0,
            "content": base64.b64encode(body).decode("ascii"),
            "tool_unit_id": None,
        }
    )
    assert len(request.content) == c.MAX_PART_BASE64_CHARS

    with pytest.raises(ValidationError):
        c.PutBatchResultPartRequest.model_validate(
            {
                **lease_values(),
                "ordinal": 1,
                "stage": "raw",
                "request_index": 0,
                "sha256": PAYLOAD_DIGEST,
                "byte_size": c.MAX_PART_BYTES + 1,
                "part_count": 1,
                "part_index": 0,
                "content": base64.b64encode(body + b"x").decode("ascii"),
                "tool_unit_id": None,
            }
        )
    with pytest.raises(ValidationError):
        c.PutBatchResultPartRequest.model_validate(
            {
                **lease_values(),
                "ordinal": 1,
                "stage": "raw",
                "request_index": 0,
                "sha256": PAYLOAD_DIGEST,
                "byte_size": 1,
                "part_count": 1,
                "part_index": 1,
                "content": base64.b64encode(b"x").decode("ascii"),
                "tool_unit_id": None,
            }
        )


@pytest.mark.parametrize("tool_unit_id", [None, ""])
def test_prepared_part_requires_the_final_nonempty_tool_unit(tool_unit_id):
    with pytest.raises(ValidationError):
        c.PutBatchResultPartRequest.model_validate(
            {
                **lease_values(),
                "ordinal": 1,
                "stage": "prepared",
                "request_index": 0,
                "sha256": PAYLOAD_DIGEST,
                "byte_size": 1,
                "part_count": 1,
                "part_index": 0,
                "content": "eA==",
                "tool_unit_id": tool_unit_id,
            }
        )


@pytest.mark.parametrize("tool_unit_id", [None, ""])
def test_prepared_read_metadata_cannot_omit_the_final_tool_unit(tool_unit_id):
    with pytest.raises(ValidationError):
        c.Payload(
            payload_id=uuid4(),
            request_index=0,
            prediction_id=uuid4(),
            prediction_created_at=datetime.now(timezone.utc),
            stage="prepared",
            sha256=PAYLOAD_DIGEST,
            byte_size=1,
            part_count=1,
            complete=True,
            tool_unit_id=tool_unit_id,
            receipt=None,
        )


def test_native_reply_validates_closed_envelope_and_payload_echoes():
    body = b'{"x":1}'
    digest = hashlib.sha256(body).hexdigest()
    request = c.PutBatchResultPartRequest.model_validate(
        {
            **lease_values(),
            "ordinal": 1,
            "stage": "raw",
            "request_index": 0,
            "sha256": digest,
            "byte_size": len(body),
            "part_count": 1,
            "part_index": 0,
            "content": base64.b64encode(body).decode("ascii"),
            "tool_unit_id": None,
        }
    )
    result = c.PutBatchResultPartResult(
        result=result_value(raw_digest=PAYLOAD_DIGEST),
        payload=c.Payload(
            payload_id=uuid4(),
            request_index=0,
            prediction_id=uuid4(),
            prediction_created_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            stage="raw",
            sha256=digest,
            byte_size=len(body),
            part_count=1,
            complete=True,
            tool_unit_id=None,
            receipt=None,
        ),
    )
    envelope = c.success_envelope(result)
    assert c.decode_reply("put_batch_result_part", request, envelope) == result

    wrong_echo = json.loads(json.dumps(envelope, default=str))
    wrong_echo["data"]["payload"]["request_index"] = 1
    with pytest.raises(c.BatchResultFailure) as error:
        c.decode_reply("put_batch_result_part", request, wrong_echo)
    assert error.value.code == "invalid_response"

    wrong_envelope = {**envelope, "unexpected": True}
    with pytest.raises(c.BatchResultFailure) as error:
        c.decode_reply("put_batch_result_part", request, wrong_envelope)
    assert error.value.code == "invalid_response"


def test_native_claim_echo_and_nullable_lease_values():
    request = c.ClaimBatchResultExecutionRequest.model_validate(
        {**scope_values(), "worker_id": WORKER_ID, "execution_id": "worker-1"}
    )
    assert request.lease_seconds == 120
    unowned_request = c.ClaimBatchResultExecutionRequest.model_validate(
        {**scope_values(), "worker_id": WORKER_ID, "execution_id": None}
    )
    assert unowned_request.execution_id is None
    with pytest.raises(ValidationError):
        c.ClaimBatchResultExecutionRequest.model_validate(
            {**scope_values(), "worker_id": WORKER_ID}
        )
    with pytest.raises(ValidationError):
        c.ClaimBatchResultExecutionRequest.model_validate(
            {**scope_values(), "worker_id": WORKER_ID, "execution_id": ""}
        )
    lease = c.Lease(
        **scope_values(),
        generation=3,
        lease_token=TOKEN,
        worker_id=WORKER_ID,
        execution_id="worker-1",
        lease_expires_at="2026-10-20T01:00:00Z",
    )
    response = c.ClaimBatchResultExecutionResult(lease=lease, progress=progress())
    assert (
        c.decode_reply(
            "claim_batch_result_execution", request, c.success_envelope(response)
        )
        == response
    )

    forged_lease = lease.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(c.BatchResultFailure) as error:
        c.decode_reply(
            "claim_batch_result_execution",
            request,
            c.success_envelope(
                c.ClaimBatchResultExecutionResult(
                    lease=forged_lease, progress=progress()
                )
            ),
        )
    assert error.value.code == "invalid_response"


def test_start_current_member_allows_safe_started_replay_echo():
    request = c.StartBatchResultMemberRequest.model_validate(
        {**lease_values(), "ordinal": 1}
    )
    member = c.Member(
        ordinal=1,
        task_id=TASK_ID,
        write_scope=None,
        manifest_member=None,
        failure_id=uuid4(),
        overwrite_done=False,
        state="captured",
        result_id=uuid4(),
        failed_prediction_id=None,
        error_code=None,
    )
    response = c.StartBatchResultMemberResult(
        progress=progress(next_ordinal=1), member=member
    )
    assert (
        c.decode_reply(
            "start_batch_result_member", request, c.success_envelope(response)
        )
        == response
    )


def test_read_result_checks_exclusive_page_cursor_and_single_part_digest():
    page_request = c.ReadBatchResultExecutionRequest(
        **scope_values(), after_ordinal=0, limit=1
    )
    member = c.Member(
        ordinal=1,
        task_id=TASK_ID,
        write_scope=None,
        manifest_member=None,
        failure_id=uuid4(),
        overwrite_done=False,
        state="planned",
        result_id=None,
        failed_prediction_id=None,
        error_code=None,
    )
    page = c.ReadBatchResultExecutionResult(
        progress=progress(), members=(member,), next_ordinal=1
    )
    assert (
        c.decode_reply(
            "read_batch_result_execution", page_request, c.success_envelope(page)
        )
        == page
    )

    body = b'{"x":1}'
    digest = hashlib.sha256(body).hexdigest()
    request = c.ReadBatchResultRequest(**scope_values(), ordinal=1, part_index=0)
    payload = c.Payload(
        payload_id=uuid4(),
        request_index=0,
        prediction_id=uuid4(),
        prediction_created_at="2026-10-20T00:00:00+00:00",
        stage="raw",
        sha256=digest,
        byte_size=len(body),
        part_count=1,
        complete=True,
        tool_unit_id=None,
        receipt=None,
    )
    good = c.ReadBatchResultResult(
        result=result_value(raw_digest=digest, raw_complete=True, state="captured"),
        payload=payload,
        content=base64.b64encode(body).decode("ascii"),
    )
    assert (
        c.decode_reply("read_batch_result", request, c.success_envelope(good)) == good
    )

    wrong_body = base64.b64encode(b'{"x":2}').decode("ascii")
    bad = good.model_copy(update={"content": wrong_body})
    with pytest.raises(c.BatchResultFailure) as error:
        c.decode_reply("read_batch_result", request, c.success_envelope(bad))
    assert error.value.code == "invalid_response"


def test_aware_utc_timestamps_accept_z_or_offset_and_reject_non_utc():
    first = c.Lease(
        **scope_values(),
        generation=1,
        lease_token=TOKEN,
        worker_id=WORKER_ID,
        execution_id=None,
        lease_expires_at="2026-10-20T01:00:00Z",
    )
    second = c.Lease.model_validate(
        {
            **scope_values(),
            "generation": 1,
            "lease_token": TOKEN,
            "worker_id": WORKER_ID,
            "execution_id": None,
            "lease_expires_at": "2026-10-20T01:00:00+00:00",
        }
    )
    assert first.lease_expires_at == second.lease_expires_at
    with pytest.raises(ValidationError):
        c.Lease.model_validate(
            {
                **scope_values(),
                "generation": 1,
                "lease_token": TOKEN,
                "worker_id": WORKER_ID,
                "execution_id": None,
                "lease_expires_at": "2026-10-20T01:00:00+01:00",
            }
        )


def test_error_envelope_has_only_closed_codes_and_fields():
    request = c.ReadBatchResultExecutionRequest.model_validate(scope_values())
    reply = {"schema": c.SCHEMA, "ok": False, "error": {"code": "lease_lost"}}
    with pytest.raises(c.BatchResultFailure) as error:
        c.decode_reply("read_batch_result_execution", request, reply)
    assert error.value.code == "lease_lost"

    with pytest.raises(c.BatchResultFailure) as error:
        c.decode_reply(
            "read_batch_result_execution",
            request,
            {**reply, "error": {"code": "lease_lost", "detail": "raw"}},
        )
    assert error.value.code == "invalid_response"


def test_business_failure_codes_are_strings_and_receipts_are_closed():
    failure = c.FinishBatchResultFailureRequest(
        **lease_values(),
        ordinal=1,
        failed_prediction_id=None,
        error_code="inference_timeout",
        context={"retryable": False},
    )
    assert failure.error_code == "inference_timeout"

    receipt = c.Receipt(
        id=uuid4(),
        created_at="2026-10-20T00:00:00Z",
        tool_unit_id="unit-1",
    )
    assert receipt.tool_unit_id == "unit-1"
    with pytest.raises(ValidationError):
        c.Receipt.model_validate(
            {
                "id": uuid4(),
                "created_at": "2026-10-20T00:00:00Z",
                "tool_unit_id": "unit-1",
                "raw_body": "not a receipt field",
            }
        )
