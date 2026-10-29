"""Native replacement authorization preserves routing and terminal evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

import asyncpg
import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry
from tests.test_gpu_arbiter_proof_recovery import (
    _RESOURCE_A,
    _cleanup_backends,
    _create_active_backends,
    _install_live_health,
)
from tests.test_gpu_plugin_admission_native import _step as _gpu_step
from tests.test_gpu_plugin_terminal_endpoint_native import _owner_connection
from tests.test_inference_routing_native import _step as _routing_step


_AUTH = "SELECT aap_inference_topology_v1.authorize_plugin_routing_replacement($1,$2,$3,$4,$5,$6)"
_COMMAND = "SELECT aap_inference_topology_v1.command_plugin_installation($1,$2,$3,$4)"


async def _command(conn, registry, revision, action, evidence=None):
    return json.loads(
        await conn.fetchval(
            _COMMAND,
            registry,
            revision,
            action,
            json.dumps(evidence) if evidence is not None else None,
        )
    )


async def _authorize(conn, registry, revision, control, endpoint, container, boot):
    return json.loads(
        await conn.fetchval(
            _AUTH,
            registry,
            revision,
            control,
            endpoint,
            container,
            boot,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("already_prepared", [False, True])
async def test_replacement_requires_exact_terminal_proof_and_preserves_routing(
    test_engine,
    already_prepared,
):
    resource = {
        "gpu_resource_id": _RESOURCE_A,
        "physical_gpu_uuid": "GPU-a",
        "logical_device": "cuda:0",
    }

    def initial(backend_id, _):
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
                "status": "disabled",
                "revision": 4,
                "resource": resource,
                "manifest": {"artifact": "artifact", "resource": {"device": "cuda"}},
            },
            "plugin_lifecycle": {"launch": launch, "stopped": []},
        }

    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=initial,
        auth_token="replacement-test-token",
        auth_method="token",
    )
    registry = ids[0]
    try:
        await _install_live_health(
            factory, ids, resource_id=_RESOURCE_A, resident_backend_ids=frozenset(ids)
        )
        async with factory() as db:
            row = await db.get(MLBackendRegistry, registry)
            endpoint, residency = row.url, row.health_meta["residency"]
            old_launch = row.extra_params["plugin_lifecycle"]["launch"]
        new_boot = f"replacement-{uuid4()}"
        new_container = "b" * 64
        control = uuid5(registry, f"plugin:bind-routing:{new_container}:{new_boot}")
        async with _owner_connection(test_engine) as owner:
            ordinary = await _authorize(
                owner, registry, 4, control, endpoint, new_container, new_boot
            )
            assert ordinary["status"] == "not_replacement"
            assert not await owner.fetchval(
                "SELECT EXISTS (SELECT 1 FROM aap_inference_routing_v1.controls WHERE id=$1)",
                control,
            )
        request = {
            "registry_id": str(registry),
            "resource_id": _RESOURCE_A,
            "endpoint": endpoint,
            "boot_id": residency["boot_id"],
            "operation_id": str(uuid4()),
            "container_id": old_launch["container_id"],
            "pool_ids": ["models"],
        }
        args = {
            "phase": "reserve",
            "request": request,
            "residency": residency,
            "preparation": None,
            "token_expires_at": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
            "ledger_source_generation": "1",
            "probe_started_at": None,
        }
        prepared = (await _gpu_step(factory, "installation_terminal", args))[
            "preparation"
        ]
        prepared = (
            await _gpu_step(
                factory,
                "installation_terminal",
                {
                    **args,
                    "phase": "authorize",
                    "preparation": prepared,
                },
            )
        )["preparation"]
        terminal = {
            **residency,
            "state": "draining",
            "draining": True,
            "gpu_loaded": False,
            "evictable": False,
            "generation": prepared["generation"],
            "pools": {"models": {"resident": False, "device": None, "provider": None}},
        }
        proof = {
            **old_launch,
            "endpoint": endpoint,
            "boot_id": request["boot_id"],
            "operation_id": request["operation_id"],
            "pool_ids": ["models"],
            "generation": prepared["generation"],
            "control_epoch": prepared["control_epoch"],
        }
        assert (
            await _gpu_step(
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
        new_launch = {**old_launch, "container_id": new_container}
        qualification = {"report": {"qualified": True}, "sha256": "d" * 64}
        async with _owner_connection(test_engine) as owner:
            await _command(owner, registry, 4, "quiesce", proof)
            await _command(owner, registry, 5, "record_launch", new_launch)
            missing_qualification = await _authorize(
                owner, registry, 6, control, endpoint, new_container, new_boot
            )
            assert missing_qualification["status"] == "rejected"
            await _command(owner, registry, 6, "approve_qualification", qualification)
            revision = 7
            old_incarnation = uuid4()
            await owner.execute(
                "UPDATE aap_inference_routing_v1.registry_state SET backend_incarnation=$2, continuity='ready' WHERE registry_id=$1",
                registry,
                old_incarnation,
            )
            if already_prepared:
                epoch = await _routing_step(
                    owner,
                    "prepare_epoch",
                    {
                        "registry_id": str(registry),
                        "control_id": str(control),
                    },
                )
                assert epoch["status"] == "ok", epoch
                before_control = await owner.fetchrow(
                    "SELECT * FROM aap_inference_routing_v1.controls WHERE id=$1",
                    control,
                )
                assert before_control["backend_incarnation"] == old_incarnation
                stolen = await _routing_step(
                    owner,
                    "prepare_epoch",
                    {
                        "registry_id": str(registry),
                        "control_id": str(uuid4()),
                    },
                )
                assert stolen["status"] == "rejected"
                before_revision = await owner.fetchval(
                    "SELECT config_revision FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
                    registry,
                )
                await owner.execute('SET ROLE "identity_api_consumer"')
                for action, evidence in (
                    ("record_launch", new_launch),
                    ("approve_qualification", qualification),
                ):
                    replay = await _command(owner, registry, revision, action, evidence)
                    assert replay["installation"]["revision"] == revision
                await owner.execute("RESET ROLE")
                assert (
                    await owner.fetchval(
                        "SELECT config_revision FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
                        registry,
                    )
                    == before_revision
                )
                for action, evidence in (
                    ("record_launch", {**new_launch, "container_id": "e" * 64}),
                    ("approve_qualification", {**qualification, "sha256": "f" * 64}),
                ):
                    with pytest.raises(asyncpg.PostgresError):
                        await _command(owner, registry, revision, action, evidence)

            async def authorize(
                *,
                rev=revision,
                cid=control,
                url=endpoint,
                container=new_launch["container_id"],
                boot=new_boot,
            ):
                return await _authorize(owner, registry, rev, cid, url, container, boot)

            await owner.execute('SET ROLE "identity_api_consumer"')
            old_boot_control = uuid5(
                registry, f"plugin:bind-routing:{new_container}:{request['boot_id']}"
            )
            wrong_container = "e" * 64
            wrong_container_control = uuid5(
                registry, f"plugin:bind-routing:{wrong_container}:{new_boot}"
            )
            for invalid in (
                {"rev": revision - 1},
                {"cid": uuid4()},
                {"url": endpoint + "/wrong"},
                {"container": wrong_container, "cid": wrong_container_control},
                {"boot": request["boot_id"], "cid": old_boot_control},
                {"boot": ""},
            ):
                assert (await authorize(**invalid))["status"] == "rejected"
            await owner.execute("RESET ROLE")
            tx = owner.transaction()
            await tx.start()
            try:
                await owner.execute(
                    "UPDATE aap_inference_gpu_v1.installation_terminal_intents SET settled=false WHERE backend_registry_id=$1",
                    registry,
                )
                assert (await authorize())["status"] == "rejected"
            finally:
                await tx.rollback()
            for mutation in (
                "UPDATE aap_inference_gpu_v1.installation_terminal_intents SET generation=generation+100 WHERE backend_registry_id=$1",
                "UPDATE aap_inference_gpu_v1.installation_terminal_intents SET control_epoch=control_epoch+100 WHERE backend_registry_id=$1",
                "UPDATE aap_inference_gpu_v1.installation_terminal_intents SET request=jsonb_set(request,'{boot_id}',to_jsonb('unmatched-old-boot'::text)) WHERE backend_registry_id=$1",
            ):
                tx = owner.transaction()
                await tx.start()
                try:
                    await owner.execute(mutation, registry)
                    assert (await authorize())["status"] == "rejected"
                finally:
                    await tx.rollback()

            history_scope, history_attempt = uuid4(), uuid4()
            await owner.execute(
                "INSERT INTO aap_inference_routing_v1.scopes "
                "(id,command_sha256,registry_id,mode,owner,operation,config_revision,eligibility_revision,"
                "authority_epoch,realm,state,closed,heartbeat_deadline) "
                "SELECT $1,$2,registry_id,'direct','history-test','predict',config_revision,eligibility_revision,"
                "authority_epoch,realm,'finished',true,now() FROM aap_inference_routing_v1.registry_state WHERE registry_id=$3",
                history_scope,
                "a" * 64,
                registry,
            )
            await owner.execute(
                "INSERT INTO aap_inference_routing_v1.attempts "
                "(id,scope_id,method,path,transport_identity_sha256,state,status_code) "
                "VALUES ($1,$2,'POST','/predict',$3,'response',200)",
                history_attempt,
                history_scope,
                "b" * 64,
            )
            history_before = await owner.fetchval(
                "SELECT row_to_json(s)::text FROM aap_inference_routing_v1.scopes s WHERE id=$1",
                history_scope,
            )
            attempt_before = await owner.fetchval(
                "SELECT row_to_json(a)::text FROM aap_inference_routing_v1.attempts a WHERE id=$1",
                history_attempt,
            )
            for state, closed in (
                ("active", False),
                ("uncertain", False),
                ("uncertain", True),
            ):
                tx = owner.transaction()
                await tx.start()
                try:
                    await owner.execute(
                        "UPDATE aap_inference_routing_v1.scopes SET state=$2,closed=$3 WHERE id=$1",
                        history_scope,
                        state,
                        closed,
                    )
                    assert (await authorize())["status"] == "rejected"
                finally:
                    await tx.rollback()
            for state in ("active", "uncertain"):
                tx = owner.transaction()
                await tx.start()
                try:
                    await owner.execute(
                        "UPDATE aap_inference_routing_v1.attempts SET state=$2,status_code=NULL WHERE id=$1",
                        history_attempt,
                        state,
                    )
                    assert (await authorize())["status"] == "rejected"
                finally:
                    await tx.rollback()
            if already_prepared:
                tx = owner.transaction()
                await tx.start()
                try:
                    await owner.execute(
                        "INSERT INTO aap_inference_routing_v1.control_attempts "
                        "(id,control_id,method,path,transport_identity_sha256,state) "
                        "VALUES ($1,$2,'POST','/control',$3,'uncertain')",
                        uuid4(),
                        control,
                        "d" * 64,
                    )
                    assert (await authorize())["status"] == "rejected"
                finally:
                    await tx.rollback()
            if not already_prepared:
                other = uuid4()
                await owner.execute(
                    "INSERT INTO aap_inference_routing_v1.controls "
                    "(id,registry_id,kind,phase,new_epoch,challenge,config_revision,realm) "
                    "SELECT $1,registry_id,'epoch','prepared',$2,$3,config_revision,realm "
                    "FROM aap_inference_routing_v1.registry_state WHERE registry_id=$4",
                    other,
                    uuid4(),
                    "c" * 64,
                    registry,
                )
                assert (await authorize())["status"] == "rejected"
                await owner.execute(
                    "DELETE FROM aap_inference_routing_v1.controls WHERE id=$1", other
                )

            before_state = await owner.fetchrow(
                "SELECT authority_epoch,backend_incarnation,config_revision FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
                registry,
            )
            before_intent = await owner.fetchval(
                "SELECT row_to_json(i)::text FROM aap_inference_gpu_v1.installation_terminal_intents i WHERE backend_registry_id=$1",
                registry,
            )
            before_fence = await owner.fetchval(
                "SELECT row_to_json(f)::text FROM gpu_backend_fences f WHERE backend_registry_id=$1",
                registry,
            )
            await owner.execute('SET ROLE "identity_api_consumer"')
            result = await authorize()
            await owner.execute("RESET ROLE")
            assert result["status"] == "authorized", result
            after_control = await owner.fetchrow(
                "SELECT * FROM aap_inference_routing_v1.controls WHERE id=$1", control
            )
            assert (
                after_control["kind"] == "epoch"
                and after_control["phase"] == "prepared"
            )
            assert after_control["backend_incarnation"] is None
            if already_prepared:
                for key in (
                    "id",
                    "new_epoch",
                    "retired_epoch",
                    "challenge",
                    "phase",
                    "config_revision",
                    "realm",
                ):
                    assert after_control[key] == before_control[key]
            assert (
                await owner.fetchrow(
                    "SELECT authority_epoch,backend_incarnation,config_revision FROM aap_inference_routing_v1.registry_state WHERE registry_id=$1",
                    registry,
                )
                == before_state
            )
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(i)::text FROM aap_inference_gpu_v1.installation_terminal_intents i WHERE backend_registry_id=$1",
                    registry,
                )
                == before_intent
            )
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(f)::text FROM gpu_backend_fences f WHERE backend_registry_id=$1",
                    registry,
                )
                == before_fence
            )
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(s)::text FROM aap_inference_routing_v1.scopes s WHERE id=$1",
                    history_scope,
                )
                == history_before
            )
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(a)::text FROM aap_inference_routing_v1.attempts a WHERE id=$1",
                    history_attempt,
                )
                == attempt_before
            )
            assert (await authorize())["status"] == "authorized"
            replay_control = await owner.fetchrow(
                "SELECT * FROM aap_inference_routing_v1.controls WHERE id=$1", control
            )
            for key in ("id", "new_epoch", "retired_epoch", "challenge", "phase"):
                assert replay_control[key] == after_control[key]
            new_incarnation = uuid4()
            finished = await _routing_step(
                owner,
                "finish_epoch",
                {
                    "control_id": str(control),
                    "reply": {
                        "schema": "aap-backend-route-control-reply/v1",
                        "command_id": str(control),
                        "registry_id": str(registry),
                        "authority_epoch": str(after_control["new_epoch"]),
                        "backend_incarnation": str(new_incarnation),
                        "challenge": after_control["challenge"],
                        "phase": "bound",
                        "active_requests": 0,
                    },
                },
            )
            assert finished["status"] == "ok", finished
            completed_control = await owner.fetchval(
                "SELECT row_to_json(c)::text FROM aap_inference_routing_v1.controls c WHERE id=$1",
                control,
            )
            completed_state = await owner.fetchval(
                "SELECT row_to_json(s)::text FROM aap_inference_routing_v1.registry_state s WHERE registry_id=$1",
                registry,
            )
            assert (await authorize())["status"] == "authorized"
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(c)::text FROM aap_inference_routing_v1.controls c WHERE id=$1",
                    control,
                )
                == completed_control
            )
            assert (
                await owner.fetchval(
                    "SELECT row_to_json(s)::text FROM aap_inference_routing_v1.registry_state s WHERE registry_id=$1",
                    registry,
                )
                == completed_state
            )
            assert (await authorize(rev=revision - 1))["status"] == "rejected"
            assert (await authorize(boot=request["boot_id"], cid=old_boot_control))[
                "status"
            ] == "rejected"
            assert (
                await authorize(container=wrong_container, cid=wrong_container_control)
            )["status"] == "rejected"
            for mutation in (
                "UPDATE aap_inference_routing_v1.registry_state SET authority_epoch=gen_random_uuid() WHERE registry_id=$1",
                "UPDATE aap_inference_routing_v1.registry_state SET backend_incarnation=gen_random_uuid() WHERE registry_id=$1",
                "UPDATE aap_inference_routing_v1.registry_state SET continuity='unverified' WHERE registry_id=$1",
                "UPDATE aap_inference_routing_v1.registry_state SET config_revision=config_revision+1 WHERE registry_id=$1",
            ):
                tx = owner.transaction()
                await tx.start()
                try:
                    await owner.execute(mutation, registry)
                    assert (await authorize())["status"] == "rejected"
                finally:
                    await tx.rollback()
    finally:
        async with _owner_connection(test_engine) as owner:
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.attempts WHERE scope_id IN "
                "(SELECT id FROM aap_inference_routing_v1.scopes WHERE registry_id=$1)",
                registry,
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
                registry,
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.plugin_replacement_bindings WHERE registry_id=$1",
                registry,
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.controls WHERE registry_id=$1",
                registry,
            )
            await owner.execute(
                "DELETE FROM aap_inference_gpu_v1.installation_terminal_intents WHERE backend_registry_id=$1",
                registry,
            )
        await _cleanup_backends(factory, ids)
