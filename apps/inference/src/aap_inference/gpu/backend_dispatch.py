"""Framework-neutral per-request GPU mode and admission policy boundary."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol
from uuid import UUID

import structlog

from aap_protocol_v2.lifecycle import AdmissionScope
from aap_inference.backend_endpoint import BackendEndpointSnapshot
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchContextFactory,
    GPUDispatchGrant,
    GPUDispatchGrantPort,
    GPUDispatchRequest,
)
from aap_inference.gpu.diagnostics import (
    record_unregistered_gpu_shadow_dispatch,
    unregistered_gpu_loading_blocked,
)
from aap_inference.gpu.native import GPUAuthorityData, GPUAuthorityDataProvider
from aap_inference.gpu.policy import (
    GPUClaimConfigurationError,
    GPUShadowDecision,
    backend_is_trusted_explicit_cpu,
    evaluate_gpu_shadow_decision,
    effective_gpu_arbiter_mode,
    gpu_shadow_observation_enabled,
    validate_gpu_claim,
)
from aap_inference.gpu.rollout_state import (
    GPUArbiterRolloutUnavailable,
    gpu_rollout_boundary_active,
    resolve_gpu_arbiter_rollout,
)
from aap_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings
from aap_inference_contract.backend_control import BackendControlRequest
from aap_inference_contract.execution import FrozenModelOperation


logger = structlog.get_logger(__name__)
_GPU_SHADOW_OBSERVER_TIMEOUT_SECONDS = 0.25


class GPUBackendDispatchClock(Protocol):
    def time(self) -> float: ...


class _SystemClock:
    def time(self) -> float:
        return time.time()


@dataclass(frozen=True)
class GPUShadowBackendFacts:
    """Credential-free projection of one registry row for GPU policy."""

    backend_id: UUID
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    eviction_priority: int
    extra_params: Mapping[str, Any]
    state: str
    last_checked_at: datetime | None
    health_meta: Mapping[str, Any] | None

    @property
    def id(self) -> UUID:
        return self.backend_id


def parse_gpu_shadow_backend_facts(value: object) -> tuple[GPUShadowBackendFacts, ...]:
    """Validate the closed Native shadow projection before policy consumes it."""

    if type(value) is not dict or set(value) != {"backends"}:
        raise ValueError("GPU shadow snapshot is malformed")
    rows = value["backends"]
    if type(rows) is not list:
        raise ValueError("GPU shadow snapshot is malformed")
    expected = {
        "backend_id",
        "gpu_resource_id",
        "vram_budget_mb",
        "eviction_priority",
        "extra_params",
        "state",
        "last_checked_at",
        "health_meta",
    }
    facts: list[GPUShadowBackendFacts] = []
    previous_id: str | None = None
    for row in rows:
        if type(row) is not dict or set(row) != expected:
            raise ValueError("GPU shadow backend fact is malformed")
        raw_id = row["backend_id"]
        resource_id = row["gpu_resource_id"]
        budget = row["vram_budget_mb"]
        priority = row["eviction_priority"]
        extra_params = row["extra_params"]
        state = row["state"]
        checked_at = row["last_checked_at"]
        health_meta = row["health_meta"]
        if (
            type(raw_id) is not str
            or type(resource_id) not in {str, type(None)}
            or type(budget) not in {int, type(None)}
            or type(priority) is not int
            or type(extra_params) is not dict
            or type(state) is not str
            or type(checked_at) not in {str, type(None)}
            or type(health_meta) not in {dict, type(None)}
            or (budget is not None and (type(budget) is not int or budget <= 0))
        ):
            raise ValueError("GPU shadow backend fact is malformed")
        try:
            backend_id = UUID(raw_id)
            parsed_checked_at = (
                None
                if checked_at is None
                else datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            )
        except (TypeError, ValueError, AttributeError):
            raise ValueError("GPU shadow backend fact is malformed") from None
        if (
            str(backend_id) != raw_id
            or (previous_id is not None and raw_id <= previous_id)
            or (
                parsed_checked_at is not None
                and (
                    parsed_checked_at.tzinfo is None
                    or parsed_checked_at.utcoffset() is None
                )
            )
        ):
            raise ValueError("GPU shadow backend fact is malformed")
        previous_id = raw_id
        facts.append(
            GPUShadowBackendFacts(
                backend_id=backend_id,
                gpu_resource_id=resource_id,
                vram_budget_mb=budget,
                eviction_priority=priority,
                extra_params=extra_params,
                state=state,
                last_checked_at=parsed_checked_at,
                health_meta=health_meta,
            )
        )
    return tuple(facts)


class BackendGPUDispatchContextFactory:
    """Create the mode-aware per-call dispatch context used by model/control owners."""

    def __init__(
        self,
        data_provider: GPUAuthorityDataProvider,
        *,
        config: GPUAuthoritySettings,
        authority_dispatch_factory: GPUDispatchContextFactory | None,
        clock: GPUBackendDispatchClock | None = None,
    ) -> None:
        self._data_provider = data_provider
        self._config = config
        self._authority_dispatch_factory = authority_dispatch_factory
        self._clock = clock or _SystemClock()

    def __call__(
        self,
        request: FrozenModelOperation | BackendControlRequest,
        opened_endpoint: BackendEndpointSnapshot,
    ) -> Callable[
        [str, AdmissionScope],
        AbstractAsyncContextManager[GPUDispatchGrantPort | None],
    ]:
        request_id = request.request_id
        expected_registry_id = (
            request.endpoint_registry_id
            if isinstance(request, FrozenModelOperation)
            else request.registry_id
        )
        if (
            not isinstance(request_id, UUID)
            or opened_endpoint.request_id != request_id
            or (
                expected_registry_id is not None
                and opened_endpoint.registry_id != expected_registry_id
            )
        ):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.NOT_READY,
                message="GPU dispatch endpoint does not match the selected request",
            )

        @asynccontextmanager
        async def dispatch_context(
            operation: str,
            scope: AdmissionScope,
        ):
            expected_operation = _request_operation(request)
            if expected_operation is not None and operation != expected_operation:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.NOT_READY,
                    message="GPU dispatch operation does not match the selected request",
                )
            async with self._dispatch(
                request, opened_endpoint, operation, scope
            ) as grant:
                yield grant

        return dispatch_context

    @asynccontextmanager
    async def _dispatch(
        self,
        request: FrozenModelOperation | BackendControlRequest,
        endpoint: BackendEndpointSnapshot,
        operation: str,
        scope: AdmissionScope,
    ):
        if operation not in {
            "predict",
            "predict_interactive",
            "warmup",
            "reload",
            "unload",
        }:
            yield None
            return

        resource_id = endpoint.gpu_resource_id
        resource_is_configured = resource_id in self._config.gpu_arbiter_resources
        unclaimed_explicit_cpu = False
        rollout_boundary_active = False

        if self._config.gpu_arbiter_rollout_enabled:
            try:
                async with self._data_provider.transaction() as data:
                    requester = await self._read_requester_facts(
                        data,
                        endpoint,
                    )
                    unclaimed_explicit_cpu = (
                        resource_id is None
                        and endpoint.vram_budget_mb is None
                        and requester is not None
                        and backend_is_trusted_explicit_cpu(
                            _requester_with_endpoint_claim(requester, endpoint),
                            now=self._now(),
                        )
                    )
                    if unclaimed_explicit_cpu:
                        effective_mode = GPUArbiterMode.OFF
                    elif resource_id is not None:
                        rollout = await resolve_gpu_arbiter_rollout(
                            data,
                            resource_id,
                            config=self._config,
                        )
                        if rollout.dispatch_blocked:
                            raise GPUArbiterDispatchError(
                                GPUArbiterErrorCode.NOT_READY,
                                message=(
                                    rollout.blocked_reason
                                    or "GPU rollout transition is not ready"
                                ),
                            )
                        assert rollout.dispatch_mode is not None
                        effective_mode = rollout.dispatch_mode
                        if not resource_is_configured:
                            rollout_boundary_active = await gpu_rollout_boundary_active(
                                data,
                                config=self._config,
                            )
                    else:
                        effective_mode = GPUArbiterMode.OFF
                        rollout_boundary_active = await gpu_rollout_boundary_active(
                            data,
                            config=self._config,
                        )
            except GPUArbiterDispatchError:
                raise
            except GPUArbiterRolloutUnavailable as exc:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    message=str(exc),
                ) from exc
            except Exception as exc:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    message="GPU rollout policy is unavailable",
                ) from exc
        else:
            effective_mode = (
                effective_gpu_arbiter_mode(resource_id, config=self._config)
                if resource_id is not None
                else GPUArbiterMode.OFF
            )

        if effective_mode is GPUArbiterMode.ENFORCE:
            try:
                validate_gpu_claim(
                    resource_id,
                    endpoint.vram_budget_mb,
                    extra_params=endpoint.extra_params,
                    config=self._config,
                )
            except GPUClaimConfigurationError as exc:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message=str(exc),
                ) from exc
            if resource_id is None:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message="GPU backend registry identity is missing",
                )
            if self._authority_dispatch_factory is None:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.NOT_READY,
                    message="GPU arbiter dispatch authority is not configured",
                )
            gpu_request = GPUDispatchRequest(
                request_id=request.request_id,
                backend_id=str(endpoint.registry_id),
                gpu_resource_id=resource_id,
                operation=operation,  # type: ignore[arg-type]
                scope=scope,
            )
            boundary_error: BaseException | None = None
            async with self._authority_dispatch_factory(gpu_request) as grant:
                try:
                    if not isinstance(grant, GPUDispatchGrant):
                        raise GPUArbiterDispatchError(
                            GPUArbiterErrorCode.UNAVAILABLE,
                            message="GPU arbiter dispatch authority returned an invalid grant",
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
                raise boundary_error
            return

        if (
            not resource_is_configured
            and not unclaimed_explicit_cpu
            and (
                rollout_boundary_active
                if self._config.gpu_arbiter_rollout_enabled
                else False
            )
        ):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CONFIG_INVALID,
                message=(
                    "GPU backend must declare a valid configured resource before "
                    "dispatch while any resource is effectively enforced"
                ),
            )

        await self._observe_dispatch(endpoint, operation)
        yield None

    async def _read_requester_facts(
        self,
        data: GPUAuthorityData,
        endpoint: BackendEndpointSnapshot,
    ) -> GPUShadowBackendFacts | None:
        result = await data.step(
            "shadow_snapshot",
            {"resource_id": None, "backend_id": str(endpoint.registry_id)},
        )
        facts = parse_gpu_shadow_backend_facts(result)
        if len(facts) > 1:
            raise ValueError("GPU requester snapshot is not unique")
        return facts[0] if facts else None

    async def _observe_dispatch(
        self,
        endpoint: BackendEndpointSnapshot,
        operation: str,
    ) -> None:
        if not gpu_shadow_observation_enabled(
            endpoint.gpu_resource_id,
            config=self._config,
        ):
            return
        try:
            async with asyncio.timeout(_GPU_SHADOW_OBSERVER_TIMEOUT_SECONDS):
                resource_id = endpoint.gpu_resource_id
                selector = (
                    {"resource_id": resource_id, "backend_id": None}
                    if isinstance(resource_id, str) and resource_id
                    else {"resource_id": None, "backend_id": str(endpoint.registry_id)}
                )
                async with self._data_provider.transaction() as data:
                    snapshot = await data.step("shadow_snapshot", selector)
                rows = parse_gpu_shadow_backend_facts(snapshot)
                requester_facts = next(
                    (row for row in rows if row.backend_id == endpoint.registry_id),
                    None,
                )
                requester = _requester_with_endpoint_claim(requester_facts, endpoint)
                decision = evaluate_gpu_shadow_decision(
                    requester,
                    rows,
                    operation=operation,
                    config=self._config,
                    now=self._now(),
                )
                if decision is not None:
                    _record_shadow_decision(decision)
        except Exception:
            logger.warning(
                "gpu_arbiter_shadow_observer_failed",
                gpu_arbiter={
                    "backend_id": str(endpoint.registry_id),
                    "resource_id": endpoint.gpu_resource_id,
                    "operation": operation,
                    "authoritative": False,
                },
                exc_info=True,
            )

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock.time(), tz=UTC)

    @asynccontextmanager
    async def unregistered_smoke_dispatch(self, url: str, operation: str):
        """Gate and observe unregistered smoke dispatch before any raw-URL POST."""

        if unregistered_gpu_loading_blocked(config=self._config):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.CONFIG_INVALID,
                message=(
                    "effective enforce 下未注册 URL 只能执行只读 health/setup；"
                    "请先注册 backend 并完成受管身份绑定"
                ),
            )
        record_unregistered_gpu_shadow_dispatch(
            url,
            operation,
            config=self._config,
        )
        yield None


def _request_operation(
    request: FrozenModelOperation | BackendControlRequest,
) -> str | None:
    if isinstance(request, FrozenModelOperation):
        return {
            "batch_predict": "predict",
            "interactive_predict": "predict_interactive",
            "pinned_tracker": "predict_interactive",
        }.get(request.operation)
    operation = request.operation
    return operation if operation in {"warmup", "reload", "unload"} else None


def _requester_with_endpoint_claim(
    facts: GPUShadowBackendFacts | None,
    endpoint: BackendEndpointSnapshot,
) -> GPUShadowBackendFacts:
    return GPUShadowBackendFacts(
        backend_id=endpoint.registry_id,
        gpu_resource_id=endpoint.gpu_resource_id,
        vram_budget_mb=endpoint.vram_budget_mb,
        eviction_priority=endpoint.eviction_priority,
        extra_params=endpoint.extra_params,
        state=facts.state if facts is not None else "unknown",
        last_checked_at=facts.last_checked_at if facts is not None else None,
        health_meta=facts.health_meta if facts is not None else None,
    )


def _record_shadow_decision(decision: GPUShadowDecision) -> None:
    logger.info(
        "gpu_arbiter_shadow_decision",
        gpu_arbiter={
            "decision": decision.decision,
            "reason": decision.reason,
            "operation": decision.operation,
            "backend_id": decision.backend_id,
            "resource_id": decision.resource_id,
            "global_mode": decision.global_mode,
            "desired_mode": decision.desired_mode,
            "effective_mode": decision.effective_mode,
            "allocatable_mb": decision.allocatable_mb,
            "committed_before_mb": decision.committed_before_mb,
            "requested_increment_mb": decision.requested_increment_mb,
            "projected_mb": decision.projected_mb,
            "shortfall_mb": decision.shortfall_mb,
            "candidates": [
                {
                    "backend_id": candidate.backend_id,
                    "vram_budget_mb": candidate.vram_budget_mb,
                    "eviction_priority": candidate.eviction_priority,
                    "generation": candidate.generation,
                }
                for candidate in decision.candidates
            ],
            "uncertain_backend_ids": list(decision.uncertain_backend_ids),
            "authoritative": False,
            "candidate_order_authoritative": False,
            "unmanaged_workload": decision.unmanaged_workload,
            "business_request_blocked": False,
        },
    )
