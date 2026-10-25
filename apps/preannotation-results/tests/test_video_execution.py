from datetime import datetime, timezone
from uuid import uuid4

import pytest

from aap_preannotation_results import contract as c
from aap_preannotation_results.video_execution import (
    SCHEMA,
    ClaimRequest,
    ExecutionClaim,
    ExecutionProof,
    GuardRequest,
    RenewRequest,
    VideoExecutionFailure,
    decode_native_reply,
)


def _claim():
    return ExecutionClaim(
        job_id=uuid4(),
        run_id=uuid4(),
        worker_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(timezone.utc),
        state="ready",
    )


def test_private_claim_reply_is_bound_to_exact_execution():
    claim = _claim()
    request = RenewRequest(**claim.proof().model_dump())
    reply = {
        "schema": SCHEMA,
        "status": "ok",
        "execution": claim.model_dump(mode="json"),
    }
    assert (
        decode_native_reply("renew", request, c.json_bytes(reply))["execution"] == claim
    )
    for field, changed in {
        "job_id": str(uuid4()),
        "run_id": str(uuid4()),
        "worker_id": str(uuid4()),
        "generation": 2,
        "lease_token": str(uuid4()),
    }.items():
        wrong = {**reply, "execution": {**reply["execution"], field: changed}}
        with pytest.raises(VideoExecutionFailure, match="^invalid_response$"):
            decode_native_reply("renew", request, wrong)


def test_claim_does_not_echo_private_values_in_repr_or_validation_errors():
    claim = _claim()
    assert str(claim.lease_token) not in repr(claim)
    assert str(claim.lease_token) not in repr(claim.proof())
    with pytest.raises(ValueError) as failure:
        ExecutionProof.model_validate(
            {**claim.proof().model_dump(), "generation": True}
        )
    assert str(claim.lease_token) not in str(failure.value)


@pytest.mark.parametrize("generation", [True, 1.0, "1", 0, 1 << 63])
def test_capability_generation_cannot_be_coerced(generation):
    with pytest.raises(ValueError):
        ExecutionProof.model_validate(
            {**_claim().proof().model_dump(), "generation": generation}
        )


def test_initial_claim_cannot_return_another_workers_capability():
    claim = _claim()
    request = ClaimRequest(job_id=claim.job_id, worker_id=uuid4())
    with pytest.raises(VideoExecutionFailure, match="^invalid_response$"):
        decode_native_reply(
            "claim",
            request,
            {
                "schema": SCHEMA,
                "status": "ok",
                "execution": claim.model_dump(mode="json"),
            },
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"aap-video-execution-data/v1","status":"ok","guarded":1}',
        b'{"schema":"aap-video-execution-data/v1","status":"ok","guarded":true,"guarded":true}',
        b'{"schema":"aap-video-execution-data/v1","status":"ok","guarded":true,"private":null}',
        b'{"schema":"aap-video-execution-data/v1","status":"rejected","reason":"raw private error"}',
        b'{"schema":"aap-video-execution-data/v1","status":"ok","guarded":NaN}',
    ],
)
def test_guard_rejects_ambiguous_or_open_envelopes(raw):
    request = GuardRequest(**_claim().proof().model_dump(), purpose="stage")
    with pytest.raises(VideoExecutionFailure, match="^invalid_response$"):
        decode_native_reply("guard", request, raw)


def test_lost_generation_has_closed_error():
    request = GuardRequest(**_claim().proof().model_dump(), purpose="fail")
    with pytest.raises(VideoExecutionFailure, match="^lease_lost$"):
        decode_native_reply(
            "guard",
            request,
            {
                "schema": SCHEMA,
                "status": "rejected",
                "reason": "lease_lost",
            },
        )
