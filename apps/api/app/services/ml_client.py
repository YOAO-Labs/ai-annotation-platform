"""API assembly for the portable Inference transport and current GPU authority."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

from aap_inference.backend_transport import BackendTransport
from aap_protocol_v2.lifecycle import AdmissionScope
from fastapi import HTTPException
import structlog

from app.config import GPUArbiterMode, settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.observability.metrics import observe_ml_backend
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchContextFactory,
    GPUDispatchGrant,
    GPUDispatchOperation,
    GPUDispatchRequest,
    GPUShadowSessionFactory,
)
from app.services.gpu_arbitration.policy import (
    GPUClaimConfigurationError,
    any_gpu_resource_effectively_enforced,
    backend_is_trusted_explicit_cpu,
    effective_gpu_arbiter_mode,
    gpu_shadow_observation_enabled,
    record_gpu_shadow_dispatch,
    validate_gpu_claim,
)
from app.services.gpu_arbitration.rollout_state import (
    GPUArbiterRolloutUnavailable,
    gpu_rollout_boundary_active,
    resolve_gpu_arbiter_rollout,
)

logger = structlog.get_logger(__name__)
_GPU_SHADOW_OBSERVER_TIMEOUT_SECONDS = 0.25


class MLBackendClient(BackendTransport):
    def __init__(
        self,
        backend: MLBackendRegistry,
        *,
        shadow_session_factory: GPUShadowSessionFactory | None = None,
        dispatch_context_factory: GPUDispatchContextFactory | None = None,
        routing_authority=None,
    ) -> None:
        self._shadow_session_factory = shadow_session_factory
        self._dispatch_context_factory = dispatch_context_factory
        from app.services.ml_routing.authority import backend_request_context

        transport_identity = {
            "url": backend.url,
            "auth_method": backend.auth_method,
            "auth_token": backend.auth_token,
            "gpu_resource_id": backend.gpu_resource_id,
            "extra_params": deepcopy(backend.extra_params),
        }
        super().__init__(
            backend,
            options=settings,
            dispatch_context=self._gpu_dispatch,
            observe=observe_ml_backend,
            error_factory=HTTPException,
            request_context=backend_request_context(
                backend,
                transport_identity,
                authority=routing_authority,
                session_factory=self._shadow_session_factory,
            ),
        )

    async def _observe_gpu_dispatch(self, operation: str) -> None:
        """Record P2 shadow decisions and fail open without changing the request."""

        if (
            self.backend_id is None
            or self._shadow_session_factory is None
            or not gpu_shadow_observation_enabled(self.gpu_resource_id)
        ):
            return
        try:
            # API 路径可能仍持有请求事务连接；严格限制旁路查询等待，避免连接池
            # 饥饿把非权威 observe 变成业务派发阻塞点。
            async with asyncio.timeout(_GPU_SHADOW_OBSERVER_TIMEOUT_SECONDS):
                await record_gpu_shadow_dispatch(
                    self.backend_id,
                    operation,
                    self._shadow_session_factory,
                )
        except Exception:  # noqa: BLE001 - observe must never block business traffic
            logger.warning(
                "gpu_arbiter_shadow_observer_failed",
                gpu_arbiter={
                    "backend_id": self.backend_id,
                    "resource_id": self.gpu_resource_id,
                    "operation": operation,
                    "authoritative": False,
                },
                exc_info=True,
            )

    @asynccontextmanager
    async def _gpu_dispatch(
        self,
        operation: GPUDispatchOperation,
        scope: AdmissionScope,
    ):
        """Wrap one residency-changing HTTP call in the mode-specific boundary."""

        resources = settings.gpu_arbiter_resources
        resource_is_configured = self.gpu_resource_id in resources
        unclaimed_explicit_cpu = (
            self.gpu_resource_id is None
            and self.vram_budget_mb is None
            and backend_is_trusted_explicit_cpu(self._backend)
        )
        rollout_boundary_active = False
        if settings.gpu_arbiter_rollout_enabled:
            if unclaimed_explicit_cpu:
                effective_mode = GPUArbiterMode.OFF
            else:
                if self._shadow_session_factory is None:
                    raise GPUArbiterDispatchError(
                        GPUArbiterErrorCode.NOT_READY,
                        message="GPU rollout resolver is not configured",
                    )
                try:
                    if self.gpu_resource_id is not None:
                        rollout = await resolve_gpu_arbiter_rollout(
                            self._shadow_session_factory,
                            self.gpu_resource_id,
                        )
                        if rollout.dispatch_blocked:
                            raise GPUArbiterDispatchError(
                                GPUArbiterErrorCode.NOT_READY,
                                message=rollout.blocked_reason
                                or "GPU rollout transition is not ready",
                            )
                        assert rollout.dispatch_mode is not None
                        effective_mode = rollout.dispatch_mode
                        if not resource_is_configured:
                            rollout_boundary_active = await gpu_rollout_boundary_active(
                                self._shadow_session_factory
                            )
                    else:
                        effective_mode = GPUArbiterMode.OFF
                        rollout_boundary_active = await gpu_rollout_boundary_active(
                            self._shadow_session_factory
                        )
                except GPUArbiterRolloutUnavailable as exc:
                    raise GPUArbiterDispatchError(
                        GPUArbiterErrorCode.UNAVAILABLE,
                        message=str(exc),
                    ) from exc
        else:
            effective_mode = (
                effective_gpu_arbiter_mode(self.gpu_resource_id)
                if self.gpu_resource_id is not None
                else GPUArbiterMode.OFF
            )
        if effective_mode is GPUArbiterMode.ENFORCE:
            try:
                validate_gpu_claim(
                    self.gpu_resource_id,
                    self.vram_budget_mb,
                    extra_params=self._extra_params,
                )
            except GPUClaimConfigurationError as exc:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message=str(exc),
                ) from exc
            if self.backend_id is None:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message="GPU backend registry identity is missing",
                )
            if self._dispatch_context_factory is None:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.NOT_READY,
                    message="GPU arbiter dispatch authority is not configured",
                )
            request = GPUDispatchRequest(
                backend_id=self.backend_id,
                gpu_resource_id=self.gpu_resource_id,
                operation=operation,
                scope=scope,
            )
            boundary_error: BaseException | None = None
            async with self._dispatch_context_factory(request) as grant:
                try:
                    if not isinstance(grant, GPUDispatchGrant):
                        raise GPUArbiterDispatchError(
                            GPUArbiterErrorCode.UNAVAILABLE,
                            message=(
                                "GPU arbiter dispatch authority returned an invalid grant"
                            ),
                        )
                    yield grant
                except BaseException as exc:
                    boundary_error = exc
                    if isinstance(grant, GPUDispatchGrant):
                        grant.report_uncertain_if_missing("request_aborted")
                    raise
                else:
                    grant.report_uncertain_if_missing("response_not_reported")
            if boundary_error is not None:
                # A faulty authority context must not turn backend errors or
                # cancellation into an apparent successful dispatch.
                raise boundary_error
            return

        if (
            not resource_is_configured
            and not unclaimed_explicit_cpu
            and (
                rollout_boundary_active
                if settings.gpu_arbiter_rollout_enabled
                else any_gpu_resource_effectively_enforced()
            )
        ):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CONFIG_INVALID,
                message=(
                    "GPU backend must declare a valid configured resource before "
                    "dispatch while any resource is effectively enforced"
                ),
            )

        await self._observe_gpu_dispatch(operation)
        yield None
