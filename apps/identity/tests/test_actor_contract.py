from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_identity.actor_contract import (
    ACTOR_PROTOCOL,
    ActorContext,
    ActorContextRequest,
    actor_failure_reply,
    actor_reply,
    decode_actor_reply,
    decode_actor_request,
)
from aap_identity.contract import IdentityFailure, IdentityUnavailable


def _request(actor_id: uuid.UUID | None = None, role="employee"):
    return {
        "schema": ACTOR_PROTOCOL,
        "actor_id": str(actor_id or uuid.uuid4()),
        "admitted_role": role,
    }


def _actor(actor_id: uuid.UUID, role="employee", **changes):
    values = {
        "id": actor_id,
        "name": "  A\u030a\U0001f600  ",
        "email": " actor@example.test ",
        "role": role,
        "is_active": True,
        "weekly_target_default": None,
    }
    values.update(changes)
    return ActorContext(**values)


def test_request_and_reply_are_closed_and_bind_current_actor_facts():
    actor_id = uuid.uuid4()
    request = decode_actor_request(_request(actor_id, "project_admin"))
    assert request == ActorContextRequest(
        schema=ACTOR_PROTOCOL, actor_id=actor_id, admitted_role="project_admin"
    )

    actor = _actor(actor_id, "project_admin", weekly_target_default=-(2**31))
    reply = actor_reply(actor)
    assert set(reply) == {"schema", "ok", "data"}
    assert decode_actor_reply(reply, request) == actor
    assert reply["data"]["name"] == actor.name
    assert reply["data"]["email"] == actor.email

    with pytest.raises(ValidationError):
        ActorContext.model_validate_json(
            json.dumps({**reply["data"], "avatar_ref": None})
        )
    with pytest.raises(ValidationError):
        ActorContext.model_validate_json(
            json.dumps({**reply["data"], "weekly_target_default": 2**31})
        )
    with pytest.raises(ValidationError):
        ActorContext.model_validate_json(
            json.dumps({**reply["data"], "weekly_target_default": True})
        )


@pytest.mark.parametrize(
    "payload",
    [
        {**_request(), "extra": "field"},
        {
            "protocol": ACTOR_PROTOCOL,
            "actor_id": str(uuid.uuid4()),
            "admitted_role": "employee",
        },
        {**_request(), "admitted_role": "reviewer"},
        {**_request(), "actor_id": True},
        {**_request(), "schema": "aap-identity-other/v1"},
    ],
)
def test_request_rejects_aliases_extra_fields_invalid_roles_and_types(payload):
    with pytest.raises(IdentityFailure) as error:
        decode_actor_request(payload)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"aap-identity-actor/v1","schema":"aap-identity-actor/v1",'
        b'"actor_id":"8b24f2f0-4e18-4a31-9f42-a716ad2a0001","admitted_role":"employee"}',
        b'{"schema":"aap-identity-actor/v1","actor_id":"8b24f2f0-4e18-4a31-9f42-a716ad2a0001",'
        b'"admitted_role":"employee","ignored":NaN}',
    ],
)
def test_request_rejects_duplicate_json_fields_and_nonfinite_values(payload):
    with pytest.raises(IdentityFailure) as error:
        decode_actor_request(payload)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "changes",
    [
        {"id": uuid.uuid4()},
        {"role": "viewer"},
        {"is_active": False},
        {"is_active": 1},
        {"weekly_target_default": 2**31},
        {"weekly_target_default": True},
        {"extra": "profile data"},
    ],
)
def test_reply_rejects_unbound_or_malformed_actor_context(changes):
    actor_id = uuid.uuid4()
    request = decode_actor_request(_request(actor_id, "employee"))
    payload = actor_reply(_actor(actor_id))
    payload["data"].update(changes)
    with pytest.raises(IdentityUnavailable):
        decode_actor_reply(payload, request)


def test_reply_rejects_nonboolean_success_and_duplicate_fields():
    actor_id = uuid.uuid4()
    request = decode_actor_request(_request(actor_id))
    with pytest.raises(IdentityUnavailable):
        decode_actor_reply(
            {
                "schema": ACTOR_PROTOCOL,
                "ok": 1,
                "data": actor_reply(_actor(actor_id))["data"],
            },
            request,
        )
    duplicate = (
        '{"schema":"aap-identity-actor/v1","ok":true,"ok":true,'
        '"data":{"id":"%s","name":"N","email":"E","role":"employee",'
        '"is_active":true,"weekly_target_default":null}}'
    ) % actor_id
    with pytest.raises(IdentityUnavailable):
        decode_actor_reply(duplicate.encode(), request)


@pytest.mark.parametrize("code", ["invalid_request", "credential_invalid"])
def test_typed_failure_codes_round_trip(code):
    request = decode_actor_request(_request())
    payload = actor_failure_reply(code)
    with pytest.raises(IdentityFailure) as error:
        decode_actor_reply(payload, request)
    assert error.value.code == code


def test_failure_reply_rejects_unknown_codes_and_protocol_alias():
    request = decode_actor_request(_request())
    for payload in (
        {"schema": ACTOR_PROTOCOL, "ok": False, "error": {"code": "user_inactive"}},
        {
            "protocol": ACTOR_PROTOCOL,
            "ok": False,
            "error": {"code": "credential_invalid"},
        },
    ):
        with pytest.raises(IdentityUnavailable):
            decode_actor_reply(payload, request)


def test_reply_requires_a_typed_request_binding():
    with pytest.raises(IdentityUnavailable):
        decode_actor_reply({}, {})
