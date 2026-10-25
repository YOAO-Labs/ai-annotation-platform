"""Strict backend-side scope retirement wire values."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Literal, Mapping
from uuid import UUID

from aap_protocol_v2.routing import RoutingProtocolError

SCOPE_RETIREMENT_PATH = "/v1/aap-routing/retire-scope"
SCOPE_RETIREMENT_PROTOCOL = "aap-backend-scope-retirement/v1"
SCOPE_RETIREMENT_REPLY_PROTOCOL = "aap-backend-scope-retirement-reply/v1"
MAX_SCOPE_RETIREMENT_BYTES = 8192

_COMMAND_FIELDS = {
    "schema",
    "retirement_id",
    "registry_id",
    "scope_id",
    "authority_epoch",
    "backend_incarnation",
    "challenge",
    "fence_until",
}
_REPLY_FIELDS = {
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
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_INT64_MAX = (1 << 63) - 1
_ACTIVE_REQUESTS_MAX = (1 << 31) - 1


@dataclass(frozen=True, slots=True)
class BackendScopeRetirementCommand:
    retirement_id: str
    registry_id: str
    scope_id: str
    authority_epoch: str
    backend_incarnation: str
    challenge: str
    fence_until: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCOPE_RETIREMENT_PROTOCOL,
            "retirement_id": self.retirement_id,
            "registry_id": self.registry_id,
            "scope_id": self.scope_id,
            "authority_epoch": self.authority_epoch,
            "backend_incarnation": self.backend_incarnation,
            "challenge": self.challenge,
            "fence_until": self.fence_until,
        }


@dataclass(frozen=True, slots=True)
class BackendScopeRetirementReply:
    retirement_id: str
    registry_id: str
    scope_id: str
    authority_epoch: str
    backend_incarnation: str
    challenge: str
    phase: Literal["draining", "retired"]
    active_requests: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCOPE_RETIREMENT_REPLY_PROTOCOL,
            "retirement_id": self.retirement_id,
            "registry_id": self.registry_id,
            "scope_id": self.scope_id,
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
        raise RoutingProtocolError("invalid scope retirement JSON object") from None
    if not isinstance(value, dict):
        raise RoutingProtocolError("expected scope retirement JSON object")
    return value


def _require_exact_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise RoutingProtocolError("unexpected scope retirement message fields")


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


def _challenge(value: Any) -> str:
    if not isinstance(value, str) or _HEX_64_RE.fullmatch(value) is None:
        raise RoutingProtocolError("invalid challenge")
    return value


def decode_backend_scope_retirement_command(
    raw: bytes,
) -> BackendScopeRetirementCommand:
    if not isinstance(raw, bytes) or len(raw) > MAX_SCOPE_RETIREMENT_BYTES:
        raise RoutingProtocolError("scope retirement body is too large")
    value = _strict_json_object(raw)
    _require_exact_fields(value, _COMMAND_FIELDS)
    if value["schema"] != SCOPE_RETIREMENT_PROTOCOL:
        raise RoutingProtocolError("unsupported scope retirement schema")
    fence_until = value["fence_until"]
    if type(fence_until) is not int or not 0 <= fence_until <= _INT64_MAX:
        raise RoutingProtocolError("invalid scope retirement fence")
    return BackendScopeRetirementCommand(
        retirement_id=_canonical_uuid(value["retirement_id"], "retirement_id"),
        registry_id=_canonical_uuid(value["registry_id"], "registry_id"),
        scope_id=_canonical_uuid(value["scope_id"], "scope_id"),
        authority_epoch=_canonical_uuid(value["authority_epoch"], "authority_epoch"),
        backend_incarnation=_canonical_uuid(
            value["backend_incarnation"], "backend_incarnation"
        ),
        challenge=_challenge(value["challenge"]),
        fence_until=fence_until,
    )


def decode_backend_scope_retirement_reply(
    value: bytes | Mapping[str, Any],
) -> BackendScopeRetirementReply:
    if isinstance(value, bytes):
        if len(value) > MAX_SCOPE_RETIREMENT_BYTES:
            raise RoutingProtocolError("scope retirement reply is too large")
        value = _strict_json_object(value)
    if not isinstance(value, Mapping):
        raise RoutingProtocolError("expected scope retirement reply object")
    _require_exact_fields(value, _REPLY_FIELDS)
    if value["schema"] != SCOPE_RETIREMENT_REPLY_PROTOCOL:
        raise RoutingProtocolError("unsupported scope retirement reply schema")
    phase = value["phase"]
    if not isinstance(phase, str) or phase not in {"draining", "retired"}:
        raise RoutingProtocolError("invalid scope retirement phase")
    active_requests = value["active_requests"]
    if (
        type(active_requests) is not int
        or not 0 <= active_requests <= _ACTIVE_REQUESTS_MAX
    ):
        raise RoutingProtocolError("invalid active request count")
    if (phase == "draining" and active_requests == 0) or (
        phase == "retired" and active_requests != 0
    ):
        raise RoutingProtocolError("scope retirement phase/count mismatch")
    return BackendScopeRetirementReply(
        retirement_id=_canonical_uuid(value["retirement_id"], "retirement_id"),
        registry_id=_canonical_uuid(value["registry_id"], "registry_id"),
        scope_id=_canonical_uuid(value["scope_id"], "scope_id"),
        authority_epoch=_canonical_uuid(value["authority_epoch"], "authority_epoch"),
        backend_incarnation=_canonical_uuid(
            value["backend_incarnation"], "backend_incarnation"
        ),
        challenge=_challenge(value["challenge"]),
        phase=phase,
        active_requests=active_requests,
    )


__all__ = [
    "BackendScopeRetirementCommand",
    "BackendScopeRetirementReply",
    "MAX_SCOPE_RETIREMENT_BYTES",
    "SCOPE_RETIREMENT_PATH",
    "SCOPE_RETIREMENT_PROTOCOL",
    "SCOPE_RETIREMENT_REPLY_PROTOCOL",
    "decode_backend_scope_retirement_command",
    "decode_backend_scope_retirement_reply",
]
