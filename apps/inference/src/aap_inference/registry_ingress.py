"""Registry environment and screenshot-seed ingress over caller-owned data."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from aap_inference_contract.capabilities import CapabilityLimits

from .capabilities import extract_capabilities
from .pool_capability import canonicalize_capability, capability_fingerprint


class RegistryRow(Protocol):
    id: UUID
    name: str
    url: str
    source: str
    auth_method: str | None
    auth_token: str | None
    gpu_resource_id: str | None
    state: str
    is_interactive: bool
    health_meta: Any
    last_checked_at: datetime | None
    error_message: str | None
    extra_params: Any


class PoolRow(Protocol):
    id: UUID
    name: str
    enabled: bool
    routing_policy: str
    legacy_instance_id: UUID | None
    routing_generation: int
    capability_fingerprint: str | None
    capability_snapshot: dict[str, Any] | None


class RegistryProbeClient(Protocol):
    async def health_meta(self) -> tuple[bool, Any]: ...

    async def setup(self) -> Any: ...


class RegistryIngressData(Protocol):
    async def by_url(self, url: str) -> RegistryRow | None: ...

    async def candidates(
        self, profile: Literal["env", "live"]
    ) -> list[RegistryRow]: ...

    async def create_registry(
        self,
        name: Any,
        url: str,
        source: Literal["env", "seed"],
        *,
        is_interactive: bool = False,
        extra_params: Any = None,
    ) -> RegistryRow: ...

    async def singleton_pool(self, registry_id: UUID) -> PoolRow | None: ...

    async def create_singleton_pool(
        self,
        row: RegistryRow,
        snapshot: dict[str, Any] | None,
        fingerprint: str | None,
    ) -> PoolRow: ...

    async def stage_observation(
        self,
        row: RegistryRow,
        *,
        profile: Literal["env", "seed"],
        usable_caps: bool,
        expected_snapshot: dict[str, Any],
    ) -> None: ...

    async def stage_env_disconnect(self, row: RegistryRow) -> None: ...

    async def stage_seed_marker(
        self, row: RegistryRow, marker: dict[str, Any], previous_extra_params: Any
    ) -> None: ...

    async def flush(self) -> None: ...

    async def sync_result(
        self, request_id: UUID, command_sha256: str
    ) -> dict[str, Any] | None: ...

    async def record_sync_result(
        self, request_id: UUID, command_sha256: str, observed_urls: int
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BackendProbe:
    backend: RegistryRow
    healthy: bool
    setup: Any
    health_meta: Any
    error: str | None = None


def endpoint_snapshot(row: RegistryRow) -> dict[str, Any]:
    """Capture the five Registry values that fence a later observation write."""
    return {
        "url": row.url,
        "auth_method": row.auth_method,
        "auth_token": row.auth_token,
        "gpu_resource_id": row.gpu_resource_id,
        "source": row.source,
    }


async def ensure_singleton_pool(data: RegistryIngressData, row: RegistryRow) -> PoolRow:
    existing = await data.singleton_pool(row.id)
    if existing is not None:
        return existing
    raw_capabilities = (row.health_meta or {}).get("capabilities")
    snapshot = None
    fingerprint = None
    if isinstance(raw_capabilities, dict):
        snapshot = canonicalize_capability(raw_capabilities)
        fingerprint = capability_fingerprint(snapshot)
    return await data.create_singleton_pool(row, snapshot, fingerprint)


async def get_or_create_seed(
    data: RegistryIngressData,
    url: str,
    name: Any,
    marker: dict[str, Any],
) -> RegistryRow:
    normalized_url = url.rstrip("/")
    row = await data.by_url(normalized_url)
    if row is None:
        row = await data.create_registry(
            name,
            normalized_url,
            source="seed",
            extra_params={"seed": marker},
        )
    else:
        previous_extra_params = row.extra_params
        row.extra_params = {
            **(row.extra_params or {}),
            "seed": marker,
        }
        await data.stage_seed_marker(row, marker, previous_extra_params)
    await ensure_singleton_pool(data, row)
    return row


async def _probe_backend(
    backend: RegistryRow,
    client_factory: Callable[[RegistryRow], RegistryProbeClient],
) -> BackendProbe:
    # The legacy client constructor is outside the fail-open probe handler.
    client = client_factory(backend)
    try:
        healthy, health_meta = await client.health_meta()
        if not healthy:
            return BackendProbe(
                backend, False, None, health_meta, "health check failed"
            )
        setup = await client.setup()
        return BackendProbe(backend, True, setup, health_meta)
    except Exception as exc:
        return BackendProbe(
            backend,
            False,
            None,
            None,
            f"{type(exc).__name__}: {exc}",
        )


async def refresh_candidates(
    data: RegistryIngressData,
    backends: Sequence[RegistryRow],
    *,
    client_factory: Callable[[RegistryRow], RegistryProbeClient],
    capability_limits_factory: Callable[[], CapabilityLimits],
    clock: Callable[[], datetime] | None = None,
) -> list[RegistryRow]:
    expected_snapshots = {row.id: endpoint_snapshot(row) for row in backends}
    probes = await asyncio.gather(
        *(_probe_backend(backend, client_factory) for backend in backends)
    )
    now = (clock if clock is not None else lambda: datetime.now(UTC))()
    for probe in probes:
        backend = probe.backend
        backend.last_checked_at = now
        if not probe.healthy:
            backend.state = "error"
            backend.error_message = probe.error
            await data.stage_observation(
                backend,
                profile="seed",
                usable_caps=False,
                expected_snapshot=expected_snapshots[backend.id],
            )
            continue

        capabilities = extract_capabilities(
            probe.setup,
            limits=capability_limits_factory(),
        )
        if not capabilities:
            backend.state = "error"
            backend.error_message = "setup response has no usable capabilities"
            await data.stage_observation(
                backend,
                profile="seed",
                usable_caps=False,
                expected_snapshot=expected_snapshots[backend.id],
            )
            continue

        backend.name = str((probe.setup or {}).get("name") or backend.name)
        backend.state = "connected"
        backend.error_message = None
        backend.is_interactive = bool(capabilities["is_interactive"])
        backend.health_meta = {
            **(probe.health_meta or {}),
            "capabilities": capabilities,
        }
        await data.stage_observation(
            backend,
            profile="seed",
            usable_caps=True,
            expected_snapshot=expected_snapshots[backend.id],
        )
    return list(backends)


def _http_client(timeout_seconds: float) -> AbstractAsyncContextManager[Any]:
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(float(timeout_seconds)))


async def _probe_setup(client: Any, base: str) -> Any:
    import httpx

    try:
        response = await client.get(f"{base}/setup")
        if response.status_code == 200:
            return response.json()
    except (httpx.TimeoutException, httpx.RequestError):
        return None
    except Exception:
        return None
    return None


async def sync_environment(
    urls: Sequence[str],
    *,
    timeout_seconds: float,
    data_factory: Callable[[], AbstractAsyncContextManager[RegistryIngressData]],
    capability_limits_factory: Callable[[], CapabilityLimits],
    client_factory: Callable[[float], AbstractAsyncContextManager[Any]] | None = None,
    setup_probe: Callable[[Any, str], Awaitable[Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
    request_id: UUID | None = None,
    command_sha256: str | None = None,
) -> int | dict[str, Any]:
    if (request_id is None) != (command_sha256 is None):
        raise ValueError("request_id and command_sha256 must be supplied together")

    factory = client_factory if client_factory is not None else _http_client
    probe_setup = setup_probe if setup_probe is not None else _probe_setup
    async with factory(timeout_seconds) as client:
        setups = await asyncio.gather(
            *(probe_setup(client, url) for url in urls),
            return_exceptions=True,
        )
    setups = [None if isinstance(setup, BaseException) else setup for setup in setups]
    observed_at = (clock if clock is not None else lambda: datetime.now(UTC))()

    async with data_factory() as data:
        if request_id is not None and command_sha256 is not None:
            cached = await data.sync_result(request_id, command_sha256)
            if cached is not None:
                return cached

        for url, setup in zip(urls, setups, strict=True):
            capabilities = (
                extract_capabilities(
                    setup,
                    limits=capability_limits_factory(),
                )
                if setup
                else None
            )
            existing = await data.by_url(url)
            if existing is not None and existing.source != "env":
                continue

            health_meta = {"capabilities": capabilities} if capabilities else None
            state = "connected" if capabilities else "error"
            is_interactive = (
                bool(capabilities["is_interactive"]) if capabilities else False
            )
            name = (setup or {}).get("name") or url.rstrip("/").split("/")[-1]

            if existing is None:
                created = await data.create_registry(
                    name,
                    url,
                    source="env",
                    is_interactive=is_interactive,
                )
                await ensure_singleton_pool(data, created)
                row = await data.by_url(url)
                if row is None:
                    raise LookupError("created env Registry row was not found")
                expected_snapshot = endpoint_snapshot(row)
                row.state = state
                row.health_meta = health_meta
                row.last_checked_at = observed_at
            else:
                row = existing
                expected_snapshot = endpoint_snapshot(row)
                row.name = name
                row.state = state
                row.is_interactive = is_interactive
                row.health_meta = health_meta
                row.last_checked_at = observed_at
            await data.stage_observation(
                row,
                profile="env",
                usable_caps=bool(capabilities),
                expected_snapshot=expected_snapshot,
            )

        observed = set(urls)
        for row in await data.candidates("env"):
            if row.url not in observed:
                row.state = "disconnected"
                await data.stage_env_disconnect(row)

        await data.flush()
        if request_id is not None and command_sha256 is not None:
            return await data.record_sync_result(request_id, command_sha256, len(urls))
        return len(urls)


__all__ = [
    "BackendProbe",
    "PoolRow",
    "RegistryIngressData",
    "RegistryRow",
    "endpoint_snapshot",
    "ensure_singleton_pool",
    "get_or_create_seed",
    "refresh_candidates",
    "sync_environment",
]
