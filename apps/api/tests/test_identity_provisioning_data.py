"""The operational adapter preserves caller transactions and rejects owner faults."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import DBAPIError

from aap_identity.provisioning_contract import (
    PROVISIONING_PROTOCOL,
    ProvisioningCreateRequest,
    ProvisioningLookupRequest,
)
from app.services.identity_commands import IdentityCommandService
from app.services.identity_provisioning_data import IdentityProvisioningData


def _db(payload=None, *, autoflush=True):
    return SimpleNamespace(
        autoflush=autoflush,
        scalar=AsyncMock(return_value=payload),
        flush=AsyncMock(),
        get=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


def _create():
    return ProvisioningCreateRequest(
        schema=PROVISIONING_PROTOCOL,
        mode="bootstrap_admin",
        account_id=uuid.UUID(int=11),
        email="  Admin@Example.test ",
        name="  Raw Name 😀  ",
        password_hash="opaque-hash",
        now=datetime(2026, 10, 21, tzinfo=UTC),
        request_id=None,
    )


def _caller_owns_transaction(db):
    db.get.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize("autoflush", [True, False])
async def test_lookup_honors_no_autoflush_and_uses_bound_email(autoflush):
    raw = json.dumps(
        {"schema": PROVISIONING_PROTOCOL, "email": "admin", "account_id": None}
    )
    db = _db(memoryview(raw.encode()), autoflush=autoflush)
    request = ProvisioningLookupRequest(schema=PROVISIONING_PROTOCOL, email="admin")
    assert await IdentityProvisioningData(db).lookup_account(request) == raw.encode()
    assert db.flush.await_count == int(autoflush)
    query, params = db.scalar.await_args.args
    assert "aap_identity_v1.read_provisioning_account(" in str(query)
    assert params == {"email": "admin"}
    _caller_owns_transaction(db)


@pytest.mark.parametrize("autoflush", [True, False])
async def test_creator_flushes_pending_work_even_when_autoflush_disabled(autoflush):
    request = _create()
    db = _db({}, autoflush=autoflush)
    calls = []

    async def flush():
        calls.append("flush")

    async def scalar(query, params):
        calls.append("write")
        assert "aap_identity_v1.create_provisioning_account(" in str(query)
        assert "opaque-hash" not in str(query)
        assert json.loads(params["request"]) == request.model_dump(
            mode="json", by_alias=True
        )
        return {}

    db.flush.side_effect = flush
    db.scalar.side_effect = scalar
    await IdentityProvisioningData(db).create_account(request)
    assert calls == ["flush", "write"]
    _caller_owns_transaction(db)


@pytest.mark.parametrize("kind", ["lookup", "create", "flush"])
@pytest.mark.parametrize("sqlstate", ["23505", "23514", "42501"])
async def test_native_and_pending_flush_errors_propagate_without_recovery(
    kind, sqlstate
):
    original = RuntimeError("database rejected operation")
    original.sqlstate = sqlstate
    error = DBAPIError("native provisioning", {}, original, False)
    db = _db()
    if kind == "flush":
        db.flush.side_effect = error
    else:
        db.scalar.side_effect = error
    with pytest.raises(DBAPIError) as raised:
        if kind == "lookup":
            await IdentityProvisioningData(db).lookup_account(
                ProvisioningLookupRequest(schema=PROVISIONING_PROTOCOL, email="admin")
            )
        else:
            await IdentityProvisioningData(db).create_account(_create())
    assert raised.value is error
    if kind == "flush":
        db.scalar.assert_not_awaited()
    _caller_owns_transaction(db)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        b'{"schema":',
        {"schema": "wrong", "email": "admin", "account_id": None},
        {"schema": PROVISIONING_PROTOCOL, "email": "other", "account_id": None},
        {
            "schema": PROVISIONING_PROTOCOL,
            "email": "admin",
            "account_id": None,
            "password_hash": "unexpected",
        },
    ],
    ids=["missing", "malformed", "protocol", "binding", "secret-extra"],
)
async def test_command_adapter_maps_untrusted_lookup_to_503_without_fallback(payload):
    db = _db(payload)
    with pytest.raises(HTTPException) as rejected:
        await IdentityCommandService(db).ensure_import_admin(environment="test")
    assert (rejected.value.status_code, rejected.value.detail) == (
        503,
        "身份认证服务暂不可用",
    )
    assert db.scalar.await_count == 1
    _caller_owns_transaction(db)


async def test_create_reply_must_match_the_generated_account(monkeypatch):
    monkeypatch.setattr(
        "aap_identity.provisioning.hash_password", lambda _plain: "opaque-hash"
    )
    db = _db()

    async def scalar(query, params):
        if "read_provisioning_account" in str(query):
            return {
                "schema": PROVISIONING_PROTOCOL,
                "email": "admin@example.test",
                "account_id": None,
            }
        command = json.loads(params["request"])
        assert command["password_hash"] == "opaque-hash"
        return {
            "schema": PROVISIONING_PROTOCOL,
            "email": command["email"],
            "account_id": str(uuid.UUID(int=99)),
        }

    db.scalar.side_effect = scalar
    with pytest.raises(HTTPException) as rejected:
        await IdentityCommandService(db).create_bootstrap_super_admin(
            email="admin@example.test",
            name="Operator",
            password="fixture-secret",
            now=datetime(2026, 10, 21, tzinfo=UTC),
        )
    assert rejected.value.status_code == 503
    assert db.scalar.await_count == 2
    _caller_owns_transaction(db)
