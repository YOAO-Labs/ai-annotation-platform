"""v0.8.6 F2 · ML Backend 周期健康检查 worker 单测。

覆盖：
- `MLBackendService.check_health` 写入 `last_checked_at` + 更新 `state`
- worker `check_all_backends` 遍历所有 backend、单个失败不阻断其他
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from aap_protocol_v2.lifecycle import (
    ManagedLifecycleCapabilities,
    managed_lifecycle_capability_sha256,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.inference_gpu_data import bind_gpu_authority_data
from app.services.inference_health_data import InferenceHealthData
from app.services.ml_backend import MLBackendService
from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER


async def _make_backend(db: AsyncSession, url: str = "http://example/"):
    return await MLBackendService(db).create_registry(
        "test-backend",
        url,
        is_interactive=True,
    )


def _install_gpu_resources(
    monkeypatch,
    *resource_ids: str,
    allocatable_mb: int = 8192,
) -> None:
    """Declare the physical resources these tests claim so validation stays real."""
    from app.config import settings

    resources = {}
    for resource_id in resource_ids:
        node_id, device = resource_id.split("/", 1)
        resources[resource_id] = {
            "node_id": node_id,
            "physical_device_token": device,
            "allocatable_mb": allocatable_mb,
            "mode": "off",
        }
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        json.dumps(resources),
        raising=True,
    )


def _install_backend_control_endpoint(monkeypatch) -> None:
    """Give the real caller-UOW control adapter a parsable owner endpoint."""
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "inference_backend_control_url",
        "http://backend-control.invalid",
        raising=True,
    )
    monkeypatch.setattr(
        settings,
        "inference_backend_control_token",
        "health-worker-test-token",
        raising=True,
    )
    monkeypatch.setattr(
        settings,
        "inference_backend_control_token_file",
        "",
        raising=True,
    )


def _install_health_observation(monkeypatch, health_meta, setup=None) -> None:
    class FakeHealthObservation:
        async def health_meta(self, *, gpu_health_challenge=None):
            if gpu_health_challenge is None:
                return await health_meta(self)
            return await health_meta(self, gpu_health_challenge=gpu_health_challenge)

        async def setup(self):
            if setup is None:
                raise RuntimeError("setup unavailable")
            return await setup(self)

    def client_factory(_db):
        return lambda _backend: FakeHealthObservation()

    monkeypatch.setattr(
        "app.services.inference_backend_control.health_observation_client_factory",
        client_factory,
        raising=True,
    )
    # The health path still constructs the real caller-UOW control adapter, whose
    # owner endpoint must parse even though only the observation port is faked.
    _install_backend_control_endpoint(monkeypatch)


async def _cleanup_gpu_test_registry(factory, backend_id: uuid.UUID) -> None:
    """Remove only this test's GPU rows after behavior has been asserted."""
    async with factory.begin() as db:
        await db.execute(
            text(
                "ALTER TABLE ml_backend_registry DISABLE TRIGGER "
                "trg_sync_gpu_backend_membership"
            )
        )
        await db.execute(
            text(
                "ALTER TABLE gpu_backend_memberships DISABLE TRIGGER "
                "trg_validate_gpu_backend_membership"
            )
        )
        await db.execute(
            text(
                "ALTER TABLE gpu_backend_fences DISABLE TRIGGER "
                "trg_validate_gpu_backend_fence_delete"
            )
        )
        arguments = {"backend_id": backend_id}
        pool_ids = tuple(
            (
                await db.execute(
                    text(
                        "SELECT pool_id FROM public.ml_backend_pool_members "
                        "WHERE registry_id=:backend_id"
                    ),
                    arguments,
                )
            ).scalars()
        )
        await db.execute(
            text(
                "DELETE FROM public.gpu_backend_memberships "
                "WHERE backend_registry_id=:backend_id"
            ),
            arguments,
        )
        await db.execute(
            text(
                "DELETE FROM public.gpu_backend_fences "
                "WHERE backend_registry_id=:backend_id"
            ),
            arguments,
        )
        await db.execute(
            text(
                "DELETE FROM public.ml_backend_pool_members "
                "WHERE registry_id=:backend_id"
            ),
            arguments,
        )
        for pool_id in pool_ids:
            await db.execute(
                text(
                    "DELETE FROM public.ml_backend_service_pools AS pool "
                    "WHERE pool.id=:pool_id AND NOT EXISTS ("
                    "SELECT 1 FROM public.ml_backend_pool_members AS member "
                    "WHERE member.pool_id=pool.id)"
                ),
                {"pool_id": pool_id},
            )
        await db.execute(
            text("DELETE FROM public.ml_backend_registry WHERE id=:backend_id"),
            arguments,
        )
        await db.execute(
            text(
                "ALTER TABLE gpu_backend_fences ENABLE TRIGGER "
                "trg_validate_gpu_backend_fence_delete"
            )
        )
        await db.execute(
            text(
                "ALTER TABLE gpu_backend_memberships ENABLE TRIGGER "
                "trg_validate_gpu_backend_membership"
            )
        )
        await db.execute(
            text(
                "ALTER TABLE ml_backend_registry ENABLE TRIGGER "
                "trg_sync_gpu_backend_membership"
            )
        )


async def test_check_health_updates_last_checked_at(
    db_session: AsyncSession, monkeypatch
):
    backend = await _make_backend(db_session)

    async def fake_health_meta(self) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, None

    _install_health_observation(monkeypatch, fake_health_meta)

    svc = MLBackendService(db_session)
    before = datetime.now(timezone.utc)
    healthy = await svc.check_health(backend.id)
    await db_session.flush()

    assert healthy is True
    fresh = await svc.get_registry(backend.id)
    assert fresh is not None
    assert fresh.state == "connected"
    assert fresh.last_checked_at is not None
    assert fresh.last_checked_at >= before


async def test_check_health_marks_error_on_failure(
    db_session: AsyncSession, monkeypatch
):
    backend = await _make_backend(db_session)
    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(
            health_meta={
                "compute": {"configured_device": "cpu"},
                "gpu_info": {"device_uuid": "GPU-stale"},
            }
        )
    )

    async def fake_health_meta(self) -> tuple[bool, dict | None]:  # noqa: ARG001
        return False, None

    _install_health_observation(monkeypatch, fake_health_meta)

    svc = MLBackendService(db_session)
    healthy = await svc.check_health(backend.id)

    assert healthy is False
    fresh = await svc.get_registry(backend.id)
    assert fresh.state == "error"
    assert fresh.last_checked_at is not None
    assert fresh.health_meta is None


