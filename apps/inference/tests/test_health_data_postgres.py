from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from aap_inference.health_data_postgres import (
    BackendHealthRow,
    HealthDataPostgresData,
    HealthDataUnavailable,
    HealthPoolMemberRow,
    HealthServicePoolRow,
    MembershipHealthRow,
    _APPLY_RECONCILIATION_SQL,
    _BACKEND_LOCK_SQL,
    _BACKEND_READ_SQL,
    _BIND_TRANSACTION_SQL,
    _TRANSACTION_SQL,
    _FUNCTIONS,
    _MEMBERSHIP_LOCK_SQL,
    _MEMBERSHIP_READ_SQL,
    _POLICY_SQL,
    _SCHEMA,
    _WRITE_BACKEND_SQL,
    health_data_policy_ready,
)

_REGISTRY_ID = UUID("4a308079-80a2-4a7f-8fca-ae39a4be5e28")
_POOL_ID = UUID("35df5fd5-9554-4d18-9296-43206c2c24b5")
_MEMBER_ID = UUID("02995a1e-2328-43aa-ac02-0d4fbfc474fc")
_NOW = datetime(2026, 10, 21, 8, 30, tzinfo=UTC)
_POLICY = {
    "schema": "aap-inference-health-data-native/v1",
    "data_protocol": "aap-inference-health-data/v1",
    "transaction": "caller_owned",
    "functions": [
        "aap_inference_health_data_v1.health_data_policy()",
        "aap_inference_health_data_v1.backend_row(uuid,boolean)",
        "aap_inference_health_data_v1.membership_row(uuid,text,boolean)",
        "aap_inference_health_data_v1.observation_clock()",
        "aap_inference_health_data_v1.lock_resource(text)",
        "aap_inference_health_data_v1.try_promotion()",
        "aap_inference_health_data_v1.write_backend(uuid,text,timestamptz,text,boolean,boolean)",
        "aap_inference_health_data_v1.member_pool(uuid)",
        "aap_inference_health_data_v1.pool_row(uuid,uuid)",
        "aap_inference_health_data_v1.member_row(uuid)",
        "aap_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)",
    ],
    "ready": True,
}


def _backend_row(**overrides):
    value = {
        "id": _REGISTRY_ID,
        "name": "Fixture backend",
        "url": "http://backend.test",
        "state": "connected",
        "is_interactive": False,
        "auth_method": "token",
        "auth_token": "synthetic-secret",
        "extra_params": '{"max_concurrency":4}',
        "gpu_resource_id": "gpu/test",
        "vram_budget_mb": 8192,
        "eviction_priority": 1,
        "health_meta": '{"gpu_info":{"memory":4}}',
        "source": "manual",
        "error_message": None,
        "last_checked_at": _NOW - timedelta(seconds=3),
        "created_at": _NOW - timedelta(days=1),
        "updated_at": _NOW - timedelta(hours=1),
    }
    value.update(overrides)
    return value


def _pool_row(**overrides):
    value = {
        "id": _POOL_ID,
        "name": "Fixture pool",
        "enabled": True,
        "routing_policy": "smooth_weighted_round_robin",
        "legacy_instance_id": _REGISTRY_ID,
        "routing_generation": 7,
        "capability_fingerprint": None,
        "capability_snapshot": None,
        "created_at": _NOW - timedelta(days=2),
        "updated_at": _NOW - timedelta(hours=2),
    }
    value.update(overrides)
    return value


def _member_row(**overrides):
    value = {
        "id": _MEMBER_ID,
        "pool_id": _POOL_ID,
        "registry_id": _REGISTRY_ID,
        "traffic_state": "active",
        "weight": 2,
        "created_at": _NOW - timedelta(days=2),
        "updated_at": _NOW - timedelta(hours=2),
    }
    value.update(overrides)
    return value


