from uuid import uuid4

import pytest

from aap_inference_contract.routing import RoutingUnavailable
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_DATA_PROTOCOL,
    decode_video_execution_status_result,
    decode_video_scope_data,
    video_execution_status_result_bytes,
)


def _retired_status():
    proof = {
        "job_id": str(uuid4()),
        "run_id": str(uuid4()),
        "worker_id": str(uuid4()),
        "generation": 2,
        "lease_token": str(uuid4()),
    }
    intent = {
        "job_id": proof["job_id"],
        "run_id": proof["run_id"],
        "generation": 1,
        "request_id": str(uuid4()),
        "command_sha256": "a" * 64,
        "scope_id": str(uuid4()),
        "state": "bound",
    }
    return proof, {
        "schema": VIDEO_EXECUTION_DATA_PROTOCOL,
        "status": "ok",
        "intent": intent,
        "scope": None,
    }


def test_retired_status_preserves_old_intent_without_returning_a_capability():
    proof, reply = _retired_status()
    data = decode_video_scope_data("status", proof, reply)
    assert data["scope"] is None
    assert data["intent"].state == "bound"
    assert str(data["intent"].scope_id) == reply["intent"]["scope_id"]
    wire = video_execution_status_result_bytes(proof, data)
    assert decode_video_execution_status_result(proof, wire) == data


@pytest.mark.parametrize("action", ["prepare", "reserve"])
def test_missing_bound_capability_cannot_authorize_a_reservation(action):
    proof, reply = _retired_status()
    proof["generation"] = reply["intent"]["generation"]
    arguments = {
        **proof,
        **{
            key: reply["intent"][key]
            for key in ("request_id", "scope_id", "command_sha256")
        },
    }
    with pytest.raises(RoutingUnavailable):
        decode_video_scope_data(action, arguments, reply)


@pytest.mark.parametrize("field", ["job_id", "run_id"])
def test_retired_status_still_rejects_a_different_execution(field):
    proof, reply = _retired_status()
    reply["intent"][field] = str(uuid4())
    with pytest.raises(RoutingUnavailable):
        decode_video_scope_data("status", proof, reply)