async def test_check_health_persists_compute_meta(
    db_session: AsyncSession, monkeypatch
):
    backend = await _make_backend(db_session)
    compute = {
        "configured_device": "cuda",
        "effective_provider": "CPUExecutionProvider",
        "cpu_fallback_supported": True,
    }

    async def fake_health_meta(self) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, {"compute": compute}

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    assert await MLBackendService(db_session).check_health(backend.id) is True
    await db_session.flush()

    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.health_meta["compute"] == compute


async def test_check_health_discards_response_after_endpoint_identity_changes(
    db_session: AsyncSession, monkeypatch
):
    backend = await _make_backend(db_session, url="http://old-endpoint/")

    async def fake_health_meta(self) -> tuple[bool, dict | None]:  # noqa: ARG001
        await MLBackendService(db_session).update(
            backend.id,
            url="http://new-endpoint/",
        )
        return True, {
            "compute": {"configured_device": "cpu"},
            "gpu_info": {"device_uuid": "GPU-old"},
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    healthy = await MLBackendService(db_session).check_health(backend.id)

    assert healthy is False
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.url == "http://new-endpoint"
    assert fresh.state == "disconnected"
    assert fresh.health_meta is None
    assert fresh.last_checked_at is None


async def test_check_health_discards_response_after_gpu_membership_changes(
    db_session: AsyncSession, monkeypatch
):
    backend = await _make_backend(db_session, url="http://gpu-membership-old/")
    _install_gpu_resources(
        monkeypatch,
        "node-health/GPU-old",
        "node-health/GPU-new",
    )
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id="node-health/GPU-old",
        vram_budget_mb=1024,
    )

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        await MLBackendService(db_session).update(
            backend.id,
            gpu_resource_id="node-health/GPU-new",
        )
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {
                "state": "resident",
                "gpu_loaded": True,
                "identity": {"gpu_resource_id": "node-health/GPU-old"},
            },
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    assert await MLBackendService(db_session).check_health(backend.id) is False
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.gpu_resource_id == "node-health/GPU-new"
    assert fresh.health_meta is None
    assert fresh.last_checked_at is None


async def test_check_health_discards_response_after_configuration_aba(
    db_session: AsyncSession, monkeypatch
):
    backend = await _make_backend(db_session, url="http://gpu-membership-epoch/")
    _install_gpu_resources(monkeypatch, "node-health/GPU-epoch")
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id="node-health/GPU-epoch",
        vram_budget_mb=1024,
    )

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        service = MLBackendService(db_session)
        await service.update(backend.id, vram_budget_mb=2048)
        await service.update(backend.id, vram_budget_mb=1024)
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {
                "state": "resident",
                "gpu_loaded": True,
                "identity": {"gpu_resource_id": "node-health/GPU-epoch"},
            },
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    assert await MLBackendService(db_session).check_health(backend.id) is False
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.vram_budget_mb == 1024
    assert fresh.health_meta is None
    assert fresh.last_checked_at is None
    membership = await InferenceHealthData(db_session).read_membership(
        backend.id, "node-health/GPU-epoch"
    )
    assert membership is not None
    assert membership.membership_epoch > 1


async def test_gpu_check_health_persists_challenge_bound_probe(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    backend = await _make_backend(db_session, url="http://gpu-proof-health/")
    resource_id = "node-health/GPU-proof"
    _install_gpu_resources(monkeypatch, resource_id)
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id=resource_id,
        vram_budget_mb=1024,
    )
    captured_challenge: str | None = None

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        nonlocal captured_challenge
        captured_challenge = gpu_health_challenge
        await asyncio.sleep(0.01)
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {
                "state": "unloaded",
                "gpu_loaded": False,
                "active_requests": 0,
                "builders": 0,
                "borrowers": 0,
                "pools": {"models": {"resident": False}},
            },
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    expected_challenge = "a" * 64
    assert (
        await MLBackendService(db_session).check_health(
            backend.id,
            gpu_health_challenge=expected_challenge,
        )
        is True
    )
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.state == "connected"
    assert fresh.health_meta is not None
    assert GPU_HEALTH_CHALLENGE_ECHO_MARKER not in fresh.health_meta
    proof = fresh.health_meta["gpu_arbiter_probe"]
    assert captured_challenge == expected_challenge
    assert proof["challenge"] == expected_challenge
    assert set(proof) == {
        "protocol_version",
        "challenge",
        "backend_registry_id",
        "gpu_resource_id",
        "membership_epoch",
        "membership_state",
        "managed_lifecycle_sha256",
        "probe_started_at",
        "observed_at",
    }
    assert proof["protocol_version"] == "1"
    assert proof["challenge"] == captured_challenge
    assert proof["backend_registry_id"] == str(backend.id)
    assert proof["gpu_resource_id"] == resource_id
    assert proof["membership_epoch"] == "1"
    assert proof["membership_state"] == "pending"
    assert proof["managed_lifecycle_sha256"] is None
    assert proof["probe_started_at"].endswith("Z")
    assert proof["observed_at"].endswith("Z")
    probe_started_at = datetime.fromisoformat(
        proof["probe_started_at"].replace("Z", "+00:00")
    )
    observed_at = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
    assert probe_started_at < observed_at
    assert fresh.last_checked_at == observed_at


async def test_gpu_check_health_binds_strict_capability_after_setup(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    backend = await _make_backend(db_session, url="http://gpu-capability-health/")
    resource_id = "node-health/GPU-capability"
    _install_gpu_resources(monkeypatch, resource_id)
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id=resource_id,
        vram_budget_mb=1024,
    )
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    setup_finished_at: datetime | None = None

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {
                "state": "unloaded",
                "gpu_loaded": False,
                "active_requests": 0,
                "builders": 0,
                "borrowers": 0,
                "draining": False,
                "evictable": False,
                "generation": None,
                "pools": {
                    "models": {
                        "resident": False,
                        "device": None,
                        "provider": None,
                    }
                },
                "boot_id": "boot-capability",
                "lifecycle_gate": "legacy",
                "control_epoch": None,
                "identity": None,
            },
        }

    async def fake_setup(self):  # noqa: ARG001
        nonlocal setup_finished_at
        setup_finished_at = await db_session.scalar(select(func.clock_timestamp()))
        return {
            "name": "managed-backend",
            "supported_prompts": ["none"],
            "managed_lifecycle": capability,
        }

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    assert await MLBackendService(db_session).check_health(backend.id) is True
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.health_meta is not None
    assert fresh.health_meta["capabilities"]["managed_lifecycle"] == capability
    proof = fresh.health_meta["gpu_arbiter_probe"]
    assert proof["managed_lifecycle_sha256"] == (
        managed_lifecycle_capability_sha256(capability)
    )
    probe_started_at = datetime.fromisoformat(
        proof["probe_started_at"].replace("Z", "+00:00")
    )
    observed_at = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
    assert setup_finished_at is not None
    assert probe_started_at < setup_finished_at <= observed_at
    assert fresh.last_checked_at == observed_at