class _Connection:
    def __init__(self, *, active=True, policy=_POLICY):
        self.active = active
        self.policy = policy
        self.calls = []
        self.rows = {}
        self.values = {_BIND_TRANSACTION_SQL: "123", _TRANSACTION_SQL: "123"}
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def is_in_transaction(self):
        return self.active

    async def fetchval(self, statement, *args):
        if statement not in {_BIND_TRANSACTION_SQL, _TRANSACTION_SQL}:
            self.calls.append(("fetchval", statement, args))
        if statement == _POLICY_SQL:
            return self.policy
        return self.values[statement]

    async def fetchrow(self, statement, *args):
        self.calls.append(("fetchrow", statement, args))
        values = self.rows[statement]
        if isinstance(values, list):
            return values.pop(0)
        return values

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1

    async def close(self):
        self.close_count += 1


def _bind_runtime(monkeypatch, connection):
    verified = []

    async def verify(actual_connection, *, schema, functions):
        verified.append((actual_connection, schema, functions))

    monkeypatch.setitem(
        sys.modules,
        "aap_owner_data.postgres",
        SimpleNamespace(verify_function_only_role=verify),
    )
    return verified


async def _bound(monkeypatch, connection):
    verified = _bind_runtime(monkeypatch, connection)
    data = await HealthDataPostgresData.bind(connection)
    assert verified == [(connection, _SCHEMA, _FUNCTIONS)]
    return data


def _uuid_subclass(value: UUID):
    subclass = type("UuidSubclass", (UUID,), {})
    return subclass(hex=value.hex)


@pytest.mark.asyncio
async def test_bind_requires_an_active_function_only_caller_transaction(monkeypatch):
    connection = _Connection(active=False)
    with pytest.raises(HealthDataUnavailable):
        await HealthDataPostgresData.bind(connection)
    assert connection.calls == []

    connection = _Connection()
    data = await _bound(monkeypatch, connection)
    assert data._connection is connection
    assert connection.calls == [("fetchval", _POLICY_SQL, ())]
    assert (
        connection.commit_count
        == connection.rollback_count
        == connection.close_count
        == 0
    )


def test_policy_requires_the_exact_native_protocol_and_function_set():
    assert health_data_policy_ready(_POLICY) is True
    assert health_data_policy_ready(json.dumps(_POLICY)) is True
    assert health_data_policy_ready({**_POLICY, "ready": 1}) is False
    assert (
        health_data_policy_ready({**_POLICY, "functions": _POLICY["functions"][:-1]})
        is False
    )
    assert health_data_policy_ready({**_POLICY, "extra": True}) is False
    assert _FUNCTIONS == {
        ("health_data_policy", ""),
        ("backend_row", "uuid, boolean"),
        ("membership_row", "uuid, text, boolean"),
        ("observation_clock", ""),
        ("lock_resource", "text"),
        ("try_promotion", ""),
        (
            "write_backend",
            "uuid, text, timestamp with time zone, text, boolean, boolean",
        ),
        ("member_pool", "uuid"),
        ("pool_row", "uuid, uuid"),
        ("member_row", "uuid"),
        (
            "apply_reconciliation",
            "uuid, uuid, text, text, text, bigint, text, text",
        ),
    }


@pytest.mark.asyncio
async def test_full_backend_rows_are_materialized_and_lock_refresh_keeps_identity(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_BACKEND_READ_SQL] = [_backend_row()]
    connection.rows[_BACKEND_LOCK_SQL] = [
        _backend_row(url="http://locked.test", health_meta='{"compute":{}}')
    ]
    data = await _bound(monkeypatch, connection)

    backend = await data.get_backend(_uuid_subclass(_REGISTRY_ID))
    assert backend.health_meta == {"gpu_info": {"memory": 4}}
    locked = await data.lock_current_backend(_REGISTRY_ID)

    assert isinstance(backend, BackendHealthRow)
    assert locked is backend
    assert type(backend.id) is UUID
    assert backend.extra_params == {"max_concurrency": 4}
    assert backend.url == "http://locked.test"
    assert backend.health_meta == {"compute": {}}
    assert "synthetic-secret" not in repr(backend)
    assert [call[1] for call in connection.calls if call[0] == "fetchrow"] == [
        _BACKEND_READ_SQL,
        _BACKEND_LOCK_SQL,
    ]
    assert connection.calls[1][2] == (_REGISTRY_ID,)


