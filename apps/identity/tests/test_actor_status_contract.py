from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from aap_identity.contract import (
    IdentityUnavailable as LegacyUnavailable,
    Principal as LegacyPrincipal,
    decode_principal_reply as legacy_decode_principal_reply,
)
from aap_identity_admission.actor_status_contract import (
    ACTOR_STATUS_PROTOCOL,
    ActorStatus,
    ActorStatusRequest,
    actor_status_reply,
    decode_actor_status_reply,
    decode_actor_status_request,
)
from aap_identity_admission.contract import (
    IdentityFailure,
    IdentityUnavailable,
    Principal,
    decode_principal_reply,
)


def test_authentication_compatibility_path_reexports_identical_classes():
    assert LegacyUnavailable is IdentityUnavailable
    assert LegacyPrincipal is Principal
    assert legacy_decode_principal_reply is decode_principal_reply


def test_actor_status_contract_allows_inactive_actor_without_role_claim():
    actor_id = uuid.uuid4()
    request = ActorStatusRequest(schema=ACTOR_STATUS_PROTOCOL, actor_id=actor_id)
    actor = ActorStatus(actor_id=actor_id, is_active=False)

    assert (
        decode_actor_status_request(
            {"schema": ACTOR_STATUS_PROTOCOL, "actor_id": str(actor_id)}
        )
        == request
    )
    reply = actor_status_reply(actor)
    assert reply == {
        "schema": ACTOR_STATUS_PROTOCOL,
        "ok": True,
        "data": {"actor": {"actor_id": str(actor_id), "is_active": False}},
    }
    assert decode_actor_status_reply(reply, request) == actor
    assert "role" not in reply["data"]["actor"]


def test_actor_status_contract_preserves_absence_and_binds_present_actor():
    actor_id = uuid.uuid4()
    request = ActorStatusRequest(schema=ACTOR_STATUS_PROTOCOL, actor_id=actor_id)
    assert decode_actor_status_reply(actor_status_reply(None), request) is None
    with pytest.raises(IdentityUnavailable):
        decode_actor_status_reply(
            actor_status_reply(ActorStatus(actor_id=uuid.uuid4(), is_active=True)),
            request,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema": ACTOR_STATUS_PROTOCOL,
            "actor_id": str(uuid.uuid4()),
            "role": "viewer",
        },
        {"protocol": ACTOR_STATUS_PROTOCOL, "actor_id": str(uuid.uuid4())},
        {"schema": "aap-identity/v1", "actor_id": str(uuid.uuid4())},
        {"schema": ACTOR_STATUS_PROTOCOL, "actor_id": "not-a-uuid"},
        {"schema": ACTOR_STATUS_PROTOCOL, "actor_id": str(uuid.uuid4()), "extra": 1},
    ],
)
def test_actor_status_request_is_closed(payload):
    with pytest.raises(IdentityFailure) as error:
        decode_actor_status_request(payload)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "actor",
    [
        {"actor_id": str(uuid.uuid4()), "is_active": 1},
        {"actor_id": str(uuid.uuid4()), "is_active": "true"},
        {"actor_id": str(uuid.uuid4()), "is_active": True, "role": "viewer"},
        {"actor_id": str(uuid.uuid4())},
    ],
)
def test_actor_status_reply_rejects_wrong_types_and_extra_profile_fields(actor):
    request = ActorStatusRequest(
        schema=ACTOR_STATUS_PROTOCOL, actor_id=uuid.UUID(actor["actor_id"])
    )
    payload = {"schema": ACTOR_STATUS_PROTOCOL, "ok": True, "data": {"actor": actor}}
    with pytest.raises(IdentityUnavailable):
        decode_actor_status_reply(payload, request)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": ACTOR_STATUS_PROTOCOL, "ok": 1, "data": {"actor": None}},
        {
            "schema": ACTOR_STATUS_PROTOCOL,
            "ok": True,
            "data": {"actor": None},
            "extra": 1,
        },
        {
            "schema": ACTOR_STATUS_PROTOCOL,
            "protocol": ACTOR_STATUS_PROTOCOL,
            "ok": True,
            "data": {"actor": None},
        },
    ],
)
def test_actor_status_reply_rejects_malformed_closed_envelopes(payload):
    request = ActorStatusRequest(schema=ACTOR_STATUS_PROTOCOL, actor_id=uuid.uuid4())
    with pytest.raises(IdentityUnavailable):
        decode_actor_status_reply(payload, request)


def test_actor_status_request_rejects_duplicate_json_fields():
    with pytest.raises(IdentityFailure) as error:
        decode_actor_status_request(
            b'{"schema":"aap-identity-actor-status/v1",'
            b'"schema":"aap-identity-actor-status/v1",'
            b'"actor_id":"00000000-0000-0000-0000-000000000001"}'
        )
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "annotator"},
        {"generation": True},
        {"is_active": 1},
        {"credential_type": "unexpected"},
    ],
)
def test_moved_admission_contract_still_rejects_invalid_principal_fields(changes):
    actor_id = uuid.uuid4()
    principal = Principal(
        actor_id=actor_id,
        name="Actor",
        email="actor@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    ).model_dump(mode="json")
    principal.update(changes)
    payload = {"schema": "aap-identity/v1", "ok": True, "data": principal}
    with pytest.raises(IdentityUnavailable):
        decode_principal_reply(json.dumps(payload).encode())