async def test_gpu_check_health_clears_stale_capability_after_invalid_setup(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    backend = await _make_backend(db_session, url="http://gpu-invalid-capability/")
    resource_id = "node-health/GPU-invalid-capability"
    _install_gpu_resources(monkeypatch, resource_id)
    stale_capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id=resource_id,
        vram_budget_mb=1024,
    )
    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(
            health_meta={
                "capabilities": {"managed_lifecycle": stale_capability},
                "gpu_arbiter_probe": {
                    "managed_lifecycle_sha256": (
                        managed_lifecycle_capability_sha256(stale_capability)
                    )
                },
            }
        )
    )

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, {GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge}

    async def fake_setup(self):  # noqa: ARG001
        return {
            "name": "partial-managed-backend",
            "supported_prompts": ["none"],
            "managed_lifecycle": {"protocol_version": "1"},
        }

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    assert await MLBackendService(db_session).check_health(backend.id) is True
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.health_meta is not None
    capabilities = fresh.health_meta["capabilities"]
    assert capabilities["managed_lifecycle"] is None
    assert any(
        warning["field"] == "managed_lifecycle" for warning in capabilities["warnings"]
    )
    assert fresh.health_meta["gpu_arbiter_probe"]["managed_lifecycle_sha256"] is None


async def test_gpu_check_health_rejects_zero_length_proof_window(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    backend = await _make_backend(db_session, url="http://gpu-zero-window/")
    resource_id = "node-health/GPU-zero-window"
    _install_gpu_resources(monkeypatch, resource_id)
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id=resource_id,
        vram_budget_mb=1024,
    )

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, {GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge}

    async def fake_setup(self):  # noqa: ARG001
        return {"name": "legacy-backend", "supported_prompts": ["none"]}

    fixed_clock = datetime.now(timezone.utc)
    original_scalar = db_session.scalar

    async def scalar_with_fixed_clock(statement, *args, **kwargs):
        if "clock_timestamp" in str(statement) or "observation_clock()" in str(
            statement
        ):
            return fixed_clock
        return await original_scalar(statement, *args, **kwargs)

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)
    monkeypatch.setattr(db_session, "scalar", scalar_with_fixed_clock)

    assert await MLBackendService(db_session).check_health(backend.id) is False
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.health_meta is None
    assert fresh.last_checked_at is None


async def test_gpu_check_health_rejects_older_health_after_slow_setup(
    test_engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = "node-health/GPU-overlapping-proof"
    _install_gpu_resources(monkeypatch, resource_id)
    async with factory.begin() as db:
        backend = await MLBackendService(db).create_registry(
            "health-overlapping-proof",
            f"http://health-overlapping-{uuid.uuid4()}.test",
            gpu_resource_id=resource_id,
            vram_budget_mb=1024,
        )
        backend_id = backend.id

    old_setup_started = asyncio.Event()
    allow_old_setup = asyncio.Event()
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")

    def task_label() -> str:
        task = asyncio.current_task()
        assert task is not None
        return task.get_name()

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        label = task_label()
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {
                "state": "unloaded",
                "gpu_loaded": False,
                "active_requests": 0,
                "builders": 0,
                "borrowers": 0,
                "draining": False,
                "evictable": False,
                "generation": None,
                "pools": {},
                "boot_id": f"boot-{label}",
                "lifecycle_gate": "legacy",
                "control_epoch": None,
                "identity": None,
            },
        }

    async def fake_setup(self):  # noqa: ARG001
        if task_label() == "older-health":
            old_setup_started.set()
            await allow_old_setup.wait()
            return {
                "name": "older-partial-backend",
                "supported_prompts": ["none"],
                "managed_lifecycle": {"protocol_version": "1"},
            }
        return {
            "name": "newer-managed-backend",
            "supported_prompts": ["none"],
            "managed_lifecycle": capability,
        }

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    async def run_health() -> bool:
        async with factory.begin() as db:
            return await MLBackendService(db).check_health(backend_id)

    older = asyncio.create_task(run_health(), name="older-health")
    newer: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(old_setup_started.wait(), timeout=2)
        newer = asyncio.create_task(run_health(), name="newer-health")
        assert await asyncio.wait_for(newer, timeout=2) is True
        allow_old_setup.set()
        assert await asyncio.wait_for(older, timeout=2) is False

        async with factory() as db:
            backend = await MLBackendService(db).get_registry(backend_id)
            assert backend is not None
            assert backend.health_meta is not None
            assert backend.health_meta["residency"]["boot_id"] == "boot-newer-health"
            assert (
                backend.health_meta["capabilities"]["managed_lifecycle"] == capability
            )
            assert backend.health_meta["gpu_arbiter_probe"][
                "managed_lifecycle_sha256"
            ] == managed_lifecycle_capability_sha256(capability)
    finally:
        allow_old_setup.set()
        for task in (older, newer):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        await _cleanup_gpu_test_registry(factory, backend_id)


async def test_gpu_check_health_rotates_challenge_and_clears_stale_probe_without_echo(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    backend = await _make_backend(db_session, url="http://gpu-unproven-health/")
    _install_gpu_resources(monkeypatch, "node-health/GPU-unproven")
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id="node-health/GPU-unproven",
        vram_budget_mb=1024,
    )
    challenges: list[str] = []

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        assert gpu_health_challenge is not None
        challenges.append(gpu_health_challenge)
        meta = {"residency": {"state": "unloaded"}}
        if len(challenges) <= 2:
            meta[GPU_HEALTH_CHALLENGE_ECHO_MARKER] = gpu_health_challenge
        return True, meta

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    service = MLBackendService(db_session)
    assert await service.check_health(backend.id) is True
    fresh = await service.get_registry(backend.id)
    assert fresh is not None
    first_challenge = fresh.health_meta["gpu_arbiter_probe"]["challenge"]

    assert await service.check_health(backend.id) is True
    fresh = await service.get_registry(backend.id)
    assert fresh is not None
    second_challenge = fresh.health_meta["gpu_arbiter_probe"]["challenge"]
    assert second_challenge != first_challenge

    assert await service.check_health(backend.id) is True
    fresh = await service.get_registry(backend.id)
    assert fresh is not None
    assert fresh.state == "connected"
    assert "gpu_arbiter_probe" not in fresh.health_meta
    assert len(challenges) == 3
    assert len(set(challenges)) == 3