@pytest.mark.asyncio
async def test_refresh_backend_reads_a_full_fresh_row_into_the_cached_object(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_BACKEND_READ_SQL] = [
        _backend_row(),
        _backend_row(state="error", extra_params='["unexpected-shape"]'),
        _backend_row(state="connected", extra_params="{}"),
    ]
    data = await _bound(monkeypatch, connection)

    backend = await data.get_backend(_REGISTRY_ID)
    await data.refresh_backend(backend)

    assert await data.get_backend(_REGISTRY_ID) is backend
    assert backend.state == "error"
    assert backend.extra_params == ["unexpected-shape"]
    assert [call[1] for call in connection.calls if call[0] == "fetchrow"] == [
        _BACKEND_READ_SQL,
        _BACKEND_READ_SQL,
        _BACKEND_READ_SQL,
    ]


@pytest.mark.asyncio
async def test_unused_jsonb_columns_are_still_decoded_before_returning_backend(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_BACKEND_READ_SQL] = [_backend_row(extra_params="not-json")]
    data = await _bound(monkeypatch, connection)

    with pytest.raises(json.JSONDecodeError):
        await data.get_backend(_REGISTRY_ID)


@pytest.mark.asyncio
async def test_membership_queries_use_fixed_lock_literals_and_preserve_tuple_fields(
    monkeypatch,
):
    connection = _Connection()
    connection.rows[_MEMBERSHIP_READ_SQL] = [
        {"membership_epoch": 12, "state": "active"}
    ]
    connection.rows[_MEMBERSHIP_LOCK_SQL] = [
        {"membership_epoch": 12, "state": "active"}
    ]
    data = await _bound(monkeypatch, connection)

    read = await data.read_membership(_REGISTRY_ID, "gpu/test")
    locked = await data.read_membership(_REGISTRY_ID, "gpu/test", for_update=True)

    assert read == MembershipHealthRow(membership_epoch=12, state="active")
    assert locked == read
    assert connection.calls[-2:] == [
        ("fetchrow", _MEMBERSHIP_READ_SQL, (_REGISTRY_ID, "gpu/test")),
        ("fetchrow", _MEMBERSHIP_LOCK_SQL, (_REGISTRY_ID, "gpu/test")),
    ]


@pytest.mark.asyncio
async def test_clock_advisory_and_backend_write_use_only_fixed_native_rpcs(monkeypatch):
    connection = _Connection()
    connection.values.update(
        {
            f"SELECT {_SCHEMA}.observation_clock()": _NOW,
            f"SELECT {_SCHEMA}.lock_resource($1::text)": None,
            f"SELECT {_SCHEMA}.try_promotion()": True,
            _WRITE_BACKEND_SQL: 1,
        }
    )
    data = await _bound(monkeypatch, connection)

    assert await data.clock_timestamp() == _NOW
    await data.lock_gpu_resource("gpu/test")
    assert await data.try_membership_promotion() is True
    assert (
        await data.write_health(
            _REGISTRY_ID,
            {
                "state": "connected",
                "last_checked_at": _NOW,
                "health_meta": None,
                "is_interactive": False,
            },
        )
        == 1
    )

    write_call = next(
        call for call in connection.calls if call[1] == _WRITE_BACKEND_SQL
    )
    assert write_call == (
        "fetchval",
        _WRITE_BACKEND_SQL,
        (_REGISTRY_ID, "connected", _NOW, "null", False, True),
    )
    assert all(
        "ml_backend_" not in statement
        for _method, statement, _args in connection.calls[1:]
    )


