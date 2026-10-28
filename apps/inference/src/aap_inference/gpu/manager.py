"""In-process dispatch-session bridge for the remote GPU authority HTTP API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import logging
import math
import secrets
from typing import Literal, Protocol
import uuid

from aap_protocol_v2.lifecycle import AdmissionScope

from aap_inference.gpu.endpoint_source import OwnedBackendEndpointSource
from aap_inference.gpu.installation_activation import activate_gpu_installation
from aap_inference.gpu.installation_quiescence import quiesce_gpu_installation
from aap_inference.gpu.control_preparation import GPUReadinessDemoter
from aap_inference.gpu.contracts import (
    GPUConfigDiagnostic,
    GPUDispatchContextFactory,
    GPUDispatchGrantPort,
    GPUDispatchRequest,
)
from aap_inference.gpu.diagnostics import (
    GPUBackendDiagnosticFacts,
    apply_backend_rollout_status,
    build_backend_gpu_config_status,
    build_resource_summaries,
    claimed_budget_by_resource,
)
from aap_inference.gpu.ledger import (
    GPUArbiterStore,
    GPUBackendDomainMember,
)
from aap_inference.gpu.membership_activation import (
    GPUMembershipPromotionResult,
    promote_gpu_resource_memberships,
)
from aap_inference.gpu.native import GPUAuthorityDataProvider, GPUCollectorDataProvider
from aap_inference.gpu.reconciliation import (
    disabled_gpu_resource_runtime_observation,
    observe_gpu_resource_runtime,
    repair_gpu_resource,
)
from aap_inference.gpu.retirement import (
    GPUTombstoneCollectionResult as GPUTombstoneCollectionFacts,
    probe_retired_gpu_membership,
    retire_gpu_backend_membership,
)
from aap_inference.gpu.rollout_control import (
    GPURolloutControlResult as GPURolloutControlFacts,
    advance_gpu_resource_rollout_control,
)
from aap_inference.gpu.rollout_state import (
    classify_gpu_arbiter_rollout,
    read_gpu_arbiter_rollout,
    read_gpu_arbiter_rollouts,
)
from aap_inference.gpu.settings import GPUAuthoritySettings, GPUArbiterMode
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference.gpu.transport import GPUBackendControlTransport
from aap_inference_contract.gpu import (
    GPUInstallationControlRequest,
    GPUInstallationQuiesceReply,
    GPUInstallationActivationReply,
    GPUActivateMembershipResult,
    GPUActivateMembershipsReply,
    GPUAdminDiagnosticsReply,
    GPUDispatchAcquireReply,
    GPUDispatchAcquireRequest,
    GPUDispatchSettleReply,
    GPUDispatchSettleRequest,
    GPURetiredMembershipReply,
    GPURolloutControlResult,
    GPURolloutControlReply,
    GPUResourceRuntimeObservationReply,
    GPUResourceRepairReply,
    GPUTombstoneCollectionReply,
    gpu_dispatch_settle_request_valid,
)

logger = logging.getLogger(__name__)


def _diagnostic_payload(
    diagnostic: GPUConfigDiagnostic,
) -> dict[str, object]:
    return {
        "code": diagnostic.code,
        "level": diagnostic.level,
        "message": diagnostic.message,
        "field": diagnostic.field,
        "resource_id": diagnostic.resource_id,
        "backend_id": (
            None if diagnostic.backend_id is None else str(diagnostic.backend_id)
        ),
    }


def _wire_uuid(value: str) -> uuid.UUID:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("GPU authority result identity is not canonical")
    return parsed


def _activation_wire_result(
    result: GPUMembershipPromotionResult,
) -> GPUActivateMembershipResult:
    return GPUActivateMembershipResult(
        backend_id=_wire_uuid(result.backend_id),
        resource_id=result.resource_id,
        membership_epoch=result.membership_epoch,
        status=result.status,
        reason=result.reason,
        runtime_epoch=result.runtime_epoch,
        control_epoch=result.control_epoch,
        requires_proof_reset=result.requires_proof_reset,
    )


def _rollout_wire_result(
    result: GPURolloutControlFacts,
) -> GPURolloutControlResult:
    return GPURolloutControlResult(
        backend_id=_wire_uuid(result.backend_id),
        resource_id=result.resource_id,
        membership_epoch=result.membership_epoch,
        transition_id=_wire_uuid(result.transition_id),
        operation=result.operation,
        status=result.status,
        reason=result.reason,
        control_epoch=result.control_epoch,
    )


def _tombstone_wire_result(
    result: GPUTombstoneCollectionFacts,
) -> GPUTombstoneCollectionReply:
    return GPUTombstoneCollectionReply(
        backend_id=result.backend_id,
        resource_id=result.resource_id,
        membership_epoch=result.membership_epoch,
        status=result.status,
        reason=result.reason,
        idempotent=result.redis_idempotent,
    )


class GPUClock(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


@dataclass
class _DispatchSession:
    context: AbstractAsyncContextManager[GPUDispatchGrantPort]
    grant: GPUDispatchGrantPort
    expiry_task: asyncio.Task[None] | None = None


class GPUAuthorityManager:
    """Own active local dispatch contexts while ordinary clients use HTTP leases."""

    def __init__(
        self,
        dispatch_context_factory: GPUDispatchContextFactory,
        *,
        data: GPUAuthorityDataProvider,
        collector_data: GPUCollectorDataProvider,
        store_factory: Callable[[], GPUArbiterStore],
        signer_factory: Callable[[], GPUAdmissionTokenSigner],
        endpoint_source: OwnedBackendEndpointSource,
        backend_transport: GPUBackendControlTransport,
        config: GPUAuthoritySettings,
        readiness_demoter: GPUReadinessDemoter,
        clock: GPUClock,
        max_dispatch_duration_seconds: float,
    ) -> None:
        if (
            isinstance(max_dispatch_duration_seconds, bool)
            or not isinstance(max_dispatch_duration_seconds, (int, float))
            or not math.isfinite(max_dispatch_duration_seconds)
            or max_dispatch_duration_seconds <= 0
        ):
            raise ValueError(
                "max_dispatch_duration_seconds must be positive and finite"
            )
        self._dispatch_context_factory = dispatch_context_factory
        self._data = data
        self._collector_data = collector_data
        self._store_factory = store_factory
        self._signer_factory = signer_factory
        self._endpoint_source = endpoint_source
        self._backend_transport = backend_transport
        self._config = config
        self._readiness_demoter = readiness_demoter
        self._clock = clock
        self._max_dispatch_duration_seconds = float(max_dispatch_duration_seconds)
        self._sessions: dict[str, _DispatchSession] = {}
        self._expiry_tasks: set[asyncio.Task[None]] = set()
        self._sessions_lock = asyncio.Lock()
        self._closed = False

    async def readiness(self) -> bool:
        """Probe private profiles and configured Redis without requiring a GPU resource."""

        if self._closed:
            return False
        try:
            async with self._data.transaction() as data:
                boundary = await data.step("rollout_boundary", {})
                if set(boundary) != {"active"} or type(boundary["active"]) is not bool:
                    return False
            async with self._collector_data.transaction():
                pass
            if not await self._endpoint_source.readiness():
                return False
            if self._config.redis_url:
                async with self._open_store() as store:
                    if not await store.ping():
                        return False
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("gpu_authority_readiness_failed", exc_info=True)
            return False

    async def acquire_dispatch(
        self,
        request: GPUDispatchAcquireRequest,
    ) -> GPUDispatchAcquireReply:
        if self._closed:
            raise RuntimeError("GPU authority manager is closed")
        dispatch_request = GPUDispatchRequest(
            request_id=request.request_id,
            backend_id=request.backend_id,
            gpu_resource_id=request.gpu_resource_id,
            operation=request.operation,
            scope=AdmissionScope(request.scope),
        )
        context = self._dispatch_context_factory(dispatch_request)
        grant = await context.__aenter__()
        session_id = secrets.token_urlsafe(32)
        session = _DispatchSession(context=context, grant=grant)
        try:
            reply = GPUDispatchAcquireReply(
                session_id=session_id,
                generation=grant.generation,
                admission_token=grant.admission_token,
            )
            async with self._sessions_lock:
                if self._closed:
                    raise RuntimeError("GPU authority manager is closed")
                self._sessions[session_id] = session
                expiry_task = asyncio.create_task(
                    self._expire_dispatch(
                        session_id,
                        self._clock.monotonic() + self._max_dispatch_duration_seconds,
                    ),
                    name="gpu-dispatch-session-expiry",
                )
                session.expiry_task = expiry_task
                self._expiry_tasks.add(expiry_task)
                expiry_task.add_done_callback(self._expiry_tasks.discard)
            return reply
        except BaseException:
            await context.__aexit__(None, None, None)
            raise

    async def settle_dispatch(
        self,
        request: GPUDispatchSettleRequest,
    ) -> GPUDispatchSettleReply:
        if not gpu_dispatch_settle_request_valid(request):
            raise ValueError("GPU dispatch outcome does not match its status/reason")
        session = await self._pop_session(request.session_id)
        if session is None:
            return GPUDispatchSettleReply(settled=False)
        await self._cancel_expiry(session.expiry_task)
        self._report_outcome(session.grant, request)
        await session.context.__aexit__(None, None, None)
        return GPUDispatchSettleReply(settled=True)

    async def activate_installation(
        self, request: GPUInstallationControlRequest
    ) -> GPUInstallationActivationReply:
        return await activate_gpu_installation(
            request,
            data=self._data,
            signer_factory=self._signer_factory,
            endpoint_source=self._endpoint_source,
            backend_transport=self._backend_transport,
            readiness_demoter=self._readiness_demoter,
            enforce=self._config.gpu_arbiter_desired_mode(request.resource_id) is GPUArbiterMode.ENFORCE,
            clock=self._clock,
        )

    async def quiesce_installation(
        self, request: GPUInstallationControlRequest
    ) -> GPUInstallationQuiesceReply:
        return await quiesce_gpu_installation(
            request,
            data=self._data,
            store_factory=self._store_factory,
            signer_factory=self._signer_factory,
            endpoint_source=self._endpoint_source,
            backend_transport=self._backend_transport,
            readiness_demoter=self._readiness_demoter,
            clock=self._clock,
        )

    async def activate_resource_memberships(
        self,
        resource_id: str,
        *,
        pending_only: bool = False,
    ) -> GPUActivateMembershipsReply:
        results = await promote_gpu_resource_memberships(
            self._data,
            resource_id,
            signer_factory=self._signer_factory,
            endpoint_source=self._endpoint_source,
            backend_transport=self._backend_transport,
            readiness_demoter=self._readiness_demoter,
            pending_only=pending_only,
        )
        return GPUActivateMembershipsReply(
            results=[_activation_wire_result(result) for result in results]
        )

    async def advance_resource_rollout_control(
        self,
        resource_id: str,
        transition_id: uuid.UUID,
        target_gate: Literal["legacy", "enforce"],
    ) -> GPURolloutControlReply:
        results = await advance_gpu_resource_rollout_control(
            self._data,
            resource_id,
            transition_id=transition_id,
            target_gate=target_gate,
            signer_factory=self._signer_factory,
            endpoint_source=self._endpoint_source,
            backend_transport=self._backend_transport,
            readiness_demoter=self._readiness_demoter,
        )
        return GPURolloutControlReply(
            results=[_rollout_wire_result(result) for result in results]
        )

    async def observe_resource(
        self,
        resource_id: str,
        *,
        allocatable_mb: int | None = None,
    ) -> GPUResourceRuntimeObservationReply:
        resource = self._config.gpu_arbiter_resources.get(resource_id)
        if allocatable_mb is not None and (
            resource is None or resource.allocatable_mb != allocatable_mb
        ):
            raise ValueError("GPU observation configuration does not match runtime")
        rollout = await read_gpu_arbiter_rollout(self._data, resource_id)
        should_observe_ledger = self._config.gpu_arbiter_desired_mode(
            resource_id
        ) is GPUArbiterMode.ENFORCE or (rollout is not None and rollout.state != "off")
        if not should_observe_ledger:
            memberships = await self._read_resource_memberships(resource_id)
            observation = disabled_gpu_resource_runtime_observation(memberships)
        else:
            async with self._open_store() as store:
                observation = await observe_gpu_resource_runtime(
                    self._data,
                    store,
                    resource_id,
                )
        return GPUResourceRuntimeObservationReply.model_validate(observation.__dict__)

    async def repair_resource(
        self,
        resource_id: str,
        allocatable_mb: int | None = None,
        *,
        force_proof_reset: bool = False,
        readiness_blocker: str | None = None,
    ) -> GPUResourceRepairReply:
        resource = self._config.gpu_arbiter_resources.get(resource_id)
        if resource is None:
            if allocatable_mb is not None or force_proof_reset or not readiness_blocker:
                raise ValueError(
                    "unconfigured GPU repair is only valid for a readiness blocker"
                )
            effective_allocatable_mb = None
        else:
            effective_allocatable_mb = resource.allocatable_mb
            if allocatable_mb is not None and resource.allocatable_mb != allocatable_mb:
                raise ValueError("GPU repair configuration does not match runtime")
        async with self._open_store() as store:
            result = await repair_gpu_resource(
                self._data,
                store,
                resource_id,
                effective_allocatable_mb,
                config=self._config,
                force_proof_reset=force_proof_reset,
                readiness_blocker=readiness_blocker,
            )
        return GPUResourceRepairReply.model_validate(result.__dict__)

    async def probe_retired_membership(
        self,
        backend_id: uuid.UUID,
        resource_id: str,
        membership_epoch: int,
    ) -> GPURetiredMembershipReply:
        async with self._data.transaction() as data:
            result = await probe_retired_gpu_membership(
                data,
                self._endpoint_source,
                self._backend_transport,
                backend_id,
                resource_id,
                membership_epoch,
            )
        return GPURetiredMembershipReply(
            backend_id=backend_id,
            resource_id=resource_id,
            membership_epoch=membership_epoch,
            reason=result.reason,
            proof_available=result.proof is not None,
        )

    async def collect_tombstone(
        self,
        backend_id: uuid.UUID,
        resource_id: str,
        membership_epoch: int,
    ) -> GPUTombstoneCollectionReply:
        async with self._open_store() as store:
            result = await retire_gpu_backend_membership(
                self._data,
                self._collector_data,
                store,
                self._endpoint_source,
                self._backend_transport,
                backend_id,
                resource_id,
                membership_epoch,
            )
        return _tombstone_wire_result(result)

    async def get_diagnostics(
        self,
        resource_id: str | None = None,
    ) -> GPUAdminDiagnosticsReply:
        arguments = {"resource_id": resource_id, "backend_id": None}
        result = await self._data.step("shadow_snapshot", arguments)
        if set(result) != {"backends"} or type(result["backends"]) is not list:
            raise ValueError("GPU diagnostics Native snapshot is malformed")
        facts: list[GPUBackendDiagnosticFacts] = []
        for item in result["backends"]:
            if type(item) is not dict or set(item) != {
                "backend_id",
                "gpu_resource_id",
                "vram_budget_mb",
                "eviction_priority",
                "extra_params",
                "state",
                "last_checked_at",
                "health_meta",
            }:
                raise ValueError("GPU diagnostics backend fact is malformed")
            backend_id = item["backend_id"]
            backend_resource_id = item["gpu_resource_id"]
            budget = item["vram_budget_mb"]
            priority = item["eviction_priority"]
            extra_params = item["extra_params"]
            state = item["state"]
            checked_at = item["last_checked_at"]
            health_meta = item["health_meta"]
            if (
                type(backend_id) is not str
                or type(backend_resource_id) not in {str, type(None)}
                or type(budget) not in {int, type(None)}
                or type(budget) is bool
                or type(priority) is not int
                or type(extra_params) is not dict
                or type(state) is not str
                or type(checked_at) not in {str, type(None)}
                or type(health_meta) not in {dict, type(None)}
            ):
                raise ValueError("GPU diagnostics backend fact is malformed")
            try:
                parsed_id = uuid.UUID(backend_id)
                parsed_checked_at = (
                    None
                    if checked_at is None
                    else datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                )
            except (ValueError, AttributeError):
                raise ValueError("GPU diagnostics backend fact is malformed") from None
            if str(parsed_id) != backend_id or (
                parsed_checked_at is not None
                and (
                    parsed_checked_at.tzinfo is None
                    or parsed_checked_at.utcoffset() is None
                )
            ):
                raise ValueError("GPU diagnostics backend fact is malformed")
            facts.append(
                GPUBackendDiagnosticFacts(
                    backend_id=parsed_id,
                    gpu_resource_id=backend_resource_id,
                    vram_budget_mb=budget,
                    state=state,
                    last_checked_at=parsed_checked_at,
                    health_meta=health_meta,
                )
            )

        resources, diagnostics = build_resource_summaries(
            facts,
            config=self._config,
        )
        if resource_id is not None:
            resources = [
                row for row in resources if row["gpu_resource_id"] == resource_id
            ]
            diagnostics = [
                item for item in diagnostics if item.resource_id in {None, resource_id}
            ]
        resources = [
            {
                **row,
                "diagnostics": [
                    _diagnostic_payload(diagnostic) for diagnostic in row["diagnostics"]
                ],
            }
            for row in resources
        ]

        snapshots = await read_gpu_arbiter_rollouts(self._data)
        rollout_by_resource = {item.resource_id: item for item in snapshots}
        for snapshot in snapshots:
            if (
                snapshot.resource_id not in self._config.gpu_arbiter_resources
                and snapshot.state != "off"
                and resource_id in {None, snapshot.resource_id}
            ):
                diagnostics.append(
                    GPUConfigDiagnostic(
                        code="gpu_rollout_resource_unconfigured",
                        level="blocker",
                        message=(
                            f"持久 rollout {snapshot.resource_id} 仍为 "
                            f"{snapshot.state}，但静态资源配置已缺失"
                        ),
                        resource_id=snapshot.resource_id,
                    )
                )
        rollout_resource_ids = set(self._config.gpu_arbiter_resources)
        rollout_resource_ids.update(
            item.resource_id for item in snapshots if item.state != "off"
        )
        if resource_id is not None:
            rollout_resource_ids &= {resource_id}
        relevant_resource_ids = tuple(sorted(rollout_resource_ids))
        decisions = {
            current_resource_id: classify_gpu_arbiter_rollout(
                current_resource_id,
                self._config.gpu_arbiter_desired_mode(current_resource_id),
                rollout_by_resource.get(current_resource_id),
                rollout_enabled=self._config.gpu_arbiter_rollout_enabled,
            )
            for current_resource_id in relevant_resource_ids
        }
        backend_totals = claimed_budget_by_resource(facts)
        backend_statuses: list[dict[str, object]] = []
        for fact in facts:
            status = build_backend_gpu_config_status(
                fact,
                backend_totals,
                config=self._config,
            )
            current_resource_id = fact.gpu_resource_id
            if (
                current_resource_id in self._config.gpu_arbiter_resources
                and current_resource_id in decisions
            ):
                apply_backend_rollout_status(
                    status,
                    current_resource_id,
                    decisions[current_resource_id],
                    rollout_by_resource.get(current_resource_id),
                    config=self._config,
                    backend_id=fact.backend_id,
                )
            backend_statuses.append(
                {
                    "backend_id": str(fact.backend_id),
                    "gpu_config": {
                        **{
                            key: value
                            for key, value in status.__dict__.items()
                            if key != "diagnostics"
                        },
                        "diagnostics": [
                            _diagnostic_payload(diagnostic)
                            for diagnostic in status.diagnostics
                        ],
                    },
                }
            )
        rollouts = [
            {
                "resource_id": current_resource_id,
                "state": decision.state,
                "effective_mode": decision.effective_mode.value,
                "target_mode": (
                    rollout_by_resource[current_resource_id].target_mode.value
                    if current_resource_id in rollout_by_resource
                    else self._config.gpu_arbiter_desired_mode(
                        current_resource_id
                    ).value
                ),
                "transition_id": (
                    str(rollout_by_resource[current_resource_id].transition_id)
                    if current_resource_id in rollout_by_resource
                    and rollout_by_resource[current_resource_id].transition_id
                    else None
                ),
                "last_transition_id": (
                    str(rollout_by_resource[current_resource_id].last_transition_id)
                    if current_resource_id in rollout_by_resource
                    and rollout_by_resource[current_resource_id].last_transition_id
                    else None
                ),
                "blocker_reason": (
                    rollout_by_resource[current_resource_id].blocker_reason
                    if current_resource_id in rollout_by_resource
                    else None
                ),
                "revision": (
                    rollout_by_resource[current_resource_id].revision
                    if current_resource_id in rollout_by_resource
                    else None
                ),
                "desired_mode": self._config.gpu_arbiter_desired_mode(
                    current_resource_id
                ).value,
                "dispatch_blocked": decision.dispatch_blocked,
                "blocked_reason": decision.blocked_reason,
            }
            for current_resource_id, decision in decisions.items()
        ]
        return GPUAdminDiagnosticsReply(
            result={
                "resources": resources,
                "diagnostics": [
                    _diagnostic_payload(diagnostic) for diagnostic in diagnostics
                ],
                "backends": backend_statuses,
                "rollouts": rollouts,
            }
        )

    async def _read_resource_memberships(
        self,
        resource_id: str,
    ) -> tuple[GPUBackendDomainMember, ...]:
        result = await self._data.step(
            "resource_runtime_observation",
            {"resource_id": resource_id},
        )
        if set(result) != {"status", "memberships"} or result["status"] != "ok":
            raise ValueError("GPU runtime membership observation is malformed")
        rows = result["memberships"]
        if type(rows) is not list:
            raise ValueError("GPU runtime membership observation is malformed")
        members: list[GPUBackendDomainMember] = []
        for item in rows:
            if type(item) is not dict or set(item) != {
                "backend_id",
                "membership_epoch",
                "state",
            }:
                raise ValueError("GPU runtime membership row is malformed")
            backend_id = item["backend_id"]
            epoch = item["membership_epoch"]
            state = item["state"]
            try:
                canonical_id = str(uuid.UUID(backend_id))
            except (TypeError, ValueError, AttributeError):
                raise ValueError(
                    "GPU runtime membership backend ID is malformed"
                ) from None
            if (
                canonical_id != backend_id
                or type(epoch) is not int
                or not 1 <= epoch <= 9_223_372_036_854_775_807
                or state not in {"pending", "active", "retiring"}
            ):
                raise ValueError("GPU runtime membership row is malformed")
            members.append(
                GPUBackendDomainMember(
                    backend_id=backend_id,
                    membership_epoch=epoch,
                    state=state,
                )
            )
        if members != sorted(members, key=lambda item: uuid.UUID(item.backend_id).int):
            raise ValueError("GPU runtime memberships are not UUID ordered")
        return tuple(members)

    @asynccontextmanager
    async def _open_store(self):
        store = self._store_factory()
        try:
            yield store
        finally:
            await store.aclose()

    async def aclose(self) -> None:
        async with self._sessions_lock:
            self._closed = True
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            expiry_tasks = tuple(self._expiry_tasks)
        for session in sessions:
            await self._cancel_expiry(session.expiry_task)
            self._mark_uncertain(session.grant)
            await session.context.__aexit__(None, None, None)
        if expiry_tasks:
            await asyncio.gather(*expiry_tasks, return_exceptions=True)

    async def _expire_dispatch(self, session_id: str, expires_at: float) -> None:
        try:
            await self._clock.sleep(max(0.0, expires_at - self._clock.monotonic()))
            session = await self._pop_session(session_id)
            if session is None:
                return
            self._mark_uncertain(session.grant)
            await session.context.__aexit__(None, None, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("GPU dispatch expiry cleanup failed")

    async def _pop_session(self, session_id: str) -> _DispatchSession | None:
        async with self._sessions_lock:
            return self._sessions.pop(session_id, None)

    @staticmethod
    def _report_outcome(
        grant: GPUDispatchGrantPort,
        request: GPUDispatchSettleRequest,
    ) -> None:
        if request.outcome == "response_received":
            if request.status_code is None:
                raise ValueError("GPU response outcome is missing its status code")
            grant.report_response(request.status_code)
        elif request.outcome == "not_sent":
            grant.report_not_sent()
        else:
            if request.reason is None:
                raise ValueError("GPU uncertain outcome is missing its reason")
            grant.report_uncertain_if_missing(request.reason)

    @staticmethod
    def _mark_uncertain(grant: GPUDispatchGrantPort) -> None:
        grant.report_uncertain_if_missing("response_not_reported")

    @staticmethod
    async def _cancel_expiry(task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def build_gpu_authority_manager(
    dispatch_context_factory: GPUDispatchContextFactory,
    *,
    data: GPUAuthorityDataProvider,
    collector_data: GPUCollectorDataProvider,
    store_factory: Callable[[], GPUArbiterStore],
    signer_factory: Callable[[], GPUAdmissionTokenSigner],
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    config: GPUAuthoritySettings,
    readiness_demoter: GPUReadinessDemoter,
    clock: GPUClock,
    max_dispatch_duration_seconds: float,
) -> GPUAuthorityManager:
    """Compose the private owner with explicit Native, Redis, signer and transport ports."""

    return GPUAuthorityManager(
        dispatch_context_factory,
        data=data,
        collector_data=collector_data,
        store_factory=store_factory,
        signer_factory=signer_factory,
        endpoint_source=endpoint_source,
        backend_transport=backend_transport,
        config=config,
        readiness_demoter=readiness_demoter,
        clock=clock,
        max_dispatch_duration_seconds=max_dispatch_duration_seconds,
    )


__all__ = [
    "GPUAuthorityManager",
    "GPUClock",
    "build_gpu_authority_manager",
]
