from __future__ import annotations

import json
import uuid

import pytest

from aap_identity.account_contract import decode_account_reply, decode_account_request
from aap_identity.contract import (
    IdentityUnavailable,
    decode_principal_reply,
    principal_reply,
)
from aap_identity.key_contract import decode_key_request
from aap_identity.presence_contract import (
    PRESENCE_PROTOCOL,
    decode_presence_reply,
    decode_presence_request,
)
from aap_identity.session_contract import decode_session_request

from account_helpers import account_record, account_request
from session_helpers import principal, session_request


@pytest.fixture(
    params=[
        "admission_reply",
        "key_request",
        "session_request",
        "account_request",
        "account_reply",
        "presence_request",
        "presence_reply",
    ]
)
def wire_message(request):
    actor = principal()
    command = account_request("read_self_account")
    messages = {
        "admission_reply": (decode_principal_reply, principal_reply(actor)),
        "key_request": (
            decode_key_request,
            {
                "schema": "aap-identity-keys/v1",
                "operation": "list_api_keys",
                "operation_id": str(uuid.uuid4()),
                "token": "fixture-bearer",
                "payload": {},
            },
        ),
        "session_request": (
            decode_session_request,
            session_request("logout").model_dump(mode="json", by_alias=True),
        ),
        "account_request": (
            decode_account_request,
            command.model_dump(mode="json", by_alias=True),
        ),
        "account_reply": (
            lambda value: decode_account_reply(
                command, value, expected_principal=actor
            ),
            {
                "schema": "aap-identity-accounts/v1",
                "ok": True,
                "operation": command.operation,
                "operation_id": str(command.operation_id),
                "principal": actor.model_dump(mode="json"),
                "data": {
                    "account": account_record().model_dump(mode="json"),
                    "preferences": None,
                    "previous_avatar_ref": None,
                    "changed": None,
                },
            },
        ),
        "presence_request": (
            decode_presence_request,
            {"schema": PRESENCE_PROTOCOL, "cutoff": "2026-10-20T01:02:03Z"},
        ),
        "presence_reply": (
            decode_presence_reply,
            {"schema": PRESENCE_PROTOCOL, "affected": 1},
        ),
    }
    return messages[request.param]


@pytest.mark.parametrize("encoded", [False, True], ids=["mapping", "json"])
@pytest.mark.parametrize(
    "retain_schema", [False, True], ids=["alias_only", "both_names"]
)
def test_wire_decoders_reject_internal_protocol_field(
    wire_message, encoded, retain_schema
):
    decode, original = wire_message

    def encode(value):
        return json.dumps(value).encode() if encoded else value

    decode(encode(original))
    malformed = {**original, "protocol": original["schema"]}
    if not retain_schema:
        del malformed["schema"]
    with pytest.raises((ValueError, IdentityUnavailable)):
        decode(encode(malformed))


def test_opaque_preference_values_keep_their_own_protocol_keys():
    patch = {"ai": {"params": {"protocol": "opaque", "schema": "future"}}}
    command = account_request("patch_preferences", payload={"patch": patch})
    assert command.payload.patch == patch