async def test_gpu_check_health_rejects_membership_state_change_at_same_epoch(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    backend = await _make_backend(db_session, url="http://gpu-state-race-health/")
    resource_id = "node-health/GPU-state-race"
    _install_gpu_resources(monkeypatch, resource_id)
    await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id=resource_id,
        vram_budget_mb=1024,
    )

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        gpu_data = await bind_gpu_authority_data(db_session)
        await gpu_data.activate_membership_fence(
            backend.id,
            resource_id=resource_id,
            membership_epoch=1,
        )
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {"state": "unloaded"},
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    assert await MLBackendService(db_session).check_health(backend.id) is False
    fresh = await MLBackendService(db_session).get_registry(backend.id)
    assert fresh is not None
    assert fresh.state == "disconnected"
    assert fresh.health_meta is None
    membership = await InferenceHealthData(db_session).read_membership(
        backend.id, resource_id
    )
    assert membership is not None
    assert (membership.membership_epoch, membership.state) == (1, "active")


async def test_gpu_check_health_holds_membership_lock_through_probe_commit(
    test_engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = "node-health/GPU-membership-lock"
    _install_gpu_resources(monkeypatch, resource_id)
    async with factory.begin() as db:
        backend = await MLBackendService(db).create_registry(
            "health-membership-lock",
            f"http://health-membership-lock-{uuid.uuid4()}.test",
            gpu_resource_id=resource_id,
            vram_budget_mb=1024,
        )
        backend_id = backend.id
    final_membership_locked = asyncio.Event()
    allow_health_commit = asyncio.Event()

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {"state": "unloaded"},
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    async def run_health() -> bool:
        async with factory() as db:
            original_execute = db.execute

            async def execute_with_commit_pause(statement, *args, **kwargs):
                result = await original_execute(statement, *args, **kwargs)
                source = str(statement)
                if (
                    "aap_inference_health_data_v1.membership_row(" in source
                    and ",true)" in source
                ):
                    final_membership_locked.set()
                    await allow_health_commit.wait()
                return result

            monkeypatch.setattr(db, "execute", execute_with_commit_pause)
            async with db.begin():
                return await MLBackendService(db).check_health(backend_id)

    async def activate_membership() -> str:
        async with factory.begin() as db:
            gpu_data = await bind_gpu_authority_data(db)
            return await gpu_data.activate_membership_fence(
                backend_id,
                resource_id=resource_id,
                membership_epoch=1,
            )

    health_task = asyncio.create_task(run_health())
    activation_task: asyncio.Task[str] | None = None
    try:
        await asyncio.wait_for(final_membership_locked.wait(), timeout=2)
        activation_task = asyncio.create_task(activate_membership())
        await asyncio.sleep(0.05)
        assert not activation_task.done()

        allow_health_commit.set()
        assert await asyncio.wait_for(health_task, timeout=2) is True
        assert await asyncio.wait_for(activation_task, timeout=2) == "1"

        async with factory() as db:
            backend = await MLBackendService(db).get_registry(backend_id)

            membership = await InferenceHealthData(db).read_membership(
                backend_id, resource_id
            )
            assert backend is not None
            assert backend.health_meta is not None
            assert (
                backend.health_meta["gpu_arbiter_probe"]["membership_state"]
                == "pending"
            )
            assert membership is not None
            assert (membership.membership_epoch, membership.state) == (1, "active")
    finally:
        allow_health_commit.set()
        for task in (health_task, activation_task):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        await _cleanup_gpu_test_registry(factory, backend_id)


async def test_gpu_health_proof_write_shares_promotion_barrier(
    test_engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = "node-health/GPU-promotion-barrier"
    _install_gpu_resources(monkeypatch, resource_id)
    async with factory.begin() as db:
        backend = await MLBackendService(db).create_registry(
            "health-promotion-barrier",
            f"http://health-promotion-barrier-{uuid.uuid4()}.test",
            gpu_resource_id=resource_id,
            vram_budget_mb=1024,
        )
        backend_id = backend.id

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": {"state": "unloaded", "boot_id": "barrier-boot"},
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    holder = factory()
    transaction = await holder.begin()
    await holder.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('aap:gpu-membership-promotion', 0))"
        )
    )

    async def run_health() -> bool:
        async with factory() as db:
            async with db.begin():
                return await MLBackendService(db).check_health(backend_id)

    health_task = asyncio.create_task(run_health())
    try:
        assert await asyncio.wait_for(health_task, timeout=2) is False
    finally:
        await transaction.rollback()
        await holder.close()

    health_task = asyncio.create_task(run_health())
    blocked_health_task: asyncio.Task[bool] | None = None
    resource_holder: AsyncSession | None = None
    resource_transaction = None
    try:
        assert await asyncio.wait_for(health_task, timeout=2) is True

        resource_holder = factory()
        resource_transaction = await resource_holder.begin()
        await resource_holder.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
            ),
            {"resource_id": resource_id},
        )
        blocked_health_task = asyncio.create_task(run_health())
        await asyncio.sleep(0.05)
        assert not blocked_health_task.done()
        async with factory.begin() as probe:
            global_barrier_free = await probe.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended('aap:gpu-membership-promotion', 0))"
                )
            )
            assert global_barrier_free is True

        await resource_transaction.rollback()
        await resource_holder.close()
        resource_transaction = None
        resource_holder = None
        assert await asyncio.wait_for(blocked_health_task, timeout=2) is True
    finally:
        if resource_transaction is not None:
            await resource_transaction.rollback()
        if resource_holder is not None:
            await resource_holder.close()
        for task in (health_task, blocked_health_task):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        await _cleanup_gpu_test_registry(factory, backend_id)


