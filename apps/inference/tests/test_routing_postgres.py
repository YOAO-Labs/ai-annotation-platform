from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from aap_inference_contract.routing import (
    ROUTING_DATA_PROTOCOL,
    ROUTING_NATIVE_ACTIONS,
    ROUTING_NATIVE_FUNCTIONS,
    ROUTING_NATIVE_POLICY_SCHEMA,
)

from aap_inference.routing_postgres import RoutingPostgresData
from aap_inference_contract.routing import RoutingUnavailable

_POLICY = {
    "schema": ROUTING_NATIVE_POLICY_SCHEMA,
    "data_protocol": ROUTING_DATA_PROTOCOL,
    "transaction": "caller_owned",
    "functions": list(ROUTING_NATIVE_FUNCTIONS),
    "actions": list(ROUTING_NATIVE_ACTIONS),
    "ready": True,
}
_COMMAND_ID = UUID("e469fa57-ff59-42ae-9fe7-366f895931f7")
_DIGEST = "a" * 64


class _Connection:
    def __init__(self, *, policy=_POLICY, active=True, transaction_id="41"):
        self.policy = policy
        self.active = active
        self.transaction_id = transaction_id
        self.reply = None
        self.calls = []
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def is_in_transaction(self):
        return self.active

    async def fetchval(self, statement, *args):
        self.calls.append((statement, args))
        if statement.endswith("routing_policy()"):
            return self.policy
        if statement.endswith("pg_current_xact_id()::text"):
            return self.transaction_id
        if statement.endswith("pg_current_xact_id_if_assigned()::text"):
            return self.transaction_id
        if ".routing_step(" in statement:
            return self.reply
        if ".scope_retirement_step(" in statement:
            return self.reply
        raise AssertionError(statement)


async def _bind(monkeypatch, connection):
    verified = []

    async def verify(actual, *, schema, functions):
        verified.append((actual, schema, functions))

    monkeypatch.setitem(
        sys.modules,
        "aap_owner_data.postgres",
        SimpleNamespace(verify_function_only_role=verify),
    )
    data = await RoutingPostgresData.bind(connection)
    assert verified == [
        (
            connection,
            "aap_inference_routing_v1",
            {
                ("routing_policy", ""),
                ("routing_step", "text, text"),
                ("scope_retirement_step", "text, text"),
                ("video_execution_scope_step", "text, text"),
            },
        )
    ]
    return data


@pytest.mark.asyncio
async def test_bind_requires_active_transaction_and_borrows_all_resources(monkeypatch):
    inactive = _Connection(active=False)
    with pytest.raises(RoutingUnavailable):
        await RoutingPostgresData.bind(inactive)
    assert inactive.calls == []

    connection = _Connection()
    await _bind(monkeypatch, connection)
    assert connection.commit_count == 0
    assert connection.rollback_count == 0
    assert connection.close_count == 0


@pytest.mark.asyncio
async def test_step_uses_only_the_fixed_rpc_and_preserves_closed_rejection(monkeypatch):
    connection = _Connection()
    data = await _bind(monkeypatch, connection)
    connection.reply = {
        "schema": ROUTING_DATA_PROTOCOL,
        "status": "rejected",
        "reason": "pool_not_enabled",
    }
    arguments = {"request_id": str(_COMMAND_ID), "command_sha256": _DIGEST}

    reply = await data.step("command_status", arguments)

    assert reply["status"] == "rejected"
    assert reply["reason"] == "pool_not_enabled"
    statement, args = connection.calls[-1]
    assert statement == (
        "SELECT aap_inference_routing_v1.routing_step($1::text,$2::text)"
    )
    assert args == (
        "command_status",
        json.dumps(arguments, sort_keys=True, separators=(",", ":")),
    )


@pytest.mark.asyncio
async def test_step_rejects_unknown_actions_and_transaction_replacement(monkeypatch):
    connection = _Connection()
    data = await _bind(monkeypatch, connection)
    with pytest.raises(RoutingUnavailable):
        await data.step("arbitrary_sql", {})
    assert len(connection.calls) == 2  # policy and bound XID only

    connection.transaction_id = "42"
    with pytest.raises(RoutingUnavailable):
        await data.step("command_status", {"request_id": str(_COMMAND_ID)})
    assert all("routing_step" not in statement for statement, _ in connection.calls)


@pytest.mark.asyncio
async def test_bind_rejects_incomplete_native_policy(monkeypatch):
    connection = _Connection(
        policy={**_POLICY, "actions": list(_POLICY["actions"][:-1])}
    )
    with pytest.raises(RoutingUnavailable):
        await _bind(monkeypatch, connection)


@pytest.mark.asyncio
async def test_step_rejects_malformed_or_oversized_native_replies(monkeypatch):
    connection = _Connection()
    data = await _bind(monkeypatch, connection)
    connection.reply = (
        '{"schema":"aap-inference-routing-data/v1","status":"ok","status":"rejected"}'
    )
    with pytest.raises(RoutingUnavailable):
        await data.step("command_status", {})


@pytest.mark.asyncio
async def test_retirement_step_uses_only_the_fixed_adjacent_rpc(monkeypatch):
    connection = _Connection()
    data = await _bind(monkeypatch, connection)
    connection.reply = {
        "schema": "aap-inference-scope-retirement-data/v1",
        "status": "ok",
        "retirement": None,
        "context": None,
    }

    reply = await data.retirement_step(
        "status",
        {
            "retirement_id": "e469fa57-ff59-42ae-9fe7-366f895931f7",
            "scope_id": "c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf",
            "capability": "private-scope-token",
        },
    )

    assert reply["status"] == "ok"
    statement, args = connection.calls[-1]
    assert statement == (
        "SELECT aap_inference_routing_v1.scope_retirement_step($1::text,$2::text)"
    )
    assert args[0] == "status"
    assert json.loads(args[1])["capability"] == "private-scope-token"
    with pytest.raises(RoutingUnavailable):
        await data.retirement_step("arbitrary_sql", {})

    connection.reply = " " * (1024 * 1024 + 1)
    with pytest.raises(RoutingUnavailable):
        await data.step("command_status", {})
