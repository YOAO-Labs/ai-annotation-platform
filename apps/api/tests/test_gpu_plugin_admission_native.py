from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from aap_inference.gpu.ledger.installation_domain import ensure_installation_domain
from app.db.models.ml_backend_registry import MLBackendRegistry
from tests.test_gpu_arbiter_proof_recovery import (
    _RESOURCE_A,
    _cleanup_backends,
    _create_active_backends,
    _install_live_health,
    _open_gpu_authority_provider,
    _residency,
    proof_store as proof_store,
)
import json

from sqlalchemy.exc import DBAPIError


async def _step(factory, action, arguments):
    async with factory.transaction() as data:
        return await data.step(action, arguments)


async def _plugin(factory, backend_id, value):
    async with factory.begin() as db:
        row = await db.get(MLBackendRegistry, backend_id)
        row.extra_params = {"max_concurrency": 4, "plugin": value}


@pytest.mark.asyncio
async def test_installation_intent_tables_are_private_to_definer(
    test_engine: AsyncEngine,
):
    factory, ids = await _create_active_backends(
        test_engine, resource_id=_RESOURCE_A, budgets=(1024,)
    )
    try:
        async with factory._authority_pool.acquire() as connection:
            for table in (
                "installation_terminal_intents",
                "installation_control_intents",
            ):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not await connection.fetchval(
                        "SELECT has_table_privilege(current_user, $1, $2)",
                        f"aap_inference_gpu_v1.{table}",
                        privilege,
                    )
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("resident", [False, True])
async def test_native_plugin_admission_and_stale_subject(
    test_engine: AsyncEngine, resident: bool
):
    active = {"status": "active", "resource": {"gpu_resource_id": _RESOURCE_A}}
    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=lambda _id, _index: {
            "max_concurrency": 4,
            "plugin": active,
        },
    )
    backend_id = ids[0]
    try:
        await _install_live_health(
            factory,
            ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(ids) if resident else frozenset(),
        )
        if resident:
            async with factory.begin() as db:
                fence = await db.get(GPUBackendFence, backend_id)
                fence.generation_high_water = 1
        else:
            async with factory.begin() as db:
                row = await db.get(MLBackendRegistry, backend_id)
                health = dict(row.health_meta)
                health["residency"] = {
                    **health["residency"],
                    "lifecycle_gate": "enforce",
                }
                row.health_meta = health
        async with factory() as db:
            initial_fence = await db.get(GPUBackendFence, backend_id)
            initial_horizon = initial_fence.token_expiry_high_water
        action = "runtime_subject" if resident else "cold_runtime_subject"
        args = {"backend_registry_id": str(backend_id), "gpu_resource_id": _RESOURCE_A}
        if not resident:
            args["expected_challenge"] = None
        # The owner-controlled status can change without changing the runtime identity.
        ready = await _step(factory, action, args)
        assert ready["status"] == "ready"
        subject = ready["subject"]
        for value in (
            {**active, "status": "prepared"},
            {**active, "status": "disabled"},
            {**active, "status": "retired"},
        ):
            await _plugin(factory, backend_id, value)
            assert await _step(factory, action, args) == {
                "status": "blocked",
                "reason": "plugin_not_active",
            }
            mutation = (
                "record_runtime_token_expiry" if resident else "prepare_cold_generation"
            )
            result = await _step(
                factory,
                mutation,
                {
                    "expected_subject": subject,
                    "token_expires_at": (
                        datetime.now(UTC) + timedelta(minutes=2)
                    ).isoformat(),
                },
            )
            assert result == {"status": "blocked", "reason": "plugin_not_active"}
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            assert fence.generation_high_water == (1 if resident else 0)
            assert fence.token_expiry_high_water == initial_horizon
        await _plugin(factory, backend_id, active)
        result = await _step(
            factory,
            mutation,
            {
                "expected_subject": subject,
                "token_expires_at": (
                    datetime.now(UTC) + timedelta(minutes=2)
                ).isoformat(),
            },
        )
        assert result["status"] == ("recorded" if resident else "prepared")
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_registry_lock_contention_fails_closed_without_affecting_eviction(
    test_engine: AsyncEngine,
):
    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=lambda _id, _index: {
            "max_concurrency": 4,
            "plugin": {
                "status": "disabled",
                "resource": {"gpu_resource_id": _RESOURCE_A},
            },
        },
    )
    backend_id = ids[0]
    try:
        await _install_live_health(
            factory, ids, resource_id=_RESOURCE_A, resident_backend_ids=frozenset(ids)
        )
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            fence.generation_high_water = 1
        async with factory() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            challenge = row.health_meta["gpu_arbiter_probe"]["challenge"]
        eviction = await _step(
            factory,
            "idle_eviction_subject",
            {
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": _RESOURCE_A,
                "expected_generation": "1",
                "challenge": challenge,
            },
        )
        assert eviction["status"] == "ready"
        async with factory.begin() as db:
            await db.execute(
                select(MLBackendRegistry)
                .where(MLBackendRegistry.id == backend_id)
                .with_for_update()
            )
            result = await _step(
                factory,
                "runtime_subject",
                {
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": _RESOURCE_A,
                },
            )
            assert result == {"status": "blocked", "reason": "registry_busy"}
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_native_terminal_intent_replay_and_full_pool_proof(
    test_engine: AsyncEngine,
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
        extra_params_factory=lambda backend_id, _index: {
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
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            fence.token_expiry_high_water = datetime.fromisoformat(expiry)
        args = {
            "phase": "reserve",
            "request": request,
            "residency": residency,
            "preparation": None,
            "token_expires_at": expiry,
            "ledger_source_generation": "1",
            "probe_started_at": None,
        }
        first = await _step(factory, "installation_terminal", args)
        assert first["status"] == "prepared"
        prepared = first["preparation"]
        assert prepared["source_generation"] == "1"
        assert prepared["generation"] == "2"
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            reserved_horizon = fence.token_expiry_high_water
            runtime_epoch = str(fence.runtime_epoch_high_water)
        cold = {
            "backend_registry_id": str(backend_id),
            "gpu_resource_id": _RESOURCE_A,
            "membership_epoch": 1,
            "budget_mb": 1024,
            "eviction_priority": 0,
            "max_concurrency": 4,
            "boot_id": residency["boot_id"],
            "observed_generation": None,
            "generation": "1",
            "control_epoch": "1",
            "runtime_epoch": runtime_epoch,
            "token_expires_at": expiry,
            "db_now": datetime.now(UTC).isoformat(),
        }
        proof_args = {"expected_subject": cold, "challenge": None}
        assert (await _step(factory, "cold_terminal_from_health", proof_args))[
            "status"
        ] == "ready"
        wrong_boot = {
            **proof_args,
            "expected_subject": {**cold, "boot_id": "other-boot"},
        }
        assert (await _step(factory, "cold_terminal_from_health", wrong_boot))[
            "reason"
        ] == "generation_changed"
        repeated = await _step(factory, "installation_terminal", args)
        assert repeated["preparation"]["generation"] == prepared["generation"]
        recovered = await _step(
            factory,
            "installation_terminal",
            {
                **args,
                "ledger_source_generation": None,
            },
        )
        assert recovered["preparation"]["generation"] == prepared["generation"]
        assert recovered["preparation"]["source_generation"] == "1"
        async with factory() as db:
            assert (
                await db.get(GPUBackendFence, backend_id)
            ).token_expiry_high_water == reserved_horizon
        probe_started = datetime.now(UTC) - timedelta(seconds=2)
        proof = {
            **args,
            "phase": "prove_idle",
            "preparation": prepared,
            "probe_started_at": probe_started.isoformat(),
        }
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            fence.token_expiry_high_water = datetime.now(UTC) + timedelta(seconds=1)
        assert (await _step(factory, "installation_terminal", proof))[
            "reason"
        ] == "terminal_idle_unproven"
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            fence.token_expiry_high_water = datetime.now(UTC) - timedelta(seconds=10)
        assert (
            await _step(
                factory,
                "installation_terminal",
                {
                    **proof,
                    "residency": {**residency, "active_requests": 1},
                },
            )
        )["reason"] == "terminal_idle_unproven"
        assert (
            await _step(
                factory,
                "installation_terminal",
                {
                    **proof,
                    "residency": {**residency, "boot_id": "other-boot"},
                },
            )
        )["reason"] == "installation_changed"
        assert (
            await _step(
                factory,
                "installation_terminal",
                {
                    **proof,
                    "preparation": {**prepared, "revision": 999},
                },
            )
        )["reason"] == "terminal_intent_changed"
        assert (await _step(factory, "installation_terminal", proof))[
            "status"
        ] == "verified"
        conflicting = {**args, "request": {**request, "operation_id": str(uuid4())}}
        assert (await _step(factory, "installation_terminal", conflicting))[
            "reason"
        ] == "terminal_intent_conflict"
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
        terminal = {
            **residency,
            "state": "draining",
            "draining": True,
            "gpu_loaded": False,
            "evictable": False,
            "generation": "2",
            "pools": {"models": {"resident": False, "device": None, "provider": None}},
        }
        verify = {
            "phase": "verify",
            "request": request,
            "residency": terminal,
            "preparation": prepared,
            "token_expires_at": prepared["token_expires_at"],
            "ledger_source_generation": "1",
            "probe_started_at": None,
        }
        assert (await _step(factory, "installation_terminal", verify))[
            "status"
        ] == "verified"
        busy = {**terminal, "active_requests": 1}
        assert (
            await _step(factory, "installation_terminal", {**verify, "residency": busy})
        )["status"] == "blocked"
        assert (
            await _step(factory, "installation_terminal", {**verify, "phase": "commit"})
        )["status"] == "verified"
        replay = await _step(
            factory, "installation_terminal", {**args, "residency": terminal}
        )
        assert replay["preparation"]["generation"] == "2"
        assert replay["preparation"]["settled"] is True
        verify = {
            **verify,
            "preparation": replay["preparation"],
            "token_expires_at": replay["preparation"]["token_expires_at"],
        }
        async with factory.begin() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            row.extra_params = {
                **row.extra_params,
                "plugin": {**row.extra_params["plugin"], "revision": 5},
            }
        assert (await _step(factory, "installation_terminal", verify))[
            "reason"
        ] == "terminal_proof_changed"
        # Model the owner-validated new launch after quiescence: the old launch
        # is stopped, its activation was removed, and the installation revision
        # has advanced. A new boot is still fenced by the old token horizon.
        new_boot = f"new-{uuid4()}"
        async with factory.begin() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            row.extra_params = {
                **row.extra_params,
                "plugin_lifecycle": {
                    "launch": {
                        "registry_id": str(backend_id),
                        "container_id": "b" * 64,
                        "resource": resource,
                    },
                    "stopped": [
                        {"container_id": "a" * 64, "boot_id": request["boot_id"]}
                    ],
                },
            }
        fresh = _residency(backend_id, _RESOURCE_A, resident=False)
        fresh.update(
            {
                "boot_id": new_boot,
                "identity": None,
                "control_epoch": None,
                "lifecycle_gate": "legacy",
            }
        )
        new_request = {
            **request,
            "boot_id": new_boot,
            "container_id": "b" * 64,
            "operation_id": str(uuid4()),
        }
        control = {
            "request": new_request,
            "phase": "reset",
            "residency": fresh,
            "probe_started_at": datetime.now(UTC).isoformat(),
            "token_expires_at": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
        }
        old_boot = {
            **control,
            "request": {**new_request, "boot_id": request["boot_id"]},
            "residency": {**fresh, "boot_id": request["boot_id"]},
        }
        assert (await _step(factory, "installation_control", old_boot))[
            "reason"
        ] == "installation_control_changed"
        assert (await _step(factory, "installation_control", control))[
            "reason"
        ] == "control_token_horizon"
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            fence.token_expiry_high_water = datetime.now(UTC) - timedelta(seconds=1)
        control["probe_started_at"] = datetime.now(UTC).isoformat()
        replacement = await _step(factory, "installation_control", control)
        assert replacement["status"] == "prepared"
        assert int(replacement["preparation"]["control_epoch"]) > int(
            prepared["control_epoch"]
        )
    finally:
        async with factory.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM aap_inference_gpu_v1.installation_terminal_intents WHERE backend_registry_id=:id"
                ),
                {"id": backend_id},
            )
            await db.execute(
                text(
                    "DELETE FROM aap_inference_gpu_v1.installation_control_intents WHERE backend_registry_id=:id"
                ),
                {"id": backend_id},
            )
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_pending_installation_control_reset_enforce_and_replay(
    test_engine: AsyncEngine,
):
    factory = await _open_gpu_authority_provider(test_engine)
    backend_id = uuid4()
    ids = (backend_id,)
    resource = {
        "gpu_resource_id": _RESOURCE_A,
        "physical_gpu_uuid": "GPU-a",
        "logical_device": "cuda:0",
    }
    plugin = {
        "installation_id": str(backend_id),
        "status": "prepared",
        "revision": 1,
        "resource": resource,
    }
    lifecycle = {
        "launch": {
            "registry_id": str(backend_id),
            "container_id": "a" * 64,
            "resource": resource,
        },
        "stopped": [],
    }
    endpoint = f"http://gpu-proof-{backend_id}.test"
    try:
        async with factory.begin() as db:
            db.add(
                MLBackendRegistry(
                    id=backend_id,
                    name="terminal-pending",
                    url=endpoint,
                    gpu_resource_id=_RESOURCE_A,
                    vram_budget_mb=1024,
                    eviction_priority=0,
                    extra_params={
                        "max_concurrency": 4,
                        "plugin": plugin,
                        "plugin_lifecycle": lifecycle,
                    },
                )
            )
        initial = _residency(backend_id, _RESOURCE_A, resident=False)
        initial.update(
            {"identity": None, "control_epoch": None, "lifecycle_gate": "legacy"}
        )
        request = {
            "registry_id": str(backend_id),
            "resource_id": _RESOURCE_A,
            "endpoint": endpoint,
            "boot_id": initial["boot_id"],
            "operation_id": str(uuid4()),
            "container_id": "a" * 64,
            "pool_ids": ["models"],
        }
        args = {
            "request": request,
            "phase": "reset",
            "residency": initial,
            "probe_started_at": datetime.now(UTC).isoformat(),
            "token_expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        }
        assert (
            await _step(
                factory, "installation_control", {**args, "phase": "mode_enforce"}
            )
        )["reason"] == "installation_reset_unconfirmed"
        async with factory.begin() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            row.extra_params = {
                "max_concurrency": 4,
                "plugin": plugin,
                "plugin_lifecycle": {
                    **lifecycle,
                    "stopped": [{"container_id": "a" * 64}],
                },
            }
        assert (await _step(factory, "installation_control", args))[
            "reason"
        ] == "installation_control_changed"
        async with factory.begin() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            row.extra_params = {
                "max_concurrency": 4,
                "plugin": plugin,
                "plugin_lifecycle": lifecycle,
            }
        args["probe_started_at"] = datetime.now(UTC).isoformat()
        args["token_expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=2)
        ).isoformat()
        first = await _step(factory, "installation_control", args)
        assert first["status"] == "prepared"
        assert first["preparation"]["control_epoch"] == "1"
        replay = await _step(factory, "installation_control", args)
        assert replay == first
        async with factory() as db:
            membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_A))
            fence = await db.get(GPUBackendFence, backend_id)
            assert membership.state == "active"
            assert fence.runtime_epoch_high_water == 1
            assert fence.control_epoch_high_water == 1
        bound = _residency(backend_id, _RESOURCE_A, resident=False)
        bound["lifecycle_gate"] = "legacy"
        acknowledged = await _step(
            factory, "installation_control", {**args, "residency": bound}
        )
        assert acknowledged["status"] == "acknowledged"
        mode = {**args, "phase": "mode_enforce", "residency": bound}
        assert (await _step(factory, "installation_control", mode))[
            "reason"
        ] == "control_token_horizon"
        await asyncio.sleep(2.1)
        mode["probe_started_at"] = datetime.now(UTC).isoformat()
        mode["token_expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=30)
        ).isoformat()
        enforce = await _step(factory, "installation_control", mode)
        assert enforce["status"] == "prepared"
        assert enforce["preparation"]["control_epoch"] == "2"
        enforced = {**bound, "control_epoch": "2", "lifecycle_gate": "enforce"}
        assert (
            await _step(
                factory, "installation_control", {**mode, "residency": enforced}
            )
        )["status"] == "complete"
        assert (
            await _step(
                factory,
                "installation_control",
                {**mode, "phase": "reset", "residency": enforced},
            )
        )["status"] == "complete"
    finally:
        async with factory.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM aap_inference_gpu_v1.installation_control_intents WHERE backend_registry_id=:id"
                ),
                {"id": backend_id},
            )
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_disabled_enforced_resident_resume_preserves_live_work(
    test_engine: AsyncEngine,
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
        extra_params_factory=lambda backend_id, _index: {
            "max_concurrency": 4,
            "plugin": {
                "installation_id": str(backend_id),
                "status": "disabled",
                "revision": 8,
                "resource": resource,
            },
            "plugin_lifecycle": {
                "launch": {
                    "registry_id": str(backend_id),
                    "container_id": "b" * 64,
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
            fence = await db.get(GPUBackendFence, backend_id)
            fence.token_expiry_high_water = datetime.now(UTC) + timedelta(minutes=2)
            residency = {
                **row.health_meta["residency"],
                "active_requests": 2,
                "borrowers": 1,
            }
            endpoint = row.url
        request = {
            "registry_id": str(backend_id),
            "resource_id": _RESOURCE_A,
            "endpoint": endpoint,
            "boot_id": residency["boot_id"],
            "operation_id": str(uuid4()),
            "container_id": "b" * 64,
            "pool_ids": ["models"],
        }
        args = {
            "request": request,
            "phase": "reset",
            "residency": residency,
            "probe_started_at": datetime.now(UTC).isoformat(),
            "token_expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        }
        result = await _step(factory, "installation_control", args)
        assert result["status"] == "complete"
        assert result["preparation"]["control_epoch"] == "1"
        ahead = {**residency, "generation": "2"}
        assert (
            await _step(factory, "installation_control", {**args, "residency": ahead})
        )["status"] == "blocked"
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            assert fence.control_epoch_high_water == 1
            assert fence.generation_high_water == 1
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin",
    [None, {}, {"status": "active", "resource": {"gpu_resource_id": "other/GPU"}}],
)
async def test_malformed_plugin_is_blocked_from_initial_fence(test_engine, plugin):
    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=lambda _id, _index: {
            "max_concurrency": 4,
            "plugin": plugin,
        },
    )
    try:
        await _install_live_health(
            factory, ids, resource_id=_RESOURCE_A, resident_backend_ids=frozenset(ids)
        )
        result = await _step(
            factory,
            "runtime_subject",
            {
                "backend_registry_id": str(ids[0]),
                "gpu_resource_id": _RESOURCE_A,
            },
        )
        assert result == {"status": "blocked", "reason": "plugin_not_active"}
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_owner_lifecycle_preserves_fence_and_requires_settled_terminal(
    test_engine,
):
    resource = {
        "gpu_resource_id": _RESOURCE_A,
        "physical_gpu_uuid": "GPU-a",
        "logical_device": "cuda:0",
    }

    def initial(backend_id, _index):
        launch = {
            "installation_id": str(backend_id),
            "registry_id": str(backend_id),
            "artifact": "sha256:" + "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "container_id": "c" * 64,
            "resource": resource,
        }
        return {
            "max_concurrency": 4,
            "plugin": {
                "installation_id": str(backend_id),
                "status": "prepared",
                "revision": 1,
                "resource": resource,
                "manifest": {"resource": {"device": "cuda"}},
            },
            "plugin_lifecycle": {"launch": launch, "qualification": {}, "stopped": []},
        }

    factory, ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
        extra_params_factory=initial,
        auth_token="secret",
    )
    backend_id = ids[0]

    async def command(revision, action, evidence=None):
        async with factory.begin() as db:
            return await db.scalar(
                text(
                    "SELECT aap_inference_topology_v1.command_plugin_installation(:id,:revision,:action,:evidence)"
                ),
                {
                    "id": backend_id,
                    "revision": revision,
                    "action": action,
                    "evidence": json.dumps(evidence) if evidence is not None else None,
                },
            )

    try:
        await _install_live_health(
            factory, ids, resource_id=_RESOURCE_A, resident_backend_ids=frozenset(ids)
        )
        async with factory() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            launch = row.extra_params["plugin_lifecycle"]["launch"]
            endpoint = row.url
            residency = row.health_meta["residency"]
            membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_A))
            epoch = membership.membership_epoch
        activation = {
            **launch,
            "endpoint": endpoint,
            "boot_id": residency["boot_id"],
            "control_epoch": "1",
        }
        await command(1, "activate", activation)
        assert (
            await _step(
                factory,
                "runtime_subject",
                {
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": _RESOURCE_A,
                },
            )
        )["status"] == "ready"
        await command(2, "disable")
        assert (
            await _step(
                factory,
                "runtime_subject",
                {
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": _RESOURCE_A,
                },
            )
        )["reason"] == "plugin_not_active"
        for column, value in (
            ("url", "http://other.test"),
            ("auth_token", "other"),
            ("vram_budget_mb", 2048),
        ):
            with pytest.raises(DBAPIError):
                async with factory.begin() as db:
                    row = await db.get(MLBackendRegistry, backend_id)
                    setattr(row, column, value)
        with pytest.raises(DBAPIError):
            async with factory.begin() as db:
                row = await db.get(MLBackendRegistry, backend_id)
                row.extra_params = {
                    **row.extra_params,
                    "plugin": {
                        **row.extra_params["plugin"],
                        "resource": {**resource, "logical_device": "cuda:1"},
                    },
                }
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
        prepared = (await _step(factory, "installation_terminal", args))["preparation"]
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
            **launch,
            "endpoint": endpoint,
            "boot_id": residency["boot_id"],
            "control_epoch": prepared["control_epoch"],
            "generation": prepared["generation"],
            "operation_id": request["operation_id"],
            "pool_ids": ["models"],
        }
        with pytest.raises(DBAPIError):
            await command(3, "quiesce", proof)
        with pytest.raises(DBAPIError):
            await command(3, "retire")
        async with factory() as db:
            assert (await db.get(MLBackendRegistry, backend_id)).auth_token == "secret"
            assert (
                await db.get(GPUBackendMembership, (backend_id, _RESOURCE_A))
            ).membership_epoch == epoch
        commit = {
            **args,
            "phase": "commit",
            "residency": terminal,
            "preparation": prepared,
            "token_expires_at": prepared["token_expires_at"],
        }
        busy = {
            **terminal,
            "gpu_loaded": True,
            "borrowers": 1,
            "pools": residency["pools"],
        }
        assert (
            await _step(factory, "installation_terminal", {**commit, "residency": busy})
        )["status"] == "blocked"
        assert (await _step(factory, "installation_terminal", commit))[
            "status"
        ] == "verified"
        await command(3, "quiesce", proof)
        await command(4, "retire")
        async with factory() as db:
            row = await db.get(MLBackendRegistry, backend_id)
            assert row.auth_token != "secret"
            assert row.gpu_resource_id is None and row.vram_budget_mb is None
            membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_A))
            assert membership.state == "retiring"
            collection = await db.scalar(
                text(
                    "SELECT aap_inference_gpu_collector_v1.gpu_collector_step('collect_tombstone',:arguments)"
                ),
                {
                    "arguments": json.dumps(
                        {
                            "phase": "prepare",
                            "backend_id": str(backend_id),
                            "resource_id": _RESOURCE_A,
                            "membership_epoch": membership.membership_epoch,
                            "proof": None,
                        }
                    )
                },
            )
            assert collection["result"] == {
                "status": "blocked",
                "reason": "waiting_token_horizon",
            }
    finally:
        async with factory.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM aap_inference_gpu_v1.installation_terminal_intents WHERE backend_registry_id=:id"
                ),
                {"id": backend_id},
            )
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_terminal_domain_bootstraps_missing_promoted_membership(
    test_engine: AsyncEngine, proof_store
):
    factory, ids = await _create_active_backends(
        test_engine, resource_id=_RESOURCE_A, budgets=(1024,)
    )
    backend_id = ids[0]
    try:
        await proof_store.mark_card_not_ready(
            _RESOURCE_A, 8192, reason="installation-drain"
        )
        before = await proof_store.snapshot(_RESOURCE_A)
        assert not before.backend_memberships
        await ensure_installation_domain(
            factory, proof_store, _RESOURCE_A, str(backend_id), 1
        )
        after = await proof_store.snapshot(_RESOURCE_A)
        assert len(after.backend_memberships) == 1
        assert after.backend_memberships[0].backend_id == str(backend_id)
        assert after.backend_memberships[0].state == "active"
        assert after.backend_memberships[0].membership_epoch == 1
        assert not after.ready
        started = await proof_store.begin_installation_drain(
            _RESOURCE_A,
            backend_id=str(backend_id),
            membership_epoch=1,
            source_generation=None,
            generation="1",
            owner_id=f"installation:{uuid4()}",
        )
        assert started.status == "owned"
    finally:
        await _cleanup_backends(factory, ids)