async def test_check_health_rechecks_epoch_after_waiting_on_registry_lock(
    test_engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = "node-health/GPU-independent-race"
    _install_gpu_resources(monkeypatch, resource_id)
    async with factory.begin() as db:
        backend = await MLBackendService(db).create_registry(
            "health-epoch-independent-race",
            f"http://health-epoch-race-{uuid.uuid4()}.test",
            gpu_resource_id=resource_id,
            vram_budget_mb=1024,
        )
        backend_id = backend.id

    health_started = asyncio.Event()
    allow_health_response = asyncio.Event()

    async def fake_health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]:  # noqa: ARG001
        health_started.set()
        await allow_health_response.wait()
        return True, {
            "residency": {
                "state": "resident",
                "gpu_loaded": True,
                "identity": {"gpu_resource_id": resource_id},
            }
        }

    async def fake_setup(self):  # noqa: ARG001
        raise RuntimeError("setup unavailable")

    _install_health_observation(monkeypatch, fake_health_meta, fake_setup)

    async def run_health() -> bool:
        async with factory.begin() as db:
            return await MLBackendService(db).check_health(backend_id)

    health_task = asyncio.create_task(run_health())
    config_db = factory()
    try:
        await health_started.wait()
        await config_db.begin()
        await MLBackendService(config_db).update(backend_id, vram_budget_mb=2048)
        allow_health_response.set()
        await asyncio.sleep(0.05)
        assert not health_task.done()
        await config_db.commit()

        assert await asyncio.wait_for(health_task, timeout=2) is False
        async with factory() as db:
            backend = await MLBackendService(db).get_registry(backend_id)
            assert backend is not None
            assert backend.vram_budget_mb == 2048
            assert backend.health_meta is None
            assert backend.last_checked_at is None
    finally:
        if config_db.in_transaction():
            await config_db.rollback()
        await config_db.close()
        if not health_task.done():
            health_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await health_task
        await _cleanup_gpu_test_registry(factory, backend_id)


async def test_check_health_returns_false_for_missing_backend(
    db_session: AsyncSession, monkeypatch
):
    _install_backend_control_endpoint(monkeypatch)
    svc = MLBackendService(db_session)
    assert await svc.check_health(uuid.uuid4()) is False


def test_worker_module_imports_and_registers_task():
    """worker 模块能 import；celery_app.tasks 注册了周期任务名。"""
    from app.workers import ml_health
    from app.workers.celery_app import celery_app

    assert hasattr(ml_health, "check_ml_backends_health")
    assert hasattr(ml_health, "repair_gpu_arbiter_resources")
    assert "app.workers.ml_health.check_ml_backends_health" in celery_app.tasks
    assert "app.workers.ml_health.repair_gpu_arbiter_resources" in celery_app.tasks
    assert "check-ml-backends-health" in celery_app.conf.beat_schedule
    assert "repair-gpu-arbiter-resources" in celery_app.conf.beat_schedule
    assert celery_app.conf.task_routes[
        "app.workers.ml_health.repair_gpu_arbiter_resources"
    ] == {"queue": "gpu.control"}
    assert (
        celery_app.conf.beat_schedule["check-ml-backends-health"]["options"]["expires"]
        == 55
    )
    assert (
        celery_app.conf.beat_schedule["repair-gpu-arbiter-resources"]["options"][
            "expires"
        ]
        == 55
    )


def test_health_and_gpu_repair_use_independent_task_locks(monkeypatch) -> None:
    from app.workers import ml_health

    calls: list[tuple[str, str]] = []

    class FakeRedis:
        def set(self, key, owner, *, nx, ex):  # noqa: ARG002
            calls.append(("set", key))
            return True

        def eval(self, script, key_count, key, owner):  # noqa: ARG002
            calls.append(("release", key))
            return 1

        def close(self):
            return None

    monkeypatch.setattr(
        ml_health.redis_sync,
        "from_url",
        lambda *args, **kwargs: FakeRedis(),
    )

    async def operation() -> dict:
        return {"ok": True}

    assert ml_health._run_with_task_lock(
        operation,
        lock_key=ml_health._HEALTH_TASK_LOCK_KEY,
        lock_seconds=10,
    ) == {"ok": True}
    assert ml_health._run_with_task_lock(
        operation,
        lock_key=ml_health._GPU_REPAIR_TASK_LOCK_KEY,
        lock_seconds=10,
    ) == {"ok": True}
    assert ml_health._HEALTH_TASK_LOCK_KEY != ml_health._GPU_REPAIR_TASK_LOCK_KEY
    assert calls == [
        ("set", ml_health._HEALTH_TASK_LOCK_KEY),
        ("release", ml_health._HEALTH_TASK_LOCK_KEY),
        ("set", ml_health._GPU_REPAIR_TASK_LOCK_KEY),
        ("release", ml_health._GPU_REPAIR_TASK_LOCK_KEY),
    ]


class _WorkerReply:
    def __init__(self, **values):
        self.__dict__.update(values)
        self._values = values

    def model_dump(self, mode="python"):  # noqa: ARG002
        return dict(self._values)


class _GPUWorkerData:
    def __init__(self, events, durable_rollouts=(), memberships=()):
        from types import SimpleNamespace

        self.events = events
        self.rollouts = {item.resource_id: item for item in durable_rollouts}
        self.memberships = tuple(memberships)
        self.SimpleNamespace = SimpleNamespace

    async def read_rollouts(self):
        self.events.append(("read_rollouts",))
        return tuple(self.rollouts.values())

    async def membership_candidates(self, resource_id, *, states):
        assert states == ("retiring",)
        self.events.append(("membership_candidates", resource_id))
        return self.memberships

    async def begin_rollout(self, resource_id, target_mode):
        current = self.rollouts.get(resource_id)
        transition_id = uuid.uuid4()
        if target_mode == "enforce":
            state = (
                "enforcing" if current and current.state == "enforcing" else "promoting"
            )
            effective_mode = "enforce" if state == "enforcing" else "off"
            active_transition_id = None if state == "enforcing" else transition_id
        elif current is not None and current.effective_mode == "enforce":
            state = "demoting"
            effective_mode = "enforce"
            active_transition_id = transition_id
        else:
            state = "off"
            effective_mode = "off"
            active_transition_id = None
        rollout = self.SimpleNamespace(
            resource_id=resource_id,
            state=state,
            effective_mode=effective_mode,
            target_mode=target_mode,
            transition_id=active_transition_id,
            last_transition_id=None,
            blocker_reason=None,
            revision=(current.revision + 1 if current is not None else 1),
        )
        self.rollouts[resource_id] = rollout
        self.events.append(("begin_rollout", resource_id, target_mode))
        return rollout

    async def complete_rollout(self, resource_id, transition_id):
        current = self.rollouts[resource_id]
        assert current.transition_id == transition_id
        effective_mode = "enforce" if current.target_mode == "enforce" else "off"
        rollout = self.SimpleNamespace(
            resource_id=resource_id,
            state="enforcing" if effective_mode == "enforce" else "off",
            effective_mode=effective_mode,
            target_mode=current.target_mode,
            transition_id=None,
            last_transition_id=transition_id,
            blocker_reason=None,
            revision=current.revision + 1,
        )
        self.rollouts[resource_id] = rollout
        self.events.append(("complete_rollout", resource_id, transition_id))
        return rollout

    async def block_rollout(self, resource_id, transition_id, reason):
        current = self.rollouts[resource_id]
        assert current.transition_id == transition_id
        rollout = self.SimpleNamespace(
            resource_id=resource_id,
            state="blocked",
            effective_mode=current.effective_mode,
            target_mode=current.target_mode,
            transition_id=transition_id,
            last_transition_id=None,
            blocker_reason=reason,
            revision=current.revision + 1,
        )
        self.rollouts[resource_id] = rollout
        self.events.append(("block_rollout", resource_id, reason))
        return rollout


