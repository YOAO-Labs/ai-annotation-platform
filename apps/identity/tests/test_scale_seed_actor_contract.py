from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.scale_seed_actor_contract import (
    SCALE_SEED_ACTOR_PROTOCOL,
    ScaleSeedActorReply,
    decode_scale_seed_actor_reply,
    scale_seed_actor_schema,
)


@pytest.mark.parametrize("kind", ["super_admin", "annotator"])
def test_reply_returns_optional_account_id_and_accepts_native_payload_forms(kind):
    account_id = uuid.uuid4()
    reply = {
        "schema": SCALE_SEED_ACTOR_PROTOCOL,
        "kind": kind,
        "account_id": str(account_id),
    }
    encoded = json.dumps(reply).encode()

    assert decode_scale_seed_actor_reply(reply, kind) == account_id
    assert decode_scale_seed_actor_reply(json.dumps(reply), kind) == account_id
    assert decode_scale_seed_actor_reply(memoryview(encoded), kind) == account_id
    assert decode_scale_seed_actor_reply(bytearray(encoded), kind) == account_id
    assert decode_scale_seed_actor_reply({**reply, "account_id": None}, kind) is None


def test_reply_model_is_closed_and_preserves_kind_binding():
    account_id = uuid.uuid4()
    reply = ScaleSeedActorReply(
        schema=SCALE_SEED_ACTOR_PROTOCOL,
        kind="annotator",
        account_id=account_id,
    )
    assert reply.account_id == account_id
    assert (
        decode_scale_seed_actor_reply(reply.model_dump_json(by_alias=True), "annotator")
        == account_id
    )

    with pytest.raises(ValidationError):
        ScaleSeedActorReply.model_validate(
            {
                "schema": SCALE_SEED_ACTOR_PROTOCOL,
                "kind": "annotator",
                "account_id": str(account_id),
                "extra": True,
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "aap-identity-other/v1", "kind": "super_admin", "account_id": None},
        {"schema": SCALE_SEED_ACTOR_PROTOCOL, "kind": "unknown", "account_id": None},
        {"schema": SCALE_SEED_ACTOR_PROTOCOL, "kind": "super_admin"},
        {
            "protocol": SCALE_SEED_ACTOR_PROTOCOL,
            "kind": "super_admin",
            "account_id": None,
        },
        {
            "schema": SCALE_SEED_ACTOR_PROTOCOL,
            "kind": "super_admin",
            "account_id": "not-a-uuid",
        },
        {
            "schema": SCALE_SEED_ACTOR_PROTOCOL,
            "kind": "super_admin",
            "account_id": None,
            "extra": 1,
        },
    ],
)
def test_reply_decoder_rejects_malformed_or_unrecognized_envelopes(payload):
    with pytest.raises(IdentityUnavailable):
        decode_scale_seed_actor_reply(payload, "super_admin")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"aap-identity-scale-seed-actors/v1","kind":"annotator","kind":"super_admin","account_id":null}',
        b'{"schema":"aap-identity-scale-seed-actors/v1","kind":"super_admin","account_id":null,"extra":NaN}',
        b'{"schema":"aap-identity-scale-seed-actors/v1","kind":"super_admin","account_id":null,"protocol":"aap-identity-scale-seed-actors/v1"}',
        b"\xff",
    ],
)
def test_reply_decoder_rejects_duplicate_nonfinite_alias_and_invalid_utf8(payload):
    with pytest.raises(IdentityUnavailable):
        decode_scale_seed_actor_reply(payload, "super_admin")


def test_reply_decoder_rejects_expected_kind_mismatch_and_invalid_expected_kind():
    payload = {
        "schema": SCALE_SEED_ACTOR_PROTOCOL,
        "kind": "annotator",
        "account_id": None,
    }
    with pytest.raises(IdentityUnavailable):
        decode_scale_seed_actor_reply(payload, "super_admin")
    with pytest.raises(IdentityUnavailable):
        decode_scale_seed_actor_reply(payload, "project_admin")  # type: ignore[arg-type]


def test_schema_records_fixed_signatures_and_same_statement_snapshot_requirement():
    schema = scale_seed_actor_schema()
    assert schema["$id"] == SCALE_SEED_ACTOR_PROTOCOL
    capabilities = schema["x-native-capabilities"]
    assert capabilities["postgres_schema"] == "aap_identity_v1"
    functions = capabilities["functions"]
    assert [function["signature"] for function in functions] == [
        "aap_identity_v1.read_scale_seed_super_admin()",
        "aap_identity_v1.read_scale_seed_annotator(uuid[])",
    ]
    assert functions[0]["arguments"] == []
    assert functions[1]["arguments"][0]["name"] == "p_candidate_ids"
    assert functions[1]["arguments"][0]["postgres_type"] == "uuid[]"
    assert "same SQL statement" in functions[1]["transaction_semantics"]
    assert "same SQL" in capabilities["transaction_boundary"]
