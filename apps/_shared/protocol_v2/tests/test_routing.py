from __future__ import annotations

import base64
import json
import time
from uuid import uuid4

import jwt
import pytest

from aap_protocol_v2.routing import (
    ROUTE_ADMISSION_AUDIENCE,
    ROUTE_ADMISSION_PROTOCOL,
    ROUTE_CONTROL_PROTOCOL,
    RoutingProtocolError,
    decode_backend_route_control_command,
    decode_backend_route_control_reply,
    decode_route_admission_token,
)


def _control_body(*, action: str, signing_key: bytes | None = None) -> bytes:
    value = {
        "schema": ROUTE_CONTROL_PROTOCOL,
        "command_id": str(uuid4()),
        "registry_id": str(uuid4()),
        "authority_epoch": str(uuid4()),
        "retired_epoch": None,
        "challenge": "a" * 64,
        "action": action,
        "signing_key": (
            None
            if signing_key is None
            else base64.urlsafe_b64encode(signing_key).decode("ascii").rstrip("=")
        ),
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _claims(**overrides):
    value = {
        "schema": ROUTE_ADMISSION_PROTOCOL,
        "aud": ROUTE_ADMISSION_AUDIENCE,
        "registry_id": str(uuid4()),
        "backend_incarnation": str(uuid4()),
        "authority_epoch": str(uuid4()),
        "scope_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "operation": "predict",
        "method": "POST",
        "path": "/predict",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    value.update(overrides)
    return value


def test_route_admission_token_requires_exact_signed_claim_contract():
    key = b"k" * 32
    token = jwt.encode(_claims(), key, algorithm="HS256", headers={"typ": "JWT"})

    claims = decode_route_admission_token(token, key)

    assert claims.method == "POST"
    assert claims.path == "/predict"

    extra_claim = jwt.encode(
        _claims(untrusted=True), key, algorithm="HS256", headers={"typ": "JWT"}
    )
    with pytest.raises(RoutingProtocolError):
        decode_route_admission_token(extra_claim, key)


def test_route_control_request_rejects_duplicate_nonfinite_and_mismatched_keys():
    valid = _control_body(action="drain")
    parsed = decode_backend_route_control_command(valid)
    assert parsed.action == "drain"
    assert parsed.signing_key is None

    duplicate = valid[:-1] + b',"action":"bind"}'
    with pytest.raises(RoutingProtocolError):
        decode_backend_route_control_command(duplicate)
    with pytest.raises(RoutingProtocolError):
        decode_backend_route_control_command(valid[:-1] + b',"extra":NaN}')
    with pytest.raises(RoutingProtocolError):
        decode_backend_route_control_command(_control_body(action="bind"))


def test_bind_control_key_and_backend_reply_are_closed_and_typed():
    bind_body = json.loads(_control_body(action="bind", signing_key=b"s" * 32))
    bind_body["signing_key"] += "="
    command = decode_backend_route_control_command(
        json.dumps(bind_body, separators=(",", ":")).encode("utf-8")
    )
    assert command.signing_key == b"s" * 32
    assert "signing_key" not in repr(command)

    reply = {
        "schema": "aap-backend-route-control-reply/v1",
        "command_id": str(uuid4()),
        "registry_id": str(uuid4()),
        "authority_epoch": str(uuid4()),
        "backend_incarnation": str(uuid4()),
        "challenge": "b" * 64,
        "phase": "bound",
        "active_requests": 3,
    }
    assert decode_backend_route_control_reply(reply).active_requests == 3
    reply["phase"] = "drained"
    with pytest.raises(RoutingProtocolError):
        decode_backend_route_control_reply(reply)


def test_uuid_and_timestamp_claims_are_strict():
    key = b"x" * 32
    claims = _claims(registry_id=str(uuid4()).upper())
    token = jwt.encode(claims, key, algorithm="HS256", headers={"typ": "JWT"})
    with pytest.raises(RoutingProtocolError):
        decode_route_admission_token(token, key)

    claims = _claims(iat=True)
    token = jwt.encode(claims, key, algorithm="HS256", headers={"typ": "JWT"})
    with pytest.raises(RoutingProtocolError):
        decode_route_admission_token(token, key)
