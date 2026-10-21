"""Owned PostgreSQL state-machine checks for routing-control recovery."""

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

_SCHEMA = "aap_inference_routing_v1"
_ROLE = "inference_routing_client"
_STEP = f"{_SCHEMA}.routing_step"


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


async def _step(connection, action: str, arguments: dict) -> dict:
    raw = await connection.fetchval(
        f"SELECT {_STEP}($1::text, $2::text)",
        action,
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )
    return json.loads(raw)


async def _seed(owner):
    registry_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    member_id = uuid.uuid4()
    old_epoch = uuid.uuid4()
    old_incarnation = uuid.uuid4()
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
        f"Native recovery {registry_id}",
        f"http://127.0.0.1/{registry_id}",
        f"routing-secret-{uuid.uuid4().hex}",
        json.dumps({"max_concurrency": 2}),
        json.dumps({"capabilities": {"model_version": "routing-recovery-test"}}),
    )
    await owner.execute(
        """INSERT INTO public.ml_backend_service_pools (
               id, name, enabled, routing_policy, legacy_instance_id,
               routing_generation, capability_fingerprint, capability_snapshot
           ) VALUES (
               $1, $2, true, 'smooth_weighted_round_robin', $3, 1, NULL, NULL
           )""",
        pool_id,
        f"Native recovery pool {pool_id}",
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
    await owner.execute(
        "UPDATE public.ml_backend_pool_members SET traffic_state='draining' WHERE id=$1",
        member_id,
    )
    await owner.execute(
        """UPDATE aap_inference_routing_v1.registry_state
           SET authority_epoch=$2, backend_incarnation=$3, continuity='ready',
               epoch_key=public.gen_random_bytes(32)
           WHERE registry_id=$1""",
        registry_id,
        old_epoch,
        old_incarnation,
    )
    realm = await owner.fetchval("SELECT aap_inference_routing_v1._routing_realm()")
    return {
        "registry_id": registry_id,
        "pool_id": pool_id,
        "member_id": member_id,
        "old_epoch": old_epoch,
        "old_incarnation": old_incarnation,
        "realm": realm,
    }


async def _cleanup(owner, world) -> None:
    registry_id = world["registry_id"]
    await owner.execute(
        """DELETE FROM aap_inference_routing_v1.control_attempts
           WHERE control_id IN (
               SELECT id FROM aap_inference_routing_v1.controls WHERE registry_id=$1
           )""",
        registry_id,
    )
    await owner.execute(
        """DELETE FROM aap_inference_routing_v1.attempts
           WHERE scope_id IN (
               SELECT id FROM aap_inference_routing_v1.scopes WHERE registry_id=$1
           )""",
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


async def _add_open_scope(owner, world, scope_id: uuid.UUID) -> None:
    state = await owner.fetchrow(
        """SELECT config_revision, eligibility_revision, authority_epoch, realm
           FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1""",
        world["registry_id"],
    )
    await owner.execute(
        """INSERT INTO aap_inference_routing_v1.scopes (
               id, command_sha256, registry_id, pool_id, project_id, mode,
               owner, operation, generation, config_revision, eligibility_revision,
               authority_epoch, realm, state, closed, heartbeat_deadline
           ) VALUES (
               $1, repeat('d', 64), $2, $3, NULL, 'off',
               'api:control-recovery-test', 'predict', 1, $4, $5,
               $6, $7, 'active', false,
               pg_catalog.statement_timestamp() + INTERVAL '1 hour'
           )""",
        scope_id,
        world["registry_id"],
        world["pool_id"],
        state["config_revision"],
        state["eligibility_revision"],
        state["authority_epoch"],
        state["realm"],
    )


async def test_unstarted_control_can_close_without_claiming_a_backend_response(
    test_engine,
):
    """Native state machine only: no physical completion proof is fabricated."""
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    client = await _connect(test_engine, role=_ROLE)
    control_id = uuid.uuid4()
    try:
        async with client.transaction():
            control = await _step(
                client,
                "prepare_control",
                {
                    "registry_id": str(world["registry_id"]),
                    "control_id": str(control_id),
                    "kind": "unload",
                    "expected_topology": None,
                },
            )
        assert control["status"] == "ok"
        async with client.transaction():
            closed = await _step(
                client,
                "finish_control",
                {"control_id": str(control_id), "capability": control["capability"]},
            )
        assert closed["phase"] == "completed"
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.control_attempts WHERE control_id=$1",
                control_id,
            )
            == 0
        )
        async with client.transaction():
            late = await _step(
                client,
                "begin_control_attempt",
                {
                    "control_id": str(control_id),
                    "capability": control["capability"],
                    "attempt_id": str(uuid.uuid4()),
                    "method": "POST",
                    "path": "/unload",
                    "transport_identity": {
                        "identity_sha256": await _identity_hash(
                            owner, world["registry_id"]
                        )
                    },
                },
            )
        assert late["status"] == "rejected"
        async with client.transaction():
            successor = await _step(
                client,
                "prepare_control",
                {
                    "registry_id": str(world["registry_id"]),
                    "control_id": str(uuid.uuid4()),
                    "kind": "reload",
                    "expected_topology": None,
                },
            )
        assert successor["status"] == "ok"
    finally:
        await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()


