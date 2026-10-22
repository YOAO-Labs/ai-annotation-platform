"""The account-lock adapter fails closed without owning its caller transaction."""

from __future__ import annotations

import json
import uuid
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from aap_identity.account_lock_contract import ACCOUNT_LOCK_PROTOCOL
from app.services.identity_account_lock_data import IdentityAccountLockData

_ACTOR_ID = uuid.UUID(int=11)


def _reply():
    return {
        "schema": ACCOUNT_LOCK_PROTOCOL,
        "accounts": [
            {
                "id": str(_ACTOR_ID),
                "name": "  Locked actor 😀 ",
                "email": "locked@example.test",
                "role": "historical-role",
                "is_active": False,
            }
        ],
    }


def _session(payload, *, autoflush=True):
    return SimpleNamespace(
        autoflush=autoflush,
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=payload),
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


def _caller_transaction_retained(db):
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("mode", ["share", "update", "key_share"])
async def test_account_values_preserve_raw_facts_and_caller_flush_policy(
    autoflush, mode
):
    db = _session(memoryview(json.dumps(_reply()).encode()), autoflush=autoflush)
    accounts = await IdentityAccountLockData(db).lock_accounts(
        [_ACTOR_ID, _ACTOR_ID, uuid.UUID(int=1)], mode=mode, nowait=False
    )
    actor = accounts[_ACTOR_ID]
    assert list(accounts) == [_ACTOR_ID]
    assert (actor.name, actor.role, actor.is_active, actor.weekly_target_default) == (
        "  Locked actor 😀 ",
        "historical-role",
        False,
        None,
    )
    with pytest.raises(FrozenInstanceError):
        actor.role = "super_admin"
    query, bindings = db.scalar.await_args.args
    assert f"aap_identity_v1.lock_accounts_{mode}(" in str(query)
    assert bindings == {
        "account_ids": [uuid.UUID(int=1), _ACTOR_ID],
        "nowait": False,
    }
    assert db.flush.await_count == int(autoflush)
    db.execute.assert_not_awaited()
    _caller_transaction_retained(db)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        b'{"schema":',
        {"schema": "wrong", "accounts": []},
        {**_reply(), "credential": "must-not-be-accepted"},
        {
            **_reply(),
            "accounts": [{**_reply()["accounts"][0], "id": str(uuid.UUID(int=99))}],
        },
        {
            **_reply(),
            "accounts": [{**_reply()["accounts"][0], "is_active": 1}],
        },
    ],
    ids=["missing", "malformed", "protocol", "extra", "wrong-binding", "bool"],
)
async def test_untrusted_owner_reply_is_503_without_read_fallback(payload):
    db = _session(payload)
    with pytest.raises(HTTPException) as rejected:
        await IdentityAccountLockData(db).lock_accounts([_ACTOR_ID], mode="share")
    assert (rejected.value.status_code, rejected.value.detail) == (
        503,
        "身份认证服务暂不可用",
    )
    assert db.scalar.await_count == 1
    db.execute.assert_not_awaited()
    _caller_transaction_retained(db)


@pytest.mark.parametrize("sqlstate", ["55P03", "40001", "40P01", "23514"])
async def test_native_database_error_propagates_without_rollback(sqlstate):
    original = RuntimeError("database rejected statement")
    original.sqlstate = sqlstate
    error = DBAPIError("account lock", {}, original, False)
    db = _session(None)
    db.scalar.side_effect = error
    with pytest.raises(DBAPIError) as raised:
        await IdentityAccountLockData(db).lock_accounts([_ACTOR_ID], mode="update")
    assert raised.value is error
    db.execute.assert_not_awaited()
    _caller_transaction_retained(db)


@pytest.mark.parametrize(
    "row",
    [
        {"actor_id": uuid.UUID(int=99), "platform_role": "employee", "is_active": True},
        {"actor_id": _ACTOR_ID, "platform_role": 1, "is_active": True},
        {"actor_id": _ACTOR_ID, "platform_role": "employee", "is_active": 1},
    ],
    ids=["wrong-binding", "role-type", "bool"],
)
async def test_initial_actor_status_rejects_unbound_or_invalid_native_facts(row):
    db = _session(None, autoflush=False)
    result = Mock()
    result.mappings.return_value.one_or_none.return_value = row
    db.execute.return_value = result
    with pytest.raises(HTTPException) as rejected:
        await IdentityAccountLockData(db).read_actor_status(_ACTOR_ID)
    assert rejected.value.status_code == 503
    db.scalar.assert_not_awaited()
    db.flush.assert_not_awaited()
    _caller_transaction_retained(db)


async def test_initial_actor_status_keeps_absence_and_raw_inactive_role():
    db = _session(None)
    result = Mock()
    result.mappings.return_value.one_or_none.side_effect = [
        None,
        {"actor_id": _ACTOR_ID, "platform_role": "historical-role", "is_active": False},
    ]
    db.execute.return_value = result
    adapter = IdentityAccountLockData(db)
    assert await adapter.read_actor_status(_ACTOR_ID) is None
    actor = await adapter.read_actor_status(_ACTOR_ID)
    assert (actor.id, actor.role, actor.is_active) == (
        _ACTOR_ID,
        "historical-role",
        False,
    )
    assert db.flush.await_count == 2
    db.scalar.assert_not_awaited()
    _caller_transaction_retained(db)


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("authorized", [True, False])
async def test_accepted_key_retains_caller_transaction_and_flush_policy(
    autoflush, authorized
):
    db = _session(authorized, autoflush=autoflush)
    key_id = uuid.UUID(int=12)
    result = await IdentityAccountLockData(db).accepted_api_key_authorized(
        _ACTOR_ID, key_id, ["datasets:read", "annotations:read", "datasets:read"]
    )
    assert result is authorized
    statement, bindings = db.scalar.await_args.args
    assert "aap_identity_v1.check_accepted_api_key(" in str(statement)
    assert bindings == {
        "actor_id": _ACTOR_ID,
        "api_key_id": key_id,
        "required_scopes": ["annotations:read", "datasets:read"],
    }
    assert db.flush.await_count == int(autoflush)
    db.execute.assert_not_awaited()
    _caller_transaction_retained(db)


@pytest.mark.parametrize("reply", [None, 0, 1, "true", {}, []])
async def test_accepted_key_untrusted_result_is_503_without_fallback(reply):
    db = _session(reply)
    with pytest.raises(HTTPException) as rejected:
        await IdentityAccountLockData(db).accepted_api_key_authorized(
            _ACTOR_ID, uuid.UUID(int=12), []
        )
    assert rejected.value.status_code == 503
    assert db.scalar.await_count == 1
    db.execute.assert_not_awaited()
    _caller_transaction_retained(db)