class _GPUWorkerClient:
    def __init__(self, events):
        self.events = events
        self.promotions = ()
        self.controls = ()
        self.collections = []
        self.activation_error = None
        self.activation_handler = None
        self.repair_handler = None
        self.observe_handler = None
        self.observation = _WorkerReply(
            protocol="aap-inference-gpu-admin/v1",
            status="ready",
            reason="",
            ready=True,
            ledger_revision=1,
            ledger_incarnation="incarnation",
            reconcile_deadline_ms=None,
            committed_mb=0,
            backend_count=0,
            active_backend_count=0,
            membership_state_counts={},
            allocation_state_counts={},
            lease_count=0,
            card_queue_count=0,
            backend_queue_count=0,
            transition_present=False,
            durable_domain_matches=True,
        )

    async def activate_resource_memberships(self, resource_id, *, pending_only=False):
        self.events.append(("activate", resource_id, pending_only))
        if self.activation_handler is not None:
            return await self.activation_handler(resource_id, pending_only=pending_only)
        if self.activation_error is not None:
            raise self.activation_error
        return type("MembershipReply", (), {"results": self.promotions})()

    async def advance_resource_rollout_control(
        self, resource_id, transition_id, target_gate
    ):
        self.events.append(("advance_control", resource_id, target_gate))
        return type("ControlReply", (), {"results": self.controls})()

    async def repair_resource(
        self,
        resource_id,
        allocatable_mb,
        *,
        force_proof_reset=False,
        readiness_blocker=None,
    ):
        self.events.append(
            (
                "repair",
                resource_id,
                allocatable_mb,
                force_proof_reset,
                readiness_blocker,
            )
        )
        if self.repair_handler is not None:
            return await self.repair_handler(
                resource_id,
                allocatable_mb,
                force_proof_reset=force_proof_reset,
                readiness_blocker=readiness_blocker,
            )
        ready = readiness_blocker is None
        return _WorkerReply(
            action="already_ready" if ready else "repair",
            status="ready" if ready else "not_ready",
            ready=ready,
            reason="" if ready else readiness_blocker,
            ledger_revision=1,
            ledger_incarnation="incarnation",
            committed_mb=0,
        )

    async def observe_resource(self, resource_id, *, allocatable_mb=None):
        self.events.append(("observe", resource_id, allocatable_mb))
        if self.observe_handler is not None:
            return await self.observe_handler(
                resource_id, allocatable_mb=allocatable_mb
            )
        return self.observation

    async def probe_retired_membership(self, backend_id, resource_id, membership_epoch):
        self.events.append(("probe_retired", backend_id, membership_epoch))
        return _WorkerReply(
            backend_id=backend_id,
            resource_id=resource_id,
            membership_epoch=membership_epoch,
            reason="retirement_proof_available",
            proof_available=True,
        )

    async def collect_tombstone(self, backend_id, resource_id, membership_epoch):
        self.events.append(("collect", backend_id, membership_epoch))
        if self.collections:
            return self.collections.pop(0)
        return _WorkerReply(
            backend_id=backend_id,
            resource_id=resource_id,
            membership_epoch=membership_epoch,
            status="collected",
            reason="",
            idempotent=False,
        )


def _install_gpu_worker(
    monkeypatch,
    *,
    resource_configs,
    durable_rollouts=(),
    memberships=(),
):
    from contextlib import asynccontextmanager

    from app.config import GPUArbiterMode, settings
    from app.workers import ml_health

    events = []
    data = _GPUWorkerData(events, durable_rollouts, memberships)
    client = _GPUWorkerClient(events)
    monkeypatch.setattr(settings, "gpu_arbiter_rollout_enabled", True)
    monkeypatch.setattr(settings, "gpu_arbiter_mode", GPUArbiterMode.ENFORCE)
    monkeypatch.setattr(
        settings, "gpu_arbiter_resources_json", json.dumps(resource_configs)
    )

    @asynccontextmanager
    async def session_factory():
        yield object()

    @asynccontextmanager
    async def open_data(_factory):
        try:
            yield data
        finally:
            events.append("uow_exit")

    monkeypatch.setattr(ml_health, "task_session_factory", session_factory)
    monkeypatch.setattr(ml_health, "open_gpu_authority_data", open_data)
    monkeypatch.setattr(ml_health, "gpu_authority_client", lambda: client)
    return ml_health, events, data, client


def _gpu_resource_config(resource_id: str, *, mode: str = "enforce") -> dict:
    device = resource_id.rsplit("/", 1)[-1]
    return {
        resource_id: {
            "node_id": "node-worker",
            "physical_device_token": device,
            "allocatable_mb": 8192,
            "mode": mode,
        }
    }


def _gpu_rollout(
    resource_id: str,
    *,
    state: str,
    effective_mode: str,
    target_mode: str,
    transition_id=None,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        resource_id=resource_id,
        state=state,
        effective_mode=effective_mode,
        target_mode=target_mode,
        transition_id=transition_id,
        last_transition_id=None,
        blocker_reason=None,
        revision=3,
    )


