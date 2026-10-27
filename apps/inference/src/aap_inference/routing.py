"""Portable orchestration for durable backend route and control ownership."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
import json
from typing import Any, Protocol
import uuid

from aap_inference_contract.routing import (
    ROUTING_DATA_PROTOCOL,
    ROUTING_RESULT_PROTOCOL,
    ROUTING_RUNTIME_RESULT_PROTOCOL,
    RoutingCommand,
    RoutingResult,
    RoutingRuntimeRequest,
    RoutingRuntimeResult,
    RoutingStatusRequest,
    RoutingUnavailable,
    decode_routing_command,
    decode_routing_result,
    decode_routing_runtime_request,
    decode_routing_runtime_result,
    decode_routing_status_request,
    decode_routing_status_result,
    make_routing_status_request,
    routing_command_sha256,
    routing_command_bytes,
)
from aap_inference_contract.video_execution import (
    VideoScopeRejected,
    decode_video_scope_data,
    decode_video_execution_status_request,
)

from .health_data_postgres import (
    BackendHealthRow,
    HealthPoolMemberRow,
    HealthServicePoolRow,
    _BACKEND_FIELDS,
    _MEMBER_FIELDS,
    _POOL_FIELDS,
    _decode_backend as _decode_health_backend,
    _decode_member as _decode_health_member,
    _decode_pool as _decode_health_pool,
    _record_values,
)
from .pool_capability import capability_fingerprint


class RoutingTransaction(Protocol):
    async def step(
        self, action: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]: ...


RoutingDataFactory = Callable[[], AbstractAsyncContextManager[RoutingTransaction]]
BackendControl = Callable[[Mapping[str, Any]], Awaitable[object]]


class TransportSubjectOpener(Protocol):
    async def __call__(
        self,
        data: RoutingTransaction,
        subject: str,
        parent_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        control: bool,
    ) -> Mapping[str, Any]: ...


_NATIVE_REPLY_KEYS = frozenset({"schema", "status"})
_MANAGED_CONTROL_PATHS = frozenset({"/unload", "/lifecycle/unload", "/reload"})
_CONTROL_FIELDS = {"control_id", "registry_id", "kind", "phase", "capability"}
_FINISHED_CONTROL_FIELDS = {"control_id", "registry_id", "kind", "phase"}
# The Native admission snapshot serializes the whole ``ml_backend_registry`` row
# (``to_jsonb(registry)``), which includes the database-managed auth_revision in
# addition to the shared health-row columns.
_ROUTING_BACKEND_FIELDS = (*_BACKEND_FIELDS, "auth_revision")
_EPOCH_FIELDS = {
    "control_id",
    "registry_id",
    "phase",
    "authority_epoch",
    "backend_incarnation",
}


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _RetryAdmission(Exception):
    """Known pre-dispatch contention; leave the transaction before waiting."""


def _json_object(value: Any) -> dict[str, Any]:
    try:
        if type(value) is str:
            parsed = json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        elif isinstance(value, Mapping):
            parsed = dict(value)
        else:
            raise RoutingUnavailable()
    except RoutingUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise RoutingUnavailable() from None
    if type(parsed) is not dict:
        raise RoutingUnavailable()
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate routing field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _exact_mapping(value: Any, expected_keys: set[str]) -> dict[str, Any]:
    result = _json_object(value)
    if set(result) != expected_keys:
        raise RoutingUnavailable()
    return result


def _canonical_uuid(value: Any) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    if type(value) is not str:
        raise RoutingUnavailable()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise RoutingUnavailable() from None
    if str(parsed) != value:
        raise RoutingUnavailable()
    return parsed


async def _native_step(
    data: RoutingTransaction,
    action: str,
    arguments: Mapping[str, Any],
    *,
    keys: set[str],
) -> dict[str, Any]:
    reply = _json_object(await data.step(action, arguments))
    if (
        reply.get("schema") != ROUTING_DATA_PROTOCOL
        or reply.get("status") not in {"ok", "rejected"}
        or not _NATIVE_REPLY_KEYS.issubset(reply)
    ):
        raise RoutingUnavailable()
    payload = {
        key: value for key, value in reply.items() if key not in _NATIVE_REPLY_KEYS
    }
    if reply["status"] == "rejected":
        reason = payload.get("reason")
        if type(reason) is not str or set(payload) not in (
            {"reason"},
            {"reason", "control_id"},
        ):
            raise RoutingUnavailable()
        if set(payload) == {"reason", "control_id"}:
            if reason != "management_busy":
                raise RoutingUnavailable()
            _canonical_uuid(payload["control_id"])
        raise _Rejected(reason)
    if set(payload) != keys:
        raise RoutingUnavailable()
    return payload


def _request_body(command: RoutingCommand) -> dict[str, Any]:
    body = command.body
    if not hasattr(body, "model_dump"):
        raise RoutingUnavailable()
    try:
        value = body.model_dump(mode="json", by_alias=True)
    except Exception:
        raise RoutingUnavailable() from None
    if type(value) is not dict:
        raise RoutingUnavailable()
    return value


def _public_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if type(value) is dict:
        return {key: _public_json(child) for key, child in value.items()}
    if type(value) is list:
        return [_public_json(child) for child in value]
    if type(value) is uuid.UUID:
        return str(value)
    return value


def _rejection(reason: str) -> dict[str, str]:
    return {"kind": "rejected", "reason": reason}


class RoutingManager:
    """Portable route authority over an operation-scoped Native data transaction."""

    def __init__(
        self,
        data_factory: RoutingDataFactory,
        *,
        health_max_age_seconds: int,
        lease_ttl_ms: int,
        backend_control: BackendControl | None = None,
        transport_subject_opener: TransportSubjectOpener | None = None,
    ) -> None:
        if (
            not callable(data_factory)
            or type(health_max_age_seconds) is not int
            or health_max_age_seconds <= 0
            or type(lease_ttl_ms) is not int
            or lease_ttl_ms <= 0
            or (backend_control is not None and not callable(backend_control))
            or (
                transport_subject_opener is not None
                and not callable(transport_subject_opener)
            )
        ):
            raise RoutingUnavailable()
        self._data_factory = data_factory
        self.health_max_age_seconds = health_max_age_seconds
        self.lease_ttl_ms = lease_ttl_ms
        self.backend_control = backend_control
        self.transport_subject_opener = transport_subject_opener

    async def readiness(self) -> bool:
        try:
            async with self._data_factory():
                return True
        except Exception:
            return False

    async def execute(
        self, command: RoutingCommand | dict | bytes | str
    ) -> RoutingResult:
        request = decode_routing_command(command)
        for attempt in range(5):
            try:
                return await self._execute_once(request, retry_contention=attempt < 4)
            except _RetryAdmission:
                await asyncio.sleep(0.01 * (attempt + 1))
        raise RoutingUnavailable()

    async def _execute_once(
        self, command: RoutingCommand, *, retry_contention: bool
    ) -> RoutingResult:
        request = decode_routing_command(command)
        digest = routing_command_sha256(request)
        if request.operation == "reconcile":
            return await self._execute_reconcile(request, digest)
        body_completed = False
        result_body: dict[str, Any] | None = None
        try:
            async with self._data_factory() as data:
                if request.operation == "video_acquire":
                    # Current claim validation precedes any successful receipt
                    # replay. Stable scope identity survives contention retries.
                    try:
                        await self._prepare_video_acquire(data, request, digest)
                    except VideoScopeRejected as rejected:
                        if retry_contention and rejected.reason in {
                            "execution_busy",
                            "management_busy",
                        }:
                            raise _RetryAdmission() from None
                        raise RoutingUnavailable() from None
                prior = await self._command_status(data, request, digest)
                if prior is not None:
                    result_body = prior
                else:
                    try:
                        result_body = await self._execute_action(data, request, digest)
                    except _Rejected as rejected:
                        if (
                            retry_contention
                            and rejected.reason == "management_busy"
                            and request.operation
                            in {"acquire", "video_acquire", "begin", "heartbeat"}
                        ):
                            raise _RetryAdmission() from None
                        result_body = _rejection(rejected.reason)
                    self._result(request, digest, "committed", result_body)
                    await self._record_command(data, request, digest, result_body)
                body_completed = True
        except (_RetryAdmission, asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if body_completed:
                return self._unknown(make_routing_status_request(request))
            if isinstance(exc, RoutingUnavailable):
                raise
            raise RoutingUnavailable() from None
        assert result_body is not None
        return self._result(request, digest, "committed", result_body)

    async def status(
        self, request: RoutingStatusRequest | dict | bytes | str
    ) -> RoutingResult:
        status_request = decode_routing_status_request(request)
        try:
            async with self._data_factory() as data:
                status = await _native_step(
                    data,
                    "command_status",
                    {
                        "request_id": str(status_request.request_id),
                        "command_sha256": status_request.command_sha256,
                    },
                    keys={"receipt"},
                )
                receipt = status["receipt"]
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RoutingUnavailable() from None
        if receipt is None:
            return self._unknown(status_request)
        parsed = self._receipt(
            receipt,
            request_id=status_request.request_id,
            command_sha256=status_request.command_sha256,
            operation=status_request.operation,
        )
        if parsed["state"] == "unknown":
            return self._unknown(status_request)
        return self._status_result(status_request, parsed)

    async def runtime(
        self, request: RoutingRuntimeRequest | dict | bytes | str
    ) -> RoutingRuntimeResult:
        runtime_request = decode_routing_runtime_request(request)
        try:
            async with self._data_factory() as data:
                facts = await _native_step(
                    data,
                    "runtime_snapshot",
                    {
                        "registry_ids": [
                            str(registry_id)
                            for registry_id in runtime_request.registry_ids
                        ]
                    },
                    keys={"observed_at", "registries"},
                )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RoutingUnavailable() from None
        try:
            return decode_routing_runtime_result(
                runtime_request,
                {
                    "schema": ROUTING_RUNTIME_RESULT_PROTOCOL,
                    "request_id": str(runtime_request.request_id),
                    "observed_at": _timestamp(facts["observed_at"]).isoformat(),
                    "registries": facts["registries"],
                },
            )
        except (ValueError, TypeError, OverflowError):
            raise RoutingUnavailable() from None

    async def close(self) -> None:
        """The manager owns no pool or connection; transactions are operation-scoped."""

    async def _execute_action(
        self,
        data: RoutingTransaction,
        request: RoutingCommand,
        digest: str,
    ) -> dict[str, Any]:
        body = _request_body(request)
        operation = request.operation
        if operation in {"acquire", "video_acquire"}:
            return await self._acquire(data, body, digest, request=request)
        if operation == "begin":
            return await self._begin(data, body, control=False)
        if operation == "settle":
            return await self._settle(data, body, control=False)
        if operation == "heartbeat":
            return await self._heartbeat(data, body)
        if operation == "close":
            return await self._close_scope(data, body)
        if operation == "scope_status":
            return await self._scope_status(data, body)
        if operation == "prepare_control":
            return await self._prepare_control(data, body)
        if operation == "finish_control":
            return await self._finish_control(data, body)
        if operation == "begin_control":
            return await self._begin(data, body, control=True)
        if operation == "settle_control":
            return await self._settle(data, body, control=True)
        raise RoutingUnavailable()

    async def _acquire(
        self,
        data: RoutingTransaction,
        body: dict[str, Any],
        digest: str,
        *,
        request: RoutingCommand | None = None,
    ) -> dict[str, Any]:
        if body["operation"].startswith("control:"):
            raise _Rejected("invalid_command")
        snapshot_result = await _native_step(
            data,
            "admission_snapshot",
            {
                "pool_id": body["pool_id"],
                "registry_id": body["registry_id"],
                "project_id": body["project_id"],
                "mode": body["mode"],
                "required_instance_id": body["required_instance_id"],
                "health_max_age_seconds": self.health_max_age_seconds,
            },
            keys={"snapshot", "pool", "members", "observed_at"},
        )
        snapshot = snapshot_result["snapshot"]
        if type(snapshot) is not str or not snapshot:
            raise RoutingUnavailable()
        pool_raw = snapshot_result["pool"]
        pool = None if pool_raw is None else _decode_routing_pool(pool_raw)
        observed_at = _timestamp(snapshot_result["observed_at"])
        members_raw = snapshot_result["members"]
        if type(members_raw) is not list:
            raise RoutingUnavailable()
        members: list[tuple[HealthPoolMemberRow, BackendHealthRow, int]] = []
        for item in members_raw:
            item_value = _exact_mapping(item, {"member", "registry", "max_concurrency"})
            member_raw = item_value["member"]
            registry_raw = item_value["registry"]
            if member_raw is None or registry_raw is None:
                continue
            member = _decode_routing_member(member_raw)
            registry = _decode_routing_backend(registry_raw)
            max_concurrency = item_value["max_concurrency"]
            if type(max_concurrency) is not int or max_concurrency <= 0:
                raise RoutingUnavailable()
            if member.registry_id != registry.id:
                raise RoutingUnavailable()
            members.append((member, registry, max_concurrency))

        candidate_ids = _eligible_registry_ids(
            body,
            pool,
            members,
            observed_at=observed_at,
            health_max_age_seconds=self.health_max_age_seconds,
        )
        reservation = {
            "snapshot": snapshot,
            "candidate_ids": [str(value) for value in candidate_ids],
            "scope_id": str(self._video_scope_id(request))
            if request is not None and request.operation == "video_acquire"
            else str(uuid.uuid4()),
            "command_sha256": digest,
            "owner": body["owner"],
            "operation": body["operation"],
            "ttl_ms": self.lease_ttl_ms,
        }
        if request is not None and request.operation == "video_acquire":
            try:
                reserved = await self._video_step(
                    data,
                    "reserve",
                    {
                        **body["execution"],
                        "request_id": str(request.request_id),
                        **{
                            key: value
                            for key, value in reservation.items()
                            if key not in {"owner", "operation"}
                        },
                    },
                )
            except VideoScopeRejected as rejected:
                if rejected.reason in {"execution_busy", "management_busy"}:
                    raise _Rejected("management_busy") from None
                raise _Rejected("scope_unavailable") from None
            return _public_json(reserved["scope"])
        reserved = await _native_step(
            data,
            "reserve_scope",
            reservation,
            keys={"scope"},
        )
        return _json_object(reserved["scope"])

    @staticmethod
    def _video_scope_id(request: RoutingCommand) -> uuid.UUID:
        return uuid.uuid5(request.request_id, "aap-video-execution-scope/v1")

    async def _prepare_video_acquire(self, data, request, digest):
        return await self._video_step(
            data,
            "prepare",
            {
                **_request_body(request)["execution"],
                "request_id": str(request.request_id),
                "command": json.loads(routing_command_bytes(request)),
                "command_sha256": digest,
                "scope_id": str(self._video_scope_id(request)),
            },
        )

    @staticmethod
    async def _video_step(data, action, arguments):
        method = getattr(data, "video_execution_scope_step", None)
        if not callable(method):
            raise RoutingUnavailable()
        reply = await method(action, arguments)
        return decode_video_scope_data(action, arguments, reply)

    async def video_execution_status(self, proof):
        """Read the current durable video intent and scope for an exact claim."""
        typed_proof = decode_video_execution_status_request(proof)
        arguments = typed_proof.model_dump(mode="json")
        try:
            async with self._data_factory() as data:
                method = getattr(data, "video_execution_scope_step", None)
                if not callable(method):
                    raise RoutingUnavailable()
                reply = await method("status", arguments)
                return decode_video_scope_data("status", arguments, reply)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RoutingUnavailable() from None

    async def _open_transport_subject(
        self,
        data: RoutingTransaction,
        subject: str,
        parent_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        control: bool,
    ) -> dict[str, str]:
        if self.transport_subject_opener is not None:
            identity = await self.transport_subject_opener(
                data, subject, parent_id, attempt_id, control=control
            )
            result = _exact_mapping(identity, {"identity_sha256"})
        else:
            opened = await _native_step(
                data,
                "open_transport_subject",
                {
                    "parent_kind": "control" if control else "scope",
                    "parent_id": str(parent_id),
                    "attempt_id": str(attempt_id),
                    "subject": subject,
                },
                keys={"transport_identity"},
            )
            result = _exact_mapping(opened["transport_identity"], {"identity_sha256"})
        digest = result["identity_sha256"]
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RoutingUnavailable()
        return {"identity_sha256": digest}

    async def _begin(
        self,
        data: RoutingTransaction,
        body: dict[str, Any],
        *,
        control: bool,
    ) -> dict[str, Any]:
        parent_id = _canonical_uuid(body["control_id" if control else "scope_id"])
        attempt_id = _canonical_uuid(body["attempt_id"])
        method = body["method"]
        path = body["path"]
        if control and (method != "POST" or path not in _MANAGED_CONTROL_PATHS):
            raise _Rejected("invalid_command")
        transport_identity = await self._open_transport_subject(
            data,
            body["transport_subject"],
            parent_id,
            attempt_id,
            control=control,
        )
        if control:
            result = await _native_step(
                data,
                "begin_control_attempt",
                {
                    "control_id": str(parent_id),
                    "capability": body["capability"],
                    "attempt_id": str(attempt_id),
                    "method": method,
                    "path": path,
                    "transport_identity": transport_identity,
                },
                keys={"attempt_id", "control", "headers"},
            )
            return {
                "attempt_id": result["attempt_id"],
                "control": _json_object(result["control"]),
                "headers": _json_object(result["headers"]),
            }
        result = await _native_step(
            data,
            "begin_attempt",
            {
                "scope_id": str(parent_id),
                "capability": body["capability"],
                "attempt_id": str(attempt_id),
                "method": method,
                "path": path,
                "transport_identity": transport_identity,
            },
            keys={"attempt_id", "scope", "headers"},
        )
        return {
            "attempt_id": result["attempt_id"],
            "scope": _json_object(result["scope"]),
            "headers": _json_object(result["headers"]),
        }

    async def _settle(
        self,
        data: RoutingTransaction,
        body: dict[str, Any],
        *,
        control: bool,
    ) -> dict[str, Any]:
        if control:
            result = await _native_step(
                data,
                "settle_control_attempt",
                {
                    "control_id": body["control_id"],
                    "capability": body["capability"],
                    "attempt_id": body["attempt_id"],
                    "kind": body["kind"],
                    "status_code": body["status_code"],
                    "reason": body["reason"],
                },
                keys={"attempt"},
            )
        else:
            result = await _native_step(
                data,
                "settle_attempt",
                {
                    "scope_id": body["scope_id"],
                    "capability": body["capability"],
                    "attempt_id": body["attempt_id"],
                    "kind": body["kind"],
                    "status_code": body["status_code"],
                    "reason": body["reason"],
                },
                keys={"attempt"},
            )
        return _attempt_view(result["attempt"])

    async def _heartbeat(
        self, data: RoutingTransaction, body: dict[str, Any]
    ) -> dict[str, Any]:
        result = await _native_step(
            data,
            "heartbeat_scope",
            {
                "scope_id": body["scope_id"],
                "capability": body["capability"],
                "ttl_ms": self.lease_ttl_ms,
            },
            keys={"scope"},
        )
        return _json_object(result["scope"])

    async def _close_scope(
        self, data: RoutingTransaction, body: dict[str, Any]
    ) -> dict[str, Any]:
        result = await _native_step(
            data,
            "close_scope",
            {
                "scope_id": body["scope_id"],
                "capability": body["capability"],
                "outcome": body["outcome"],
            },
            keys={"scope"},
        )
        return _json_object(result["scope"])

    async def _scope_status(
        self, data: RoutingTransaction, body: dict[str, Any]
    ) -> dict[str, Any]:
        result = await _native_step(
            data,
            "scope_status",
            {"scope_id": body["scope_id"], "capability": body["capability"]},
            keys={"scope", "attempts"},
        )
        if type(result["attempts"]) is not list:
            raise RoutingUnavailable()
        return {
            "scope": _json_object(result["scope"]),
            "attempts": [_attempt_view(item) for item in result["attempts"]],
        }

    async def _prepare_control(
        self, data: RoutingTransaction, body: dict[str, Any]
    ) -> dict[str, Any]:
        # Native rechecks and takes the persistent control hold in the same XID.
        # This explicit read preserves the API's pre-control topology error phase.
        await _native_step(
            data,
            "quiescence",
            {"registry_id": body["registry_id"]},
            keys={
                "topology",
                "active_scopes",
                "uncertain_scopes",
                "continuity",
                "control_id",
            },
        )
        result = await _native_step(
            data,
            "prepare_control",
            {
                "control_id": body["control_id"],
                "registry_id": body["registry_id"],
                "kind": body["kind"],
                "expected_topology": body["expected_topology"],
            },
            keys=_CONTROL_FIELDS,
        )
        return result

    async def _finish_control(
        self, data: RoutingTransaction, body: dict[str, Any]
    ) -> dict[str, Any]:
        result = await _native_step(
            data,
            "finish_control",
            {"control_id": body["control_id"], "capability": body["capability"]},
            keys=_FINISHED_CONTROL_FIELDS,
        )
        return {**result, "capability": body["capability"]}

    async def _execute_reconcile(
        self, request: RoutingCommand, digest: str
    ) -> RoutingResult:
        if self.backend_control is None:
            raise RoutingUnavailable()
        body = _request_body(request)
        prepared: dict[str, Any] | None = None
        early_result: dict[str, Any] | None = None
        body_completed = False
        try:
            async with self._data_factory() as data:
                prior = await self._command_status(data, request, digest)
                if prior is not None:
                    return self._result(request, digest, "committed", prior)
                try:
                    prepared_result = await _native_step(
                        data,
                        "prepare_epoch",
                        {
                            "control_id": body["control_id"],
                            "registry_id": body["registry_id"],
                        },
                        keys={
                            "control_id",
                            "registry_id",
                            "phase",
                            "new_epoch",
                            "challenge",
                            "retired_epoch",
                            "backend_incarnation",
                            "endpoint",
                            "signing_key",
                        },
                    )
                    prepared = prepared_result
                except _Rejected as rejected:
                    early_result = _rejection(rejected.reason)
                    self._result(request, digest, "committed", early_result)
                    await self._record_command(data, request, digest, early_result)
                body_completed = True
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            if body_completed:
                return self._unknown(make_routing_status_request(request))
            raise RoutingUnavailable() from None
        if early_result is not None:
            return self._result(request, digest, "committed", early_result)
        assert prepared is not None
        private_context = self._validate_private_epoch(prepared, body)
        if private_context["phase"] == "completed":
            return await self._record_reconcile_progress(
                request,
                digest,
                {
                    "control_id": private_context["control_id"],
                    "registry_id": private_context["registry_id"],
                    "phase": "completed",
                    "authority_epoch": private_context["new_epoch"],
                    "backend_incarnation": private_context["backend_incarnation"],
                },
            )
        try:
            raw_reply = await self.backend_control(private_context)
            reply = _decode_backend_control_reply(raw_reply)
            self._bind_backend_control_reply(reply, private_context)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # The persistent hold remains. A new command ID may poll and resume it.
            return self._unknown(make_routing_status_request(request))

        if reply.phase != "bound" or reply.active_requests != 0:
            progress = {
                "control_id": str(private_context["control_id"]),
                "registry_id": str(private_context["registry_id"]),
                "phase": reply.phase,
                "authority_epoch": str(reply.authority_epoch),
                "backend_incarnation": str(reply.backend_incarnation),
            }
            return await self._record_reconcile_progress(request, digest, progress)

        body_completed = False
        final_body: dict[str, Any] | None = None
        try:
            async with self._data_factory() as data:
                prior = await self._command_status(data, request, digest)
                if prior is not None:
                    return self._result(request, digest, "committed", prior)
                try:
                    finished = await _native_step(
                        data,
                        "finish_epoch",
                        {
                            "control_id": str(private_context["control_id"]),
                            "reply": reply.as_dict(),
                        },
                        keys=_EPOCH_FIELDS,
                    )
                    final_body = finished
                except _Rejected as rejected:
                    final_body = _rejection(rejected.reason)
                self._result(request, digest, "committed", final_body)
                await self._record_command(data, request, digest, final_body)
                body_completed = True
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            if body_completed:
                return self._unknown(make_routing_status_request(request))
            raise RoutingUnavailable() from None
        assert final_body is not None
        return self._result(request, digest, "committed", final_body)

    async def _record_reconcile_progress(
        self,
        request: RoutingCommand,
        digest: str,
        result_body: dict[str, Any],
    ) -> RoutingResult:
        body_completed = False
        try:
            async with self._data_factory() as data:
                prior = await self._command_status(data, request, digest)
                if prior is not None:
                    return self._result(request, digest, "committed", prior)
                self._result(request, digest, "committed", result_body)
                await self._record_command(data, request, digest, result_body)
                body_completed = True
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            if body_completed:
                return self._unknown(make_routing_status_request(request))
            raise RoutingUnavailable() from None
        return self._result(request, digest, "committed", result_body)

    def _validate_private_epoch(
        self, value: Mapping[str, Any], body: Mapping[str, Any]
    ) -> dict[str, Any]:
        fields = _exact_mapping(
            value,
            {
                "control_id",
                "registry_id",
                "phase",
                "new_epoch",
                "challenge",
                "retired_epoch",
                "backend_incarnation",
                "endpoint",
                "signing_key",
            },
        )
        if (
            _canonical_uuid(fields["control_id"]) != _canonical_uuid(body["control_id"])
            or _canonical_uuid(fields["registry_id"])
            != _canonical_uuid(body["registry_id"])
            or fields["phase"]
            not in {
                "prepared",
                "draining",
                "drained",
                "bound",
                "uncertain",
                "completed",
            }
            or type(fields["challenge"]) is not str
            or len(fields["challenge"]) != 64
            or any(
                character not in "0123456789abcdef" for character in fields["challenge"]
            )
            or type(fields["signing_key"]) is not str
        ):
            raise RoutingUnavailable()
        _canonical_uuid(fields["new_epoch"])
        if fields["retired_epoch"] is not None:
            _canonical_uuid(fields["retired_epoch"])
        if fields["backend_incarnation"] is not None:
            _canonical_uuid(fields["backend_incarnation"])
        encoded_key = fields["signing_key"]
        if not encoded_key or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in encoded_key
        ):
            raise RoutingUnavailable()
        try:
            key_bytes = base64.b64decode(
                encoded_key + "=" * ((4 - len(encoded_key) % 4) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, TypeError):
            raise RoutingUnavailable() from None
        if (
            len(key_bytes) != 32
            or base64.urlsafe_b64encode(key_bytes).rstrip(b"=").decode("ascii")
            != encoded_key
        ):
            raise RoutingUnavailable()
        endpoint = _exact_mapping(
            fields["endpoint"], {"url", "auth_method", "auth_token"}
        )
        if (
            type(endpoint["url"]) is not str
            or not endpoint["url"]
            or endpoint["auth_method"] is not None
            and type(endpoint["auth_method"]) is not str
            or endpoint["auth_token"] is not None
            and type(endpoint["auth_token"]) is not str
        ):
            raise RoutingUnavailable()
        return fields

    @staticmethod
    def _bind_backend_control_reply(
        reply: Any, private_context: Mapping[str, Any]
    ) -> None:
        if (
            str(reply.command_id) != private_context["control_id"]
            or str(reply.registry_id) != private_context["registry_id"]
            or str(reply.authority_epoch) != private_context["new_epoch"]
            or reply.challenge != private_context["challenge"]
            or (reply.phase == "bound" and reply.active_requests != 0)
        ):
            raise RoutingUnavailable()

    async def _command_status(
        self, data: RoutingTransaction, request: RoutingCommand, digest: str
    ) -> dict[str, Any] | None:
        result = await _native_step(
            data,
            "command_status",
            {
                "request_id": str(request.request_id),
                "command_sha256": digest,
            },
            keys={"receipt"},
        )
        if result["receipt"] is None:
            return None
        receipt = self._receipt(
            result["receipt"],
            request_id=request.request_id,
            command_sha256=digest,
            operation=request.operation,
        )
        if receipt["state"] != "committed":
            raise RoutingUnavailable()
        return receipt["result"]

    async def _record_command(
        self,
        data: RoutingTransaction,
        request: RoutingCommand,
        digest: str,
        result: Mapping[str, Any],
    ) -> None:
        arguments = {
            "request_id": str(request.request_id),
            "command_sha256": digest,
            "result": _public_json(dict(result)),
        }
        if request.operation == "video_acquire":
            recorded = await self._video_step(
                data,
                "record",
                {
                    **_request_body(request)["execution"],
                    **arguments,
                },
            )
        else:
            recorded = await _native_step(
                data,
                "record_command",
                {"operation": request.operation, **arguments},
                keys={"receipt"},
            )
        receipt = self._receipt(
            recorded["receipt"],
            request_id=request.request_id,
            command_sha256=digest,
            operation=request.operation,
        )
        if receipt["result"] != _public_json(dict(result)):
            raise RoutingUnavailable()

    def _receipt(
        self,
        raw: Any,
        *,
        request_id: uuid.UUID,
        command_sha256: str,
        operation: str,
    ) -> dict[str, Any]:
        receipt = _exact_mapping(
            raw,
            {
                "request_id",
                "command_sha256",
                "operation",
                "state",
                "result",
            },
        )
        if (
            _canonical_uuid(receipt["request_id"]) != request_id
            or receipt["command_sha256"] != command_sha256
            or receipt["operation"] != operation
            or receipt["state"] not in {"caller_pending", "committed", "unknown"}
        ):
            raise RoutingUnavailable()
        if receipt["state"] == "unknown":
            if receipt["result"] is not None:
                raise RoutingUnavailable()
        elif type(receipt["result"]) is not dict:
            raise RoutingUnavailable()
        return receipt

    @staticmethod
    def _result(
        request: RoutingCommand,
        digest: str,
        state: str,
        body: Mapping[str, Any],
    ) -> RoutingResult:
        try:
            return decode_routing_result(
                request,
                {
                    "schema": ROUTING_RESULT_PROTOCOL,
                    "request_id": str(request.request_id),
                    "command_sha256": digest,
                    "operation": request.operation,
                    "state": state,
                    "result": _public_json(dict(body)),
                },
            )
        except (ValueError, TypeError, OverflowError):
            raise RoutingUnavailable() from None

    @staticmethod
    def _unknown(request: RoutingStatusRequest) -> RoutingResult:
        return RoutingResult(
            schema=ROUTING_RESULT_PROTOCOL,
            request_id=request.request_id,
            command_sha256=request.command_sha256,
            operation=request.operation,
            state="unknown",
            result=None,
        )

    @staticmethod
    def _status_result(
        request: RoutingStatusRequest, receipt: Mapping[str, Any]
    ) -> RoutingResult:
        try:
            return decode_routing_status_result(
                request,
                {
                    "schema": ROUTING_RESULT_PROTOCOL,
                    "request_id": str(request.request_id),
                    "command_sha256": request.command_sha256,
                    "operation": request.operation,
                    "state": receipt["state"],
                    "result": _public_json(receipt["result"]),
                },
            )
        except (ValueError, TypeError, OverflowError):
            raise RoutingUnavailable() from None


def _decode_backend_control_reply(value: object) -> Any:
    try:
        from aap_protocol_v2.routing import decode_backend_route_control_reply

        return decode_backend_route_control_reply(value)
    except RoutingUnavailable:
        raise
    except Exception:
        raise RoutingUnavailable() from None


def _decode_routing_backend(value: Any) -> BackendHealthRow:
    # The Native admission snapshot serializes the whole registry row, so the
    # routing wire also carries the database-managed ``auth_revision``. Validate
    # it as part of the frozen subject instead of ignoring an unknown column.
    values = _record_values(_json_object(value), _ROUTING_BACKEND_FIELDS)
    auth_revision = values.pop("auth_revision")
    if type(auth_revision) is not int or auth_revision < 0:
        raise RoutingUnavailable()
    values["last_checked_at"] = _timestamp(values["last_checked_at"], nullable=True)
    values["created_at"] = _timestamp(values["created_at"])
    values["updated_at"] = _timestamp(values["updated_at"])
    return _decode_health_backend(values)


def _decode_routing_pool(value: Any) -> HealthServicePoolRow:
    values = _record_values(_json_object(value), _POOL_FIELDS)
    values["created_at"] = _timestamp(values["created_at"])
    values["updated_at"] = _timestamp(values["updated_at"])
    return _decode_health_pool(values)


def _decode_routing_member(value: Any) -> HealthPoolMemberRow:
    values = _record_values(_json_object(value), _MEMBER_FIELDS)
    values["created_at"] = _timestamp(values["created_at"])
    values["updated_at"] = _timestamp(values["updated_at"])
    return _decode_health_member(values)


def _attempt_view(value: Any) -> dict[str, Any]:
    attempt = _exact_mapping(value, {"id", "state", "status_code", "reason"})
    return {
        "attempt_id": str(_canonical_uuid(attempt["id"])),
        "state": attempt["state"],
        "status_code": attempt["status_code"],
        "reason": attempt["reason"],
    }


def _eligible_registry_ids(
    body: Mapping[str, Any],
    pool: HealthServicePoolRow | None,
    members: list[tuple[HealthPoolMemberRow, BackendHealthRow, int]],
    *,
    observed_at: datetime,
    health_max_age_seconds: int,
) -> list[uuid.UUID]:
    mode = body["mode"]
    required_instance = (
        _canonical_uuid(body["required_instance_id"])
        if body["required_instance_id"] is not None
        else None
    )
    registry_id = (
        _canonical_uuid(body["registry_id"])
        if body["registry_id"] is not None
        else None
    )
    if mode == "direct":
        exact_id = required_instance or registry_id
        if exact_id is None:
            raise _Rejected("pool_unavailable")
        return [exact_id]
    if pool is None:
        raise _Rejected("pool_unavailable")

    active_members = [
        (member, backend, max_concurrency)
        for member, backend, max_concurrency in members
        if member.pool_id == pool.id and member.traffic_state == "active"
    ]
    singleton_without_fingerprint = (
        pool.capability_fingerprint is None and len(active_members) == 1
    )
    eligible: list[uuid.UUID] = []
    max_age = timedelta(seconds=health_max_age_seconds)
    for member, backend, _max_concurrency in active_members:
        if required_instance is not None and backend.id != required_instance:
            continue
        if backend.state != "connected":
            continue
        health_fresh = (
            backend.last_checked_at is not None
            and backend.last_checked_at >= observed_at - max_age
        )
        raw_capabilities = (
            (backend.health_meta or {}).get("capabilities")
            if backend.health_meta
            else None
        )
        fingerprint_ok = False
        if pool.capability_fingerprint is not None and raw_capabilities is not None:
            fingerprint_ok = (
                capability_fingerprint(raw_capabilities) == pool.capability_fingerprint
            )
        elif singleton_without_fingerprint and raw_capabilities is not None:
            fingerprint_ok = True
        if health_fresh and fingerprint_ok:
            eligible.append(backend.id)

    if mode in {"off", "observe"}:
        legacy = pool.legacy_instance_id
        if legacy is None:
            raise _Rejected("pool_not_enabled")
        if required_instance is not None:
            if required_instance != legacy or legacy not in eligible:
                raise _Rejected("pool_unavailable")
        return [legacy]
    if mode == "enforce":
        return eligible
    raise RoutingUnavailable()


def _timestamp(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (OverflowError, ValueError):
            raise RoutingUnavailable() from None
    else:
        raise RoutingUnavailable()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RoutingUnavailable()
    return parsed.astimezone(UTC)


__all__ = ["RoutingManager"]
