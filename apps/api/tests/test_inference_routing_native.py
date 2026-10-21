"""Owned PostgreSQL acceptance for fixed Native durable-routing actions."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

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

_SCHEMA = "aap_inference_routing_v1"
_ROLE = "inference_routing_client"
_POLICY = f"{_SCHEMA}.routing_policy()"
_STEP = f"{_SCHEMA}.routing_step"
_REGISTRY_FIELDS = {
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
}
_POOL_FIELDS = {
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
}
_MEMBER_FIELDS = {
    "id",
    "pool_id",
    "registry_id",
    "traffic_state",
    "weight",
    "created_at",
    "updated_at",
}
_ACTIONS = [
    "command_status",
    "record_command",
    "admission_snapshot",
    "reserve_scope",
    "scope_status",
    "begin_attempt",
    "settle_attempt",
    "heartbeat_scope",
    "close_scope",
    "quiescence",
    "prepare_control",
    "finish_control",
    "prepare_epoch",
    "finish_epoch",
    "capture_transport_subject",
    "open_transport_subject",
    "begin_control_attempt",
    "settle_control_attempt",
    "runtime_snapshot",
]


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect(test_engine, *, role: str | None = None):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=15
    )
    if role is not None:
        await connection.execute(f"SET ROLE {role}")
        assert await connection.fetchval("SELECT current_user") == role
    return connection


async def _step(connection, action: str, args: dict) -> dict:
    raw = await connection.fetchval(
        f"SELECT {_STEP}($1::text, $2::text)",
        action,
        json.dumps(args, ensure_ascii=False, separators=(",", ":")),
    )
    return json.loads(raw)


async def _seed(owner):
    registry_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await owner.execute(
        """INSERT INTO public.ml_backend_registry (
               id, name, url, state, is_interactive, auth_method, auth_token,
               extra_params, gpu_resource_id, vram_budget_mb, eviction_priority,
               health_meta, source, error_message, last_checked_at
           ) VALUES (
               $1, $2, $3, 'connected', false, 'token', $4,
               $5::jsonb, NULL, NULL, 0, $6::jsonb, 'manual', NULL, now()
           )""",
        registry_id,
        f"Native routing {registry_id}",
        f"http://127.0.0.1/{registry_id}",
        f"routing-secret-{uuid.uuid4().hex}",
        json.dumps({"max_concurrency": 2}),
        json.dumps({"capabilities": {"model_version": "routing-test"}}),
    )
    await owner.execute(
        """INSERT INTO public.ml_backend_service_pools (
               id, name, enabled, routing_policy, legacy_instance_id,
               routing_generation, capability_fingerprint, capability_snapshot
           ) VALUES (
               $1, $2, true, 'smooth_weighted_round_robin', $3, 1, NULL, NULL
           )""",
        pool_id,
        f"Native routing pool {pool_id}",
        registry_id,
    )
    await owner.execute(
        """INSERT INTO public.ml_backend_pool_members (
               id, pool_id, registry_id, traffic_state, weight
           ) VALUES ($1, $2, $3, 'active', 1)""",
        member_id,
        pool_id,
        registry_id,
    )
    return {"registry_id": registry_id, "pool_id": pool_id, "member_id": member_id}


async def _cleanup(owner, world) -> None:
    registry_id = world["registry_id"]
    await owner.execute(
        "DELETE FROM aap_inference_routing_v1.control_attempts WHERE control_id IN "
        "(SELECT id FROM aap_inference_routing_v1.controls WHERE registry_id=$1)",
        registry_id,
    )
    await owner.execute(
        "DELETE FROM aap_inference_routing_v1.attempts WHERE scope_id IN "
        "(SELECT id FROM aap_inference_routing_v1.scopes WHERE registry_id=$1)",
        registry_id,
    )
    await owner.execute(
        "DELETE FROM aap_inference_routing_v1.scopes WHERE registry_id=$1", registry_id
    )
    await owner.execute(
        "DELETE FROM aap_inference_routing_v1.controls WHERE registry_id=$1",
        registry_id,
    )
    await owner.execute(
        "DELETE FROM aap_inference_routing_v1.weights WHERE registry_id=$1", registry_id
    )
    await owner.execute(
        "DELETE FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
        registry_id,
    )
    await owner.execute(
        "DELETE FROM public.project_ml_backend_pool WHERE pool_id=$1", world["pool_id"]
    )
    await owner.execute(
        "DELETE FROM public.ml_backend_pool_members WHERE registry_id=$1", registry_id
    )
    await owner.execute(
        "DELETE FROM public.ml_backend_service_pools WHERE id=$1", world["pool_id"]
    )
    await owner.execute(
        "DELETE FROM public.ml_backend_registry WHERE id=$1", registry_id
    )


async def _identity_hash(owner, registry_id: uuid.UUID) -> str:
    return await owner.fetchval(
        """SELECT pg_catalog.encode(public.digest(
                   pg_catalog.convert_to(pg_catalog.jsonb_build_object(
                       'url', registry.url,
                       'auth_method', registry.auth_method,
                       'auth_token', registry.auth_token,
                       'gpu_resource_id', registry.gpu_resource_id,
                       'extra_params', registry.extra_params
                   )::text, 'UTF8'), 'sha256'), 'hex')
           FROM public.ml_backend_registry AS registry WHERE registry.id=$1""",
        registry_id,
    )


async def test_routing_core_policy_keeps_its_two_fixed_functions(test_engine):
    await assert_owned_test_database(test_engine)
    connection = await _connect(test_engine, role=_ROLE)
    transaction = connection.transaction()
    try:
        await transaction.start()
        policy = json.loads(await connection.fetchval(f"SELECT {_POLICY}"))
        assert policy == {
            "schema": "aap-inference-routing-native/v1",
            "data_protocol": "aap-inference-routing-data/v1",
            "transaction": "caller_owned",
            "functions": [
                f"{_SCHEMA}.routing_policy()",
                f"{_SCHEMA}.routing_step(text,text)",
            ],
            "actions": _ACTIONS,
            "ready": True,
        }
        await connection.execute("SAVEPOINT routing_table_denial")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(
                f"SELECT secret FROM {_SCHEMA}.authority_secret WHERE singleton"
            )
        await connection.execute("ROLLBACK TO SAVEPOINT routing_table_denial")
        await connection.execute("RELEASE SAVEPOINT routing_table_denial")
        await connection.execute("SAVEPOINT routing_function_denial")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(f"SELECT {_SCHEMA}._routing_secret()")
        await connection.execute("ROLLBACK TO SAVEPOINT routing_function_denial")
        await connection.execute("RELEASE SAVEPOINT routing_function_denial")
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    finally:
        await connection.close()


async def test_epoch_without_control_auth_does_not_leave_an_unrecoverable_hold(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    client = await _connect(test_engine, role=_ROLE)
    try:
        await owner.execute(
            "UPDATE public.ml_backend_registry SET auth_method='none',auth_token=NULL WHERE id=$1",
            world["registry_id"],
        )
        async with client.transaction():
            reply = await _step(
                client,
                "prepare_epoch",
                {
                    "control_id": str(uuid.uuid4()),
                    "registry_id": str(world["registry_id"]),
                },
            )
        assert reply == {
            "schema": "aap-inference-routing-data/v1",
            "status": "rejected",
            "reason": "backend_not_ready",
        }
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.controls WHERE registry_id=$1",
                world["registry_id"],
            )
            == 0
        )
        # Configuration can still be repaired before any management HTTP is sent.
        await owner.execute(
            "UPDATE public.ml_backend_registry SET auth_method='token',auth_token='repaired' WHERE id=$1",
            world["registry_id"],
        )
    finally:
        await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()


async def test_scope_cannot_issue_a_management_operation_token(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    client = await _connect(test_engine, role=_ROLE)
    try:
        async with client.transaction():
            snapshot = await _step(
                client,
                "admission_snapshot",
                {
                    "pool_id": None,
                    "registry_id": str(world["registry_id"]),
                    "project_id": None,
                    "mode": "direct",
                    "required_instance_id": None,
                    "health_max_age_seconds": 60,
                },
            )
            assert snapshot["status"] == "ok"
            reply = await _step(
                client,
                "reserve_scope",
                {
                    "snapshot": snapshot["snapshot"],
                    "candidate_ids": [str(world["registry_id"])],
                    "scope_id": str(uuid.uuid4()),
                    "command_sha256": "c" * 64,
                    "owner": "scope-spoof-test",
                    "operation": "control:unload",
                    "ttl_ms": 30000,
                },
            )
            assert reply["status"] == "rejected"
            assert reply["reason"] == "invalid_command"
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
                world["registry_id"],
            )
            == 0
        )
    finally:
        await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()


async def test_routing_snapshot_scope_attempt_and_writer_fence(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    registry_id = world["registry_id"]
    pool_id = world["pool_id"]
    identity_hash = await _identity_hash(owner, registry_id)
    client = await _connect(test_engine, role=_ROLE)
    transaction = client.transaction()
    started = False
    try:
        await transaction.start()
        started = True
        snapshot = await _step(
            client,
            "admission_snapshot",
            {
                "pool_id": str(pool_id),
                "registry_id": None,
                "project_id": None,
                "mode": "off",
                "required_instance_id": None,
                "health_max_age_seconds": 60,
            },
        )
        assert snapshot["status"] == "ok"
        assert set(snapshot["pool"]) == _POOL_FIELDS
        assert len(snapshot["members"]) == 1
        assert set(snapshot["members"][0]["member"]) == _MEMBER_FIELDS
        assert set(snapshot["members"][0]["registry"]) == _REGISTRY_FIELDS
        scope_id = uuid.uuid4()
        reserved = await _step(
            client,
            "reserve_scope",
            {
                "snapshot": snapshot["snapshot"],
                "candidate_ids": [str(registry_id)],
                "scope_id": str(scope_id),
                "command_sha256": "a" * 64,
                "owner": "api:routing-native-test",
                "operation": "predict",
                "ttl_ms": 30000,
            },
        )
        assert reserved["status"] == "ok"
        capability = reserved["scope"]["capability"]
        attempt_id = uuid.uuid4()
        began = await _step(
            client,
            "begin_attempt",
            {
                "scope_id": str(scope_id),
                "capability": capability,
                "attempt_id": str(attempt_id),
                "method": "POST",
                "path": "/predict",
                "transport_identity": {"identity_sha256": identity_hash},
            },
        )
        assert began["status"] == "ok"
        assert began["attempt_id"] == str(attempt_id)
        assert began["headers"] == {}
        closed = await _step(
            client,
            "close_scope",
            {"scope_id": str(scope_id), "capability": capability, "outcome": "cancel"},
        )
        assert closed["status"] == "ok"
        assert closed["scope"]["state"] == "uncertain"
        status = await _step(
            client,
            "scope_status",
            {"scope_id": str(scope_id), "capability": capability},
        )
        assert status["attempts"] == [
            {
                "id": str(attempt_id),
                "state": "uncertain",
                "status_code": None,
                "reason": "scope_closed",
            }
        ]
        await transaction.commit()
        started = False

        owner_transaction = owner.transaction()
        await owner_transaction.start()
        await owner.execute("SAVEPOINT routing_config_fence")
        with pytest.raises(asyncpg.PostgresError) as caught:
            await owner.execute(
                "UPDATE public.ml_backend_registry SET url=$2 WHERE id=$1",
                registry_id,
                f"http://127.0.0.1/changed-{registry_id}",
            )
        assert caught.value.sqlstate == "55P03"
        await owner.execute("ROLLBACK TO SAVEPOINT routing_config_fence")
        await owner.execute("RELEASE SAVEPOINT routing_config_fence")

        await owner.execute(
            "UPDATE public.ml_backend_pool_members SET traffic_state='draining' "
            "WHERE registry_id=$1",
            registry_id,
        )
        await owner.execute(
            "UPDATE public.ml_backend_service_pools SET routing_generation=routing_generation+1 "
            "WHERE id=$1",
            pool_id,
        )
        await owner_transaction.commit()
        check = client.transaction()
        await check.start()
        result = await _step(client, "quiescence", {"registry_id": str(registry_id)})
        assert result == {
            "schema": "aap-inference-routing-data/v1",
            "status": "rejected",
            "reason": "member_not_quiescent",
        }
        await check.rollback()
    finally:
        if started:
            await transaction.rollback()
        await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()


async def test_runtime_snapshot_reports_physical_attempts_without_expiring_scopes(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    client = await _connect(test_engine, role=_ROLE)
    registry_id = world["registry_id"]
    pool_id = world["pool_id"]
    absent_registry_id = uuid.uuid4()
    try:
        state = await owner.fetchrow(
            """SELECT config_revision, eligibility_revision, authority_epoch, realm
               FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1""",
            registry_id,
        )
        assert state is not None

        async def add_scope(scope_id, state_name, *, closed, seconds):
            await owner.execute(
                """INSERT INTO aap_inference_routing_v1.scopes (
                       id, command_sha256, registry_id, pool_id, project_id, mode,
                       owner, operation, generation, config_revision, eligibility_revision,
                       authority_epoch, realm, state, closed, heartbeat_deadline
                   ) VALUES (
                       $1, repeat('a', 64), $2, $3, NULL, 'off',
                       'api:runtime-snapshot-test', 'predict', 1, $4, $5,
                       $6, $7, $8, $9,
                       pg_catalog.statement_timestamp()
                           + ($10::double precision * INTERVAL '1 second')
                   )""",
                scope_id,
                registry_id,
                pool_id,
                state["config_revision"],
                state["eligibility_revision"],
                state["authority_epoch"],
                state["realm"],
                state_name,
                closed,
                seconds,
            )

        expired_reserved_id = uuid.uuid4()
        live_reserved_id = uuid.uuid4()
        uncertain_scope_id = uuid.uuid4()
        active_attempt_scope_id = uuid.uuid4()
        await add_scope(expired_reserved_id, "reserved", closed=False, seconds=-60)
        await add_scope(live_reserved_id, "reserved", closed=False, seconds=300)
        await add_scope(uncertain_scope_id, "uncertain", closed=True, seconds=-300)
        await add_scope(active_attempt_scope_id, "finished", closed=True, seconds=-300)

        uncertain_attempt_id = uuid.uuid4()
        active_attempt_id = uuid.uuid4()
        for attempt_id, scope_id, attempt_state, reason in (
            (uncertain_attempt_id, uncertain_scope_id, "uncertain", "request_timeout"),
            (active_attempt_id, active_attempt_scope_id, "active", None),
        ):
            await owner.execute(
                """INSERT INTO aap_inference_routing_v1.attempts (
                       id, scope_id, method, path, transport_identity_sha256,
                       state, status_code, reason
                   ) VALUES ($1, $2, 'POST', '/predict', repeat('b', 64), $3, NULL, $4)""",
                attempt_id,
                scope_id,
                attempt_state,
                reason,
            )

        control_id = uuid.uuid4()
        await owner.execute(
            """INSERT INTO aap_inference_routing_v1.controls (
                   id, registry_id, kind, phase, new_epoch, retired_epoch, challenge,
                   backend_incarnation, config_revision, realm, expected_topology
               ) VALUES ($1, $2, 'unload', 'prepared', NULL, NULL, repeat('c', 64),
                         NULL, $3, $4, NULL)""",
            control_id,
            registry_id,
            state["config_revision"],
            state["realm"],
        )
        expired_before = await owner.fetchrow(
            "SELECT state, updated_at FROM aap_inference_routing_v1.scopes WHERE id=$1",
            expired_reserved_id,
        )

        reply = await _step(
            client,
            "runtime_snapshot",
            {"registry_ids": [str(absent_registry_id), str(registry_id)]},
        )
        assert set(reply) == {"schema", "status", "observed_at", "registries"}
        assert reply["schema"] == "aap-inference-routing-data/v1"
        assert reply["status"] == "ok"
        observed_at = datetime.fromisoformat(
            reply["observed_at"].replace("Z", "+00:00")
        )
        assert observed_at.tzinfo == timezone.utc
        assert reply["registries"] == [
            {
                "registry_id": str(registry_id),
                "open_scopes": 3,
                "active_attempts": 1,
                "uncertain_attempts": 1,
                "continuity": "unverified",
                "authority_epoch": None,
                "backend_incarnation": None,
                "control_id": str(control_id),
            }
        ]
        expired_after = await owner.fetchrow(
            "SELECT state, updated_at FROM aap_inference_routing_v1.scopes WHERE id=$1",
            expired_reserved_id,
        )
        assert expired_after == expired_before

        empty = await _step(client, "runtime_snapshot", {"registry_ids": []})
        assert empty["registries"] == []
        for registry_ids in (
            [str(registry_id), str(registry_id)],
            [str(uuid.uuid4()) for _ in range(513)],
        ):
            with pytest.raises(asyncpg.PostgresError) as caught:
                await _step(client, "runtime_snapshot", {"registry_ids": registry_ids})
            assert caught.value.sqlstate == "22023"
    finally:
        await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()
