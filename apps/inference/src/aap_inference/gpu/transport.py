"""Explicit endpoint-opening and routed backend transport ports for GPU owner code."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol
from uuid import UUID

from aap_protocol_v2.lifecycle import (
    DrainTransitionResponse,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetRequest,
    LifecycleResetResponse,
    ManagedUnloadResponse,
)
from aap_inference.backend_endpoint import (
    BackendEndpointBinding,
    BackendEndpointSnapshot,
)
from aap_inference.gpu.contracts import GPUDispatchGrantPort


GPUControlOperation = Literal[
    "mode",
    "reset",
    "drain",
    "cancel_drain",
    "unload",
    "terminal_drain",
]


class GPUBackendControlTransport(Protocol):
    """Parent-bound routed HTTP operations with owner-local route issuers."""

    def control_binding(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        operation: GPUControlOperation,
    ) -> AbstractAsyncContextManager[BackendEndpointBinding]: ...

    async def health_meta(
        self,
        endpoint: BackendEndpointSnapshot,
        *,
        request_id: UUID,
        gpu_health_challenge: str,
        timeout_seconds: float,
    ) -> tuple[bool, Mapping[str, object]]: ...

    async def lifecycle_mode(
        self,
        endpoint: BackendEndpointSnapshot,
        request: LifecycleModeRequest,
        admission_token: str,
        *,
        request_id: UUID,
        timeout_seconds: float,
    ) -> LifecycleModeResponse: ...

    async def lifecycle_reset(
        self,
        endpoint: BackendEndpointSnapshot,
        request: LifecycleResetRequest,
        admission_token: str,
        *,
        request_id: UUID,
        timeout_seconds: float,
    ) -> LifecycleResetResponse: ...

    async def lifecycle_drain(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        request_id: UUID,
        timeout_seconds: float,
    ) -> DrainTransitionResponse: ...

    async def lifecycle_cancel_drain(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        request_id: UUID,
        timeout_seconds: float,
    ) -> DrainTransitionResponse: ...

    async def lifecycle_unload(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        request_id: UUID,
        timeout_seconds: float,
    ) -> ManagedUnloadResponse: ...

    async def lifecycle_terminal_drain(
        self,
        endpoint: BackendEndpointSnapshot,
        grant: GPUDispatchGrantPort,
        *,
        registry_id: UUID,
        request_id: UUID,
        timeout_seconds: float,
    ) -> dict[str, object]: ...


__all__ = ["GPUBackendControlTransport", "GPUControlOperation"]