async def test_gpu_worker_skips_invalid_configuration_before_opening_authority(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.workers import ml_health

    monkeypatch.setattr(settings, "gpu_arbiter_resources_json", "{invalid")

    def unexpected():
        raise AssertionError("invalid configuration must stop before authority access")

    monkeypatch.setattr(ml_health, "gpu_authority_client", unexpected)
    monkeypatch.setattr(ml_health, "task_session_factory", unexpected)

    result = await ml_health._repair_gpu_arbiter_resources()

    assert result == {
        "skipped": True,
        "reason": "gpu_resources_config_invalid",
        "resources": [],
    }


async def test_gpu_worker_skips_disabled_rollout_before_opening_authority(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.workers import ml_health

    monkeypatch.setattr(settings, "gpu_arbiter_resources_json", "{}")
    monkeypatch.setattr(settings, "gpu_arbiter_rollout_enabled", False)

    def unexpected():
        raise AssertionError("disabled rollout must stop before authority access")

    monkeypatch.setattr(ml_health, "gpu_authority_client", unexpected)
    monkeypatch.setattr(ml_health, "task_session_factory", unexpected)

    result = await ml_health._repair_gpu_arbiter_resources()

    assert result == {
        "skipped": True,
        "reason": "gpu_rollout_disabled",
        "resources": [],
    }


async def test_gpu_worker_promotes_through_owner_and_finishes_retired_collection(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    resource_id = "node-worker/GPU-owner-promotion"
    retired_id = uuid.uuid4()
    membership = SimpleNamespace(backend_id=retired_id, membership_epoch=7)
    ml_health, events, data, client = _install_gpu_worker(
        monkeypatch,
        resource_configs=_gpu_resource_config(resource_id),
        memberships=(membership,),
    )
    client.collections = [
        _WorkerReply(
            backend_id=retired_id,
            resource_id=resource_id,
            membership_epoch=7,
            status="blocked",
            reason="proof_not_yet_available",
            idempotent=False,
        ),
        _WorkerReply(
            backend_id=retired_id,
            resource_id=resource_id,
            membership_epoch=7,
            status="collected",
            reason="",
            idempotent=True,
        ),
    ]

    result = await ml_health._repair_gpu_arbiter_resources()

    assert result["skipped"] is False
    assert result["resources"][0]["rollout"]["state"] == "enforcing"
    assert "protocol" not in result["resources"][0]["observation"]
    assert result["resources"][0]["collections"] == [
        {
            "backend_id": str(retired_id),
            "resource_id": resource_id,
            "membership_epoch": 7,
            "status": "collected",
            "reason": "",
            "redis_idempotent": True,
        }
    ]
    operation_names = [call[0] for call in events if isinstance(call, tuple)]
    assert operation_names == [
        "read_rollouts",
        "begin_rollout",
        "membership_candidates",
        "collect",
        "activate",
        "advance_control",
        "repair",
        "membership_candidates",
        "probe_retired",
        "collect",
        "repair",
        "observe",
        "complete_rollout",
    ]
    begin_index = events.index(("begin_rollout", resource_id, "enforce"))
    activate_index = events.index(("activate", resource_id, True))
    assert events.index("uow_exit", begin_index) < activate_index
    repairs = [
        call for call in events if isinstance(call, tuple) and call[0] == "repair"
    ]
    assert [call[2] for call in repairs] == [8192, 8192]
    assert [call[4] for call in repairs] == [None, None]
    assert data.rollouts[resource_id].effective_mode == "enforce"


async def test_gpu_worker_demotes_missing_resource_only_after_not_ready_and_quiescence(
    monkeypatch,
) -> None:
    resource_id = "node-worker/GPU-removed-config"
    previous = _gpu_rollout(
        resource_id,
        state="enforcing",
        effective_mode="enforce",
        target_mode="enforce",
    )
    ml_health, events, data, _client = _install_gpu_worker(
        monkeypatch,
        resource_configs={},
        durable_rollouts=(previous,),
    )

    result = await ml_health._repair_gpu_arbiter_resources()

    resource = result["resources"][0]
    assert resource["status"] == "legacy_acknowledged"
    assert resource["ready"] is False
    assert resource["rollout"]["state"] == "off"
    assert resource["observation"] is None
    repair_index = next(
        i
        for i, call in enumerate(events)
        if isinstance(call, tuple) and call[0] == "repair"
    )
    observe_index = next(
        i
        for i, call in enumerate(events)
        if isinstance(call, tuple) and call[0] == "observe"
    )
    control_index = next(
        i
        for i, call in enumerate(events)
        if isinstance(call, tuple) and call[0] == "advance_control"
    )
    assert repair_index < observe_index < control_index
    assert events[repair_index][2] is None
    assert events[repair_index][4] == "rollout_demotion_in_progress"
    assert events[observe_index][2] is None
    assert events[control_index][2] == "legacy"
    assert data.rollouts[resource_id].state == "off"


async def test_gpu_worker_fail_closes_after_owner_promotion_failure(
    monkeypatch,
) -> None:
    resource_id = "node-worker/GPU-promotion-failure"
    ml_health, events, data, client = _install_gpu_worker(
        monkeypatch,
        resource_configs=_gpu_resource_config(resource_id),
    )
    client.activation_error = RuntimeError("owner unavailable")

    result = await ml_health._repair_gpu_arbiter_resources()

    resource = result["resources"][0]
    assert resource["ready"] is False
    assert resource["rollout"]["state"] == "blocked"
    repair = next(
        call for call in events if isinstance(call, tuple) and call[0] == "repair"
    )
    assert repair[3:] == (True, "membership_promotion_unconfirmed")
    assert not any(
        isinstance(call, tuple) and call[0] == "advance_control" for call in events
    )
    assert (
        data.rollouts[resource_id].blocker_reason == "membership_promotion_unconfirmed"
    )


async def test_gpu_worker_batch_timeout_latches_and_blocks_transition(
    monkeypatch,
) -> None:
    resource_id = "node-worker/GPU-batch-timeout"
    ml_health, events, _data, client = _install_gpu_worker(
        monkeypatch,
        resource_configs=_gpu_resource_config(resource_id),
    )
    monkeypatch.setattr(ml_health, "_GPU_REPAIR_BATCH_TIMEOUT_SECONDS", 0.01)

    async def slow_activation(_resource_id, *, pending_only=False):  # noqa: ARG001
        await asyncio.sleep(10)
        return type("MembershipReply", (), {"results": ()})()

    client.activation_handler = slow_activation

    result = await ml_health._repair_gpu_arbiter_resources()

    assert result["resources"][0]["reason"] == "gpu_arbiter_repair_batch_timeout"
    assert result["resources"][0]["rollout"]["state"] == "blocked"
    assert any(
        isinstance(call, tuple)
        and call[0] == "repair"
        and call[4] == "gpu_arbiter_repair_batch_timeout"
        for call in events
    )


async def test_gpu_worker_preserves_four_resource_concurrency(monkeypatch) -> None:
    resource_ids = [f"node-worker/GPU-{index}" for index in range(5)]
    resources = {}
    for resource_id in resource_ids:
        resources.update(_gpu_resource_config(resource_id))
    ml_health, _events, _data, client = _install_gpu_worker(
        monkeypatch,
        resource_configs=resources,
    )
    active = 0
    maximum = 0
    first_wave = asyncio.Event()

    async def hold_repair(
        resource_id,
        allocatable_mb,
        *,
        force_proof_reset=False,
        readiness_blocker=None,
    ):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            first_wave.set()
        await first_wave.wait()
        await asyncio.sleep(0)
        active -= 1
        return _WorkerReply(
            action="already_ready",
            status="ready",
            ready=True,
            reason="",
            ledger_revision=1,
            ledger_incarnation="incarnation",
            committed_mb=0,
        )

    client.repair_handler = hold_repair

    result = await ml_health._repair_gpu_arbiter_resources()

    assert maximum == 4
    assert [row["resource_id"] for row in result["resources"]] == sorted(resource_ids)


async def test_stats_publisher_skips_owner_when_no_subscribers(monkeypatch) -> None:
    from app.workers import ml_health

    calls = {"owner": 0, "closed": 0}

    class FakeRedis:
        def get(self, key):
            assert key == "ml-backend-stats:subscribers"
            return "0"

        def close(self):
            calls["closed"] += 1

    monkeypatch.setattr(
        ml_health.redis_sync, "from_url", lambda *_args, **_kwargs: FakeRedis()
    )

    def unexpected(_db):
        calls["owner"] += 1
        raise AssertionError("no subscriber means no stats-owner call")

    monkeypatch.setattr(ml_health, "backend_control_service", unexpected)
    result = await ml_health._publish_stats_async()

    assert result == {"skipped": True, "subscribers": 0}
    assert calls == {"owner": 0, "closed": 1}


async def test_stats_publisher_flattens_owner_metadata_to_legacy_payload(
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager

    from app.workers import ml_health

    backend_id = str(uuid.uuid4())
    bindings = [{"backend_id": str(uuid.uuid4()), "backend_name": "sam2"}]
    timestamp = "2026-10-26T00:00:00+00:00"
    meta = {
        "gpu_info": {"memory_used_mb": 448},
        "host": {"load": 0.2},
        "cache": {"hits": 4},
        "model_version": "sam2.1",
        "loaded": False,
        "idle_unload_seconds": 600,
        "last_request_age_seconds": 3.2,
        "pool": {"loaded_variants": []},
        "video_pool": {"active_sessions": 0},
        "compute": {"effective_device": "cpu"},
        "residency": {"gpu_loaded": True, "builders": 1},
        "unapproved_owner_field": "must not be published",
    }
    snapshot = _WorkerReply(
        physical_key="http://ml.local:9000|auth:1234abcd",
        url_host="ml.local:9000",
        backend_id=backend_id,
        backend_name="sam2",
        bindings=bindings,
        state="ok",
        timestamp=timestamp,
        meta=meta,
    )
    expected_snapshot = {
        "physical_key": "http://ml.local:9000|auth:1234abcd",
        "url_host": "ml.local:9000",
        "backend_id": backend_id,
        "backend_name": "sam2",
        "bindings": bindings,
        "state": "ok",
        "timestamp": timestamp,
        **{
            key: value for key, value in meta.items() if key != "unapproved_owner_field"
        },
    }
    redis_calls = []
    owner_calls = []

    class FakeRedis:
        def get(self, key):
            assert key == "ml-backend-stats:subscribers"
            return "3"

        def publish(self, channel, payload):
            redis_calls.append((channel, json.loads(payload)))

        def close(self):
            pass

    @asynccontextmanager
    async def fake_session():
        yield object()

    class FakeControl:
        async def stats_snapshots(self):
            owner_calls.append("stats")
            return (snapshot,)

    monkeypatch.setattr(
        ml_health.redis_sync, "from_url", lambda *_args, **_kwargs: FakeRedis()
    )
    monkeypatch.setattr(ml_health, "task_session", fake_session)
    monkeypatch.setattr(ml_health, "backend_control_service", lambda _db: FakeControl())

    result = await ml_health._publish_stats_async()

    assert result == {"published": 1, "subscribers": 3}
    assert owner_calls == ["stats"]
    assert redis_calls[0][0] == "ml-backend-stats:global"
    published = redis_calls[0][1]["backends"][0]
    assert redis_calls[0][1]["backends"] == [expected_snapshot]
    assert "meta" not in published
    assert "unapproved_owner_field" not in published
    assert published["loaded"] is False
    assert published["residency"]["gpu_loaded"] is True
    assert redis_calls[0][1]["timestamp"]


async def test_health_sweep_uses_public_topology_ids_and_isolates_failures(
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.workers import ml_health

    backend_ids = [uuid.uuid4() for _ in range(3)]
    states = {}
    calls = []
    commit_calls = []

    class FakeTopology:
        def __init__(self, _db):
            pass

        async def registry_catalog(self):
            return [SimpleNamespace(id=backend_id) for backend_id in backend_ids]

        async def registry_facts(self, backend_id):
            return SimpleNamespace(state=states.get(backend_id, "unknown"))

    class FakeControl:
        async def check_health(self, backend_id):
            calls.append(backend_id)
            if backend_id == backend_ids[2]:
                raise RuntimeError("single backend failure")
            states[backend_id] = (
                "connected" if backend_id == backend_ids[0] else "error"
            )
            return backend_id == backend_ids[0]

    class FakeSession:
        async def commit(self):
            commit_calls.append("commit")

    @asynccontextmanager
    async def fake_session():
        yield FakeSession()

    monkeypatch.setattr(ml_health, "task_session", fake_session)
    monkeypatch.setattr(ml_health, "InferenceTopologyData", FakeTopology)
    monkeypatch.setattr(ml_health, "backend_control_service", lambda _db: FakeControl())

    result = await ml_health.check_all_backends(jitter_max_seconds=0)

    assert result == {
        "checked": 3,
        "results": [
            {"id": str(backend_ids[0]), "state": "connected", "healthy": True},
            {"id": str(backend_ids[1]), "state": "error", "healthy": False},
            {"id": str(backend_ids[2]), "state": "error", "healthy": False},
        ],
    }
    assert calls == backend_ids
    assert commit_calls == ["commit", "commit"]
