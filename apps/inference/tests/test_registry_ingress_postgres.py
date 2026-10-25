from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS,
)

from aap_inference.health_data_postgres import (
    BackendHealthRow,
    HealthDataUnavailable,
    HealthServicePoolRow,
)
from aap_inference.registry_ingress_postgres import (
    RegistryIngressPostgresData,
    _APPLY_OBSERVATION_SQL,
    _BIND_TRANSACTION_SQL,
    _BY_URL_SQL,
    _CANDIDATES_SQL,
    _CREATE_REGISTRY_SQL,
    _CREATE_SINGLETON_POOL_SQL,
    _DISCONNECT_ENV_SQL,
    _FUNCTIONS,
    _POLICY_SQL,
    _RECORD_SYNC_RESULT_SQL,
    _SCHEMA,
    _SINGLETON_POOL_SQL,
    _SYNC_RESULT_SQL,
    _TAG_SEED_SQL,
    _TRANSACTION_SQL,
)

_REGISTRY_ID = UUID("e123e4d9-0630-42b6-8c04-267ea6caa7d3")
_POOL_ID = UUID("19eb2b8c-07df-4a59-9a8b-6f89b8dffecb")
_REQUEST_ID = UUID("c4c804e8-c6b6-443b-88c9-83cc1bc4b48a")
_NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)
_DIGEST = "a" * 64
_POLICY = {
    "schema": "aap-inference-registry-ingress-native/v1",
    "data_protocol": "aap-inference-registry-ingress/v1",
    "transaction": "caller_owned",
    "functions": list(REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS),
    "ready": True,
}


def _registry_row(**overrides):
    row = {
        "id": _REGISTRY_ID,
        "name": "Backend",
        "url": "http://backend.test",
        "state": "connected",
        "is_interactive": True,
        "auth_method": "token",
        "auth_token": "synthetic-secret",
        "extra_params": '{"max_concurrency":4}',
        "gpu_resource_id": None,
        "vram_budget_mb": None,
        "eviction_priority": 0,
        "health_meta": '{"capabilities":{"models":[]}}',
        "source": "env",
        "error_message": None,
        "last_checked_at": _NOW - timedelta(seconds=4),
        "created_at": _NOW - timedelta(days=3),
        "updated_at": _NOW - timedelta(hours=1),
    }
    row.update(overrides)
    return row


def _pool_row(**overrides):
    row = {
        "id": _POOL_ID,
        "name": "Backend",
        "enabled": False,
        "routing_policy": "smooth_weighted_round_robin",
        "legacy_instance_id": _REGISTRY_ID,
        "routing_generation": 1,
        "capability_fingerprint": None,
        "capability_snapshot": "null",
        "created_at": _NOW - timedelta(days=3),
        "updated_at": _NOW - timedelta(hours=1),
    }
    row.update(overrides)
    return row


class _Connection:
    def __init__(self, *, active=True, xid="123", policy=_POLICY):
        self.active = active
        self.xid = xid
        self.policy = policy
        self.calls = []
        self.rows = {}
        self.rowsets = {}
        self.values = {}
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def is_in_transaction(self):
        return self.active

    async def fetchval(self, statement, *args):
        if statement == _POLICY_SQL:
            self.calls.append(("fetchval", statement, args))
            return self.policy
        if statement == _BIND_TRANSACTION_SQL:
            return self.xid
        if statement == _TRANSACTION_SQL:
            return self.xid
        self.calls.append(("fetchval", statement, args))
        return self.values[statement]

    async def fetchrow(self, statement, *args):
        self.calls.append(("fetchrow", statement, args))
        rows = self.rows[statement]
        if isinstance(rows, list):
            return rows.pop(0)
        return rows

    async def fetch(self, statement, *args):
        self.calls.append(("fetch", statement, args))
        return self.rowsets[statement].pop(0)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1

    async def close(self):
        self.close_count += 1


def _install_role_verifier(monkeypatch):
    verified = []

    async def verify(connection, *, schema, functions):
        verified.append((connection, schema, functions))

    monkeypatch.setitem(
        sys.modules,
        "aap_owner_data.postgres",
        SimpleNamespace(verify_function_only_role=verify),
    )
    return verified


