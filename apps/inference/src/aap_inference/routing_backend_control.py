"""Authenticated HTTP control for the actual backend route drain and bind."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from aap_protocol_v2.routing import (
    ROUTE_CONTROL_PATH,
    ROUTE_CONTROL_PROTOCOL,
    ROUTE_STATUS_PATH,
    MAX_ROUTE_CONTROL_BYTES,
    BackendRouteControlReply,
    RoutingProtocolError,
    decode_backend_route_control_reply,
)

_CONTROL_CONTEXT_FIELDS = frozenset(
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
    }
)
_ENDPOINT_FIELDS = frozenset({"url", "auth_method", "auth_token"})
_HEX_64_RE = re.compile(r"[0-9a-f]{64}\Z")
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_DEFAULT_TIMEOUT_SECONDS = 5.0


class BackendRoutingControlUnavailable(Exception):
    """The backend did not provide a complete, bound route-control reply."""


def _canonical_uuid(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str:
        raise BackendRoutingControlUnavailable()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise BackendRoutingControlUnavailable() from None
    if str(parsed) != value:
        raise BackendRoutingControlUnavailable()
    return value


def _private_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CONTROL_CONTEXT_FIELDS:
        raise BackendRoutingControlUnavailable()
    context = dict(value)
    _canonical_uuid(context["control_id"])
    _canonical_uuid(context["registry_id"])
    _canonical_uuid(context["new_epoch"])
    _canonical_uuid(context["retired_epoch"], nullable=True)
    _canonical_uuid(context["backend_incarnation"], nullable=True)
    if (
        type(context["phase"]) is not str
        or context["phase"]
        not in {"prepared", "draining", "drained", "bound", "uncertain"}
        or type(context["challenge"]) is not str
        or _HEX_64_RE.fullmatch(context["challenge"]) is None
        or type(context["signing_key"]) is not str
    ):
        raise BackendRoutingControlUnavailable()
    encoded_key = context["signing_key"]
    if _BASE64URL_RE.fullmatch(encoded_key) is None:
        raise BackendRoutingControlUnavailable()
    try:
        key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
    except (ValueError, base64.binascii.Error):
        raise BackendRoutingControlUnavailable() from None
    if (
        len(key) != 32
        or base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii") != encoded_key
    ):
        raise BackendRoutingControlUnavailable()

    endpoint = context["endpoint"]
    if not isinstance(endpoint, Mapping) or set(endpoint) != _ENDPOINT_FIELDS:
        raise BackendRoutingControlUnavailable()
    url = endpoint["url"]
    auth_method = endpoint["auth_method"]
    auth_token = endpoint["auth_token"]
    if (
        type(url) is not str
        or not url
        or any(character.isspace() or ord(character) < 0x20 for character in url)
        or type(auth_method) is not str
        or auth_method != "token"
        or type(auth_token) is not str
        or not auth_token
        or not auth_token.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in auth_token)
    ):
        raise BackendRoutingControlUnavailable()
    try:
        parsed_url = urlsplit(url)
        _ = parsed_url.port
    except ValueError:
        raise BackendRoutingControlUnavailable() from None
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.hostname is None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise BackendRoutingControlUnavailable()
    context["endpoint"] = dict(endpoint)
    return context


def _strict_reply_object(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise BackendRoutingControlUnavailable() from None
    if type(parsed) is not dict:
        raise BackendRoutingControlUnavailable()
    return parsed


def _control_command(context: Mapping[str, Any], *, action: str) -> tuple[bytes, str]:
    signing_key = context["signing_key"] if action == "bind" else None
    command = {
        "schema": ROUTE_CONTROL_PROTOCOL,
        "command_id": context["control_id"],
        "registry_id": context["registry_id"],
        "authority_epoch": context["new_epoch"],
        "retired_epoch": context["retired_epoch"],
        "challenge": context["challenge"],
        "action": action,
        "signing_key": signing_key,
    }
    try:
        encoded = json.dumps(
            command,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BackendRoutingControlUnavailable() from None
    if len(encoded) > MAX_ROUTE_CONTROL_BYTES:
        raise BackendRoutingControlUnavailable()
    return encoded, context["endpoint"]["auth_token"]


class BackendRoutingControl:
    """Drain and bind using one bounded HTTP attempt per phase."""

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or timeout_seconds > 120
            or (transport_factory is not None and not callable(transport_factory))
        ):
            raise ValueError("Invalid backend routing control settings")
        self.timeout_seconds = float(timeout_seconds)
        self._transport_factory = transport_factory

    async def __call__(self, private_context: Mapping[str, Any]) -> dict[str, Any]:
        context = _private_context(private_context)
        drain_reply = await self._post(context, action="drain")
        if drain_reply.phase == "bound":
            return drain_reply.as_dict()
        if drain_reply.phase == "draining":
            return drain_reply.as_dict()
        if drain_reply.phase != "drained" or drain_reply.active_requests != 0:
            raise BackendRoutingControlUnavailable()

        bind_reply = await self._post(context, action="bind")
        if bind_reply.phase == "bound" and bind_reply.active_requests == 0:
            return bind_reply.as_dict()
        if bind_reply.phase in {"draining", "drained"}:
            return bind_reply.as_dict()
        raise BackendRoutingControlUnavailable()

    async def status(self, private_context: Mapping[str, Any]) -> dict[str, Any]:
        """Read the authenticated controller status for a frozen incarnation."""
        context = _private_context(private_context)
        incarnation = context["backend_incarnation"]
        if incarnation is None:
            raise BackendRoutingControlUnavailable()
        endpoint = context["endpoint"]
        url = endpoint["url"].rstrip("/") + ROUTE_STATUS_PATH
        headers = {
            "Authorization": f"Bearer {endpoint['auth_token']}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        transport = (
            self._transport_factory() if self._transport_factory is not None else None
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=transport,
                ) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if (
                            response.headers.get("content-encoding", "identity")
                            .strip()
                            .lower()
                            != "identity"
                            or response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                            != "application/json"
                        ):
                            raise BackendRoutingControlUnavailable()
                        raw_reply = await self._read_response(response)
                        if response.status_code != 200:
                            raise BackendRoutingControlUnavailable()
        except (BackendRoutingControlUnavailable, asyncio.CancelledError):
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise BackendRoutingControlUnavailable() from None
        try:
            reply = decode_backend_route_control_reply(_strict_reply_object(raw_reply))
        except (RoutingProtocolError, ValueError, TypeError, UnicodeError):
            raise BackendRoutingControlUnavailable() from None
        if (
            reply.registry_id != context["registry_id"]
            or reply.backend_incarnation != incarnation
        ):
            raise BackendRoutingControlUnavailable()
        return reply.as_dict()

    async def _post(
        self, context: Mapping[str, Any], *, action: str
    ) -> BackendRouteControlReply:
        raw_body, auth_token = _control_command(context, action=action)
        endpoint = context["endpoint"]
        url = endpoint["url"].rstrip("/") + ROUTE_CONTROL_PATH
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }
        transport = (
            self._transport_factory() if self._transport_factory is not None else None
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, content=raw_body
                ) as response:
                    raw_reply = await self._read_response(response)
                    if response.status_code != 200:
                        raise BackendRoutingControlUnavailable()
        except (BackendRoutingControlUnavailable, asyncio.CancelledError):
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            # The command may have reached the backend. The persistent Native
            # hold remains in place; a later reconciliation uses the same ID.
            raise BackendRoutingControlUnavailable() from None

        try:
            reply = decode_backend_route_control_reply(_strict_reply_object(raw_reply))
        except (RoutingProtocolError, ValueError, TypeError, UnicodeError):
            raise BackendRoutingControlUnavailable() from None
        if (
            reply.command_id != context["control_id"]
            or reply.registry_id != context["registry_id"]
            or reply.authority_epoch != context["new_epoch"]
            or reply.challenge != context["challenge"]
            or (
                context["backend_incarnation"] is not None
                and reply.backend_incarnation != context["backend_incarnation"]
            )
        ):
            raise BackendRoutingControlUnavailable()
        return reply

    @staticmethod
    async def _read_response(response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            if not declared.isascii() or not declared.isdigit():
                raise BackendRoutingControlUnavailable()
            normalized = declared.lstrip("0") or "0"
            if len(normalized) > len(str(MAX_ROUTE_CONTROL_BYTES)):
                raise BackendRoutingControlUnavailable()
            if int(normalized) > MAX_ROUTE_CONTROL_BYTES:
                raise BackendRoutingControlUnavailable()
        result = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            result.extend(chunk)
            if len(result) > MAX_ROUTE_CONTROL_BYTES:
                raise BackendRoutingControlUnavailable()
        return bytes(result)


async def execute_backend_control(
    private_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Default callback shared by the Inference owner and embedded API path."""
    return await BackendRoutingControl()(private_context)


__all__ = [
    "BackendRoutingControl",
    "BackendRoutingControlUnavailable",
    "execute_backend_control",
]
