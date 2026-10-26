"""Control RPCs keep UUID/tuple JSON interoperability without relaxing the wire."""

from __future__ import annotations

import uuid

import pytest

from aap_inference_contract.backend_control import (
    BACKEND_CONTROL_PROTOCOL,
    BackendControlReply,
    BackendRequestOnlyEndpoint,
    BackendRequestOnlyProbeRequest,
    backend_control_reply_bytes,
    backend_control_request_bytes,
    decode_backend_control_reply,
    decode_backend_control_request,
)


def _request():
    return BackendRequestOnlyProbeRequest(
        kind="request_only",
        schema=BACKEND_CONTROL_PROTOCOL,
        operation="environment_observation",
        request_id=uuid.uuid4(),
        endpoints=(
            BackendRequestOnlyEndpoint(url="https://one.example/base/"),
            BackendRequestOnlyEndpoint(url="https://two.example/"),
        ),
        timeout_seconds=2,
    )


def test_control_request_json_preserves_uuid_and_endpoint_array():
    request = _request()
    decoded = decode_backend_control_request(backend_control_request_bytes(request))
    assert decoded == request
    assert isinstance(decoded.request_id, uuid.UUID)
    assert isinstance(decoded.endpoints, tuple)


def test_control_reply_json_preserves_request_binding_and_observation_array():
    request = _request()
    reply = BackendControlReply(
        schema=BACKEND_CONTROL_PROTOCOL,
        request_id=request.request_id,
        result={"results": [{"url": "https://one.example/base/", "ok": True}]},
    )
    raw = backend_control_reply_bytes(reply)
    assert decode_backend_control_reply(request, raw) == reply
    with pytest.raises(ValueError):
        decode_backend_control_reply(_request(), raw)


@pytest.mark.parametrize("defect", ["duplicate_request", "nonfinite_timeout"])
def test_control_request_rejects_duplicate_or_nonfinite_json(defect):
    raw = backend_control_request_bytes(_request())
    if defect == "duplicate_request":
        raw = b'{"request_id":"duplicate",' + raw[1:]
    else:
        raw = raw.replace(b'"timeout_seconds":2', b'"timeout_seconds":NaN')
    with pytest.raises(ValueError):
        decode_backend_control_request(raw)
