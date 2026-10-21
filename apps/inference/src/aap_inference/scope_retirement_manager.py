"""Portable coordinator for one immutable backend-scope retirement intent."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
import uuid

from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_ACTIONS,
    SCOPE_RETIREMENT_DATA_PROTOCOL,
    SCOPE_RETIREMENT_NATIVE_FUNCTIONS,
    ScopeRetirementRejected,
    ScopeRetirementRequest,
    ScopeRetirementResult,
    ScopeRetirementUnavailable,
    ScopeRetirementView,
    decode_scope_retirement_request,
)

_CHALLENGE = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT_FIELDS = frozenset(
    {
        "retirement_id",
        "scope_id",
        "registry_id",
        "authority_epoch",
        "backend_incarnation",
        "challenge",
        "fence_until",
        "endpoint",
    }
)
_ENDPOINT_FIELDS = frozenset({"url", "auth_method", "auth_token"})
_RETIREMENT_FIELDS = frozenset(
    {
        "retirement_id",
        "scope_id",
        "registry_id",
        "authority_epoch",
        "backend_incarnation",
        "phase",
        "closed",
    }
)
_DATA_ENVELOPE_FIELDS = frozenset({"schema", "status"})


class ScopeRetirementTransaction(Protocol):
    async def retirement_step(
        self, action: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]: ...


ScopeRetirementDataFactory = Callable[
    [], AbstractAsyncContextManager[ScopeRetirementTransaction]
]
BackendScopeRetirement = Callable[[Mapping[str, Any]], Awaitable[object]]


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate scope-retirement field")
        result[key] = value
    return result


def _object(value: object) -> dict[str, Any]:
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
            raise ScopeRetirementUnavailable()
    except ScopeRetirementUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise ScopeRetirementUnavailable() from None
    if type(parsed) is not dict:
        raise ScopeRetirementUnavailable()
    return parsed


def _canonical_uuid(value: object) -> str:
    if type(value) is not str:
        raise ScopeRetirementUnavailable()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ScopeRetirementUnavailable() from None
    if str(parsed) != value:
        raise ScopeRetirementUnavailable()
    return value


def _native_reply(value: object, *, expected: set[str]) -> dict[str, Any]:
    reply = _object(value)
    if (
        reply.get("schema") != SCOPE_RETIREMENT_DATA_PROTOCOL
        or type(reply.get("status")) is not str
        or reply["status"] not in {"ok", "rejected"}
        or not _DATA_ENVELOPE_FIELDS.issubset(reply)
    ):
        raise ScopeRetirementUnavailable()
    payload = {
        key: child for key, child in reply.items() if key not in _DATA_ENVELOPE_FIELDS
    }
    if reply["status"] == "rejected":
        if set(payload) != {"reason"} or type(payload["reason"]) is not str:
            raise ScopeRetirementUnavailable()
        try:
            ScopeRetirementRejected(reason=payload["reason"])
        except (TypeError, ValueError):
            raise ScopeRetirementUnavailable() from None
        raise _Rejected(payload["reason"])
    if set(payload) != expected:
        raise ScopeRetirementUnavailable()
    return payload


def _view(value: object, request: ScopeRetirementRequest) -> ScopeRetirementView:
    retirement = _object(value)
    if set(retirement) != _RETIREMENT_FIELDS:
        raise ScopeRetirementUnavailable()
    try:
        view = ScopeRetirementView.model_validate_json(
            json.dumps(retirement, allow_nan=False, separators=(",", ":"))
        )
    except Exception:
        raise ScopeRetirementUnavailable() from None
    if (
        view.retirement_id != request.retirement_id
        or view.scope_id != request.scope_id
        or view.closed is not True
    ):
        raise ScopeRetirementUnavailable()
    return view


def _context(value: object, view: ScopeRetirementView) -> dict[str, Any]:
    context = _object(value)
    if set(context) != _CONTEXT_FIELDS:
        raise ScopeRetirementUnavailable()
    for field in (
        "retirement_id",
        "scope_id",
        "registry_id",
        "authority_epoch",
        "backend_incarnation",
    ):
        _canonical_uuid(context[field])
    endpoint = context["endpoint"]
    if not isinstance(endpoint, Mapping) or set(endpoint) != _ENDPOINT_FIELDS:
        raise ScopeRetirementUnavailable()
    if (
        type(context["challenge"]) is not str
        or _CHALLENGE.fullmatch(context["challenge"]) is None
        or type(context["fence_until"]) is not int
        or context["fence_until"] < 0
        or context["retirement_id"] != str(view.retirement_id)
        or context["scope_id"] != str(view.scope_id)
        or context["registry_id"] != str(view.registry_id)
        or context["authority_epoch"] != str(view.authority_epoch)
        or context["backend_incarnation"] != str(view.backend_incarnation)
        or type(endpoint["url"]) is not str
        or not endpoint["url"]
        or type(endpoint["auth_method"]) is not str
        or type(endpoint["auth_token"]) is not str
        or not endpoint["auth_token"]
    ):
        raise ScopeRetirementUnavailable()
    return dict(context)


def _intent(
    payload: Mapping[str, Any],
    request: ScopeRetirementRequest,
    *,
    action: str,
) -> tuple[ScopeRetirementView | None, dict[str, Any] | None]:
    if action not in {"prepare", "finish", "status"}:
        raise ScopeRetirementUnavailable()
    if set(payload) != {"retirement", "context"}:
        raise ScopeRetirementUnavailable()
    raw_view = payload["retirement"]
    context = payload["context"]
    if raw_view is None:
        if context is not None:
            raise ScopeRetirementUnavailable()
        return None, None
    view = _view(raw_view, request)
    if action == "status" or view.phase == "completed":
        if context is not None:
            raise ScopeRetirementUnavailable()
        return view, None
    if context is None:
        raise ScopeRetirementUnavailable()
    return view, _context(context, view)


def _reply_dict(value: object) -> dict[str, Any]:
    try:
        if isinstance(value, Mapping):
            raw = dict(value)
        elif callable(getattr(value, "as_dict", None)):
            raw = value.as_dict()
        else:
            raise ScopeRetirementUnavailable()
        from aap_protocol_v2.scope_retirement import (
            decode_backend_scope_retirement_reply,
        )

        reply = decode_backend_scope_retirement_reply(raw)
        normalized = reply.as_dict()
    except ScopeRetirementUnavailable:
        raise
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise ScopeRetirementUnavailable() from None
    if not isinstance(normalized, Mapping):
        raise ScopeRetirementUnavailable()
    return dict(normalized)


def _bind_reply(value: object, context: Mapping[str, Any]) -> dict[str, Any]:
    reply = _reply_dict(value)
    expected = {
        "schema",
        "retirement_id",
        "registry_id",
        "scope_id",
        "authority_epoch",
        "backend_incarnation",
        "challenge",
        "phase",
        "active_requests",
    }
    if set(reply) != expected:
        raise ScopeRetirementUnavailable()
    for field in (
        "retirement_id",
        "registry_id",
        "scope_id",
        "authority_epoch",
        "backend_incarnation",
    ):
        _canonical_uuid(reply[field])
        if reply[field] != context[field]:
            raise ScopeRetirementUnavailable()
    if (
        reply["challenge"] != context["challenge"]
        or reply["phase"] not in {"draining", "retired"}
        or type(reply["active_requests"]) is not int
        or reply["active_requests"] < 0
        or (reply["phase"] == "retired" and reply["active_requests"] != 0)
    ):
        raise ScopeRetirementUnavailable()
    return reply


class ScopeRetirementManager:
    """Run durable prepare and finish steps around an external backend exchange."""

    def __init__(
        self,
        data_factory: ScopeRetirementDataFactory,
        *,
        backend_retirement: BackendScopeRetirement,
    ) -> None:
        if not callable(data_factory) or not callable(backend_retirement):
            raise ScopeRetirementUnavailable()
        self._data_factory = data_factory
        self._backend_retirement = backend_retirement

    async def execute(
        self, request: ScopeRetirementRequest | dict | bytes | str
    ) -> ScopeRetirementResult:
        typed = decode_scope_retirement_request(request)
        arguments = self._arguments(typed)
        try:
            payload = await self._step("prepare", arguments)
            view, context = _intent(payload, typed, action="prepare")
        except _Rejected as rejected:
            return self._rejected(typed, rejected.reason)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # A prepare acknowledgement can be lost at commit. Status exposes
            # only the public phase, so recovery returns that phase and leaves
            # the next explicit execute call to resume through prepare.
            try:
                payload = await self._step("status", arguments)
                view, _ = _intent(payload, typed, action="status")
            except _Rejected as rejected:
                return self._rejected(typed, rejected.reason)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return self._unknown(typed)
            if view is None:
                return self._unknown(typed)
            return self._committed(typed, view)
        if view is None:
            return self._unknown(typed)
        if view.phase == "completed":
            return self._committed(typed, view)
        if context is None:
            return self._unknown(typed)

        try:
            backend_reply = await self._backend_retirement(context)
            bound_reply = _bind_reply(backend_reply, context)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._unknown(typed)

        finish_arguments = {
            **arguments,
            "reply": bound_reply,
        }
        try:
            payload = await self._step("finish", finish_arguments)
            finished_view, _ = _intent(payload, typed, action="finish")
            if finished_view is None:
                return self._unknown(typed)
            return self._committed(typed, finished_view)
        except _Rejected as rejected:
            return self._rejected(typed, rejected.reason)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # The backend reply may already have been accepted. One read can
            # recover the durable phase; it does not repeat the control call.
            try:
                payload = await self._step("status", arguments)
                observed, _ = _intent(payload, typed, action="status")
            except _Rejected as rejected:
                return self._rejected(typed, rejected.reason)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return self._unknown(typed)
            if observed is None:
                return self._unknown(typed)
            return self._committed(typed, observed)

    async def status(
        self, request: ScopeRetirementRequest | dict | bytes | str
    ) -> ScopeRetirementResult:
        typed = decode_scope_retirement_request(request)
        try:
            payload = await self._step("status", self._arguments(typed))
            view, _ = _intent(payload, typed, action="status")
        except _Rejected as rejected:
            return self._rejected(typed, rejected.reason)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._unknown(typed)
        if view is None:
            return self._unknown(typed)
        return self._committed(typed, view)

    async def _step(self, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if action not in SCOPE_RETIREMENT_ACTIONS:
            raise ScopeRetirementUnavailable()
        try:
            async with self._data_factory() as data:
                policy = _native_reply(
                    await data.retirement_step("policy", {}),
                    expected={"ready", "functions"},
                )
                if (
                    type(policy["ready"]) is not bool
                    or policy["ready"] is not True
                    or policy["functions"] != list(SCOPE_RETIREMENT_NATIVE_FUNCTIONS)
                ):
                    raise ScopeRetirementUnavailable()
                if action == "policy":
                    return policy
                return _native_reply(
                    await data.retirement_step(action, arguments),
                    expected={"retirement", "context"},
                )
        except (ScopeRetirementUnavailable, _Rejected):
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ScopeRetirementUnavailable() from None

    @staticmethod
    def _arguments(request: ScopeRetirementRequest) -> dict[str, str]:
        return {
            "retirement_id": str(request.retirement_id),
            "scope_id": str(request.scope_id),
            "capability": request.capability,
        }

    @staticmethod
    def _committed(
        request: ScopeRetirementRequest,
        result: ScopeRetirementView | ScopeRetirementRejected,
    ) -> ScopeRetirementResult:
        return ScopeRetirementResult(
            schema="aap-inference-scope-retirement-result/v1",
            retirement_id=request.retirement_id,
            scope_id=request.scope_id,
            state="committed",
            result=result,
        )

    @classmethod
    def _rejected(cls, request: ScopeRetirementRequest, reason: str):
        try:
            rejected = ScopeRetirementRejected(reason=reason)
        except (TypeError, ValueError):
            raise ScopeRetirementUnavailable() from None
        return cls._committed(request, rejected)

    @staticmethod
    def _unknown(request: ScopeRetirementRequest) -> ScopeRetirementResult:
        return ScopeRetirementResult(
            schema="aap-inference-scope-retirement-result/v1",
            retirement_id=request.retirement_id,
            scope_id=request.scope_id,
            state="unknown",
            result=None,
        )


__all__ = ["ScopeRetirementManager"]
