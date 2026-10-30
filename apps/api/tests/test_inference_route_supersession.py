"""Real owner transactions protect model routes across challenged health refreshes."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
from fastapi import FastAPI
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.backend_endpoint import BackendEndpointSnapshot
from aap_inference.owner_transport import (
    ModelDispatchDenied,
    RoutingModelRequestContextFactory,
)
from aap_inference.request_tracking import http_client
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionRouteBinding,
)
from aap_inference_contract.routing import (
    RoutingRejectedResult,
    make_routing_command,
)
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _cleanup, _connect, _seed
from tests.test_inference_routing_owner_integration import _start_uvicorn, _stop_uvicorn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize(
    "case",
    [
        "health_refresh",
        "unchanged_legacy",
        "legacy_capability",
        "pool",
        "member",
        "endpoint",
        "boot",
        "boot_at_open",
        "project",
        "offline",
        "cancel",
        "send_pool",
        "send_member",
        "send_endpoint",
        "send_boot",
        "send_project",
        "cancel_before_send",
    ],
)
async def test_model_route_health_refresh_keeps_identity_and_fences_changes(
    test_engine, case
):
    await assert_owned_test_database(test_engine)
    routing_dsn = os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"]
    owner = await _connect(test_engine)
    runtime = RoutingRuntime(routing_dsn)
    app = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()
    gate_entered = asyncio.Event()
    calls = []

    @app.post("/predict")
    async def predict():
        calls.append("predict")
        entered.set()
        if case == "cancel":
            await release.wait()
        return {"ok": True}

    server, task, listener, url = await _start_uvicorn(app)
    world = await _seed(owner)
    receipts_before = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
        )
    )
    project_id = user_id = None
    try:
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            user = await create_user(
                db, "super_admin", f"route-{uuid.uuid4()}@test.local", "Route owner"
            )
            project = await create_project(db, owner_id=user.id)
            project_id, user_id = project.id, user.id
            await db.commit()
        await owner.execute(
            "INSERT INTO public.project_ml_backend_pool(id,project_id,pool_id,enabled) VALUES($1,$2,$3,true)",
            uuid.uuid4(),
            project_id,
            world["pool_id"],
        )
        health = {
            "capabilities": {"model_version": "routing-test"},
            "residency": {"boot_id": "original-boot"},
        }
        await owner.execute(
            "UPDATE public.ml_backend_registry SET url=$2,gpu_resource_id='fixture-host/test-device',vram_budget_mb=512,health_meta=$3::jsonb,last_checked_at=now() WHERE id=$1",
            world["registry_id"],
            url,
            json.dumps(health),
        )
        if case != "unchanged_legacy":
            await owner.execute(
                "UPDATE public.ml_backend_registry SET state='connected',health_meta=$2::jsonb,last_checked_at=now() WHERE id=$1",
                world["registry_id"],
                json.dumps(health),
            )
        row = await owner.fetchrow(
            "SELECT auth_method,auth_token,extra_params FROM public.ml_backend_registry WHERE id=$1",
            world["registry_id"],
        )
        extra = (
            json.loads(row["extra_params"])
            if isinstance(row["extra_params"], str)
            else row["extra_params"]
        )
        endpoint_url = url
        acquire = make_routing_command(
            "acquire",
            {
                "pool_id": str(world["pool_id"]),
                "registry_id": None,
                "project_id": str(project_id),
                "mode": "off",
                "owner": "test:model",
                "operation": "batch_predict",
                "required_instance_id": None,
            },
        )
        acquired = await runtime.execute(acquire)
        assert acquired.state == "committed" and not isinstance(
            acquired.result, RoutingRejectedResult
        )
        original = acquired.result
        assert original.state == "reserved"
        original_revision = original.eligibility_revision
        if case == "legacy_capability":
            # Rows created before the migration have no captured revision.
            await owner.execute(
                "UPDATE aap_inference_routing_v1.scopes SET reservation_eligibility_revision=NULL,reservation_boot_id=NULL WHERE id=$1",
                original.id,
            )
            legacy = await runtime.execute(
                make_routing_command(
                    "scope_status",
                    {"scope_id": str(original.id), "capability": original.capability},
                )
            )
            assert legacy.result.scope.capability == original.capability
            assert legacy.result.scope.eligibility_revision == original_revision
            return
        if case != "unchanged_legacy":
            # A mandatory challenged health observation changes only live health data.
            health["probe"] = {"challenge": uuid.uuid4().hex}
            if case in {"boot", "boot_at_open"}:
                health["residency"]["boot_id"] = "replacement-boot"
            await owner.execute(
                "UPDATE public.ml_backend_registry SET health_meta=$2::jsonb,last_checked_at=now() WHERE id=$1",
                world["registry_id"],
                json.dumps(health),
            )
        if case == "pool":
            await owner.execute(
                "UPDATE public.ml_backend_service_pools SET routing_generation=routing_generation+1 WHERE id=$1",
                world["pool_id"],
            )
        elif case == "member":
            await owner.execute(
                "UPDATE public.ml_backend_pool_members SET traffic_state='disabled' WHERE id=$1",
                world["member_id"],
            )
        elif case == "project":
            await owner.execute(
                "UPDATE public.project_ml_backend_pool SET enabled=false WHERE project_id=$1 AND pool_id=$2",
                project_id,
                world["pool_id"],
            )
        elif case == "offline":
            await owner.execute(
                "UPDATE public.ml_backend_registry SET state='disconnected' WHERE id=$1",
                world["registry_id"],
            )
        elif case == "endpoint":
            endpoint_url = url + "/rotated"

        attempt_id = uuid.uuid4()
        binding = ModelExecutionRouteBinding(
            original.id, attempt_id, original.capability
        )
        request_health = (
            None
            if case == "unchanged_legacy"
            else {
                "residency": {
                    "boot_id": "replacement-boot"
                    if case == "boot_at_open"
                    else "original-boot"
                }
            }
        )
        operation = FrozenModelOperation(
            request_id=attempt_id,
            operation="batch_predict",
            task_inputs=({"id": "fixture-task", "file_path": "fixture://image"},),
            context=None,
            project_id=project_id,
            requested_registry_id=world["registry_id"],
            requested_pool_id=world["pool_id"],
            endpoint_registry_id=world["registry_id"],
            required_instance_id=None,
            route_owner="test:model",
            route_operation="batch_predict",
            selected_pool_id=world["pool_id"],
            selected_member_id=world["member_id"],
            topology_generation=original.generation,
            health_meta=request_health,
            route_binding=binding,
        )
        endpoint = BackendEndpointSnapshot(
            registry_id=world["registry_id"],
            request_id=attempt_id,
            purpose="execution",
            authority=binding.endpoint_authority(),
            endpoint_sha256="0" * 64,
            url=endpoint_url,
            auth_method=row["auth_method"],
            auth_token=row["auth_token"],
            gpu_resource_id="fixture-host/test-device",
            vram_budget_mb=512,
            eviction_priority=0,
            extra_params=extra,
        )
        request_context = RoutingModelRequestContextFactory(runtime)(
            operation, endpoint, binding
        )
        if case.startswith("send_") or case == "cancel_before_send":
            dispatch = runtime.model_dispatch

            async def mutate_before_start(**arguments):
                if arguments["action"] == "start":
                    if case == "cancel_before_send":
                        gate_entered.set()
                        await release.wait()
                    elif case == "send_pool":
                        await owner.execute(
                            "UPDATE public.ml_backend_service_pools SET routing_generation=routing_generation+1 WHERE id=$1",
                            world["pool_id"],
                        )
                    elif case == "send_member":
                        await owner.execute(
                            "UPDATE public.ml_backend_pool_members SET traffic_state='disabled' WHERE id=$1",
                            world["member_id"],
                        )
                    elif case == "send_project":
                        await owner.execute(
                            "UPDATE public.project_ml_backend_pool SET enabled=false WHERE project_id=$1 AND pool_id=$2",
                            project_id,
                            world["pool_id"],
                        )
                    elif case == "send_endpoint":
                        with pytest.raises(asyncpg.LockNotAvailableError):
                            await owner.execute(
                                "UPDATE public.ml_backend_registry SET url=$2 WHERE id=$1",
                                world["registry_id"],
                                url + "/rotated",
                            )
                    elif case == "send_boot":
                        health["residency"]["boot_id"] = "replacement-boot"
                        await owner.execute(
                            "UPDATE public.ml_backend_registry SET health_meta=$2::jsonb,last_checked_at=now() WHERE id=$1",
                            world["registry_id"],
                            json.dumps(health),
                        )
                return await dispatch(**arguments)

            runtime.model_dispatch = mutate_before_start

        async with http_client(request_context, timeout=10, trust_env=False) as client:
            if case in {"health_refresh", "unchanged_legacy", "send_endpoint"}:
                assert (await client.post(url + "/predict")).json() == {"ok": True}
                assert calls == ["predict"]
            elif case == "cancel":
                sending = asyncio.create_task(client.post(url + "/predict"))
                await asyncio.wait_for(entered.wait(), timeout=5)
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await sending
                release.set()
                assert calls == ["predict"]
            elif case == "cancel_before_send":
                sending = asyncio.create_task(client.post(url + "/predict"))
                await asyncio.wait_for(gate_entered.wait(), timeout=5)
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await sending
                assert calls == []
            else:
                with pytest.raises(ModelDispatchDenied):
                    await client.post(url + "/predict")
                assert calls == []
        status = await runtime.execute(
            make_routing_command(
                "scope_status",
                {"scope_id": str(original.id), "capability": original.capability},
            )
        )
        if case in {"health_refresh", "unchanged_legacy", "send_endpoint"}:
            assert status.result.scope.id == original.id
            assert status.result.scope.capability == original.capability
            if case in {"health_refresh", "send_endpoint"}:
                assert status.result.scope.eligibility_revision > original_revision
            else:
                assert status.result.scope.eligibility_revision == original_revision
            assert status.result.attempts[0].state == "response"
            assert not await runtime.model_dispatch(
                scope_id=original.id,
                capability=original.capability,
                attempt_id=attempt_id,
                action="not_sent",
                binding=None,
            )
            assert not await runtime.model_dispatch(
                scope_id=original.id,
                capability=original.capability,
                attempt_id=attempt_id,
                action="start",
                binding={
                    "member_id": str(world["member_id"]),
                    "boot_id": None if case == "unchanged_legacy" else "original-boot",
                    "revalidate": True,
                    "health_max_age_seconds": runtime.manager.health_max_age_seconds,
                    "model_id": None,
                    "model_variants": None,
                    "check_approval": False,
                    "approval": None,
                },
            )
            assert calls == ["predict"]
        elif case == "cancel":
            assert (
                len(status.result.attempts) == 1
                and status.result.attempts[0].state == "uncertain"
            )
        elif case.startswith("send_") or case == "cancel_before_send":
            assert status.result.scope.id == original.id
            assert status.result.scope.capability == original.capability
            assert len(status.result.attempts) == 1
            assert status.result.attempts[0].state == "not_sent"
            assert status.result.attempts[0].reason == "not_sent"
            closed = await runtime.execute(
                make_routing_command(
                    "close",
                    {
                        "scope_id": str(original.id),
                        "capability": original.capability,
                        "outcome": "cancel",
                    },
                )
            )
            assert closed.result.state == "cancelled"
        else:
            assert status.result.scope.eligibility_revision == original_revision
            assert not status.result.attempts
        if case == "project":
            replay_attempt = uuid.uuid4()
            identity = {
                "url": url,
                "auth_method": row["auth_method"],
                "auth_token": row["auth_token"],
                "gpu_resource_id": "fixture-host/test-device",
                "extra_params": extra,
            }
            replay_binding = {
                "member_id": str(world["member_id"]),
                "boot_id": "original-boot",
                "revalidate": True,
                "health_max_age_seconds": runtime.manager.health_max_age_seconds,
                "model_id": None,
                "model_variants": None,
                "check_approval": False,
                "approval": None,
            }
            async with runtime.transport_subject_issuer(
                parent_kind="scope",
                parent_id=original.id,
                capability=original.capability,
                attempt_id=replay_attempt,
                transport_identity=identity,
            ) as subject:
                command = make_routing_command(
                    "begin",
                    {
                        "scope_id": str(original.id),
                        "capability": original.capability,
                        "attempt_id": str(replay_attempt),
                        "method": "POST",
                        "path": "/predict",
                        "transport_subject": subject,
                    },
                )
                first = await runtime.begin_model(
                    command,
                    member_id=world["member_id"],
                    boot_id="original-boot",
                    revalidate=True,
                    binding=replay_binding,
                )
                assert isinstance(first.result, RoutingRejectedResult)
                await owner.execute(
                    "UPDATE public.project_ml_backend_pool SET enabled=true WHERE project_id=$1 AND pool_id=$2",
                    project_id,
                    world["pool_id"],
                )
                replay = await runtime.begin_model(
                    command,
                    member_id=world["member_id"],
                    boot_id="original-boot",
                    revalidate=True,
                    binding=replay_binding,
                )
                assert replay.result == first.result
                assert calls == []
    finally:
        release.set()
        try:
            after = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
                )
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.command_receipts WHERE id=ANY($1::uuid[])",
                list(after - receipts_before),
            )
            await _cleanup(owner, world)
            if project_id is not None:
                await owner.execute(
                    "DELETE FROM public.projects WHERE id=$1", project_id
                )
            if user_id is not None:
                await owner.execute("DELETE FROM public.users WHERE id=$1", user_id)
        finally:
            await runtime.close()
            await owner.close()
            await _stop_uvicorn(server, task, listener)
