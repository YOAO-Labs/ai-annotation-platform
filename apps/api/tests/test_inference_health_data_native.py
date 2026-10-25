"""Owned PostgreSQL acceptance for fixed caller-transaction health data RPCs."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_SCHEMA = "aap_inference_health_data_v1"
_ROLE = "inference_health_data_client"
_POLICY = f"{_SCHEMA}.health_data_policy()"
_BACKEND_ROW = f"{_SCHEMA}.backend_row"
_MEMBERSHIP_ROW = f"{_SCHEMA}.membership_row"
_OBSERVATION_CLOCK = f"{_SCHEMA}.observation_clock()"
_LOCK_RESOURCE = f"{_SCHEMA}.lock_resource"
_TRY_PROMOTION = f"{_SCHEMA}.try_promotion()"
_WRITE_BACKEND = f"{_SCHEMA}.write_backend"
_MEMBER_POOL = f"{_SCHEMA}.member_pool"
_POOL_ROW = f"{_SCHEMA}.pool_row"
_MEMBER_ROW = f"{_SCHEMA}.member_row"
_APPLY = f"{_SCHEMA}.apply_reconciliation"
_FUNCTIONS = (
    f"{_SCHEMA}.health_data_policy()",
    f"{_SCHEMA}.backend_row(uuid,boolean)",
    f"{_SCHEMA}.membership_row(uuid,text,boolean)",
    f"{_SCHEMA}.observation_clock()",
    f"{_SCHEMA}.lock_resource(text)",
    f"{_SCHEMA}.try_promotion()",
    f"{_SCHEMA}.write_backend(uuid,text,timestamptz,text,boolean,boolean)",
    f"{_SCHEMA}.member_pool(uuid)",
    f"{_SCHEMA}.pool_row(uuid,uuid)",
    f"{_SCHEMA}.member_row(uuid)",
    f"{_SCHEMA}.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)",
)
_CATALOG_FUNCTIONS = (
    f"{_SCHEMA}.health_data_policy()",
    f"{_SCHEMA}.backend_row(uuid, boolean)",
    f"{_SCHEMA}.membership_row(uuid, text, boolean)",
    f"{_SCHEMA}.observation_clock()",
    f"{_SCHEMA}.lock_resource(text)",
    f"{_SCHEMA}.try_promotion()",
    f"{_SCHEMA}.write_backend(uuid, text, timestamp with time zone, text, boolean, boolean)",
    f"{_SCHEMA}.member_pool(uuid)",
    f"{_SCHEMA}.pool_row(uuid, uuid)",
    f"{_SCHEMA}.member_row(uuid)",
    f"{_SCHEMA}.apply_reconciliation(uuid, uuid, text, text, text, bigint, text, text)",
)
_POLICY_VALUE = {
    "schema": "aap-inference-health-data-native/v1",
    "data_protocol": "aap-inference-health-data/v1",
    "transaction": "caller_owned",
    "functions": list(_FUNCTIONS),
    "ready": True,
}
_REGISTRY_COLUMNS = [
    "id",
    "name",
    "url",
    "state",
    "is_interactive",
    "auth_method",
    "auth_token",
    "extra_params",
    "gpu_resource_id",
    "vram_budget_mb",
    "eviction_priority",
    "health_meta",
    "source",
    "error_message",
    "last_checked_at",
    "created_at",
    "updated_at",
]
_POOL_COLUMNS = [
    "id",
    "name",
    "enabled",
    "routing_policy",
    "legacy_instance_id",
    "routing_generation",
    "capability_fingerprint",
    "capability_snapshot",
    "created_at",
    "updated_at",
]
_MEMBER_COLUMNS = [
    "id",
    "pool_id",
    "registry_id",
    "traffic_state",
    "weight",
    "created_at",
    "updated_at",
]
_HEALTH_TABLES = (
    "public.ml_backend_registry",
    "public.gpu_backend_memberships",
    "public.ml_backend_service_pools",
    "public.ml_backend_pool_members",
)


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect(test_engine, *, role: str | None = None):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    if role is not None:
        await connection.execute(f"SET ROLE {role}")
        assert await connection.fetchval("SELECT current_user") == role
    return connection


async def _seed_world(owner):
    registry_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    member_id = uuid.uuid4()
    gpu_resource_id = f"mig/{uuid.uuid4().hex[:8]}"
    await owner.execute(
        """INSERT INTO public.ml_backend_registry (
               id, name, url, state, is_interactive, auth_method, auth_token,
               extra_params, gpu_resource_id, vram_budget_mb, eviction_priority,
               health_meta, source, error_message, last_checked_at
           ) VALUES (
               $1, $2, $3, 'disconnected', false, 'token', $4,
               $5::jsonb, NULL, NULL, 7, $6::jsonb, 'manual', NULL, NULL
           )""",
        registry_id,
        f"Native health {registry_id}",
        f"http://127.0.0.1/{registry_id}",
        f"health-secret-{uuid.uuid4().hex}",
        json.dumps({"max_concurrency": 5, "unused": ["preserved", 9]}),
        json.dumps({"initial": True, "nested": {"version": 1}}),
    )
    await owner.execute(
        """INSERT INTO public.ml_backend_service_pools (
               id, name, enabled, routing_policy, legacy_instance_id,
               routing_generation, capability_fingerprint, capability_snapshot
           ) VALUES (
               $1, $2, true, 'smooth_weighted_round_robin', $3, 1, NULL, NULL
           )""",
        pool_id,
        f"Native pool {pool_id}",
        registry_id,
    )
    await owner.execute(
        """INSERT INTO public.ml_backend_pool_members (
               id, pool_id, registry_id, traffic_state, weight
           ) VALUES ($1, $2, $3, 'active', 4)""",
        member_id,
        pool_id,
        registry_id,
    )
    return {
        "registry_id": registry_id,
        "pool_id": pool_id,
        "member_id": member_id,
        "gpu_resource_id": gpu_resource_id,
    }


async def _cleanup_world(owner, world) -> None:
    registry_id = world["registry_id"]
    await owner.execute(
        "DELETE FROM public.ml_backend_pool_members WHERE registry_id = $1",
        registry_id,
    )
    await owner.execute(
        "DELETE FROM public.ml_backend_service_pools WHERE id = $1",
        world["pool_id"],
    )
    await owner.execute(
        "DELETE FROM public.ml_backend_registry WHERE id = $1",
        registry_id,
    )


async def _assert_error(connection, statement: str, *args, sqlstate: str, message=None):
    with pytest.raises(asyncpg.PostgresError) as caught:
        await connection.fetchrow(statement, *args)
    assert caught.value.sqlstate == sqlstate
    if message is not None:
        assert caught.value.message == message


async def _assert_error_in_transaction(
    connection, statement: str, *args, sqlstate: str, message=None
):
    await connection.execute("SAVEPOINT health_data_expected_error")
    try:
        await _assert_error(
            connection, statement, *args, sqlstate=sqlstate, message=message
        )
    finally:
        await connection.execute("ROLLBACK TO SAVEPOINT health_data_expected_error")
        await connection.execute("RELEASE SAVEPOINT health_data_expected_error")


async def test_health_data_fixed_reads_and_health_update_preserve_typed_rows(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed_world(owner)
    client = await _connect(test_engine, role=_ROLE)
    started = False
    transaction = client.transaction()
    try:
        await transaction.start()
        started = True
        assert json.loads(await client.fetchval(f"SELECT {_POLICY}")) == _POLICY_VALUE

        backend = await client.fetchrow(
            f"SELECT * FROM {_BACKEND_ROW}($1::uuid, false)", world["registry_id"]
        )
        assert list(backend.keys()) == _REGISTRY_COLUMNS
        assert backend["id"] == world["registry_id"]
        assert backend["state"] == "disconnected"
        assert backend["gpu_resource_id"] is None
        assert json.loads(backend["extra_params"]) == {
            "max_concurrency": 5,
            "unused": ["preserved", 9],
        }
        assert json.loads(backend["health_meta"]) == {
            "initial": True,
            "nested": {"version": 1},
        }
        assert (
            await client.fetchrow(
                f"SELECT * FROM {_BACKEND_ROW}($1::uuid, false)", uuid.uuid4()
            )
            is None
        )

        locked_backend = await client.fetchrow(
            f"SELECT * FROM {_BACKEND_ROW}($1::uuid, true)",
            world["registry_id"],
        )
        assert list(locked_backend.keys()) == _REGISTRY_COLUMNS

        assert (
            await client.fetchrow(
                f"SELECT * FROM {_MEMBERSHIP_ROW}($1::uuid,$2::text,false)",
                world["registry_id"],
                world["gpu_resource_id"],
            )
            is None
        )
        assert (
            await client.fetchrow(
                f"SELECT * FROM {_MEMBERSHIP_ROW}($1::uuid,$2::text,true)",
                world["registry_id"],
                "missing/0",
            )
            is None
        )

        checked_at = await client.fetchval(f"SELECT {_OBSERVATION_CLOCK}")
        assert checked_at.tzinfo is not None
        await client.fetchval(
            f"SELECT {_LOCK_RESOURCE}($1::text)", world["gpu_resource_id"]
        )
        assert await client.fetchval(f"SELECT {_TRY_PROMOTION}") is True
        assert await client.fetchval(f"SELECT {_TRY_PROMOTION}") is True

        rowcount = await client.fetchval(
            f"SELECT {_WRITE_BACKEND}("
            "$1::uuid,$2::text,$3::timestamptz,$4::text,$5::boolean,$6::boolean)",
            world["registry_id"],
            "connected",
            checked_at,
            '{"capabilities":{"image":true}}',
            True,
            True,
        )
        assert rowcount == 1
        updated = await client.fetchrow(
            f"SELECT * FROM {_BACKEND_ROW}($1::uuid, false)",
            world["registry_id"],
        )
        assert updated["state"] == "connected"
        assert updated["is_interactive"] is True
        assert updated["last_checked_at"] == checked_at
        assert json.loads(updated["health_meta"]) == {"capabilities": {"image": True}}

        rowcount = await client.fetchval(
            f"SELECT {_WRITE_BACKEND}("
            "$1::uuid,$2::text,$3::timestamptz,$4::text,$5::boolean,$6::boolean)",
            world["registry_id"],
            "error",
            checked_at,
            "null",
            None,
            False,
        )
        assert rowcount == 1
        updated = await client.fetchrow(
            f"SELECT * FROM {_BACKEND_ROW}($1::uuid, false)",
            world["registry_id"],
        )
        assert updated["state"] == "error"
        assert updated["is_interactive"] is True
        assert updated["health_meta"] == "null"

        await _assert_error_in_transaction(
            client,
            f"SELECT {_WRITE_BACKEND}("
            "$1::uuid,$2::text,$3::timestamptz,$4::text,$5::boolean,$6::boolean)",
            world["registry_id"],
            "disconnected",
            checked_at,
            '{"valid":true}',
            None,
            False,
            sqlstate="22023",
            message="invalid Inference health data request",
        )
        await _assert_error_in_transaction(
            client,
            f"SELECT {_WRITE_BACKEND}("
            "$1::uuid,$2::text,$3::timestamptz,$4::text,$5::boolean,$6::boolean)",
            world["registry_id"],
            "connected",
            checked_at,
            '{"valid":true}',
            True,
            None,
            sqlstate="22023",
            message="invalid Inference health data request",
        )

        await transaction.commit()
        started = False
    finally:
        if started:
            await transaction.rollback()
        await client.close()
        await _cleanup_world(owner, world)
        await owner.close()


async def test_health_data_pool_reconciliation_is_versioned_and_atomic(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed_world(owner)
    client = await _connect(test_engine, role=_ROLE)
    transaction = client.transaction()
    started = False
    seed_fingerprint = "a" * 64
    drift_fingerprint = "b" * 64
    seed_snapshot = '{"capabilities":{"image":true,"video":false}}'
    drift_snapshot = '{"capabilities":{"image":false,"video":true}}'
    try:
        await transaction.start()
        started = True

        assert (
            await client.fetchval(
                f"SELECT {_MEMBER_POOL}($1::uuid)", world["registry_id"]
            )
            == world["pool_id"]
        )
        pool = await client.fetchrow(
            f"SELECT * FROM {_POOL_ROW}($1::uuid,$2::uuid)",
            world["pool_id"],
            world["registry_id"],
        )
        assert list(pool.keys()) == _POOL_COLUMNS
        assert pool["routing_generation"] == 1
        assert pool["capability_fingerprint"] is None
        assert pool["capability_snapshot"] is None

        member = await client.fetchrow(
            f"SELECT * FROM {_MEMBER_ROW}($1::uuid)", world["registry_id"]
        )
        assert list(member.keys()) == _MEMBER_COLUMNS
        assert member["id"] == world["member_id"]
        assert member["pool_id"] == world["pool_id"]
        assert member["traffic_state"] == "active"

        seeded = await client.fetchrow(
            f"SELECT * FROM {_APPLY}("
            "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::bigint,"
            "$7::text,$8::text)",
            world["registry_id"],
            world["pool_id"],
            "seed",
            seed_snapshot,
            seed_fingerprint,
            1,
            None,
            "active",
        )
        assert seeded["pool_updated_at"] is not None
        assert seeded["member_updated_at"] is None

        pool = await client.fetchrow(
            f"SELECT * FROM {_POOL_ROW}($1::uuid,$2::uuid)",
            world["pool_id"],
            world["registry_id"],
        )
        assert pool["routing_generation"] == 2
        assert pool["capability_fingerprint"] == seed_fingerprint
        assert json.loads(pool["capability_snapshot"]) == json.loads(seed_snapshot)

        disabled = await client.fetchrow(
            f"SELECT * FROM {_APPLY}("
            "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::bigint,"
            "$7::text,$8::text)",
            world["registry_id"],
            world["pool_id"],
            "disable",
            drift_snapshot,
            drift_fingerprint,
            2,
            seed_fingerprint,
            "active",
        )
        assert disabled["pool_updated_at"] is not None
        assert disabled["member_updated_at"] is not None

        pool = await client.fetchrow(
            f"SELECT * FROM {_POOL_ROW}($1::uuid,$2::uuid)",
            world["pool_id"],
            world["registry_id"],
        )
        member = await client.fetchrow(
            f"SELECT * FROM {_MEMBER_ROW}($1::uuid)", world["registry_id"]
        )
        assert pool["enabled"] is False
        assert pool["routing_generation"] == 3
        assert pool["capability_fingerprint"] == seed_fingerprint
        assert member["traffic_state"] == "disabled"

        await _assert_error_in_transaction(
            client,
            f"SELECT * FROM {_APPLY}("
            "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::bigint,"
            "$7::text,$8::text)",
            world["registry_id"],
            world["pool_id"],
            "disable",
            drift_snapshot,
            "c" * 64,
            2,
            seed_fingerprint,
            "active",
            sqlstate="40001",
            message="health reconciliation snapshot changed",
        )
        await _assert_error_in_transaction(
            client,
            f"SELECT * FROM {_APPLY}("
            "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::bigint,"
            "$7::text,$8::text)",
            world["registry_id"],
            world["pool_id"],
            "clear",
            drift_snapshot,
            "c" * 64,
            3,
            seed_fingerprint,
            "disabled",
            sqlstate="22023",
            message="invalid health reconciliation request",
        )

        await transaction.commit()
        started = False
    finally:
        if started:
            await transaction.rollback()
        await client.close()
        await _cleanup_world(owner, world)
        await owner.close()


async def test_health_data_client_has_only_fixed_functions_and_no_raw_table_access(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _connect(test_engine, role=_ROLE)
    try:
        assert json.loads(await client.fetchval(f"SELECT {_POLICY}")) == _POLICY_VALUE
        metadata = await owner.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                          pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature,
                      pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                      function.prosecdef AS security_definer,
                      function.proconfig AS settings,
                      EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(COALESCE(
                              function.proacl,
                              pg_catalog.acldefault('f', function.proowner)
                          )) AS privilege
                          WHERE privilege.grantee = 0
                            AND privilege.privilege_type = 'EXECUTE'
                      ) AS public_execute
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE namespace.nspname = $1
                 AND function.prokind = 'f'
               ORDER BY signature""",
            _SCHEMA,
        )
        assert [row["signature"] for row in metadata] == sorted(_CATALOG_FUNCTIONS)
        for function in metadata:
            assert function["owner"] == "inference_code"
            assert function["security_definer"] is True
            assert function["public_execute"] is False
            settings = function["settings"]
            assert settings is not None
            assert "search_path=pg_catalog, pg_temp" in settings
            assert "TimeZone=UTC" in settings
            assert "DateStyle=ISO, YMD" in settings
            assert "extra_float_digits=3" in settings

        actual = await owner.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                          pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE namespace.nspname = $1
                 AND pg_catalog.has_function_privilege($2, function.oid, 'EXECUTE')""",
            _SCHEMA,
            _ROLE,
        )
        assert {row["signature"] for row in actual} == set(_CATALOG_FUNCTIONS)
        assert await owner.fetchval(
            "SELECT pg_catalog.has_schema_privilege($1, $2, 'USAGE')",
            _ROLE,
            _SCHEMA,
        )
        assert not await owner.fetchval(
            "SELECT pg_catalog.pg_has_role($1, 'inference_code', 'MEMBER')",
            _ROLE,
        )
        for table in _HEALTH_TABLES:
            assert not await owner.fetchval(
                "SELECT pg_catalog.has_table_privilege($1, $2, 'SELECT')",
                _ROLE,
                table,
            )
            assert not await owner.fetchval(
                "SELECT pg_catalog.has_table_privilege($1, $2, 'UPDATE')",
                _ROLE,
                table,
            )
    finally:
        await client.close()
        await owner.close()