async def _bind(monkeypatch, connection, *, autoflush=True):
    verified = _install_role_verifier(monkeypatch)
    data = await RegistryIngressPostgresData.bind(connection, autoflush=autoflush)
    assert verified == [(connection, _SCHEMA, _FUNCTIONS)]
    return data


@pytest.mark.asyncio
async def test_bind_checks_live_caller_xid_and_never_owns_transaction(monkeypatch):
    connection = _Connection(active=False)
    with pytest.raises(HealthDataUnavailable):
        await RegistryIngressPostgresData.bind(connection)
    assert connection.calls == []

    connection = _Connection()
    data = await _bind(monkeypatch, connection)
    assert data.autoflush is True
    assert connection.calls == [("fetchval", _POLICY_SQL, ())]
    assert (
        connection.commit_count,
        connection.rollback_count,
        connection.close_count,
    ) == (0, 0, 0)

    connection.xid = "124"
    with pytest.raises(HealthDataUnavailable):
        await data.by_url("http://backend.test")
    assert connection.calls == [("fetchval", _POLICY_SQL, ())]


def test_role_whitelist_and_policy_are_exact():
    assert _FUNCTIONS == {
        ("registry_ingress_policy", ""),
        ("registry_by_url", "text"),
        ("registry_candidates", "text"),
        ("create_registry", "uuid, text, text, text, boolean, text"),
        ("singleton_pool", "uuid"),
        ("create_singleton_pool", "uuid, uuid, uuid, text, text, text"),
        (
            "apply_observation",
            "uuid, text, text, text, boolean, text, timestamp with time zone, text, boolean, text",
        ),
        ("disconnect_env_registry", "uuid, text"),
        ("tag_seed_registry", "uuid, text"),
        ("sync_result", "uuid, text"),
        ("record_sync_result", "uuid, text, integer"),
    }
    assert _POLICY["functions"] == [
        "aap_inference_registry_ingress_v1." + item.split(".", 1)[1]
        for item in REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS
    ]