@pytest.mark.asyncio
async def test_terminal_first_claim_promotes_pending_legacy_resident(
    test_engine: AsyncEngine,
):
    factory = await _open_gpu_authority_provider(test_engine)
    backend_id = uuid4()
    resource = {
        "gpu_resource_id": _RESOURCE_A,
        "physical_gpu_uuid": "GPU-a",
        "logical_device": "cuda:0",
    }
    endpoint = f"http://gpu-proof-{backend_id}.test"
    try:
        async with factory.begin() as db:
            db.add(
                MLBackendRegistry(
                    id=backend_id,
                    name="terminal-legacy",
                    url=endpoint,
                    gpu_resource_id=_RESOURCE_A,
                    vram_budget_mb=1024,
                    eviction_priority=0,
                    extra_params={
                        "max_concurrency": 4,
                        "plugin": {
                            "installation_id": str(backend_id),
                            "status": "prepared",
                            "revision": 1,
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
            )
        residency = _residency(backend_id, _RESOURCE_A, resident=True)
        residency.update(
            identity=None,
            control_epoch=None,
            lifecycle_gate="legacy",
            generation=None,
            evictable=False,
            active_requests=1,
            borrowers=1,
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
        args = {
            "phase": "reserve",
            "request": request,
            "residency": residency,
            "preparation": None,
            "token_expires_at": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
            "ledger_source_generation": None,
            "probe_started_at": None,
        }
        preparation = (await _step(factory, "installation_terminal", args))[
            "preparation"
        ]
        assert preparation["generation"] == "1" and preparation["control_epoch"] == "1"
        assert (await _step(factory, "installation_terminal", args))["preparation"][
            "generation"
        ] == "1"
        preparation = (
            await _step(
                factory,
                "installation_terminal",
                {
                    **args,
                    "phase": "authorize",
                    "preparation": preparation,
                },
            )
        )["preparation"]
        terminal = {
            **residency,
            "identity": {
                "audience": "aap-gpu-lifecycle",
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": _RESOURCE_A,
            },
            "control_epoch": "1",
            "lifecycle_gate": "enforce",
            "state": "draining",
            "draining": True,
            "gpu_loaded": False,
            "generation": "1",
            "active_requests": 0,
            "borrowers": 0,
            "pools": {"models": {"resident": False, "device": None, "provider": None}},
        }
        verify = {
            **args,
            "phase": "verify",
            "residency": terminal,
            "preparation": preparation,
            "token_expires_at": preparation["token_expires_at"],
        }
        assert (await _step(factory, "installation_terminal", verify))[
            "status"
        ] == "verified"
        assert (
            await _step(factory, "installation_terminal", {**verify, "phase": "commit"})
        )["status"] == "verified"
    finally:
        async with factory.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM aap_inference_gpu_v1.installation_terminal_intents WHERE backend_registry_id=:id"
                ),
                {"id": backend_id},
            )
        await _cleanup_backends(factory, (backend_id,))
