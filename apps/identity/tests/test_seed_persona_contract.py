from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityFailure, IdentityUnavailable
from aap_identity.seed_persona_contract import (
    IDENTITY_SCHEMA,
    SEED_PERSONA_PROTOCOL,
    SeedPersonaReply,
    SeedPersonaRequest,
    decode_seed_persona_reply,
    decode_seed_persona_request,
    seed_persona_schema,
)


def _request(
    *,
    account_id: uuid.UUID | None = None,
    email: str = "employee@example.test",
    name: str = " 标注用户 ",
    password_hash: str = "bcrypt-secret",
    role: str = "employee",
    group_name: str | None = "标注组A",
) -> SeedPersonaRequest:
    return SeedPersonaRequest(
        schema=SEED_PERSONA_PROTOCOL,
        account_id=account_id or uuid.uuid4(),
        email=email,
        name=name,
        password_hash=password_hash,
        role=role,
        group_name=group_name,
    )


def _reply(request: SeedPersonaRequest, *, email: str | None = None) -> dict:
    return SeedPersonaReply(
        schema=SEED_PERSONA_PROTOCOL,
        email=request.email if email is None else email,
        account_id=request.account_id,
    ).model_dump(mode="json", by_alias=True)


def test_request_preserves_seed_fields_and_hides_password_hash():
    request = _request()
    assert request.email == "employee@example.test"
    assert request.name == " 标注用户 "
    assert request.group_name == "标注组A"
    assert "bcrypt-secret" not in repr(request)
    assert (
        decode_seed_persona_request(request.model_dump(mode="json", by_alias=True))
        == request
    )


@pytest.mark.parametrize(
    ("role", "group_name"),
    [
        ("super_admin", None),
        ("project_admin", "管理组"),
        ("employee", "标注组B"),
        ("viewer", None),
    ],
)
def test_request_accepts_all_seed_platform_roles(role, group_name):
    request = _request(role=role, group_name=group_name)
    assert request.role == role
    assert request.group_name == group_name


def test_request_is_closed_strict_and_requires_nullable_group_name():
    raw = {
        "schema": SEED_PERSONA_PROTOCOL,
        "account_id": str(uuid.uuid4()),
        "email": "persona@example.test",
        "name": "Persona",
        "password_hash": "opaque hash",
        "role": "employee",
        "group_name": None,
    }
    assert SeedPersonaRequest.model_validate_json(json.dumps(raw)).group_name is None
    for missing in raw:
        with pytest.raises(ValidationError):
            SeedPersonaRequest.model_validate_json(
                json.dumps({key: value for key, value in raw.items() if key != missing})
            )

    for invalid in (
        {**raw, "role": "annotator"},
        {**raw, "role": 1},
        {**raw, "email": 1},
        {**raw, "group_name": 1},
        {**raw, "unexpected": True},
        {
            "protocol": SEED_PERSONA_PROTOCOL,
            **{k: v for k, v in raw.items() if k != "schema"},
        },
    ):
        with pytest.raises(ValidationError):
            SeedPersonaRequest.model_validate_json(json.dumps(invalid))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"aap-identity-seed-persona/v1","account_id":"00000000-0000-0000-0000-000000000001","account_id":"00000000-0000-0000-0000-000000000002","email":"x","name":"x","password_hash":"x","role":"employee","group_name":null}',
        b'{"schema":"aap-identity-seed-persona/v1","account_id":"00000000-0000-0000-0000-000000000001","email":"x","name":"x","password_hash":"x","role":"employee","group_name":null,"extra":NaN}',
        b'{"schema":"aap-identity-seed-persona/v1","account_id":"00000000-0000-0000-0000-000000000001","email":"x","name":"x","password_hash":"x","role":"employee","group_name":null,"protocol":"aap-identity-seed-persona/v1"}',
    ],
)
def test_request_decoder_rejects_duplicate_nonfinite_and_protocol_alias(payload):
    with pytest.raises(IdentityFailure, match="invalid_request"):
        decode_seed_persona_request(payload)


def test_reply_is_exact_and_bound_to_request_email_and_account_id():
    request = _request()
    assert decode_seed_persona_reply(_reply(request), request) == request.account_id

    other_id = SeedPersonaReply(
        schema=SEED_PERSONA_PROTOCOL,
        email=request.email,
        account_id=uuid.uuid4(),
    ).model_dump(mode="json", by_alias=True)
    for invalid in (
        _reply(request, email="other@example.test"),
        {**_reply(request), "account_id": str(uuid.uuid4())},
        {**_reply(request), "extra": "must not escape"},
        {**_reply(request), "password_hash": "must not be returned"},
        {
            "protocol": SEED_PERSONA_PROTOCOL,
            **{k: v for k, v in _reply(request).items() if k != "schema"},
        },
        other_id,
    ):
        with pytest.raises(IdentityUnavailable):
            decode_seed_persona_reply(invalid, request)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"aap-identity-seed-persona/v1","email":"x","email":"y","account_id":"00000000-0000-0000-0000-000000000001"}',
        b'{"schema":"aap-identity-seed-persona/v1","email":"x","account_id":"00000000-0000-0000-0000-000000000001","extra":Infinity}',
        b'{"schema":"aap-identity-seed-persona/v1","email":"x","account_id":"00000000-0000-0000-0000-000000000001","protocol":"aap-identity-seed-persona/v1"}',
    ],
)
def test_reply_decoder_rejects_duplicate_nonfinite_and_protocol_alias(payload):
    with pytest.raises(IdentityUnavailable):
        decode_seed_persona_reply(payload, _request(email="x"))


def test_reply_decoder_revalidates_typed_request_before_binding():
    request = _request()
    forged = request.model_copy(update={"protocol": "aap-identity-other/v1"})
    with pytest.raises(IdentityUnavailable):
        decode_seed_persona_reply(_reply(request), forged)


def test_schema_describes_only_the_fixed_caller_transaction_function():
    schema = seed_persona_schema()
    assert schema["$id"] == SEED_PERSONA_PROTOCOL
    capabilities = schema["x-native-capabilities"]
    assert capabilities["postgres_schema"] == IDENTITY_SCHEMA
    assert [item["signature"] for item in capabilities["functions"]] == [
        "aap_identity_v1.create_seed_persona_account(jsonb)"
    ]
    function = capabilities["functions"][0]
    assert function["result_postgres_type"] == "jsonb"
    assert "insert-only" in function["transaction_semantics"]
    assert "caller owns" in capabilities["transaction_boundary"].lower()