@pytest.mark.asyncio
async def test_registry_reads_materialize_full_json_and_preserve_cached_identity(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [
        _registry_row(),
        _registry_row(extra_params="not-json"),
    ]
    data = await _bind(monkeypatch, connection)

    row = await data.by_url("http://backend.test")
    assert isinstance(row, BackendHealthRow)
    assert row.extra_params == {"max_concurrency": 4}
    assert row.health_meta == {"capabilities": {"models": []}}
    assert "synthetic-secret" not in repr(row)
    row.name = "caller-pending"

    with pytest.raises(json.JSONDecodeError):
        await data.by_url("http://backend.test")
    assert row.name == "caller-pending"
    assert [call[1] for call in connection.calls if call[0] == "fetchrow"] == [
        _BY_URL_SQL,
        _BY_URL_SQL,
    ]


@pytest.mark.asyncio
async def test_candidate_and_singleton_queries_autoflush_staged_changes(monkeypatch):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [_registry_row()]
    connection.rowsets[_CANDIDATES_SQL] = [[_registry_row()]]
    connection.rows[_SINGLETON_POOL_SQL] = [_pool_row()]
    connection.values[_APPLY_OBSERVATION_SQL] = _NOW
    data = await _bind(monkeypatch, connection)

    row = await data.by_url("http://backend.test")
    row.state = "error"
    row.error_message = "health check failed"
    await data.stage_observation(
        row,
        profile="env",
        usable_caps=False,
        expected_snapshot={
            "url": row.url,
            "auth_method": row.auth_method,
            "auth_token": row.auth_token,
            "gpu_resource_id": row.gpu_resource_id,
            "source": row.source,
        },
    )

    candidates = await data.candidates("env")
    pool = await data.singleton_pool(_REGISTRY_ID)

    rpc_calls = [call for call in connection.calls if call[1] != _POLICY_SQL]
    assert [call[1] for call in rpc_calls] == [
        _BY_URL_SQL,
        _APPLY_OBSERVATION_SQL,
        _CANDIDATES_SQL,
        _SINGLETON_POOL_SQL,
    ]
    assert candidates[0] is row
    assert isinstance(pool, HealthServicePoolRow)
    assert rpc_calls[1][2][:5] == (
        _REGISTRY_ID,
        "env",
        "error",
        "Backend",
        True,
    )
    assert json.loads(rpc_calls[1][2][5]) == row.health_meta
    assert json.loads(rpc_calls[1][2][9]) == {
        "url": "http://backend.test",
        "auth_method": "token",
        "auth_token": "synthetic-secret",
        "gpu_resource_id": None,
        "source": "env",
    }


@pytest.mark.asyncio
async def test_autoflush_false_keeps_stages_pending_across_plain_identity_reads(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [_registry_row(), _registry_row(state="connected")]
    connection.values[_APPLY_OBSERVATION_SQL] = _NOW
    data = await _bind(monkeypatch, connection, autoflush=False)

    row = await data.by_url("http://backend.test")
    row.state = "error"
    await data.stage_observation(
        row,
        profile="seed",
        usable_caps=False,
        expected_snapshot={"url": row.url},
    )
    result = await data.by_url("http://backend.test")

    assert result is row
    assert row.state == "error"
    assert all(call[1] != _APPLY_OBSERVATION_SQL for call in connection.calls)
    await data.flush()
    assert connection.calls[-1][1] == _APPLY_OBSERVATION_SQL


@pytest.mark.asyncio
async def test_marker_and_multiple_profile_writes_flush_in_order_and_capture_values(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [_registry_row()]
    connection.rows[_TAG_SEED_SQL] = [
        {
            "extra_params": '{"keep":true,"seed":{"managed_by":"screenshot-seed","profile":"screenshots","revision":"screenshots-2026-08-g"}}',
            "updated_at": _NOW,
        }
    ]
    connection.values[_APPLY_OBSERVATION_SQL] = _NOW + timedelta(seconds=1)
    data = await _bind(monkeypatch, connection)

    row = await data.by_url("http://backend.test")
    previous_extra = row.extra_params
    marker = {
        "managed_by": "screenshot-seed",
        "profile": "screenshots",
        "revision": "screenshots-2026-08-g",
    }
    row.extra_params = {**row.extra_params, "seed": marker}
    await data.stage_seed_marker(row, marker, previous_extra)
    first_snapshot = {
        "url": row.url,
        "auth_method": row.auth_method,
        "auth_token": row.auth_token,
        "gpu_resource_id": row.gpu_resource_id,
        "source": row.source,
    }
    row.state = "connected"
    row.name = "Seed result"
    row.health_meta = {"capabilities": {"profile": "seed"}}
    row.last_checked_at = _NOW + timedelta(seconds=2)
    await data.stage_observation(
        row,
        profile="seed",
        usable_caps=True,
        expected_snapshot=first_snapshot,
    )
    row.state = "error"
    row.name = "Env result"
    row.health_meta = None
    row.last_checked_at = _NOW + timedelta(seconds=3)
    await data.stage_observation(
        row,
        profile="env",
        usable_caps=False,
        expected_snapshot=first_snapshot,
    )
    calls_before_flush = len(connection.calls)
    assert calls_before_flush == 2

    await data.flush()

    operations = [call for call in connection.calls if call[1] != _POLICY_SQL]
    assert [call[1] for call in operations] == [
        _BY_URL_SQL,
        _TAG_SEED_SQL,
        _APPLY_OBSERVATION_SQL,
        _APPLY_OBSERVATION_SQL,
    ]
    assert json.loads(operations[2][2][5]) == {"capabilities": {"profile": "seed"}}
    assert operations[2][2][1:5] == (
        "seed",
        "connected",
        "Seed result",
        True,
    )
    assert operations[3][2][1:5] == (
        "env",
        "error",
        "Env result",
        True,
    )
    assert operations[3][2][5] == "null"
    assert row.extra_params == {
        "keep": True,
        "seed": marker,
    }
    assert row.updated_at == _NOW + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_stage_defers_json_serialization_until_native_flush(monkeypatch):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [_registry_row()]
    data = await _bind(monkeypatch, connection)

    row = await data.by_url("http://backend.test")
    row.health_meta = {"not_json": object()}
    await data.stage_observation(
        row,
        profile="env",
        usable_caps=True,
        expected_snapshot={"url": row.url},
    )
    calls_before_flush = len(connection.calls)

    with pytest.raises(TypeError):
        await data.flush()

    assert len(connection.calls) == calls_before_flush


@pytest.mark.asyncio
async def test_env_disconnect_is_staged_with_exact_url_fence(monkeypatch):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [_registry_row()]
    connection.values[_DISCONNECT_ENV_SQL] = _NOW
    data = await _bind(monkeypatch, connection)

    row = await data.by_url("http://backend.test")
    row.state = "disconnected"
    await data.stage_env_disconnect(row)
    calls_before_flush = len(connection.calls)
    assert calls_before_flush == 2

    await data.flush()

    assert connection.calls[-1] == (
        "fetchval",
        _DISCONNECT_ENV_SQL,
        (_REGISTRY_ID, "http://backend.test"),
    )
    assert row.updated_at == _NOW


@pytest.mark.asyncio
async def test_create_boundaries_flush_prior_stages_and_preserve_json_null(monkeypatch):
    connection = _Connection()
    connection.rows[_BY_URL_SQL] = [_registry_row()]
    connection.rows[_CREATE_REGISTRY_SQL] = [
        _registry_row(
            id=UUID("ec4c804e-8c6b-46b6-843c-c1bc4b48a001"), name="Seed", source="seed"
        )
    ]
    connection.rows[_CREATE_SINGLETON_POOL_SQL] = [_pool_row()]
    connection.values[_APPLY_OBSERVATION_SQL] = _NOW
    data = await _bind(monkeypatch, connection)

    row = await data.by_url("http://backend.test")
    row.state = "error"
    await data.stage_observation(
        row,
        profile="env",
        usable_caps=False,
        expected_snapshot={"url": row.url},
    )
    created = await data.create_registry("Seed", "http://seed.test", "seed")
    pool = await data.create_singleton_pool(created, None, None)

    operations = [call for call in connection.calls if call[1] != _POLICY_SQL]
    assert [call[1] for call in operations] == [
        _BY_URL_SQL,
        _APPLY_OBSERVATION_SQL,
        _CREATE_REGISTRY_SQL,
        _CREATE_SINGLETON_POOL_SQL,
    ]
    assert operations[2][2][5] == "{}"
    assert operations[3][2][3] == "Seed"
    assert operations[3][2][4] == "null"
    assert operations[3][2][5] is None
    assert isinstance(pool, HealthServicePoolRow)


@pytest.mark.asyncio
async def test_sync_receipts_are_bound_to_request_digest_and_stay_caller_pending(
    monkeypatch,
):
    connection = _Connection()
    result = {
        "schema": "aap-inference-registry-env-sync-result/v1",
        "request_id": str(_REQUEST_ID),
        "command_sha256": _DIGEST,
        "state": "caller_pending",
        "observed_urls": 2,
    }
    connection.values[_SYNC_RESULT_SQL] = result
    connection.values[_RECORD_SYNC_RESULT_SQL] = result
    data = await _bind(monkeypatch, connection)

    existing = await data.sync_result(_REQUEST_ID, _DIGEST)
    recorded = await data.record_sync_result(_REQUEST_ID, _DIGEST, 2)

    assert existing == recorded == result
    assert [call[1] for call in connection.calls if call[0] == "fetchval"] == [
        _POLICY_SQL,
        _SYNC_RESULT_SQL,
        _RECORD_SYNC_RESULT_SQL,
    ]
    assert connection.calls[-2][2] == (_REQUEST_ID, _DIGEST)
    assert connection.calls[-1][2] == (_REQUEST_ID, _DIGEST, 2)
