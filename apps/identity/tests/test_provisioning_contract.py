from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.provisioning_contract import (
    PROVISIONING_PROTOCOL,
    ProvisioningCreateRequest,
    ProvisioningLookupRequest,
    ProvisioningReply,
    decode_provisioning_reply,
)

_UNSET = object()


def _lookup(email="admin"):
    return ProvisioningLookupRequest(schema=PROVISIONING_PROTOCOL, email=email)


def _create(
    *,
    mode="bootstrap_admin",
    account_id=None,
    email="admin@example.test",
    name="  Admin ",
    password_hash="bcrypt-hash",
    now=_UNSET,
    request_id=None,
):
    return ProvisioningCreateRequest(
        schema=PROVISIONING_PROTOCOL,
        mode=mode,
        account_id=account_id or uuid.uuid4(),
        email=email,
        name=name,
        password_hash=password_hash,
        now=datetime(2026, 10, 21, tzinfo=UTC) if now is _UNSET else now,
        request_id=request_id,
    )


def _reply(email: str, account_id: uuid.UUID | None) -> dict:
    return ProvisioningReply(
        schema=PROVISIONING_PROTOCOL, email=email, account_id=account_id
    ).model_dump(mode="json", by_alias=True)


def test_lookup_request_is_closed_strict_and_does_not_normalize_email():
    lookup = _lookup("  Admin@Example.Test  ")
    assert lookup.email == "  Admin@Example.Test  "
    assert lookup.model_dump(mode="json", by_alias=True) == {
        "schema": PROVISIONING_PROTOCOL,
        "email": "  Admin@Example.Test  ",
    }
    for raw in (
        {"schema": PROVISIONING_PROTOCOL, "email": "admin", "extra": True},
        {"protocol": PROVISIONING_PROTOCOL, "email": "admin"},
        {"schema": PROVISIONING_PROTOCOL, "email": 1},
    ):
        with pytest.raises(ValidationError):
            ProvisioningLookupRequest.model_validate_json(json.dumps(raw))


def test_create_requires_every_field_including_nullable_values():
    account_id = uuid.uuid4()
    raw = {
        "schema": PROVISIONING_PROTOCOL,
        "mode": "import_admin",
        "account_id": str(account_id),
        "email": "admin",
        "name": "超级管理员",
        "password_hash": "opaque bcrypt output",
        "now": None,
        "request_id": None,
    }
    request = ProvisioningCreateRequest.model_validate_json(json.dumps(raw))
    assert request.account_id == account_id
    assert request.now is None and request.request_id is None
    for missing in raw:
        with pytest.raises(ValidationError):
            ProvisioningCreateRequest.model_validate_json(
                json.dumps({key: value for key, value in raw.items() if key != missing})
            )


def test_create_modes_preserve_exact_legacy_import_fields_and_aware_time():
    now = datetime(2026, 10, 21, 12, 30, tzinfo=UTC)
    bootstrap = _create(
        email="  MixedCase@Example.test ",
        name="  Raw \u540d\u79f0\U0001f600  ",
        now=now,
        request_id=" request-1 ",
    )
    assert bootstrap.email == "  MixedCase@Example.test "
    assert bootstrap.name == "  Raw \u540d\u79f0\U0001f600  "
    assert bootstrap.now == now
    assert bootstrap.request_id == " request-1 "
    assert "bcrypt-hash" not in repr(bootstrap)
    assert (
        ProvisioningCreateRequest.model_validate_json(
            bootstrap.model_dump_json(by_alias=True)
        ).now
        == now
    )

    imported = _create(
        mode="import_admin",
        email="admin",
        name="超级管理员",
        now=None,
        request_id=None,
    )
    assert imported.mode == "import_admin"
    assert imported.now is None and imported.request_id is None

    create_fields = {
        "schema": PROVISIONING_PROTOCOL,
        "mode": "import_admin",
        "account_id": str(uuid.uuid4()),
        "email": "admin",
        "name": "超级管理员",
        "password_hash": "hash",
        "now": None,
        "request_id": None,
    }
    invalid = (
        {**create_fields, "mode": "bootstrap_admin"},
        {**create_fields, "email": "Admin"},
        {**create_fields, "name": "wrong"},
        {**create_fields, "now": now.isoformat()},
        {**create_fields, "request_id": "id"},
    )
    for request in invalid:
        with pytest.raises(ValidationError):
            ProvisioningCreateRequest.model_validate_json(json.dumps(request))

    naive = {
        **imported.model_dump(mode="json", by_alias=True),
        "mode": "bootstrap_admin",
        "email": "admin",
        "name": "超级管理员",
        "now": "2026-10-21T12:30:00",
    }
    with pytest.raises(ValidationError):
        ProvisioningCreateRequest.model_validate_json(json.dumps(naive))


def test_create_has_no_new_text_length_caps_or_hash_repr():
    oversized = "x" * 10000
    secret_hash = "hash-secret-" + oversized
    request = _create(email=oversized, name=oversized, password_hash=secret_hash)
    assert request.email == oversized
    assert request.name == oversized
    assert request.password_hash == secret_hash
    assert secret_hash not in repr(request)


def test_reply_is_closed_and_lookup_can_return_missing_or_existing_id():
    request = _lookup("exact@example.test")
    assert decode_provisioning_reply(_reply(request.email, None), request) is None
    account_id = uuid.uuid4()
    assert (
        decode_provisioning_reply(_reply(request.email, account_id), request)
        == account_id
    )
    with pytest.raises(IdentityUnavailable):
        decode_provisioning_reply(
            {**_reply(request.email, None), "password_hash": "must not be returned"},
            request,
        )


def test_create_reply_must_bind_email_and_requested_account_id():
    request = _create()
    assert decode_provisioning_reply(
        _reply(request.email, request.account_id), request
    ) == (request.account_id)
    for reply in (
        _reply("other@example.test", request.account_id),
        _reply(request.email, None),
        _reply(request.email, uuid.uuid4()),
    ):
        with pytest.raises(IdentityUnavailable):
            decode_provisioning_reply(reply, request)


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": PROVISIONING_PROTOCOL, "email": "admin", "account_id": None},
        {"schema": "aap-identity-other/v1", "email": "admin", "account_id": None},
        {
            "schema": PROVISIONING_PROTOCOL,
            "email": "admin",
            "account_id": None,
            "extra": 1,
        },
        b'{"schema":"aap-identity-provisioning/v1","email":"admin","email":"other",'
        b'"account_id":null}',
        b'{"schema":"aap-identity-provisioning/v1","email":"admin","account_id":null,"extra":NaN}',
    ],
)
def test_reply_rejects_alias_duplicates_nonfinite_and_extra_fields(payload):
    with pytest.raises(IdentityUnavailable):
        decode_provisioning_reply(payload, _lookup())


def test_reply_rejects_deep_malformed_json():
    nested = b"[" * 3000 + b"0" + b"]" * 3000
    payload = (
        b'{"schema":"aap-identity-provisioning/v1","email":"admin",'
        b'"account_id":null,"extra":' + nested + b"}"
    )
    with pytest.raises(IdentityUnavailable):
        decode_provisioning_reply(payload, _lookup())
