from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_workspace.owner_transfer_contract import (
    OWNER_TRANSFER_PROTOCOL,
    OwnerTransferError,
    OwnerTransferFailure,
    OwnerTransferInvalidRequest,
    OwnerTransferRequest,
    OwnerTransferResult,
    OwnerTransferSuccess,
    OwnerTransferUnavailable,
    decode_owner_transfer_policy,
    decode_owner_transfer_reply,
    decode_owner_transfer_request,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_NEW_OWNER_ID = uuid.UUID("b6cdd33b-8bca-4a4c-bd0b-846dc8ca6be5")
_PREVIOUS_OWNER_ID = uuid.UUID("16a512e8-7084-4ad6-b6b8-f83a8f4ade6c")
_NOW = datetime(2026, 10, 21, 12, 30, tzinfo=UTC)


def _request(**overrides):
    values = {
        "schema": OWNER_TRANSFER_PROTOCOL,
        "operation_id": _OPERATION_ID,
        "actor_id": _ACTOR_ID,
        "project_id": _PROJECT_ID,
        "new_owner_id": _NEW_OWNER_ID,
    }
    values.update(overrides)
    return OwnerTransferRequest(**values)


def _success(request: OwnerTransferRequest | None = None, *, changed=True):
    request = request or _request()
    previous = _PREVIOUS_OWNER_ID if changed else request.new_owner_id
    return OwnerTransferSuccess(
        schema=OWNER_TRANSFER_PROTOCOL,
        ok=True,
        operation_id=request.operation_id,
        actor_id=request.actor_id,
        project_id=request.project_id,
        new_owner_id=request.new_owner_id,
        data=OwnerTransferResult(
            previous_owner_id=previous,
            updated_at=_NOW,
            changed=changed,
        ),
    )


def test_request_has_exact_closed_uuid_only_shape_and_accepts_nil_uuids():
    request = _request(
        operation_id=uuid.UUID(int=0),
        actor_id=uuid.UUID(int=0),
        project_id=uuid.UUID(int=0),
        new_owner_id=uuid.UUID(int=0),
    )
    wire = request.model_dump(mode="json", by_alias=True)
    assert set(wire) == {
        "schema",
        "operation_id",
        "actor_id",
        "project_id",
        "new_owner_id",
    }
    assert wire["new_owner_id"] == str(uuid.UUID(int=0))
    assert decode_owner_transfer_request(wire) == request


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": OWNER_TRANSFER_PROTOCOL},
        {**_request().model_dump(mode="json", by_alias=True), "extra": None},
        b'{"schema":"aap-workspace-owner-transfer/v1","schema":"other"}',
        b'{"schema":"aap-workspace-owner-transfer/v1","operation_id":"x","bad":NaN}',
        b'{"schema":"aap-workspace-owner-transfer/v1","extra":'
        + b"[" * 300
        + b"0"
        + b"]" * 300
        + b"}",
    ],
)
def test_invalid_duplicate_nonfinite_extra_and_deep_requests_are_distinct(payload):
    with pytest.raises(OwnerTransferInvalidRequest):
        decode_owner_transfer_request(payload)


def test_forged_request_models_are_revalidated():
    forged = _request().model_copy(update={"protocol": "aap-other/v1"})
    with pytest.raises(OwnerTransferInvalidRequest):
        decode_owner_transfer_request(forged)

    success = _success().model_dump(mode="json", by_alias=True)
    with pytest.raises(OwnerTransferUnavailable):
        decode_owner_transfer_reply(forged, success)

    constructed = OwnerTransferRequest.model_construct(
        protocol="aap-other/v1",
        operation_id=_OPERATION_ID,
        actor_id=_ACTOR_ID,
        project_id=_PROJECT_ID,
        new_owner_id=_NEW_OWNER_ID,
    )
    with pytest.raises(OwnerTransferInvalidRequest):
        decode_owner_transfer_request(constructed)


def test_policy_reply_is_exact():
    assert (
        decode_owner_transfer_policy({"schema": OWNER_TRANSFER_PROTOCOL}).protocol
        == OWNER_TRANSFER_PROTOCOL
    )
    with pytest.raises(OwnerTransferUnavailable):
        decode_owner_transfer_policy(
            {"schema": OWNER_TRANSFER_PROTOCOL, "capabilities": []}
        )


def test_success_binds_all_ids_and_same_owner_changed_relation():
    request = _request()
    success = _success(request)
    decoded = decode_owner_transfer_reply(
        request, success.model_dump(mode="json", by_alias=True)
    )
    assert decoded.data.changed is True

    no_op = _success(request, changed=False)
    assert (
        decode_owner_transfer_reply(
            request, no_op.model_dump(mode="json", by_alias=True)
        ).data.changed
        is False
    )

    for key, value in (
        ("operation_id", str(uuid.uuid4())),
        ("actor_id", str(uuid.uuid4())),
        ("project_id", str(uuid.uuid4())),
        ("new_owner_id", str(uuid.uuid4())),
        ("ok", 1),
    ):
        reply = success.model_dump(mode="json", by_alias=True)
        reply[key] = value
        with pytest.raises(OwnerTransferUnavailable):
            decode_owner_transfer_reply(request, reply)

    inconsistent = success.model_dump(mode="json", by_alias=True)
    inconsistent["data"]["changed"] = False
    with pytest.raises(OwnerTransferUnavailable):
        decode_owner_transfer_reply(request, inconsistent)


def test_failure_has_no_extra_binding_fields_and_validates_status_and_rollback():
    request = _request()
    failure = OwnerTransferFailure(
        schema=OWNER_TRANSFER_PROTOCOL,
        ok=False,
        error=OwnerTransferError(
            status_code=409,
            detail={"reason": "project_busy"},
            rollback=False,
        ),
    )
    wire = failure.model_dump(mode="json", by_alias=True)
    assert set(wire) == {"schema", "ok", "error"}
    assert decode_owner_transfer_reply(request, wire).error.rollback is False

    for status_code in (399, 600, True):
        invalid = dict(wire)
        invalid["error"] = {**wire["error"], "status_code": status_code}
        with pytest.raises(OwnerTransferUnavailable):
            decode_owner_transfer_reply(request, invalid)

    invalid_bool = dict(wire)
    invalid_bool["error"] = {**wire["error"], "rollback": 1}
    with pytest.raises(OwnerTransferUnavailable):
        decode_owner_transfer_reply(request, invalid_bool)


def test_success_timestamp_must_be_aware_text():
    request = _request()
    wire = _success(request).model_dump(mode="json", by_alias=True)
    naive = json.loads(json.dumps(wire))
    naive["data"]["updated_at"] = "2026-10-21T12:30:00"
    with pytest.raises(OwnerTransferUnavailable):
        decode_owner_transfer_reply(request, naive)
    for value in (True, 1):
        bad = json.loads(json.dumps(wire))
        bad["data"]["updated_at"] = value
        with pytest.raises(OwnerTransferUnavailable):
            decode_owner_transfer_reply(request, bad)


def test_public_failure_error_is_json_value_and_status_limits_are_strict():
    with pytest.raises(ValidationError):
        OwnerTransferError(status_code=409, detail="busy", rollback=1)