@pytest.mark.asyncio
async def test_pool_and_member_rows_stage_a_seed_until_native_flush(monkeypatch):
    connection = _Connection()
    member_pool_sql = f"SELECT {_SCHEMA}.member_pool($1::uuid)"
    pool_sql = f"SELECT * FROM {_SCHEMA}.pool_row($1::uuid, $2::uuid)"
    member_sql = f"SELECT * FROM {_SCHEMA}.member_row($1::uuid)"
    connection.values[member_pool_sql] = _uuid_subclass(_POOL_ID)
    connection.rows[pool_sql] = [_pool_row(capability_snapshot='{"models":[]}')]
    connection.rows[member_sql] = [_member_row()]
    connection.rows[_APPLY_RECONCILIATION_SQL] = {
        "pool_updated_at": _NOW,
        "member_updated_at": None,
    }
    data = await _bound(monkeypatch, connection)

    pool_id = await data.read_member_pool(_REGISTRY_ID)
    pool = await data.load_pool(pool_id, registry_id=_REGISTRY_ID)
    member = await data.load_pool_member(_REGISTRY_ID)
    assert isinstance(pool, HealthServicePoolRow)
    assert isinstance(member, HealthPoolMemberRow)
    assert type(pool_id) is UUID
    assert pool.capability_snapshot == {"models": []}
    assert pool.id == _POOL_ID
    assert member.id == _MEMBER_ID

    snapshot = {"models": [{"id": "model-1"}]}
    fingerprint = "a" * 64
    old_updated_at = pool.updated_at
    pool.capability_snapshot = snapshot
    pool.capability_fingerprint = fingerprint
    pool.routing_generation += 1
    calls_before_stage = len(connection.calls)
    await data.stage_pool_reconciliation(
        _REGISTRY_ID,
        pool,
        member,
        operation="seed",
        snapshot=snapshot,
        fingerprint=fingerprint,
        expected_generation=7,
        expected_pool_fingerprint=None,
        expected_member_state="active",
    )
    assert len(connection.calls) == calls_before_stage
    assert pool.updated_at == old_updated_at
    # A later failed refresh must neither mutate nor discard the staged command.
    snapshot["models"][0]["id"] = "caller-edited"
    pool_sql = f"SELECT * FROM {_SCHEMA}.pool_row($1::uuid, $2::uuid)"
    connection.rows[pool_sql] = [_pool_row(capability_snapshot="not-json")]
    with pytest.raises(ValueError):
        await data.load_pool(_POOL_ID, registry_id=_REGISTRY_ID)

    await data.flush()

    apply_call = connection.calls[-1]
    assert apply_call[0] == "fetchrow"
    assert apply_call[1] == _APPLY_RECONCILIATION_SQL
    assert apply_call[2] == (
        _REGISTRY_ID,
        _POOL_ID,
        "seed",
        '{"models":[{"id":"model-1"}]}',
        fingerprint,
        7,
        None,
        "active",
    )
    assert pool.updated_at == _NOW
    assert member.updated_at == _NOW - timedelta(hours=2)
    before_empty_flush = len(connection.calls)
    await data.flush()
    assert len(connection.calls) == before_empty_flush


@pytest.mark.asyncio
async def test_disable_transition_updates_member_and_pool_timestamps(monkeypatch):
    connection = _Connection()
    pool_sql = f"SELECT * FROM {_SCHEMA}.pool_row($1::uuid, $2::uuid)"
    member_sql = f"SELECT * FROM {_SCHEMA}.member_row($1::uuid)"
    connection.rows[pool_sql] = [_pool_row(capability_fingerprint="b" * 64)]
    connection.rows[member_sql] = [_member_row()]
    connection.rows[_APPLY_RECONCILIATION_SQL] = {
        "pool_updated_at": _NOW,
        "member_updated_at": _NOW + timedelta(milliseconds=2),
    }
    data = await _bound(monkeypatch, connection)

    pool = await data.load_pool(_POOL_ID, registry_id=_REGISTRY_ID)
    member = await data.load_pool_member(_REGISTRY_ID)
    old_member_updated_at = member.updated_at
    pool.enabled = False
    pool.routing_generation += 1
    member.traffic_state = "disabled"
    snapshot = {"models": [{"id": "model-2"}]}
    await data.stage_pool_reconciliation(
        _REGISTRY_ID,
        pool,
        member,
        operation="disable",
        snapshot=snapshot,
        fingerprint="c" * 64,
        expected_generation=7,
        expected_pool_fingerprint="b" * 64,
        expected_member_state="active",
    )

    await data.flush()

    assert pool.updated_at == _NOW
    assert member.updated_at == _NOW + timedelta(milliseconds=2)
    assert member.updated_at != old_member_updated_at
    assert connection.calls[-1][1] == _APPLY_RECONCILIATION_SQL


