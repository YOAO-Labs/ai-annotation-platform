"""API assembly for independent durable routing transactions and HTTP subjects."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
import uuid

from aap_inference_contract.routing import (
    RoutingRejectedResult,
    RoutingScopeView,
    RoutingUnavailable,
    decode_routing_quiescence_result,
    decode_routing_result,
    decode_routing_runtime_result,
    make_routing_command,
    make_routing_quiescence_request,
    make_routing_runtime_request,
)
from aap_inference_contract.routing_client import RoutingHttpClient
from aap_inference_contract.scope_retirement import (
    ScopeRetirementRequest,
    ScopeRetirementResult,
    ScopeRetirementUnavailable,
    decode_scope_retirement_request,
    decode_scope_retirement_result,
)
from aap_inference_contract.video_execution import (
    VideoScopeRejected,
    decode_video_execution_status_request,
)
from aap_workspace.config import secret_source

from app.config import settings


class RoutingRejected(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class RoutingAuthority:
    """Adapter that sends all durable routing authority calls to the configured owner."""

    @staticmethod
    def _http_client() -> RoutingHttpClient:
        try:
            token = secret_source(
                settings.inference_routing_token,
                settings.inference_routing_token_file,
            )
            return RoutingHttpClient(
                settings.inference_routing_url,
                token,
                timeout_seconds=settings.inference_routing_timeout_seconds,
            )
        except (ValueError, OSError):
            raise RoutingUnavailable() from None

    async def execute(self, operation, body):
        command = make_routing_command(operation, body)
        try:
            reply = await self._http_client().execute(command)
            reply = decode_routing_result(command, reply)
            if reply.state != "committed":
                raise RoutingUnavailable()
            if isinstance(reply.result, RoutingRejectedResult):
                raise RoutingRejected(reply.result.reason)
            return reply.result
        except (ValueError, OSError):
            raise RoutingUnavailable() from None

    async def acquire(
        self,
        *,
        pool_id=None,
        registry_id=None,
        project_id=None,
        mode="direct",
        owner="api",
        operation="backend_http",
        required_instance_id=None,
        execution=None,
    ):
        body = {
            "pool_id": str(pool_id) if pool_id else None,
            "registry_id": str(registry_id) if registry_id else None,
            "project_id": str(project_id) if project_id else None,
            "mode": mode,
            "owner": owner,
            "operation": operation,
            "required_instance_id": str(required_instance_id)
            if required_instance_id
            else None,
        }
        if execution is not None:
            body["execution"] = execution
        return await self.execute(
            "video_acquire" if execution is not None else "acquire",
            body,
        )

    async def runtime_snapshot(self, registry_ids):
        request = make_routing_runtime_request(registry_ids)
        try:
            reply = await self._http_client().runtime(request)
            return decode_routing_runtime_result(request, reply)
        except RoutingUnavailable:
            raise
        except Exception:
            raise RoutingUnavailable() from None

    async def quiescence(self, registry_id: uuid.UUID):
        request = make_routing_quiescence_request(registry_id)
        try:
            reply = await self._http_client().quiescence(request)
            return decode_routing_quiescence_result(request, reply)
        except RoutingUnavailable:
            raise
        except Exception:
            raise RoutingUnavailable() from None

    async def retire_scope(
        self,
        request: ScopeRetirementRequest | dict | bytes | str,
    ) -> ScopeRetirementResult:
        return await self._scope_retirement_call(request, status=False)

    async def scope_retirement_status(
        self,
        request: ScopeRetirementRequest | dict | bytes | str,
    ) -> ScopeRetirementResult:
        return await self._scope_retirement_call(request, status=True)

    async def _scope_retirement_call(self, request, *, status: bool):
        try:
            typed_request = decode_scope_retirement_request(request)
            client = self._http_client()
            reply = (
                await client.scope_retirement_status(typed_request)
                if status
                else await client.retire_scope(typed_request)
            )
            return decode_scope_retirement_result(typed_request, reply)
        except RoutingUnavailable:
            raise
        except (ValueError, OSError, ScopeRetirementUnavailable):
            raise RoutingUnavailable() from None

    async def video_execution_status(self, proof):
        """Read a proof-bound video execution status from the configured owner."""
        try:
            typed_proof = decode_video_execution_status_request(proof)
            return await self._http_client().video_execution_status(typed_proof)
        except RoutingUnavailable:
            raise
        except (ValueError, OSError, VideoScopeRejected):
            raise RoutingUnavailable() from None

    async def close(self, scope, outcome="success"):
        return await self.execute(
            "close",
            {
                "scope_id": str(scope.id),
                "capability": scope.capability,
                "outcome": outcome,
            },
        )


@dataclass(frozen=True)
class ScopeBinding:
    authority: RoutingAuthority
    scope: RoutingScopeView


@dataclass(frozen=True)
class ControlBinding:
    authority: RoutingAuthority
    control: Any


_SCOPE: ContextVar[ScopeBinding | None] = ContextVar(
    "aap_api_route_scope", default=None
)
_CONTROL: ContextVar[ControlBinding | None] = ContextVar(
    "aap_api_route_control", default=None
)


@asynccontextmanager
async def scope_binding(authority, scope):
    token = _SCOPE.set(ScopeBinding(authority, scope))
    try:
        yield
    finally:
        _SCOPE.reset(token)


@asynccontextmanager
async def control_binding(authority, control):
    token = _CONTROL.set(ControlBinding(authority, control))
    try:
        yield
    finally:
        _CONTROL.reset(token)


def current_control_binding() -> ControlBinding | None:
    """Return the active lifecycle control for owner-local transport composition."""
    return _CONTROL.get()


@asynccontextmanager
async def lifecycle_control(db, registry_id, kind, *, expected_topology=None):
    from dataclasses import asdict
    from .contracts import quiescence_error, MLBackendQuiescenceError

    authority = RoutingAuthority()
    topology = asdict(expected_topology) if expected_topology is not None else None
    if topology is not None:
        topology = {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in topology.items()
        }
    try:
        control = await authority.execute(
            "prepare_control",
            {
                "control_id": str(uuid.uuid4()),
                "registry_id": str(registry_id),
                "kind": kind,
                "expected_topology": topology,
            },
        )
    except RoutingRejected as exc:
        raise quiescence_error(exc.reason) from None
    except RoutingUnavailable:
        raise MLBackendQuiescenceError(
            "ml_backend_router_unavailable",
            "durable routing owner unavailable",
            unavailable=True,
        ) from None
    binding = ControlBinding(authority, control)
    try:
        async with control_binding(authority, control):
            yield binding
    finally:
        try:
            await authority.execute(
                "finish_control",
                {
                    "control_id": str(control.control_id),
                    "capability": control.capability,
                },
            )
        except Exception:
            # Unresolved actual HTTP attempts retain the persistent hold.
            import logging

            logging.getLogger(__name__).warning("backend_control_hold_retained")
