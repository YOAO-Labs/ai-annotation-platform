"""Owned PostgreSQL acceptance for fixed Registry ingress Native functions."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

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

_SCHEMA = "aap_inference_registry_ingress_v1"
_ROLE = "inference_registry_ingress_client"
_POLICY = f"{_SCHEMA}.registry_ingress_policy()"
_REGISTRY_BY_URL = f"{_SCHEMA}.registry_by_url"
_REGISTRY_CANDIDATES = f"{_SCHEMA}.registry_candidates"
_CREATE_REGISTRY = f"{_SCHEMA}.create_registry"
_SINGLETON_POOL = f"{_SCHEMA}.singleton_pool"
_CREATE_SINGLETON_POOL = f"{_SCHEMA}.create_singleton_pool"
_APPLY_OBSERVATION = f"{_SCHEMA}.apply_observation"
_DISCONNECT = f"{_SCHEMA}.disconnect_env_registry"
_TAG_SEED = f"{_SCHEMA}.tag_seed_registry"
_SYNC_RESULT = f"{_SCHEMA}.sync_result"
_RECORD_SYNC_RESULT = f"{_SCHEMA}.record_sync_result"
_RECEIPTS = f"{_SCHEMA}.env_sync_receipts"
_FUNCTIONS = (
    f"{_SCHEMA}.registry_ingress_policy()",
    f"{_SCHEMA}.registry_by_url(text)",
    f"{_SCHEMA}.registry_candidates(text)",
    f"{_SCHEMA}.create_registry(uuid,text,text,text,boolean,text)",
    f"{_SCHEMA}.singleton_pool(uuid)",
    f"{_SCHEMA}.create_singleton_pool(uuid,uuid,uuid,text,text,text)",
    f"{_SCHEMA}.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)",
    f"{_SCHEMA}.disconnect_env_registry(uuid,text)",
    f"{_SCHEMA}.tag_seed_registry(uuid,text)",
    f"{_SCHEMA}.sync_result(uuid,text)",
    f"{_SCHEMA}.record_sync_result(uuid,text,integer)",
)
_CATALOG_FUNCTIONS = (
    f"{_SCHEMA}.registry_ingress_policy()",
    f"{_SCHEMA}.registry_by_url(text)",
    f"{_SCHEMA}.registry_candidates(text)",
    f"{_SCHEMA}.create_registry(uuid, text, text, text, boolean, text)",
    f"{_SCHEMA}.singleton_pool(uuid)",
    f"{_SCHEMA}.create_singleton_pool(uuid, uuid, uuid, text, text, text)",
    f"{_SCHEMA}.apply_observation(uuid, text, text, text, boolean, text, timestamp with time zone, text, boolean, text)",
    f"{_SCHEMA}.disconnect_env_registry(uuid, text)",
    f"{_SCHEMA}.tag_seed_registry(uuid, text)",
    f"{_SCHEMA}.sync_result(uuid, text)",
    f"{_SCHEMA}.record_sync_result(uuid, text, integer)",
)
_POLICY_VALUE = {
    "schema": "aap-inference-registry-ingress-native/v1",
    "data_protocol": "aap-inference-registry-ingress/v1",
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
_MARKER = {
    "managed_by": "screenshot-seed",
    "profile": "screenshots",
    "revision": "screenshots-2026-08-g",
}


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


def _json(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


async def _create_registry(
    connection,
    registry_id: uuid.UUID,
    name: str | None,
    url: str,
    source: str,
    *,
    is_interactive: bool = False,
    extra_params: dict | None = None,
):
    extra_json = json.dumps(extra_params or {}, separators=(",", ":"))
    return await connection.fetchrow(
        f"SELECT * FROM {_CREATE_REGISTRY}("
        "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text)",
        registry_id,
        name,
        url,
        source,
        is_interactive,
        extra_json,
    )


async def _cleanup_registries(owner, registry_ids, request_ids=()):
    if registry_ids:
        await owner.execute(
            "DELETE FROM public.ml_backend_pool_members "
            "WHERE registry_id = ANY($1::uuid[])",
            list(registry_ids),
        )
        await owner.execute(
            "DELETE FROM public.ml_backend_service_pools "
            "WHERE legacy_instance_id = ANY($1::uuid[])",
            list(registry_ids),
        )
        await owner.execute(
            "DELETE FROM public.ml_backend_registry WHERE id = ANY($1::uuid[])",
            list(registry_ids),
        )
    if request_ids:
        await owner.execute(
            f"DELETE FROM {_RECEIPTS} WHERE request_id = ANY($1::uuid[])",
            list(request_ids),
        )


async def _assert_error(connection, statement, *args, sqlstate, message=None):
    with pytest.raises(asyncpg.PostgresError) as caught:
        await connection.fetchrow(statement, *args)
    assert caught.value.sqlstate == sqlstate
    if message is not None:
        assert caught.value.message == message


async def _assert_error_in_transaction(
    connection, statement, *args, sqlstate, message=None
):
    await connection.execute("SAVEPOINT registry_ingress_expected_error")
    try:
        await _assert_error(
            connection,
            statement,
            *args,
            sqlstate=sqlstate,
            message=message,
        )
    finally:
        await connection.execute(
            "ROLLBACK TO SAVEPOINT registry_ingress_expected_error"
        )
        await connection.execute("RELEASE SAVEPOINT registry_ingress_expected_error")


def _snapshot(
    url, source, *, auth_method="none", auth_token=None, gpu_resource_id=None
):
    return json.dumps(
        {
            "url": url,
            "auth_method": auth_method,
            "auth_token": auth_token,
            "gpu_resource_id": gpu_resource_id,
            "source": source,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def test_registry_create_rows_singletons_and_observation_phases(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _connect(test_engine, role=_ROLE)
    registry_ids = [uuid.uuid4(), uuid.uuid4()]
    env_id, seed_id = registry_ids
    env_url = f"http://ingress-env-{env_id}.example.test"
    seed_url = f"http://ingress-seed-{seed_id}.example.test"
    try:
        env = await _create_registry(client, env_id, "Initial env name", env_url, "env")
        seed = await _create_registry(
            client,
            seed_id,
            "Seed name",
            seed_url,
            "seed",
            extra_params={"seed": _MARKER},
        )
        assert list(env.keys()) == _REGISTRY_COLUMNS
        assert list(seed.keys()) == _REGISTRY_COLUMNS
        assert env["state"] == "disconnected"
        assert env["auth_method"] == "none"
        assert env["auth_token"] is None
        assert env["gpu_resource_id"] is None
        assert env["vram_budget_mb"] is None
        assert env["eviction_priority"] == 0
        assert env["health_meta"] is None
        assert env["error_message"] is None
        assert env["last_checked_at"] is None
        assert _json(env["extra_params"]) == {}
        assert (
            await client.fetchrow(f"SELECT * FROM {_SINGLETON_POOL}($1::uuid)", seed_id)
            is None
        )

        found = await client.fetchrow(
            f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)", env_url
        )
        assert list(found.keys()) == _REGISTRY_COLUMNS
        assert found["id"] == env_id
        assert (
            await client.fetchrow(
                f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)",
                f"http://missing-{uuid.uuid4()}.example.test",
            )
            is None
        )
        env_candidates = await client.fetch(
            f"SELECT * FROM {_REGISTRY_CANDIDATES}($1::text)", "env"
        )
        live_candidates = await client.fetch(
            f"SELECT * FROM {_REGISTRY_CANDIDATES}($1::text)", "live"
        )
        assert env_id in {row["id"] for row in env_candidates}
        assert seed_id not in {row["id"] for row in env_candidates}
        assert env_id in {row["id"] for row in live_candidates}
        assert seed_id not in {row["id"] for row in live_candidates}

        null_pool_id, null_member_id = uuid.uuid4(), uuid.uuid4()
        created_pool = await client.fetchrow(
            f"SELECT * FROM {_CREATE_SINGLETON_POOL}("
            "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text)",
            env_id,
            null_pool_id,
            null_member_id,
            "Initial env name",
            None,
            None,
        )
        assert list(created_pool.keys()) == _POOL_COLUMNS
        assert created_pool["id"] == null_pool_id
        assert created_pool["enabled"] is False
        assert created_pool["routing_policy"] == "smooth_weighted_round_robin"
        assert created_pool["legacy_instance_id"] == env_id
        assert created_pool["routing_generation"] == 1
        assert created_pool["capability_fingerprint"] is None
        assert created_pool["capability_snapshot"] is None
        member = await owner.fetchrow(
            """SELECT id, pool_id, registry_id, traffic_state, weight
               FROM public.ml_backend_pool_members
               WHERE registry_id = $1""",
            env_id,
        )
        assert dict(member) == {
            "id": null_member_id,
            "pool_id": null_pool_id,
            "registry_id": env_id,
            "traffic_state": "active",
            "weight": 1,
        }

        actual_pool = await client.fetchrow(
            f"SELECT * FROM {_CREATE_SINGLETON_POOL}("
            "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text)",
            env_id,
            None,
            None,
            None,
            "this is intentionally invalid JSON",
            "not-a-fingerprint",
        )
        assert actual_pool["id"] == null_pool_id

        json_null_pool_id = uuid.uuid4()
        json_null_pool = await client.fetchrow(
            f"SELECT * FROM {_CREATE_SINGLETON_POOL}("
            "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text)",
            seed_id,
            json_null_pool_id,
            uuid.uuid4(),
            "Seed name",
            "null",
            None,
        )
        assert json_null_pool["id"] == json_null_pool_id
        assert _json(json_null_pool["capability_snapshot"]) is None
        assert (
            await client.fetchval(
                "SELECT pg_catalog.jsonb_typeof($1::jsonb)",
                json_null_pool["capability_snapshot"],
            )
            == "null"
        )

        checked_at = datetime.now(UTC)
        expected_env = _snapshot(env_url, "env")
        applied = await client.fetchval(
            f"SELECT {_APPLY_OBSERVATION}("
            "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text,"
            "$7::timestamptz,$8::text,$9::boolean,$10::text)",
            env_id,
            "env",
            "error",
            "Environment observed",
            False,
            "null",
            checked_at,
            "ignored by env profile",
            False,
            expected_env,
        )
        assert applied is not None
        env = await client.fetchrow(
            f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)", env_url
        )
        assert env["name"] == "Environment observed"
        assert env["state"] == "error"
        assert env["is_interactive"] is False
        assert env["last_checked_at"] == checked_at
        assert env["error_message"] is None
        assert _json(env["health_meta"]) is None

        await _assert_error(
            client,
            f"SELECT {_APPLY_OBSERVATION}("
            "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text,"
            "$7::timestamptz,$8::text,$9::boolean,$10::text)",
            env_id,
            "env",
            "connected",
            "Stale observation",
            True,
            '{"capabilities":{}}',
            datetime.now(UTC),
            None,
            True,
            _snapshot(f"{env_url}/stale", "env"),
            sqlstate="40001",
            message="registry ingress observation changed",
        )

        success_at = datetime.now(UTC)
        success = await client.fetchval(
            f"SELECT {_APPLY_OBSERVATION}("
            "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text,"
            "$7::timestamptz,$8::text,$9::boolean,$10::text)",
            env_id,
            "env",
            "connected",
            "Environment setup name",
            True,
            '{"capabilities":{"image":true}}',
            success_at,
            "ignored by env profile",
            True,
            expected_env,
        )
        assert success is not None
        env = await client.fetchrow(
            f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)", env_url
        )
        assert env["state"] == "connected"
        assert env["is_interactive"] is True
        assert _json(env["health_meta"]) == {"capabilities": {"image": True}}
        assert env["error_message"] is None

        await owner.execute(
            """UPDATE public.ml_backend_registry
               SET name = 'Seed before probe',
                   is_interactive = true,
                   health_meta = '{"prior":true}'::jsonb,
                   error_message = 'previous error'
               WHERE id = $1""",
            seed_id,
        )
        seed_failure_at = datetime.now(UTC)
        failed_seed = await client.fetchval(
            f"SELECT {_APPLY_OBSERVATION}("
            "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text,"
            "$7::timestamptz,$8::text,$9::boolean,$10::text)",
            seed_id,
            "seed",
            "error",
            None,
            False,
            "intentionally invalid JSON, must be ignored",
            seed_failure_at,
            "TimeoutException: setup unavailable",
            False,
            _snapshot(seed_url, "seed"),
        )
        assert failed_seed is not None
        seed = await client.fetchrow(
            f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)", seed_url
        )
        assert seed["name"] == "Seed before probe"
        assert seed["state"] == "error"
        assert seed["is_interactive"] is True
        assert _json(seed["health_meta"]) == {"prior": True}
        assert seed["error_message"] == "TimeoutException: setup unavailable"
        assert seed["last_checked_at"] == seed_failure_at

        seed_success_at = datetime.now(UTC)
        await client.fetchval(
            f"SELECT {_APPLY_OBSERVATION}("
            "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text,"
            "$7::timestamptz,$8::text,$9::boolean,$10::text)",
            seed_id,
            "seed",
            "connected",
            "Seed setup name",
            False,
            '{"capabilities":{"video":true}}',
            seed_success_at,
            "ignored on seed success",
            True,
            _snapshot(seed_url, "seed"),
        )
        seed = await client.fetchrow(
            f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)", seed_url
        )
        assert seed["name"] == "Seed setup name"
        assert seed["state"] == "connected"
        assert seed["is_interactive"] is False
        assert _json(seed["health_meta"]) == {"capabilities": {"video": True}}
        assert seed["error_message"] is None

        disconnected_at = await client.fetchval(
            f"SELECT {_DISCONNECT}($1::uuid,$2::text)", env_id, env_url
        )
        assert disconnected_at is not None
        env = await client.fetchrow(
            f"SELECT * FROM {_REGISTRY_BY_URL}($1::text)", env_url
        )
        assert env["state"] == "disconnected"
        assert _json(env["health_meta"]) == {"capabilities": {"image": True}}
        assert env["last_checked_at"] == success_at
        repeated_disconnect = await client.fetchval(
            f"SELECT {_DISCONNECT}($1::uuid,$2::text)", env_id, env_url
        )
        assert repeated_disconnect == disconnected_at

        await _assert_error(
            client,
            f"SELECT {_DISCONNECT}($1::uuid,$2::text)",
            env_id,
            f"{env_url}/stale",
            sqlstate="40001",
            message="registry ingress environment row changed",
        )
    finally:
        await client.close()
        await _cleanup_registries(owner, registry_ids)
        await owner.close()


async def test_seed_marker_retains_gpu_membership_mutation_and_managed_fence(
    test_engine,
):
    from tests.test_inference_health_data_native import _seed_world, _cleanup_world

    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _connect(test_engine, role=_ROLE)
    world = await _seed_world(owner)
    registry_id = world["registry_id"]
    try:
        await owner.execute(
            "UPDATE ml_backend_registry SET gpu_resource_id=$2,vram_budget_mb=1024 WHERE id=$1",
            registry_id,
            world["gpu_resource_id"],
        )
        original_epoch = await owner.fetchval(
            "SELECT membership_epoch FROM gpu_backend_memberships WHERE backend_registry_id=$1 AND state='pending'",
            registry_id,
        )
        await client.fetchrow(
            f"SELECT * FROM {_TAG_SEED}($1,$2)", registry_id, json.dumps(_MARKER)
        )
        assert (
            await owner.fetchval(
                "SELECT membership_epoch FROM gpu_backend_memberships WHERE backend_registry_id=$1 AND state='pending'",
                registry_id,
            )
            == original_epoch + 1
        )
        assert (
            _json(
                await owner.fetchval(
                    "SELECT extra_params FROM ml_backend_registry WHERE id=$1",
                    registry_id,
                )
            )["seed"]
            == _MARKER
        )
        tx = owner.transaction()
        await tx.start()
        try:
            await owner.execute(
                "UPDATE ml_backend_registry SET extra_params=extra_params-'seed' WHERE id=$1",
                registry_id,
            )
            await owner.execute(
                "UPDATE gpu_backend_fences SET runtime_epoch_high_water=1 WHERE backend_registry_id=$1",
                registry_id,
            )
            await owner.execute("SET LOCAL ROLE inference_registry_ingress_client")
            await owner.fetchrow(
                f"SELECT * FROM {_TAG_SEED}($1,$2)", registry_id, json.dumps(_MARKER)
            )
        except asyncpg.CheckViolationError as exc:
            assert exc.constraint_name == "ck_gpu_backend_membership_managed_mutation"
        else:
            pytest.fail("the original managed GPU mutation fence must remain active")
        finally:
            await tx.rollback()
        assert (
            _json(
                await owner.fetchval(
                    "SELECT extra_params FROM ml_backend_registry WHERE id=$1",
                    registry_id,
                )
            )["seed"]
            == _MARKER
        )
    finally:
        await client.close()
        try:
            await _cleanup_world(owner, world)
        finally:
            await owner.close()


async def test_seed_marker_merge_preserves_legacy_falsey_json_behavior(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _connect(test_engine, role=_ROLE)
    registry_id = uuid.uuid4()
    url = f"http://seed-marker-{registry_id}.example.test"
    try:
        await _create_registry(client, registry_id, "Seed marker", url, "seed")

        falsey_values = (
            None,
            "null",
            "false",
            "0",
            "0.0",
            '""',
            "[]",
        )
        for json_value in falsey_values:
            if json_value is None:
                await owner.execute(
                    "UPDATE public.ml_backend_registry SET extra_params = NULL WHERE id = $1",
                    registry_id,
                )
            else:
                await owner.execute(
                    "UPDATE public.ml_backend_registry "
                    "SET extra_params = $2::jsonb WHERE id = $1",
                    registry_id,
                    json_value,
                )
            reply = await client.fetchrow(
                f"SELECT * FROM {_TAG_SEED}($1::uuid,$2::text)",
                registry_id,
                json.dumps(_MARKER, separators=(",", ":")),
            )
            assert _json(reply["extra_params"]) == {"seed": _MARKER}
            assert reply["updated_at"] is not None

        await owner.execute(
            "UPDATE public.ml_backend_registry "
            "SET extra_params = $2::jsonb WHERE id = $1",
            registry_id,
            json.dumps({"keep": "value", "seed": {"old": True}}),
        )
        merged = await client.fetchrow(
            f"SELECT * FROM {_TAG_SEED}($1::uuid,$2::text)",
            registry_id,
            json.dumps(_MARKER, separators=(",", ":")),
        )
        assert _json(merged["extra_params"]) == {
            "keep": "value",
            "seed": _MARKER,
        }
        repeated = await client.fetchrow(
            f"SELECT * FROM {_TAG_SEED}($1::uuid,$2::text)",
            registry_id,
            json.dumps(_MARKER, separators=(",", ":")),
        )
        assert _json(repeated["extra_params"]) == _json(merged["extra_params"])
        assert repeated["updated_at"] == merged["updated_at"]

        await owner.execute(
            "UPDATE public.ml_backend_registry "
            "SET extra_params = 'true'::jsonb WHERE id = $1",
            registry_id,
        )
        await _assert_error(
            client,
            f"SELECT * FROM {_TAG_SEED}($1::uuid,$2::text)",
            registry_id,
            json.dumps(_MARKER, separators=(",", ":")),
            sqlstate="22023",
            message="invalid Registry seed extra parameters",
        )
    finally:
        await client.close()
        await _cleanup_registries(owner, [registry_id])
        await owner.close()


async def test_registry_environment_sync_receipts_are_transaction_bound_and_private(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _connect(test_engine, role=_ROLE)
    request_id = uuid.uuid4()
    rollback_request_id = uuid.uuid4()
    command_hash = "a" * 64
    try:
        assert (
            _json(
                await client.fetchval(
                    f"SELECT {_SYNC_RESULT}($1::uuid,$2::text)",
                    request_id,
                    command_hash,
                )
            )
            is None
        )

        transaction = client.transaction()
        await transaction.start()
        pending = _json(
            await client.fetchval(
                f"SELECT {_RECORD_SYNC_RESULT}($1::uuid,$2::text,$3::integer)",
                request_id,
                command_hash,
                3,
            )
        )
        assert pending == {
            "schema": "aap-inference-registry-env-sync-result/v1",
            "request_id": str(request_id),
            "command_sha256": command_hash,
            "state": "caller_pending",
            "observed_urls": 3,
        }
        assert (
            _json(
                await client.fetchval(
                    f"SELECT {_SYNC_RESULT}($1::uuid,$2::text)",
                    request_id,
                    command_hash,
                )
            )
            == pending
        )
        await _assert_error_in_transaction(
            client,
            f"SELECT {_RECORD_SYNC_RESULT}($1::uuid,$2::text,$3::integer)",
            request_id,
            command_hash,
            4,
            sqlstate="22023",
            message="Registry environment sync request identity reused",
        )
        await _assert_error_in_transaction(
            client,
            f"SELECT {_SYNC_RESULT}($1::uuid,$2::text)",
            request_id,
            "b" * 64,
            sqlstate="22023",
            message="Registry environment sync request identity reused",
        )
        await transaction.commit()

        committed = _json(
            await client.fetchval(
                f"SELECT {_SYNC_RESULT}($1::uuid,$2::text)",
                request_id,
                command_hash,
            )
        )
        assert {**pending, "state": "committed"} == committed
        assert (
            _json(
                await client.fetchval(
                    f"SELECT {_RECORD_SYNC_RESULT}($1::uuid,$2::text,$3::integer)",
                    request_id,
                    command_hash,
                    3,
                )
            )
            == committed
        )

        rollback = client.transaction()
        await rollback.start()
        await client.fetchval(
            f"SELECT {_RECORD_SYNC_RESULT}($1::uuid,$2::text,$3::integer)",
            rollback_request_id,
            "c" * 64,
            0,
        )
        await rollback.rollback()
        assert (
            await client.fetchval(
                f"SELECT {_SYNC_RESULT}($1::uuid,$2::text)",
                rollback_request_id,
                "c" * 64,
            )
            is None
        )

        table_columns = await owner.fetch(
            """SELECT column_name
               FROM information_schema.columns
               WHERE table_schema = $1 AND table_name = 'env_sync_receipts'
               ORDER BY ordinal_position""",
            _SCHEMA,
        )
        assert [row["column_name"] for row in table_columns] == [
            "request_id",
            "command_sha256",
            "observed_urls",
            "created_xid",
            "created_at",
        ]
        assert not await owner.fetchval(
            "SELECT pg_catalog.has_table_privilege($1,$2,'SELECT')",
            _ROLE,
            _RECEIPTS,
        )
    finally:
        await client.close()
        await _cleanup_registries(owner, [], [request_id, rollback_request_id])
        await owner.close()


async def test_registry_ingress_client_has_only_the_eleven_fixed_functions(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _connect(test_engine, role=_ROLE)
    try:
        policy = _json(await client.fetchval(f"SELECT {_POLICY}"))
        assert policy == _POLICY_VALUE

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
        for table in (
            _RECEIPTS,
            "public.ml_backend_registry",
            "public.ml_backend_service_pools",
            "public.ml_backend_pool_members",
        ):
            assert not await owner.fetchval(
                "SELECT pg_catalog.has_table_privilege($1,$2,'SELECT')",
                _ROLE,
                table,
            )
            assert not await owner.fetchval(
                "SELECT pg_catalog.has_table_privilege($1,$2,'UPDATE')",
                _ROLE,
                table,
            )
    finally:
        await client.close()
        await owner.close()
