"""Strict backend-side route admission and control wire values."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Literal, Mapping
from uuid import UUID

import jwt

ROUTE_ADMISSION_HEADER = "x-aap-route-admission"
ROUTE_CONTROL_PATH = "/v1/aap-routing/control"
ROUTE_STATUS_PATH = "/v1/aap-routing/status"
ROUTE_CONTROL_PROTOCOL = "aap-backend-route-control/v1"
ROUTE_CONTROL_REPLY_PROTOCOL = "aap-backend-route-control-reply/v1"
ROUTE_ADMISSION_PROTOCOL = "aap-route-admission/v1"
ROUTE_ADMISSION_AUDIENCE = "ml-route"
MAX_ROUTE_TOKEN_BYTES = 8192
MAX_ROUTE_CONTROL_BYTES = 8192

_UUID_FIELDS = {
    "registry_id",
    "backend_incarnation",
    "authority_epoch",
    "scope_id",
    "attempt_id",
}
_CLAIM_FIELDS = {
    "schema",
    "aud",
    "registry_id",
    "backend_incarnation",
    "authority_epoch",
    "scope_id",
    "attempt_id",
    "operation",
    "method",
    "path",
    "iat",
    "exp",
}
_CONTROL_FIELDS = {
    "schema",
    "command_id",
    "registry_id",
    "authority_epoch",
    "retired_epoch",
    "challenge",
    "action",
    "signing_key",
}
_REPLY_FIELDS = {
    "schema",
    "command_id",
    "registry_id",
    "authority_epoch",
    "backend_incarnation",
    "challenge",
    "phase",
    "active_requests",
}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_METHOD_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Z-]+$")
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BASE64URL_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


class RoutingProtocolError(ValueError):
    """A malformed or contradictory backend routing message."""


@dataclass(frozen=True, slots=True)
class RouteAdmissionClaims:
    registry_id: str
    backend_incarnation: str
    authority_epoch: str
    scope_id: str
    attempt_id: str
    operation: str
    method: str
    path: str
    issued_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class BackendRouteControlCommand:
    command_id: str
    registry_id: str
    authority_epoch: str
    retired_epoch: str | None
    challenge: str
    action: Literal["drain", "bind"]
    signing_key: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class BackendRouteControlReply:
    command_id: str
    registry_id: str
    authority_epoch: str
    backend_incarnation: str
    challenge: str
    phase: Literal["draining", "drained", "bound"]
    active_requests: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ROUTE_CONTROL_REPLY_PROTOCOL,
            "command_id": self.command_id,
            "registry_id": self.registry_id,
            "authority_epoch": self.authority_epoch,
            "backend_incarnation": self.backend_incarnation,
            "challenge": self.challenge,
            "phase": self.phase,
            "active_requests": self.active_requests,
        }


def _reject_constant(value: str) -> None:
    raise RoutingProtocolError(f"non-finite JSON number: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingProtocolError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except RoutingProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RoutingProtocolError("invalid JSON object") from None
    if not isinstance(value, dict):
        raise RoutingProtocolError("expected JSON object")
    return value


def _require_exact_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise RoutingProtocolError("unexpected routing message fields")


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise RoutingProtocolError(f"invalid {field_name}")
    try:
        parsed = UUID(value)
    except ValueError:
        raise RoutingProtocolError(f"invalid {field_name}") from None
    if str(parsed) != value:
        raise RoutingProtocolError(f"invalid {field_name}")
    return value


def _base64url_decode(value: Any) -> bytes:
    if not isinstance(value, str) or _BASE64URL_KEY_RE.fullmatch(value) is None:
        raise RoutingProtocolError("invalid signing key")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error):
        raise RoutingProtocolError("invalid signing key") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if value not in {canonical, canonical.rstrip("=")}:
        raise RoutingProtocolError("invalid signing key")
    if len(decoded) != 32:
        raise RoutingProtocolError("invalid signing key")
    return decoded


def decode_backend_route_control_command(raw: bytes) -> BackendRouteControlCommand:
    if len(raw) > MAX_ROUTE_CONTROL_BYTES:
        raise RoutingProtocolError("routing control body is too large")
    value = _strict_json_object(raw)
    _require_exact_fields(value, _CONTROL_FIELDS)
    if value["schema"] != ROUTE_CONTROL_PROTOCOL:
        raise RoutingProtocolError("unsupported routing control schema")
    command_id = _canonical_uuid(value["command_id"], "command_id")
    registry_id = _canonical_uuid(value["registry_id"], "registry_id")
    authority_epoch = _canonical_uuid(value["authority_epoch"], "authority_epoch")
    retired_epoch = (
        None
        if value["retired_epoch"] is None
        else _canonical_uuid(value["retired_epoch"], "retired_epoch")
    )
    challenge = value["challenge"]
    if not isinstance(challenge, str) or _HEX_64_RE.fullmatch(challenge) is None:
        raise RoutingProtocolError("invalid challenge")
    action = value["action"]
    if not isinstance(action, str) or action not in {"drain", "bind"}:
        raise RoutingProtocolError("invalid routing control action")
    if action == "drain":
        if value["signing_key"] is not None:
            raise RoutingProtocolError("drain must not include a signing key")
        signing_key = None
    else:
        signing_key = _base64url_decode(value["signing_key"])
    return BackendRouteControlCommand(
        command_id=command_id,
        registry_id=registry_id,
        authority_epoch=authority_epoch,
        retired_epoch=retired_epoch,
        challenge=challenge,
        action=action,
        signing_key=signing_key,
    )


def decode_backend_route_control_reply(value: Any) -> BackendRouteControlReply:
    if not isinstance(value, Mapping):
        raise RoutingProtocolError("expected routing control reply object")
    _require_exact_fields(value, _REPLY_FIELDS)
    if value["schema"] != ROUTE_CONTROL_REPLY_PROTOCOL:
        raise RoutingProtocolError("unsupported routing control reply schema")
    command_id = _canonical_uuid(value["command_id"], "command_id")
    registry_id = _canonical_uuid(value["registry_id"], "registry_id")
    authority_epoch = _canonical_uuid(value["authority_epoch"], "authority_epoch")
    backend_incarnation = _canonical_uuid(
        value["backend_incarnation"], "backend_incarnation"
    )
    challenge = value["challenge"]
    if not isinstance(challenge, str) or _HEX_64_RE.fullmatch(challenge) is None:
        raise RoutingProtocolError("invalid challenge")
    phase = value["phase"]
    if not isinstance(phase, str) or phase not in {"draining", "drained", "bound"}:
        raise RoutingProtocolError("invalid routing control phase")
    active_requests = value["active_requests"]
    if type(active_requests) is not int or active_requests < 0:
        raise RoutingProtocolError("invalid active request count")
    if (phase == "draining" and active_requests == 0) or (
        phase == "drained" and active_requests != 0
    ):
        raise RoutingProtocolError("routing control phase/count mismatch")
    return BackendRouteControlReply(
        command_id=command_id,
        registry_id=registry_id,
        authority_epoch=authority_epoch,
        backend_incarnation=backend_incarnation,
        challenge=challenge,
        phase=phase,
        active_requests=active_requests,
    )


def _decode_compact_json(segment: str) -> dict[str, Any]:
    if not segment or _BASE64URL_RE.fullmatch(segment) is None:
        raise RoutingProtocolError("invalid admission token")
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, base64.binascii.Error):
        raise RoutingProtocolError("invalid admission token") from None
    return _strict_json_object(raw)


def decode_route_admission_token(
    token: str,
    signing_key: bytes,
    *,
    now: int | None = None,
) -> RouteAdmissionClaims:
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode("utf-8", errors="ignore")) > MAX_ROUTE_TOKEN_BYTES
    ):
        raise RoutingProtocolError("invalid admission token")
    parts = token.split(".")
    if len(parts) != 3 or _BASE64URL_RE.fullmatch(parts[2]) is None:
        raise RoutingProtocolError("invalid admission token")
    header = _decode_compact_json(parts[0])
    unverified_claims = _decode_compact_json(parts[1])
    if set(header) != {"alg", "typ"} or header != {"alg": "HS256", "typ": "JWT"}:
        raise RoutingProtocolError("invalid admission token header")
    _require_exact_fields(unverified_claims, _CLAIM_FIELDS)
    if unverified_claims.get("schema") != ROUTE_ADMISSION_PROTOCOL:
        raise RoutingProtocolError("invalid admission token schema")
    if unverified_claims.get("aud") != ROUTE_ADMISSION_AUDIENCE:
        raise RoutingProtocolError("invalid admission token audience")
    for field_name in _UUID_FIELDS:
        _canonical_uuid(unverified_claims.get(field_name), field_name)
    operation = unverified_claims.get("operation")
    method = unverified_claims.get("method")
    path = unverified_claims.get("path")
    if (
        not isinstance(operation, str)
        or not operation
        or len(operation) > 96
        or any(ord(char) < 0x20 for char in operation)
    ):
        raise RoutingProtocolError("invalid admission operation")
    if not isinstance(method, str) or _TOKEN_METHOD_RE.fullmatch(method) is None:
        raise RoutingProtocolError("invalid admission method")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "?" in path
        or "#" in path
        or any(ord(char) < 0x20 for char in path)
    ):
        raise RoutingProtocolError("invalid admission path")
    issued_at = unverified_claims.get("iat")
    expires_at = unverified_claims.get("exp")
    if type(issued_at) is not int or type(expires_at) is not int:
        raise RoutingProtocolError("invalid admission timestamps")
    current_time = int(time.time()) if now is None else now
    if (
        expires_at <= issued_at
        or issued_at > current_time
        or expires_at <= current_time
    ):
        raise RoutingProtocolError("expired admission token")
    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            audience=ROUTE_ADMISSION_AUDIENCE,
            options={"require": sorted(_CLAIM_FIELDS), "verify_iat": True},
        )
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError):
        raise RoutingProtocolError("invalid admission token") from None
    if claims != unverified_claims:
        raise RoutingProtocolError("invalid admission token")
    return RouteAdmissionClaims(
        registry_id=unverified_claims["registry_id"],
        backend_incarnation=unverified_claims["backend_incarnation"],
        authority_epoch=unverified_claims["authority_epoch"],
        scope_id=unverified_claims["scope_id"],
        attempt_id=unverified_claims["attempt_id"],
        operation=operation,
        method=method,
        path=path,
        issued_at=issued_at,
        expires_at=expires_at,
    )


__all__ = [
    "BackendRouteControlCommand",
    "BackendRouteControlReply",
    "MAX_ROUTE_CONTROL_BYTES",
    "MAX_ROUTE_TOKEN_BYTES",
    "ROUTE_ADMISSION_AUDIENCE",
    "ROUTE_ADMISSION_HEADER",
    "ROUTE_ADMISSION_PROTOCOL",
    "ROUTE_CONTROL_PATH",
    "ROUTE_CONTROL_PROTOCOL",
    "ROUTE_CONTROL_REPLY_PROTOCOL",
    "ROUTE_STATUS_PATH",
    "RouteAdmissionClaims",
    "RoutingProtocolError",
    "decode_backend_route_control_command",
    "decode_backend_route_control_reply",
    "decode_route_admission_token",
]