@pytest.mark.asyncio
async def test_pool_refresh_discards_a_staged_intent_and_reloads_the_same_dto(
    monkeypatch,
):
    connection = _Connection()
    pool_sql = f"SELECT * FROM {_SCHEMA}.pool_row($1::uuid, $2::uuid)"
    member_sql = f"SELECT * FROM {_SCHEMA}.member_row($1::uuid)"
    connection.rows[pool_sql] = [
        _pool_row(),
        _pool_row(
            enabled=False,
            routing_generation=8,
            capability_fingerprint="d" * 64,
        ),
    ]
    connection.rows[member_sql] = [_member_row()]
    data = await _bound(monkeypatch, connection)

    pool = await data.load_pool(_POOL_ID, registry_id=_REGISTRY_ID)
    member = await data.load_pool_member(_REGISTRY_ID)
    pool.routing_generation += 1
    pool.capability_fingerprint = "c" * 64
    await data.stage_pool_reconciliation(
        _REGISTRY_ID,
        pool,
        member,
        operation="seed",
        snapshot={"models": []},
        fingerprint="c" * 64,
        expected_generation=7,
        expected_pool_fingerprint=None,
        expected_member_state="active",
    )

    refreshed = await data.load_pool(_POOL_ID, registry_id=_REGISTRY_ID)
    before_flush = len(connection.calls)
    await data.flush()

    assert refreshed is pool
    assert pool.enabled is False
    assert pool.routing_generation == 8
    assert pool.capability_fingerprint == "d" * 64
    assert len(connection.calls) == before_flush
    assert all(call[1] != _APPLY_RECONCILIATION_SQL for call in connection.calls)


@pytest.mark.asyncio
async def test_bind_rejects_a_stale_or_malformed_native_policy(monkeypatch):
    connection = _Connection(policy={**_POLICY, "ready": "true"})
    _bind_runtime(monkeypatch, connection)

    with pytest.raises(HealthDataUnavailable):
        await HealthDataPostgresData.bind(connection)


@pytest.mark.asyncio
async def test_cached_identity_still_materializes_unused_database_json(monkeypatch):
    connection = _Connection()
    connection.rows[_BACKEND_READ_SQL] = [
        _backend_row(),
        _backend_row(health_meta="not-json"),
    ]
    data = await _bound(monkeypatch, connection)
    backend = await data.get_backend(_REGISTRY_ID)
    backend.url = "http://caller-pending.test"
    with pytest.raises(ValueError):
        await data.get_backend(_REGISTRY_ID)
    assert backend.url == "http://caller-pending.test"


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [True, False])
async def test_binding_rejects_ended_or_replaced_caller_transaction(monkeypatch, ended):
    connection = _Connection()
    connection.rows[_BACKEND_READ_SQL] = [_backend_row()]
    data = await _bound(monkeypatch, connection)
    calls = list(connection.calls)
    connection.active = not ended
    connection.values[_TRANSACTION_SQL] = "124"
    with pytest.raises(HealthDataUnavailable):
        await data.get_backend(_REGISTRY_ID)
    assert connection.calls == calls
    assert (
        connection.commit_count,
        connection.rollback_count,
        connection.close_count,
    ) == (0, 0, 0)
