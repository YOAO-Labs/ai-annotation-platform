"""API assembly for independent durable routing transactions and HTTP subjects."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from typing import Any
import uuid

from aap_inference_contract.routing import (
    ROUTING_DATA_PROTOCOL,
    RoutingRejectedResult,
    RoutingScopeView,
    RoutingUnavailable,
    decode_routing_result,
    make_routing_command,
    make_routing_status_request,
    routing_native_policy_ready,
)
from aap_inference_contract.routing_client import RoutingHttpClient
from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_ACTIONS,
    SCOPE_RETIREMENT_DATA_PROTOCOL,
    SCOPE_RETIREMENT_REPLY_MAX_BYTES,
    SCOPE_RETIREMENT_REQUEST_MAX_BYTES,
    ScopeRetirementRequest,
    ScopeRetirementResult,
    ScopeRetirementUnavailable,
    decode_scope_retirement_request,
    decode_scope_retirement_result,
)
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_SCOPE_ACTIONS,
    VideoScopeRejected,
    decode_video_execution_status_request,
    decode_video_execution_status_result,
    decode_video_scope_data,
    video_execution_status_result_bytes,
)
from aap_workspace.config import secret_source
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_object_session

from app.config import settings


class RoutingRejected(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _SqlAlchemyData:
    def __init__(self, connection, xid):
        self.connection = connection
        self.xid = xid

    @classmethod
    async def bind(cls, connection):
        policy = await connection.scalar(
            text("SELECT aap_inference_routing_v1.routing_policy()")
        )
        if isinstance(policy, str):
            policy = json.loads(policy)
        if not routing_native_policy_ready(policy):
            raise RoutingUnavailable()
        xid = await connection.scalar(
            text("SELECT pg_catalog.pg_current_xact_id()::text")
        )
        return cls(connection, xid)

    async def step(self, action, arguments):
        xid = await self.connection.scalar(
            text("SELECT pg_catalog.pg_current_xact_id_if_assigned()::text")
        )
        if xid != self.xid or not self.connection.in_transaction():
            raise RoutingUnavailable()
        result = await self.connection.scalar(
            text("SELECT aap_inference_routing_v1.routing_step(:action,:arguments)"),
            {
                "action": action,
                "arguments": json.dumps(
                    arguments, allow_nan=False, separators=(",", ":")
                ),
            },
        )
        return json.loads(result) if isinstance(result, str) else result

    async def retirement_step(self, action, arguments):
        if (
            type(action) is not str
            or action not in SCOPE_RETIREMENT_ACTIONS
            or not isinstance(arguments, dict)
            or any(type(key) is not str for key in arguments)
        ):
            raise RoutingUnavailable()
        xid = await self.connection.scalar(
            text("SELECT pg_catalog.pg_current_xact_id_if_assigned()::text")
        )
        if xid != self.xid or not self.connection.in_transaction():
            raise RoutingUnavailable()
        try:
            encoded = json.dumps(
                arguments, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise RoutingUnavailable() from None
        if len(encoded) > SCOPE_RETIREMENT_REQUEST_MAX_BYTES:
            raise RoutingUnavailable()
        result = await self.connection.scalar(
            text(
                "SELECT aap_inference_routing_v1.scope_retirement_step(:action,:arguments)"
            ),
            {"action": action, "arguments": encoded.decode("utf-8")},
        )

        def unique_object(pairs):
            parsed = {}
            for key, value in pairs:
                if key in parsed:
                    raise ValueError("Duplicate scope-retirement field")
                parsed[key] = value
            return parsed

        def reject_constant(value):
            raise ValueError(f"Invalid JSON number: {value}")

        try:
            if type(result) is str:
                raw = result.encode("utf-8", errors="strict")
            elif isinstance(result, dict):
                raw = json.dumps(result, allow_nan=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            else:
                raise RoutingUnavailable()
            if len(raw) > SCOPE_RETIREMENT_REPLY_MAX_BYTES:
                raise RoutingUnavailable()
            result = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except RoutingUnavailable:
            raise
        except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
            raise RoutingUnavailable() from None
        if (
            not isinstance(result, dict)
            or result.get("schema") != SCOPE_RETIREMENT_DATA_PROTOCOL
            or type(result.get("status")) is not str
            or result["status"] not in {"ok", "rejected"}
        ):
            raise RoutingUnavailable()
        payload_keys = set(result) - {"schema", "status"}
        if result["status"] == "rejected":
            if (
                payload_keys != {"reason"}
                or type(result.get("reason")) is not str
                or result["reason"]
                not in {
                    "scope_unavailable",
                    "backend_not_ready",
                    "transport_changed",
                    "management_busy",
                    "invalid_command",
                    "retirement_conflict",
                }
            ):
                raise RoutingUnavailable()
        elif payload_keys != (
            {"ready", "functions"} if action == "policy" else {"retirement", "context"}
        ):
            raise RoutingUnavailable()
        return result

    async def video_execution_scope_step(self, action, arguments):
        if type(action) is not str or action not in VIDEO_EXECUTION_SCOPE_ACTIONS:
            raise RoutingUnavailable()
        xid = await self.connection.scalar(
            text("SELECT pg_catalog.pg_current_xact_id_if_assigned()::text")
        )
        if xid != self.xid or not self.connection.in_transaction():
            raise RoutingUnavailable()
        encoded = json.dumps(arguments, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise RoutingUnavailable()
        raw = await self.connection.scalar(
            text(
                "SELECT aap_inference_routing_v1.video_execution_scope_step(:action,:arguments)"
            ),
            {"action": action, "arguments": encoded},
        )
        if type(raw) is str:
            raw = json.loads(raw)
        try:
            decode_video_scope_data(action, arguments, raw)
        except VideoScopeRejected:
            pass
        return raw


class RoutingAuthority:
    """The configured owner; it never borrows the caller's business transaction."""

    def __init__(self, engine: AsyncEngine | AsyncConnection | None = None):
        self.engine = engine.engine if isinstance(engine, AsyncConnection) else engine

    @asynccontextmanager
    async def data_transaction(self):
        caller_error = False
        try:
            async with self._data_transaction() as data:
                try:
                    yield data
                except BaseException:
                    caller_error = True
                    raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except RoutingUnavailable:
            raise
        except Exception:
            if caller_error:
                raise
            raise RoutingUnavailable() from None

    @asynccontextmanager
    async def _data_transaction(self):
        from aap_inference.routing_postgres import RoutingPostgresData

        try:
            dsn = secret_source(
                settings.inference_routing_database_url,
                settings.inference_routing_database_url_file,
            )
        except (ValueError, OSError):
            raise RoutingUnavailable() from None
        if dsn:
            import asyncpg

            connection = await asyncpg.connect(
                dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
                timeout=5,
                command_timeout=settings.inference_routing_timeout_seconds,
                server_settings={"application_name": "aap-api-routing"},
            )
            try:
                async with connection.transaction():
                    yield await RoutingPostgresData.bind(connection)
            finally:
                try:
                    await connection.close(timeout=2)
                except BaseException:
                    connection.terminate()
                    raise
        else:
            if self.engine is None:
                raise RoutingUnavailable()
            async with self.engine.begin() as connection:
                yield await _SqlAlchemyData.bind(connection)

    async def execute(self, operation, body):
        command = make_routing_command(operation, body)
        url = settings.inference_routing_url
        try:
            if url:
                if secret_source(
                    settings.inference_routing_database_url,
                    settings.inference_routing_database_url_file,
                ):
                    raise RoutingUnavailable()
                client = RoutingHttpClient(
                    url,
                    secret_source(
                        settings.inference_routing_token,
                        settings.inference_routing_token_file,
                    ),
                    timeout_seconds=settings.inference_routing_timeout_seconds,
                )
                reply = await client.execute(command)
            else:
                from aap_inference.routing import RoutingManager
                from aap_inference.routing_backend_control import (
                    execute_backend_control,
                )

                manager = RoutingManager(
                    self.data_transaction,
                    health_max_age_seconds=settings.ml_backend_router_health_max_age_seconds,
                    lease_ttl_ms=settings.ml_backend_router_lease_ttl_seconds * 1000,
                    backend_control=execute_backend_control,
                )
                try:
                    reply = await manager.execute(command)
                except RoutingUnavailable:
                    # Only a receipt read may recover an ambiguous commit.
                    reply = await manager.status(make_routing_status_request(command))
                else:
                    if reply.state != "committed":
                        reply = await manager.status(
                            make_routing_status_request(command)
                        )
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
        from aap_inference_contract.routing import (
            make_routing_runtime_request,
            decode_routing_runtime_result,
        )

        request = make_routing_runtime_request(registry_ids)
        try:
            url = settings.inference_routing_url
            if url:
                if secret_source(
                    settings.inference_routing_database_url,
                    settings.inference_routing_database_url_file,
                ):
                    raise RoutingUnavailable()
                client = RoutingHttpClient(
                    url,
                    secret_source(
                        settings.inference_routing_token,
                        settings.inference_routing_token_file,
                    ),
                    timeout_seconds=settings.inference_routing_timeout_seconds,
                )
                reply = await client.runtime(request)
            else:
                from aap_inference.routing import RoutingManager

                manager = RoutingManager(
                    self.data_transaction,
                    health_max_age_seconds=settings.ml_backend_router_health_max_age_seconds,
                    lease_ttl_ms=settings.ml_backend_router_lease_ttl_seconds * 1000,
                )
                reply = await manager.runtime(request)
            return decode_routing_runtime_result(request, reply)
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
            url = settings.inference_routing_url
            if url:
                if secret_source(
                    settings.inference_routing_database_url,
                    settings.inference_routing_database_url_file,
                ):
                    raise ScopeRetirementUnavailable()
                client = RoutingHttpClient(
                    url,
                    secret_source(
                        settings.inference_routing_token,
                        settings.inference_routing_token_file,
                    ),
                    timeout_seconds=settings.inference_routing_timeout_seconds,
                )
                reply = (
                    await client.scope_retirement_status(typed_request)
                    if status
                    else await client.retire_scope(typed_request)
                )
            else:
                from aap_inference.scope_retirement_manager import (
                    ScopeRetirementManager,
                )
                from aap_inference.scope_retirement_transport import (
                    execute_backend_scope_retirement,
                )

                manager = ScopeRetirementManager(
                    self.data_transaction,
                    backend_retirement=execute_backend_scope_retirement,
                )
                reply = (
                    await manager.status(typed_request)
                    if status
                    else await manager.execute(typed_request)
                )
            return decode_scope_retirement_result(typed_request, reply)
        except (ValueError, OSError, ScopeRetirementUnavailable):
            raise RoutingUnavailable() from None

    async def video_execution_status(self, proof):
        """Read a proof-bound video execution status from its configured owner."""
        try:
            typed_proof = decode_video_execution_status_request(proof)
            url = settings.inference_routing_url
            if url:
                if secret_source(
                    settings.inference_routing_database_url,
                    settings.inference_routing_database_url_file,
                ):
                    raise RoutingUnavailable()
                client = RoutingHttpClient(
                    url,
                    secret_source(
                        settings.inference_routing_token,
                        settings.inference_routing_token_file,
                    ),
                    timeout_seconds=settings.inference_routing_timeout_seconds,
                )
                return await client.video_execution_status(typed_proof)

            from aap_inference.routing import RoutingManager

            manager = RoutingManager(
                self.data_transaction,
                health_max_age_seconds=settings.ml_backend_router_health_max_age_seconds,
                lease_ttl_ms=settings.ml_backend_router_lease_ttl_seconds * 1000,
            )
            data = await manager.video_execution_status(typed_proof)
            encoded = video_execution_status_result_bytes(typed_proof, data)
            return decode_video_execution_status_result(typed_proof, encoded)
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


