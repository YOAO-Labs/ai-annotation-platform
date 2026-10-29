"""Ordinary-worker HTTP client for the remote GPU dispatch authority."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchContextFactory,
    GPUDispatchOutcome,
    GPUDispatchOutcomeChannel,
    GPUDispatchRequest,
    GPUDispatchUncertainReason,
)
from aap_inference_contract.gpu import (
    GPU_ADMIN_HTTP_PROTOCOL,
    GPU_ADMIN_ACTIVATE_PATH,
    GPU_ADMIN_COLLECT_PATH,
    GPU_ADMIN_DIAGNOSTICS_PATH,
    GPU_ADMIN_INSTALLATION_ACTIVATE_PATH,
    GPU_ADMIN_INSTALLATION_QUIESCE_PATH,
    GPU_ADMIN_OBSERVE_PATH,
    GPU_ADMIN_PROBE_PATH,
    GPU_ADMIN_REPAIR_PATH,
    GPU_ADMIN_ROLLOUT_PATH,
    GPU_DISPATCH_ACQUIRE_PATH,
    GPU_DISPATCH_HTTP_PROTOCOL,
    GPU_DISPATCH_SETTLE_PATH,
    GPUAuthorityErrorReply,
    GPUActivateMembershipsReply,
    GPUActivateMembershipsRequest,
    GPUAdminDiagnosticsReply,
    GPUAdminDiagnosticsRequest,
    GPUDispatchAcquireReply,
    GPUDispatchAcquireRequest,
    GPUDispatchSettleReply,
    GPUDispatchSettleRequest,
    GPUInstallationActivationReply,
    GPUInstallationControlRequest,
    GPUInstallationQuiesceReply,
    GPURetiredMembershipReply,
    GPURetiredMembershipRequest,
    GPURolloutControlReply,
    GPURolloutControlRequest,
    GPUResourceObservationRequest,
    GPUResourceRepairReply,
    GPUResourceRepairRequest,
    GPUResourceRuntimeObservationReply,
    GPUTombstoneCollectionReply,
    gpu_dispatch_settle_request_valid,
)

logger = logging.getLogger(__name__)

# The public response has a newly serialized body and a different HTTP connection.
_PRIVATE_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-type",
        "content-encoding",
        "content-language",
        "content-location",
        "content-range",
        "content-disposition",
        "content-md5",
        "digest",
        "etag",
        "last-modified",
    }
)


def _project_error_headers(
    transport_headers: Mapping[str, str], error_headers: Mapping[str, str]
) -> dict[str, str]:
    sources = (transport_headers, error_headers)
    excluded = set(_PRIVATE_RESPONSE_HEADERS)
    for source in sources:
        for name, value in source.items():
            if name.lower() == "connection":
                excluded.update(token.strip().lower() for token in value.split(","))
    # Error-body headers take precedence even when their casing differs.
    projected: dict[str, tuple[str, str]] = {}
    for source in sources:
        for name, value in source.items():
            if name.lower() not in excluded:
                projected[name.lower()] = (name, value)
    return dict(projected.values())


class GPUHTTPResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class GPUHTTPTransport(Protocol):
    async def post(self, url: str, *, json: dict[str, Any]) -> GPUHTTPResponse: ...


@dataclass(frozen=True)
class GPUClientDispatchGrant:
    generation: str
    admission_token: str = field(repr=False)
    session_id: str = field(repr=False)
    _outcome_channel: GPUDispatchOutcomeChannel = field(
        default_factory=GPUDispatchOutcomeChannel,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def outcome(self) -> GPUDispatchOutcome | None:
        return self._outcome_channel.outcome

    def report_response(self, status_code: int) -> bool:
        return self._outcome_channel.report_response(status_code)

    def report_not_sent(self) -> bool:
        return self._outcome_channel.report_not_sent()

    def request_started(self) -> None:
        self._outcome_channel.request_started()

    def report_uncertain_if_missing(self, reason: GPUDispatchUncertainReason) -> bool:
        return self._outcome_channel.report_uncertain_if_missing(reason)


class GPUAuthorityHTTPClient:
    """Typed HTTP transport; it never owns private data, Redis, or signing keys."""

    def __init__(self, base_url: str, http: GPUHTTPTransport) -> None:
        if not base_url or base_url != base_url.strip():
            raise ValueError("GPU authority base_url must be non-empty and canonical")
        self._base_url = base_url.rstrip("/")
        self._http = http

    def dispatch_context_factory(self) -> GPUDispatchContextFactory:
        return self.dispatch_context

    @asynccontextmanager
    async def dispatch_context(self, request: GPUDispatchRequest):
        acquire_request = GPUDispatchAcquireRequest(
            request_id=request.request_id,
            backend_id=request.backend_id,
            gpu_resource_id=request.gpu_resource_id,
            operation=request.operation,
            scope=request.scope.value,
        )
        response = await self._post(
            GPU_DISPATCH_ACQUIRE_PATH,
            acquire_request.model_dump(mode="json"),
        )
        reply = self._decode_success(response, GPUDispatchAcquireReply)
        grant = GPUClientDispatchGrant(
            generation=reply.generation,
            admission_token=reply.admission_token,
            session_id=reply.session_id,
        )
        try:
            yield grant
        finally:
            if grant.outcome is None:
                grant.report_uncertain_if_missing("response_not_reported")
            settle_request = self._settle_request(grant)
            await self._settle_cancellation_safe(settle_request)

    async def activate_installation(
        self, request: GPUInstallationControlRequest
    ) -> GPUInstallationActivationReply:
        response = await self._post(
            GPU_ADMIN_INSTALLATION_ACTIVATE_PATH, request.model_dump(mode="json")
        )
        reply = self._decode_success(response, GPUInstallationActivationReply)
        self._validate_installation_reply(request, reply)
        return reply

    async def quiesce_installation(
        self, request: GPUInstallationControlRequest
    ) -> GPUInstallationQuiesceReply:
        response = await self._post(
            GPU_ADMIN_INSTALLATION_QUIESCE_PATH, request.model_dump(mode="json")
        )
        reply = self._decode_success(response, GPUInstallationQuiesceReply)
        self._validate_installation_reply(request, reply)
        if (
            reply.operation_id != request.operation_id
            or reply.container_id != request.container_id
        ):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.UNAVAILABLE,
                message="GPU authority returned mismatched installation proof",
            )
        return reply

    @staticmethod
    def _validate_installation_reply(
        request: GPUInstallationControlRequest, reply: GPUInstallationActivationReply
    ) -> None:
        if (
            reply.registry_id != request.registry_id
            or reply.resource_id != request.resource_id
            or reply.endpoint != request.endpoint
            or reply.boot_id != request.boot_id
            or reply.pool_ids != request.pool_ids
        ):
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.UNAVAILABLE,
                message="GPU authority returned mismatched installation identity",
            )

    async def activate_resource_memberships(
        self,
        resource_id: str,
        *,
        pending_only: bool = False,
    ) -> GPUActivateMembershipsReply:
        request = GPUActivateMembershipsRequest(
            resource_id=resource_id,
            pending_only=pending_only,
        )
        response = await self._post(
            GPU_ADMIN_ACTIVATE_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPUActivateMembershipsReply)

    async def advance_resource_rollout_control(
        self,
        resource_id: str,
        transition_id: UUID,
        target_gate: str,
    ) -> GPURolloutControlReply:
        request = GPURolloutControlRequest(
            resource_id=resource_id,
            transition_id=transition_id,
            target_gate=target_gate,
        )
        response = await self._post(
            GPU_ADMIN_ROLLOUT_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPURolloutControlReply)

    async def observe_resource(
        self,
        resource_id: str,
        *,
        allocatable_mb: int | None = None,
    ) -> GPUResourceRuntimeObservationReply:
        request = GPUResourceObservationRequest(
            resource_id=resource_id,
            allocatable_mb=allocatable_mb,
        )
        response = await self._post(
            GPU_ADMIN_OBSERVE_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPUResourceRuntimeObservationReply)

    async def repair_resource(
        self,
        resource_id: str,
        allocatable_mb: int | None = None,
        *,
        force_proof_reset: bool = False,
        readiness_blocker: str | None = None,
    ) -> GPUResourceRepairReply:
        request = GPUResourceRepairRequest(
            resource_id=resource_id,
            allocatable_mb=allocatable_mb,
            force_proof_reset=force_proof_reset,
            readiness_blocker=readiness_blocker,
        )
        response = await self._post(
            GPU_ADMIN_REPAIR_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPUResourceRepairReply)

    async def probe_retired_membership(
        self,
        backend_id: UUID,
        resource_id: str,
        membership_epoch: int,
    ) -> GPURetiredMembershipReply:
        request = GPURetiredMembershipRequest(
            backend_id=backend_id,
            resource_id=resource_id,
            membership_epoch=membership_epoch,
        )
        response = await self._post(
            GPU_ADMIN_PROBE_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPURetiredMembershipReply)

    async def collect_tombstone(
        self,
        backend_id: UUID,
        resource_id: str,
        membership_epoch: int,
    ) -> GPUTombstoneCollectionReply:
        request = GPURetiredMembershipRequest(
            backend_id=backend_id,
            resource_id=resource_id,
            membership_epoch=membership_epoch,
        )
        response = await self._post(
            GPU_ADMIN_COLLECT_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPUTombstoneCollectionReply)

    async def get_diagnostics(
        self,
        resource_id: str | None = None,
    ) -> GPUAdminDiagnosticsReply:
        request = GPUAdminDiagnosticsRequest(resource_id=resource_id)
        response = await self._post(
            GPU_ADMIN_DIAGNOSTICS_PATH,
            request.model_dump(mode="json"),
        )
        return self._decode_success(response, GPUAdminDiagnosticsReply)

    async def _post(self, path: str, body: dict[str, Any]) -> GPUHTTPResponse:
        try:
            return await self._http.post(f"{self._base_url}{path}", json=body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.UNAVAILABLE,
                message="GPU authority HTTP request failed",
            ) from exc

    def _decode_success(self, response: GPUHTTPResponse, model: type[Any]) -> Any:
        if response.status_code < 200 or response.status_code >= 300:
            self._raise_remote_error(response)
        try:
            result = model.model_validate_json(response.content)
        except Exception as exc:
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.UNAVAILABLE,
                message="GPU authority returned an invalid response",
            ) from exc
        if result.protocol not in {
            GPU_DISPATCH_HTTP_PROTOCOL,
            GPU_ADMIN_HTTP_PROTOCOL,
        }:
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.UNAVAILABLE,
                message="GPU authority returned an incompatible response",
            )
        return result

    @staticmethod
    def _raise_remote_error(response: GPUHTTPResponse) -> None:
        try:
            error = GPUAuthorityErrorReply.model_validate_json(response.content)
            if error.status_code != response.status_code:
                raise ValueError("HTTP and GPU error status differ")
            headers = _project_error_headers(response.headers, error.headers)
            raise GPUArbiterDispatchError.from_remote(
                status_code=error.status_code,
                detail=error.detail,
                headers=headers,
                error_code=error.error_code,
                retry_after_s=error.retry_after_s,
            )
        except GPUArbiterDispatchError:
            raise
        except Exception as exc:
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.UNAVAILABLE,
                message="GPU authority returned an invalid error response",
            ) from exc

    @staticmethod
    def _settle_request(grant: GPUClientDispatchGrant) -> GPUDispatchSettleRequest:
        outcome = grant.outcome
        if outcome is None:
            return GPUDispatchSettleRequest(
                session_id=grant.session_id,
                outcome="uncertain",
                reason="response_not_reported",
            )
        if outcome.kind == "response_received":
            return GPUDispatchSettleRequest(
                session_id=grant.session_id,
                outcome="response_received",
                status_code=outcome.status_code,
            )
        if outcome.kind == "not_sent":
            return GPUDispatchSettleRequest(
                session_id=grant.session_id,
                outcome="not_sent",
            )
        return GPUDispatchSettleRequest(
            session_id=grant.session_id,
            outcome="uncertain",
            reason=outcome.reason,
        )

    async def _settle_cancellation_safe(
        self,
        request: GPUDispatchSettleRequest,
    ) -> None:
        if not gpu_dispatch_settle_request_valid(request):
            logger.error("GPU dispatch client produced an invalid one-shot outcome")
            return
        task = asyncio.create_task(self._settle(request))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        try:
            task.result()
        except Exception:
            logger.warning("GPU authority outcome settlement failed", exc_info=True)
        if cancelled:
            raise asyncio.CancelledError

    async def _settle(self, request: GPUDispatchSettleRequest) -> None:
        try:
            response = await self._http.post(
                f"{self._base_url}{GPU_DISPATCH_SETTLE_PATH}",
                json=request.model_dump(mode="json"),
            )
            reply = self._decode_success(response, GPUDispatchSettleReply)
            if not reply.settled:
                logger.warning("GPU authority dispatch outcome was already unknown")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "GPU authority dispatch outcome could not be delivered", exc_info=True
            )


__all__ = ["GPUAuthorityHTTPClient", "GPUClientDispatchGrant", "GPUHTTPTransport"]
