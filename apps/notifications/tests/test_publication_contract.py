from __future__ import annotations

import json
from pathlib import Path
import tomllib
import uuid

import pytest

from aap_notifications.publication_contract import (
    IMMEDIATE_PUBLICATION_PROTOCOL,
    ImmediatePublicationRequest,
    ImmediatePublicationUnavailable,
    PublicationAuthorization,
    decode_publication_reply,
    decode_publication_request,
    encode_publication_request,
    notification_publication_json_schema,
    publication_allowed,
)


def _authorization(**overrides) -> PublicationAuthorization:
    values = {
        "restricted": True,
        "project_id": uuid.UUID("40000000-0000-4000-8000-000000000001"),
        "export_required": False,
        "member": False,
        "reviewer": False,
        "owner": False,
        "super_admin": False,
    }
    values.update(overrides)
    return PublicationAuthorization(**values)


def _event(payload):
    return {
        "id": "40000000-0000-4000-8000-000000000002",
        "type": "future.business.event",
        "target_type": "task",
        "target_id": "40000000-0000-4000-8000-000000000003",
        "payload": payload,
        "created_at": "2026-10-24T12:34:56.123456+08:00",
    }


def test_publication_authorization_matches_membership_export_owner_policy():
    assert publication_allowed(_authorization(member=True))
    assert publication_allowed(_authorization(owner=True, export_required=True))
    assert publication_allowed(_authorization(super_admin=True, export_required=True))
    assert not publication_allowed(_authorization(member=True, export_required=True))
    assert not publication_allowed(_authorization(project_id=None, member=True))
    assert publication_allowed(
        _authorization(restricted=False, project_id=None, export_required=True)
    )


@pytest.mark.parametrize(
    "payload", [None, "legacy scalar", 17, ["a", 2], {"nested": [True]}]
)
def test_request_codec_preserves_native_json_payload_shapes(payload):
    request = ImmediatePublicationRequest(
        schema=IMMEDIATE_PUBLICATION_PROTOCOL,
        user_id=uuid.UUID("40000000-0000-4000-8000-000000000004"),
        message=_event(payload),
        authorization=_authorization(member=True),
    )

    decoded = decode_publication_request(encode_publication_request(request))

    assert decoded.user_id == request.user_id
    assert decoded.message == request.message
    assert decoded.message["payload"] == payload
    assert decoded.authorization == request.authorization


def test_request_codec_keeps_deep_payloads_and_integer_values():
    payload: dict[str, object] = {"second": 2, "first": 2**100}
    for _ in range(350):
        payload = {"nested": payload}
    request = ImmediatePublicationRequest(
        schema=IMMEDIATE_PUBLICATION_PROTOCOL,
        user_id=uuid.UUID("40000000-0000-4000-8000-000000000004"),
        message=_event(payload),
        authorization=_authorization(member=True),
    )

    encoded = encode_publication_request(request)
    decoded = decode_publication_request(encoded)
    wire_message = json.loads(encoded)["message"]

    assert list(wire_message) == list(request.message)
    wire_payload = wire_message["payload"]
    for _ in range(350):
        wire_payload = wire_payload["nested"]
    assert list(wire_payload) == ["second", "first"]
    assert wire_payload["first"] == 2**100
    assert decoded.message == request.message


def test_sync_extensions_remain_open_and_require_null_authorization():
    request = ImmediatePublicationRequest(
        schema=IMMEDIATE_PUBLICATION_PROTOCOL,
        user_id=uuid.UUID("40000000-0000-4000-8000-000000000004"),
        message={
            **_event({"extension": 2**70}),
            "type": "notifications.sync",
            "reason": "refresh",
        },
        authorization=None,
    )

    decoded = decode_publication_request(encode_publication_request(request))

    assert decoded.message == request.message
    with pytest.raises(ImmediatePublicationUnavailable):
        decode_publication_request(
            {
                "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
                "user_id": str(request.user_id),
                "message": _event({}),
                "authorization": None,
            }
        )
    with pytest.raises(ImmediatePublicationUnavailable):
        decode_publication_request(
            {
                "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
                "user_id": str(request.user_id),
                "message": request.message,
                "authorization": _authorization(member=True).model_dump(mode="json"),
            }
        )


def test_open_event_type_may_equal_sync_marker_when_authorization_is_present():
    message = _event({"kind": "persisted-notification"})
    message["type"] = "notifications.sync"
    request = ImmediatePublicationRequest(
        schema=IMMEDIATE_PUBLICATION_PROTOCOL,
        user_id=uuid.UUID("40000000-0000-4000-8000-000000000004"),
        message=message,
        authorization=_authorization(member=True),
    )

    decoded = decode_publication_request(encode_publication_request(request))

    assert decoded.message == message
    assert decoded.authorization == request.authorization


def test_request_contract_never_accepts_a_caller_supplied_channel():
    raw = {
        "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
        "user_id": "40000000-0000-4000-8000-000000000004",
        "message": _event({}),
        "authorization": {
            "restricted": False,
            "project_id": None,
            "export_required": False,
            "member": False,
            "reviewer": False,
            "owner": False,
            "super_admin": False,
        },
        "channel": "notify:other-user",
    }

    with pytest.raises(ImmediatePublicationUnavailable):
        decode_publication_request(raw)


@pytest.mark.parametrize(
    "authorization",
    [
        {"restricted": 1},
        {"restricted": True, "unexpected": False},
        {"restricted": True},
    ],
)
def test_request_decoder_rejects_open_or_non_boolean_authorization(authorization):
    raw = {
        "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
        "user_id": "40000000-0000-4000-8000-000000000004",
        "message": _event({}),
        "authorization": authorization,
    }

    with pytest.raises(ImmediatePublicationUnavailable):
        decode_publication_request(raw)


@pytest.mark.parametrize(
    "reply",
    [
        {"schema": IMMEDIATE_PUBLICATION_PROTOCOL, "published": 1},
        {"schema": IMMEDIATE_PUBLICATION_PROTOCOL, "published": True, "extra": 1},
        {"schema": "wrong", "published": True},
    ],
)
def test_reply_decoder_requires_closed_schema_and_strict_boolean(reply):
    with pytest.raises(ImmediatePublicationUnavailable):
        decode_publication_reply(reply)


def test_publication_schema_and_checked_resource_match():
    root = Path(__file__).resolve().parents[1]
    schema_bytes = (root / "src/aap_notifications/publication.schema.json").read_bytes()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())

    schema = json.loads(schema_bytes)
    assert schema == notification_publication_json_schema()
    assert schema["$id"] == IMMEDIATE_PUBLICATION_PROTOCOL
    assert (
        schema["$defs"]["ImmediatePublicationRequest"]["properties"]["message"][
            "anyOf"
        ][0]["$ref"]
        == "#/$defs/NotificationEvent"
    )
    assert (
        schema["$defs"]["ImmediatePublicationRequest"]["properties"]["message"][
            "anyOf"
        ][1]["$ref"]
        == "#/$defs/NotificationSyncEvent"
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"][
        "src/aap_notifications/publication.schema.json"
    ] == ("aap_notifications/publication.schema.json")
    assert (
        "starlette>=1.0.0" in pyproject["project"]["optional-dependencies"]["runtime"]
    )
    assert (
        "uvicorn[standard]>=0.46.0"
        in pyproject["project"]["optional-dependencies"]["runtime"]
    )
    assert pyproject["project"]["optional-dependencies"]["publication-client"] == [
        "httpx>=0.28.1"
    ]