@asynccontextmanager
async def lifecycle_control(db, registry_id, kind, *, expected_topology=None):
    from dataclasses import asdict
    from .contracts import quiescence_error, MLBackendQuiescenceError

    authority = RoutingAuthority(db.bind)
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
    try:
        async with control_binding(authority, control):
            yield authority
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


class _RequestGrant:
    def __init__(self, authority, parent, attempt_id, headers, *, control=False):
        self.authority, self.parent, self.attempt_id = authority, parent, attempt_id
        self.headers, self.control = headers, control

    async def _settle(self, kind, status_code=None, reason=None):
        key = "control_id" if self.control else "scope_id"
        parent_id = self.parent.control_id if self.control else self.parent.id
        await self.authority.execute(
            "settle_control" if self.control else "settle",
            {
                key: str(parent_id),
                "capability": self.parent.capability,
                "attempt_id": str(self.attempt_id),
                "kind": kind,
                "status_code": status_code,
                "reason": reason,
            },
        )

    async def response(self, status_code):
        await self._settle("response", status_code=status_code)

    async def uncertain(self, reason):
        await self._settle("uncertain", reason=reason)


class _ReadGrant:
    headers = {}

    async def response(self, _status_code):
        pass

    async def uncertain(self, _reason):
        pass


def backend_request_context(backend, identity, *, authority=None, session_factory=None):
    session = (
        async_object_session(backend)
        if inspect(backend, raiseerr=False) is not None
        else None
    )
    bound_authority = authority or RoutingAuthority(session.bind if session else None)
    registry_id = backend.id

    @asynccontextmanager
    async def request_context(request):
        from aap_inference.request_tracking import GPU_UNLOAD_ROUTE_EXTENSION

        if request.method in {"GET", "HEAD", "OPTIONS"}:
            yield _ReadGrant()
            return
        source_binding = _SCOPE.get()
        source_control = _CONTROL.get()
        gpu_unload = request.extensions.get(GPU_UNLOAD_ROUTE_EXTENSION) is True
        if gpu_unload and (request.method != "POST" or request.url.path != "/unload"):
            raise RoutingUnavailable()
        binding = (
            source_binding
            if source_binding
            and source_binding.scope.registry_id == registry_id
            and not gpu_unload
            else None
        )
        control = (
            source_control
            if source_control
            and source_control.control.registry_id == registry_id
            and not gpu_unload
            else None
        )
        owned_scope = None
        current_authority = (
            source_control.authority
            if source_control
            else source_binding.authority
            if source_binding
            else bound_authority
        )
        if (
            authority is None
            and source_control is None
            and source_binding is None
            and bound_authority.engine is None
            and session_factory is not None
        ):
            # Detached control rows use their explicit session source. This
            # source session closes before control transactions or backend I/O.
            async with session_factory() as source_session:
                current_authority = RoutingAuthority(source_session.bind)
        parent = control.control if control else binding.scope if binding else None
        if parent is None:
            owned_scope = parent = await current_authority.acquire(
                registry_id=registry_id,
                operation="gpu:unload" if gpu_unload else "backend_http",
            )
        if parent.registry_id != registry_id:
            raise RoutingUnavailable()
        parent_id = parent.control_id if control else parent.id
        attempt_id = uuid.uuid4()
        try:
            # The short subject issuer remains live only until begin authenticates
            # the actual constructed transport, then closes before backend I/O.
            async with current_authority.data_transaction() as data:
                captured = await data.step(
                    "capture_transport_subject",
                    {
                        "parent_kind": "control" if control else "scope",
                        "parent_id": str(parent_id),
                        "capability": parent.capability,
                        "attempt_id": str(attempt_id),
                        "transport_identity": identity,
                    },
                )
                if (
                    captured.get("schema") != ROUTING_DATA_PROTOCOL
                    or captured.get("status") != "ok"
                    or set(captured) != {"schema", "status", "subject"}
                ):
                    raise RoutingUnavailable()
                begun = await current_authority.execute(
                    "begin_control" if control else "begin",
                    {
                        "control_id" if control else "scope_id": str(parent_id),
                        "capability": parent.capability,
                        "attempt_id": str(attempt_id),
                        "method": request.method,
                        "path": request.url.path,
                        "transport_subject": captured["subject"],
                    },
                )
            yield _RequestGrant(
                current_authority,
                parent,
                attempt_id,
                begun.headers,
                control=bool(control),
            )
        finally:
            if owned_scope is not None:
                await current_authority.close(owned_scope)

    @asynccontextmanager
    async def mapped_request_context(request):
        from .contracts import RejectionReason, RoutingError

        try:
            async with request_context(request) as grant:
                yield grant
        except RoutingRejected as exc:
            reason = {
                "pool_not_enabled": RejectionReason.POOL_NOT_ENABLED,
                "pool_saturated": RejectionReason.POOL_SATURATED,
                "generation_mismatch": RejectionReason.GENERATION_MISMATCH,
            }.get(exc.reason, RejectionReason.POOL_UNAVAILABLE)
            raise RoutingError(reason, "backend execution admission rejected") from None
        except RoutingUnavailable:
            raise RoutingError(
                RejectionReason.ROUTER_UNAVAILABLE, "durable routing owner unavailable"
            ) from None
        except Exception:
            raise RoutingError(
                RejectionReason.ROUTER_UNAVAILABLE, "durable routing owner unavailable"
            ) from None

    return mapped_request_context
