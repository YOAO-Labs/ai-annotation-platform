"""Portable coordinator for an explicitly fenced, offline video restore."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
from typing import Any

from aap_protocol_v2.routing import (
    BackendRouteControlReply,
    RoutingProtocolError,
    decode_backend_route_control_reply,
)

from .restore_contract import (
    RESTORE_ACTIONS,
    RestoreCommand,
    RestoreRejected,
    RestoreResult,
    RestoreUnavailable,
    decode_native_envelope,
    decode_restore_command,
    decode_restore_context,
    decode_restore_record,
)


class _NativeRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _arguments_json(action: str, arguments: Mapping[str, Any]) -> str:
    if action not in RESTORE_ACTIONS or not isinstance(arguments, Mapping):
        raise RestoreUnavailable()
    value = dict(arguments)
    if action in {"policy", "inspect"}:
        if value:
            raise RestoreUnavailable()
    elif action == "prepare":
        if set(value) != {"command"}:
            raise RestoreUnavailable()
        value["command"] = decode_restore_command(value["command"]).as_dict()
    elif action in {"status", "finish"}:
        if set(value) != {"restore_id"}:
            raise RestoreUnavailable()
        from .restore_contract import _canonical_uuid

        _canonical_uuid(value["restore_id"])
    else:
        if set(value) != {"restore_id", "registry_id", "reply"}:
            raise RestoreUnavailable()
        from .restore_contract import _canonical_uuid

        _canonical_uuid(value["restore_id"])
        registry_id = _canonical_uuid(value["registry_id"])
        try:
            reply = decode_backend_route_control_reply(value["reply"])
        except (RoutingProtocolError, TypeError, ValueError, AttributeError):
            raise RestoreUnavailable() from None
        if reply.registry_id != registry_id:
            raise RestoreUnavailable()
        value["reply"] = reply.as_dict()
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(raw.encode("utf-8", errors="strict")) > 65_536:
            raise RestoreUnavailable()
        parsed = json.loads(raw)
    except RestoreUnavailable:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise RestoreUnavailable() from None
    if type(parsed) is not dict:
        raise RestoreUnavailable()
    return raw


def _bound_routing_reply(value: object, context: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if isinstance(value, Mapping):
            raw = dict(value)
        elif callable(getattr(value, "as_dict", None)):
            raw = value.as_dict()
        else:
            raise RestoreUnavailable()
        if not isinstance(raw, Mapping):
            raise RestoreUnavailable()
        reply: BackendRouteControlReply = decode_backend_route_control_reply(raw)
    except RestoreUnavailable:
        raise
    except (RoutingProtocolError, TypeError, ValueError, AttributeError):
        raise RestoreUnavailable() from None
    if (
        reply.registry_id != context["registry_id"]
        or reply.backend_incarnation != context["backend_incarnation"]
    ):
        raise RestoreUnavailable()
    return reply.as_dict()


def _own_control_reply(
    value: object,
    context: Mapping[str, Any],
    *,
    require_bound: bool = False,
) -> dict[str, Any]:
    reply = _bound_routing_reply(value, context)
    if (
        reply["command_id"] != context["control_id"]
        or reply["authority_epoch"] != context["new_epoch"]
        or reply["challenge"] != context["challenge"]
        or (
            require_bound
            and (reply["phase"] != "bound" or reply["active_requests"] != 0)
        )
    ):
        raise RestoreUnavailable()
    return reply


def _checked_envelope(value: object, *, action: str, command: RestoreCommand | None):
    envelope = decode_native_envelope(value, action=action)
    if envelope["status"] == "rejected":
        raise _NativeRejected(envelope["reason"])
    if action in {"policy", "inspect"}:
        return envelope
    record = decode_restore_record(envelope["restore"], command=command)
    envelope["restore"] = record
    contexts = [decode_restore_context(context) for context in envelope["contexts"]]
    envelope["contexts"] = contexts
    return envelope


class VideoRestoreManager:
    """Coordinate persisted Native intent with authenticated backend controls.

    The `data` object exposes `step(action, arguments)`, with one bounded Native
    transaction per call. The backend controller must provide authenticated
    `status(context)` and `__call__(context)` operations. No generated control
    identifiers or signing material are created in this process.
    """

    def __init__(self, data: Any, *, backend_control: Any = None) -> None:
        if not callable(getattr(data, "step", None)):
            raise RestoreUnavailable()
        if backend_control is None:
            from .routing_backend_control import BackendRoutingControl

            backend_control = BackendRoutingControl()
        if not callable(getattr(backend_control, "status", None)) or not callable(
            backend_control
        ):
            raise RestoreUnavailable()
        self._data = data
        self._backend_control = backend_control

    async def inspect(self) -> dict[str, str]:
        try:
            envelope = await self._step("inspect", {})
        except _NativeRejected as rejected:
            raise RestoreRejected(rejected.reason) from None
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RestoreUnavailable() from None
        return dict(envelope["identity"])

    async def status(self, restore_id: str) -> RestoreResult:
        from .restore_contract import _canonical_uuid

        try:
            _canonical_uuid(restore_id)
            envelope = await self._step("status", {"restore_id": restore_id})
        except _NativeRejected as rejected:
            return self._rejected(rejected.reason)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._unknown()
        record = envelope["restore"]
        state = "completed" if record["phase"] == "completed" else "pending"
        return RestoreResult(state=state, record=record)

    async def execute(
        self, command: RestoreCommand | Mapping[str, Any] | bytes | str
    ) -> RestoreResult:
        typed = decode_restore_command(command)
        try:
            envelope = await self._step(
                "prepare", {"command": typed.as_dict()}, command=typed
            )
        except _NativeRejected as rejected:
            return self._rejected(rejected.reason)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # A prepare acknowledgement can be lost at commit. Recover only the
            # public state for this same restore id and command digest.
            recovered = await self._recover(typed)
            if recovered is None:
                return self._unknown()
            envelope = recovered

        record = envelope["restore"]
        if record["phase"] == "completed":
            return RestoreResult(state="completed", record=record)

        contexts = envelope["contexts"]
        by_registry = {context["registry_id"]: context for context in contexts}
        for initial_context in contexts:
            context = by_registry.get(initial_context["registry_id"])
            if context is None:
                return RestoreResult(state="pending", record=record)
            if context["phase"] == "bound":
                continue
            try:
                status_reply = await self._backend_control.status(context)
                status = _bound_routing_reply(status_reply, context)
                observed = await self._step(
                    "observe",
                    {
                        "restore_id": typed.restore_id,
                        "registry_id": context["registry_id"],
                        "reply": status,
                    },
                    command=typed,
                )
                refreshed = observed["contexts"]
                context = next(
                    (
                        candidate
                        for candidate in refreshed
                        if candidate["registry_id"] == initial_context["registry_id"]
                    ),
                    None,
                )
                if context is None or not self._same_control(initial_context, context):
                    raise RestoreUnavailable()
                control_reply = await self._backend_control(context)
                bound = _own_control_reply(control_reply, context)
                if bound["phase"] != "bound" or bound["active_requests"] != 0:
                    return RestoreResult(state="pending", record=observed["restore"])
                recorded = await self._step(
                    "record_bound",
                    {
                        "restore_id": typed.restore_id,
                        "registry_id": context["registry_id"],
                        "reply": bound,
                    },
                    command=typed,
                )
                envelope = recorded
                record = recorded["restore"]
                by_registry = {
                    candidate["registry_id"]: candidate
                    for candidate in recorded["contexts"]
                }
            except _NativeRejected as rejected:
                return self._rejected(rejected.reason)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # Prepared state remains the durable global write fence. The
                # next explicit invocation reuses every persisted control id.
                return RestoreResult(state="pending", record=record)

        # A persisted bound context is only a historical observation. Re-read
        # every target controller immediately before the Native atomic finish
        # so a cold restart or changed route cannot reuse stale retirement proof.
        try:
            for context in by_registry.values():
                if context["phase"] != "bound":
                    return RestoreResult(state="pending", record=record)
                live_reply = await self._backend_control.status(context)
                _own_control_reply(live_reply, context, require_bound=True)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return RestoreResult(state="pending", record=record)

        try:
            finished = await self._step(
                "finish", {"restore_id": typed.restore_id}, command=typed
            )
        except _NativeRejected as rejected:
            return self._rejected(rejected.reason)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            recovered = await self._recover(typed)
            if recovered is None:
                return RestoreResult(state="pending", record=record)
            if recovered["restore"]["phase"] == "completed":
                return RestoreResult(state="completed", record=recovered["restore"])
            return RestoreResult(state="pending", record=recovered["restore"])
        if finished["restore"]["phase"] != "completed":
            return RestoreResult(state="pending", record=finished["restore"])
        return RestoreResult(state="completed", record=finished["restore"])

    async def _recover(self, command: RestoreCommand) -> dict[str, Any] | None:
        try:
            envelope = await self._step(
                "status", {"restore_id": command.restore_id}, command=command
            )
        except Exception:
            return None
        if envelope["restore"]["command_sha256"] != self._command_digest(command):
            return None
        return envelope

    async def _step(
        self,
        action: str,
        arguments: Mapping[str, Any],
        *,
        command: RestoreCommand | None = None,
    ) -> dict[str, Any]:
        if action not in RESTORE_ACTIONS:
            raise RestoreUnavailable()
        _arguments_json(action, arguments)
        try:
            raw = await self._data.step(action, dict(arguments))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RestoreUnavailable() from None
        return _checked_envelope(raw, action=action, command=command)

    @staticmethod
    def _same_control(
        original: Mapping[str, Any], refreshed: Mapping[str, Any]
    ) -> bool:
        return (
            all(
                refreshed[field] == original[field]
                for field in (
                    "control_id",
                    "registry_id",
                    "new_epoch",
                    "challenge",
                    "backend_incarnation",
                    "endpoint",
                    "signing_key",
                )
            )
            and refreshed["retired_epoch"] is not None
        )

    @staticmethod
    def _command_digest(command: RestoreCommand) -> str:
        from .restore_contract import command_sha256

        return command_sha256(command)

    @staticmethod
    def _rejected(reason: str) -> RestoreResult:
        try:
            RestoreRejected(reason)
        except RestoreUnavailable:
            return RestoreResult(state="unknown")
        return RestoreResult(state="rejected", reason=reason)

    @staticmethod
    def _unknown() -> RestoreResult:
        return RestoreResult(state="unknown")


__all__ = ["VideoRestoreManager"]