async def test_unresolved_control_recovers_under_same_id_after_scope_gate(test_engine):
    """Exercise Native recovery; Root's HTTP suite proves the real drain/bind exchange."""
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    client = await _connect(test_engine, role=_ROLE)
    registry_id = world["registry_id"]
    control_id = uuid.uuid4()
    blocker_scope_id = uuid.uuid4()
    try:
        client_tx = client.transaction()
        await client_tx.start()
        quiescence = await _step(
            client, "quiescence", {"registry_id": str(registry_id)}
        )
        assert quiescence["status"] == "ok"
        owner_tx = owner.transaction()
        await owner_tx.start()
        await owner.execute("SAVEPOINT routing_quiescence_lock")
        with pytest.raises(asyncpg.PostgresError) as lock_error:
            await owner.fetchval(
                """SELECT id FROM public.ml_backend_registry
                   WHERE id=$1 FOR SHARE NOWAIT""",
                registry_id,
            )
        assert lock_error.value.sqlstate == "55P03"
        await owner.execute("ROLLBACK TO SAVEPOINT routing_quiescence_lock")
        await owner.execute("RELEASE SAVEPOINT routing_quiescence_lock")
        await owner_tx.commit()

        prepared = await _step(
            client,
            "prepare_control",
            {
                "control_id": str(control_id),
                "registry_id": str(registry_id),
                "kind": "unload",
                "expected_topology": None,
            },
        )
        assert prepared["status"] == "ok"
        old_capability = prepared["capability"]
        await client_tx.commit()

        identity_hash = await _identity_hash(owner, registry_id)
        failure_attempt_id = uuid.uuid4()
        uncertain_attempt_id = uuid.uuid4()
        async with client.transaction():
            first_begin = await _step(
                client,
                "begin_control_attempt",
                {
                    "control_id": str(control_id),
                    "capability": old_capability,
                    "attempt_id": str(failure_attempt_id),
                    "method": "POST",
                    "path": "/unload",
                    "transport_identity": {"identity_sha256": identity_hash},
                },
            )
            assert first_begin["status"] == "ok"
            failed_response = await _step(
                client,
                "settle_control_attempt",
                {
                    "control_id": str(control_id),
                    "capability": old_capability,
                    "attempt_id": str(failure_attempt_id),
                    "kind": "response",
                    "status_code": 503,
                    "reason": None,
                },
            )
            assert failed_response["status"] == "ok"
            second_begin = await _step(
                client,
                "begin_control_attempt",
                {
                    "control_id": str(control_id),
                    "capability": old_capability,
                    "attempt_id": str(uncertain_attempt_id),
                    "method": "POST",
                    "path": "/unload",
                    "transport_identity": {"identity_sha256": identity_hash},
                },
            )
            assert second_begin["status"] == "ok"
            uncertain = await _step(
                client,
                "settle_control_attempt",
                {
                    "control_id": str(control_id),
                    "capability": old_capability,
                    "attempt_id": str(uncertain_attempt_id),
                    "kind": "uncertain",
                    "status_code": None,
                    "reason": "request_timeout",
                },
            )
            assert uncertain["status"] == "ok"

        await _add_open_scope(owner, world, blocker_scope_id)
        blocked = await _step(
            client,
            "prepare_epoch",
            {"control_id": str(control_id), "registry_id": str(registry_id)},
        )
        assert blocked == {
            "schema": "aap-inference-routing-data/v1",
            "status": "rejected",
            "reason": "member_not_quiescent",
        }
        before_recovery = await owner.fetchrow(
            """SELECT kind, phase, recovered_kind
               FROM aap_inference_routing_v1.controls WHERE id=$1""",
            control_id,
        )
        assert tuple(before_recovery) == ("unload", "uncertain", None)
        await owner.execute(
            "DELETE FROM aap_inference_routing_v1.scopes WHERE id=$1", blocker_scope_id
        )

        async with client.transaction():
            recovered = await _step(
                client,
                "prepare_epoch",
                {"control_id": str(control_id), "registry_id": str(registry_id)},
            )
            assert recovered["status"] == "ok"
            assert recovered["control_id"] == str(control_id)
            assert recovered["phase"] == "prepared"
            assert recovered["retired_epoch"] == str(world["old_epoch"])

        new_capability = await owner.fetchval(
            f"SELECT {_SCHEMA}._routing_control_capability($1)", control_id
        )
        assert new_capability != old_capability
        old_token_settlement = await _step(
            client,
            "settle_control_attempt",
            {
                "control_id": str(control_id),
                "capability": old_capability,
                "attempt_id": str(uncertain_attempt_id),
                "kind": "uncertain",
                "status_code": None,
                "reason": "request_timeout",
            },
        )
        assert old_token_settlement["status"] == "rejected"
        assert old_token_settlement["reason"] == "management_busy"

        stolen = await _step(
            client,
            "prepare_epoch",
            {"control_id": str(uuid.uuid4()), "registry_id": str(registry_id)},
        )
        assert stolen["status"] == "rejected"
        assert stolen["reason"] == "management_busy"
        replay = await _step(
            client,
            "prepare_epoch",
            {"control_id": str(control_id), "registry_id": str(registry_id)},
        )
        assert replay["new_epoch"] == recovered["new_epoch"]
        assert replay["challenge"] == recovered["challenge"]

        reply = {
            "schema": "aap-backend-route-control-reply/v1",
            "command_id": str(control_id),
            "registry_id": str(registry_id),
            "authority_epoch": recovered["new_epoch"],
            "backend_incarnation": str(uuid.uuid4()),
            "challenge": recovered["challenge"],
            "phase": "bound",
            "active_requests": 1,
        }
        not_drained = await _step(
            client, "finish_epoch", {"control_id": str(control_id), "reply": reply}
        )
        assert not_drained["status"] == "rejected"
        assert not_drained["reason"] == "member_not_quiescent"
        unresolved_before_bound = await owner.fetchval(
            """SELECT state FROM aap_inference_routing_v1.control_attempts
               WHERE id=$1""",
            uncertain_attempt_id,
        )
        assert unresolved_before_bound == "uncertain"

        reply["active_requests"] = 0
        finished = await _step(
            client, "finish_epoch", {"control_id": str(control_id), "reply": reply}
        )
        assert finished["status"] == "ok"
        assert finished["phase"] == "completed"
        rows = await owner.fetch(
            """SELECT id, state, status_code, reason
               FROM aap_inference_routing_v1.control_attempts
               WHERE control_id=$1 ORDER BY id""",
            control_id,
        )
        attempts = {row["id"]: row for row in rows}
        assert attempts[failure_attempt_id]["state"] == "response"
        assert attempts[failure_attempt_id]["status_code"] == 503
        assert attempts[uncertain_attempt_id]["state"] == "reconciled"
        control = await owner.fetchrow(
            """SELECT kind, phase, recovered_kind
               FROM aap_inference_routing_v1.controls WHERE id=$1""",
            control_id,
        )
        assert tuple(control) == ("epoch", "completed", "unload")
    finally:
        await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()
