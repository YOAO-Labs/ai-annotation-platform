"""Native authorization and identity fencing for terminal-only GPU endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text

from app.db.models.ml_backend_registry import MLBackendRegistry
from tests.test_gpu_arbiter_proof_recovery import (
    _RESOURCE_A,
    _cleanup_backends,
    _create_active_backends,
    _install_live_health,
)
from tests.test_gpu_plugin_admission_native import _step
from tests.test_inference_routing_native import (
    _connect,
    _identity_hash,
    _step as _routing_step,
)


@asynccontextmanager
async def _owner_connection(test_engine):
    connection = await _connect(test_engine)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_terminal_endpoint_requires_exact_authorized_unsettled_intent(
    test_engine,
):
    resource = {
        "gpu_resource_id": _RESOURCE_A,
        "physical_gpu_uuid": "GPU-a",
        "logical_device": "cuda:0",
    }
    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=lambda backend_id, _: {
            "max_concurrency": 4,
            "plugin": {
                "installation_id": str(backend_id),
                "status": "disabled",
                "revision": 4,
                "resource": resource,
            },
            "plugin_lifecycle": {
                "launch": {
                    "registry_id": str(backend_id),
                    "container_id": "a" * 64,
                    "resource": resource,
                },
                "stopped": [],
            },
        },
    )
    backend_id = ids[0]
    try:
        await _install_live_health(
            factory, ids, resource_id=_RESOURCE_A, resident_backend_ids=frozenset(ids)
        )
        async with factory.begin() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            residency = row.health_meta["residency"]
            endpoint = row.url
            digest = await db.scalar(
                text(
                    "SELECT encode(digest(convert_to(aap_inference_backend_endpoint_v1._endpoint_record(:id)::text, 'UTF8'), 'sha256'), 'hex')"
                ),
                {"id": backend_id},
            )
        request = {
            "registry_id": str(backend_id),
            "resource_id": _RESOURCE_A,
            "endpoint": endpoint,
            "boot_id": residency["boot_id"],
            "operation_id": str(uuid4()),
            "container_id": "a" * 64,
            "pool_ids": ["models"],
        }
        expiry = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
        args = {
            "phase": "reserve",
            "request": request,
            "residency": residency,
            "preparation": None,
            "token_expires_at": expiry,
            "ledger_source_generation": "1",
            "probe_started_at": None,
        }
        prepared = (await _step(factory, "installation_terminal", args))["preparation"]
        request_id = uuid4()
        sql = "SELECT aap_inference_backend_endpoint_v1.open_installation_terminal_endpoint($1,$2,$3,$4,$5)"
        async with factory.begin() as db:
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f',p.proowner))) a "
                    "WHERE p.oid='aap_inference_backend_endpoint_v1.open_installation_terminal_endpoint(uuid,uuid,text,text,text)'::regprocedure "
                    "AND a.grantee=0 AND a.privilege_type='EXECUTE')"
                )
            )
            opener_oid = await db.scalar(
                text(
                    "SELECT 'aap_inference_backend_endpoint_v1.open_installation_terminal_endpoint(uuid,uuid,text,text,text)'::regprocedure::oid"
                )
            )
        async with factory._authority_pool.acquire() as conn:
            assert not await conn.fetchval(
                "SELECT has_function_privilege(current_user, $1::oid, 'EXECUTE')",
                opener_oid,
            )
        async with _owner_connection(test_engine) as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.fetchval(
                    "SELECT aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject($1,$2,$3,$4)",
                    backend_id,
                    request_id,
                    "terminal_drain",
                    None,
                )
        async with _owner_connection(test_engine) as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.fetchval(
                    sql,
                    backend_id,
                    request_id,
                    json.dumps(request),
                    json.dumps(prepared),
                    digest,
                )
        prepared = (
            await _step(
                factory,
                "installation_terminal",
                {**args, "phase": "authorize", "preparation": prepared},
            )
        )["preparation"]
        async with _owner_connection(test_engine) as conn:

            async def open_endpoint(
                req=request, prep=prepared, sha=digest, registry=backend_id
            ):
                return await conn.fetchval(
                    sql, registry, request_id, json.dumps(req), json.dumps(prep), sha
                )

            snapshot = json.loads(await open_endpoint())
            assert (
                snapshot["purpose"] == "terminal_drain"
                and snapshot["authority"] is None
            )

            assert (
                snapshot["registry_id"] == str(backend_id)
                and snapshot["endpoint_sha256"] == digest
            )
            for req in (
                {**request, "boot_id": "wrong"},
                {**request, "endpoint": endpoint + "/other"},
                {**request, "operation_id": str(uuid4())},
                {**request, "registry_id": str(uuid4())},
            ):
                with pytest.raises(asyncpg.PostgresError):
                    await open_endpoint(req=req, prep=prepared)
            for prep in (
                {**prepared, "generation": "999"},
                {**prepared, "control_epoch": "999"},
                {**prepared, "runtime_epoch": "999"},
                {**prepared, "membership_epoch": 999},
                {
                    **prepared,
                    "token_expires_at": (
                        datetime.now(UTC) + timedelta(seconds=30)
                    ).isoformat(),
                },
            ):
                with pytest.raises(asyncpg.PostgresError):
                    await open_endpoint(prep=prep)
            with pytest.raises(asyncpg.PostgresError):
                await open_endpoint(prep=prepared, sha="0" * 64)
            with pytest.raises(asyncpg.PostgresError):
                await open_endpoint(prep=prepared, registry=uuid4())
        terminal = {
            **residency,
            "state": "draining",
            "draining": True,
            "gpu_loaded": False,
            "evictable": False,
            "generation": prepared["generation"],
            "pools": {"models": {"resident": False, "device": None, "provider": None}},
        }
        committed = await _step(
            factory,
            "installation_terminal",
            {
                **args,
                "phase": "commit",
                "preparation": prepared,
                "residency": terminal,
                "token_expires_at": prepared["token_expires_at"],
            },
        )
        assert committed["status"] == "verified"
        replay = await _step(
            factory,
            "installation_terminal",
            {
                **args,
                "phase": "authorize",
                "preparation": {**prepared, "settled": True},
                "residency": terminal,
                "token_expires_at": prepared["token_expires_at"],
            },
        )
        assert (
            replay["status"] == "prepared" and replay["preparation"]["settled"] is True
        )
        async with _owner_connection(test_engine) as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.fetchval(
                    sql,
                    backend_id,
                    request_id,
                    json.dumps(request),
                    json.dumps(replay["preparation"]),
                    digest,
                )
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_terminal_commands_preserve_uncertain_route_and_require_exact_proof(
    test_engine,
):
    resource = {
        "gpu_resource_id": _RESOURCE_A,
        "physical_gpu_uuid": "GPU-a",
        "logical_device": "cuda:0",
    }

    def extra(backend_id, _):
        launch = {
            "installation_id": str(backend_id),
            "registry_id": str(backend_id),
            "artifact": "artifact",
            "image_id": "sha256:" + "c" * 64,
            "container_id": "a" * 64,
            "resource": resource,
        }
        return {
            "max_concurrency": 4,
            "plugin": {
                "installation_id": str(backend_id),
                "status": "active",
                "revision": 3,
                "resource": resource,
                "manifest": {"artifact": "artifact", "resource": {"device": "cuda"}},
            },
            "plugin_lifecycle": {"launch": launch, "stopped": []},
        }

    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=extra,
        auth_token="terminal-test-token",
    )
    backend_id = ids[0]
    scope_id, attempt_id = uuid4(), uuid4()
    try:
        await _install_live_health(
            factory, ids, resource_id=_RESOURCE_A, resident_backend_ids=frozenset(ids)
        )
        async with factory() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            residency, endpoint = row.health_meta["residency"], row.url
            launch = row.extra_params["plugin_lifecycle"]["launch"]
        async with _owner_connection(test_engine) as owner:
            identity = await _identity_hash(owner, backend_id)
            async with owner.transaction():
                snapshot = await _routing_step(
                    owner,
                    "admission_snapshot",
                    {
                        "pool_id": None,
                        "registry_id": str(backend_id),
                        "project_id": None,
                        "mode": "direct",
                        "required_instance_id": None,
                        "health_max_age_seconds": 60,
                    },
                )
                assert snapshot["status"] == "ok", snapshot
                reserved = await _routing_step(
                    owner,
                    "reserve_scope",
                    {
                        "snapshot": snapshot["snapshot"],
                        "candidate_ids": [str(backend_id)],
                        "scope_id": str(scope_id),
                        "command_sha256": "a" * 64,
                        "owner": "api:terminal-hold-test",
                        "operation": "predict",
                        "ttl_ms": 30000,
                    },
                )
                assert reserved["status"] == "ok", reserved
            capability = reserved["scope"]["capability"]
            began = await _routing_step(
                owner,
                "begin_attempt",
                {
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "attempt_id": str(attempt_id),
                    "method": "POST",
                    "path": "/predict",
                    "transport_identity": {"identity_sha256": identity},
                },
            )
            assert began["status"] == "ok"
            closed = await _routing_step(
                owner,
                "close_scope",
                {
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "outcome": "cancel",
                },
            )
            assert closed["scope"]["state"] == "uncertain"
            control_id = uuid4()
            await owner.execute(
                "INSERT INTO aap_inference_routing_v1.controls "
                "(id,registry_id,kind,phase,new_epoch,challenge,config_revision,realm) "
                "SELECT $1,registry_id,'epoch','uncertain',$2,$3,config_revision,realm "
                "FROM aap_inference_routing_v1.registry_state WHERE registry_id=$4",
                control_id,
                uuid4(),
                "b" * 64,
                backend_id,
            )
            held_control = await owner.fetchval(
                "SELECT row_to_json(c)::text FROM aap_inference_routing_v1.controls c WHERE id=$1",
                control_id,
            )
            blocked_scope = uuid4()
            async with owner.transaction():
                held_snapshot = await _routing_step(
                    owner,
                    "admission_snapshot",
                    {
                        "pool_id": None,
                        "registry_id": str(backend_id),
                        "project_id": None,
                        "mode": "direct",
                        "required_instance_id": None,
                        "health_max_age_seconds": 60,
                    },
                )
                assert held_snapshot["status"] == "ok", held_snapshot
                ordinary_route = await _routing_step(
                    owner,
                    "reserve_scope",
                    {
                        "snapshot": held_snapshot["snapshot"],
                        "candidate_ids": [str(backend_id)],
                        "scope_id": str(blocked_scope),
                        "command_sha256": "c" * 64,
                        "owner": "api:ordinary-route-test",
                        "operation": "predict",
                        "ttl_ms": 30000,
                    },
                )
            assert ordinary_route["status"] == "rejected"
            assert ordinary_route["reason"] == "management_busy"
            assert not await owner.fetchval(
                "SELECT EXISTS (SELECT 1 FROM aap_inference_routing_v1.scopes WHERE id=$1)",
                blocked_scope,
            )
            before = await owner.fetchrow(
                "SELECT config_revision FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
                backend_id,
            )
            with pytest.raises(asyncpg.PostgresError) as blocked:
                await owner.execute(
                    "UPDATE public.ml_backend_registry SET url=url||'/changed' WHERE id=$1",
                    backend_id,
                )
            assert blocked.value.sqlstate == "55P03"
            command = "SELECT aap_inference_topology_v1.command_plugin_installation($1,$2,$3,$4)"
            disabled = json.loads(
                await owner.fetchval(command, backend_id, 3, "disable", None)
            )
            assert disabled["installation"]["status"] == "disabled"
            assert (
                await _routing_step(
                    owner, "quiescence", {"registry_id": str(backend_id)}
                )
            )["status"] == "rejected"
            with pytest.raises(asyncpg.PostgresError):
                await owner.fetchval(command, backend_id, 4, "retire", None)
            request = {
                "registry_id": str(backend_id),
                "resource_id": _RESOURCE_A,
                "endpoint": endpoint,
                "boot_id": residency["boot_id"],
                "operation_id": str(uuid4()),
                "container_id": launch["container_id"],
                "pool_ids": ["models"],
            }
            expiry = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
            args = {
                "phase": "reserve",
                "request": request,
                "residency": residency,
                "preparation": None,
                "token_expires_at": expiry,
                "ledger_source_generation": "1",
                "probe_started_at": None,
            }
            prepared = (await _step(factory, "installation_terminal", args))[
                "preparation"
            ]
            prepared = (
                await _step(
                    factory,
                    "installation_terminal",
                    {
                        **args,
                        "phase": "authorize",
                        "preparation": prepared,
                    },
                )
            )["preparation"]
            proof = {
                **launch,
                "endpoint": endpoint,
                "boot_id": request["boot_id"],
                "operation_id": request["operation_id"],
                "pool_ids": ["models"],
                "generation": prepared["generation"],
                "control_epoch": prepared["control_epoch"],
            }
            with pytest.raises(asyncpg.PostgresError):
                await owner.fetchval(
                    command, backend_id, 4, "quiesce", json.dumps(proof)
                )
            terminal = {
                **residency,
                "state": "draining",
                "draining": True,
                "gpu_loaded": False,
                "evictable": False,
                "generation": prepared["generation"],
                "pools": {
                    "models": {"resident": False, "device": None, "provider": None}
                },
            }
            assert (
                await _step(
                    factory,
                    "installation_terminal",
                    {
                        **args,
                        "phase": "commit",
                        "preparation": prepared,
                        "residency": terminal,
                        "token_expires_at": prepared["token_expires_at"],
                    },
                )
            )["status"] == "verified"
            with pytest.raises(asyncpg.PostgresError):
                await owner.fetchval(
                    command,
                    backend_id,
                    4,
                    "quiesce",
                    json.dumps({**proof, "operation_id": str(uuid4())}),
                )
            quiesced = json.loads(
                await owner.fetchval(
                    command, backend_id, 4, "quiesce", json.dumps(proof)
                )
            )
            assert quiesced["installation"]["revision"] == 5
            retired = json.loads(
                await owner.fetchval(command, backend_id, 5, "retire", None)
            )
            assert retired["installation"]["status"] == "retired"
            assert (
                await owner.fetchval(
                    "SELECT config_revision FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
                    backend_id,
                )
                == before["config_revision"] + 3
            )
            status = await _routing_step(
                owner,
                "scope_status",
                {"scope_id": str(scope_id), "capability": capability},
            )
            assert status["scope"]["state"] == "uncertain"
            assert status["attempts"][0]["id"] == str(attempt_id)
            assert status["attempts"][0]["state"] == "uncertain"
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(c)::text FROM aap_inference_routing_v1.controls c WHERE id=$1",
                    control_id,
                )
                == held_control
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM aap_inference_routing_v1.plugin_terminal_update_guard"
                )
                == 0
            )
    finally:
        async with _owner_connection(test_engine) as owner:
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.attempts WHERE scope_id=$1",
                scope_id,
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.scopes WHERE id=$1", scope_id
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.controls WHERE registry_id=$1",
                backend_id,
            )
        await _cleanup_backends(factory, ids)
