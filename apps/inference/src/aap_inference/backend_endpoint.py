"""Purpose-limited access to exact registered backend endpoint snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from aap_inference_contract.backend_endpoint import (
    BackendEndpointAuthority,
    BackendEndpointBinding,
    BackendEndpointPurpose,
    BackendEndpointSubject,
    decode_backend_endpoint_subject,
    make_backend_endpoint_subject,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_CAPTURE_FIELDS = {"status", "subject"}

_SNAPSHOT_FIELDS = {
    "schema",
    "request_id",
    "registry_id",
    "purpose",
    "authority",
    "endpoint_sha256",
    "url",
    "auth_method",
    "auth_token",
    "gpu_resource_id",
    "vram_budget_mb",
    "eviction_priority",
    "extra_params",
}


class BackendEndpointUnavailable(Exception):
    """The sealed endpoint capability or Native opener is unavailable."""

    def __init__(self) -> None:
        super().__init__("Inference backend endpoint unavailable")


class BackendEndpointAuthorityRejected(Exception):
    """The existing routing authority rejected the requested transport handoff."""

    def __init__(self, reason: str) -> None:
        if type(reason) is not str or re.fullmatch(r"[a-z0-9_]{1,96}", reason) is None:
            raise ValueError("invalid endpoint authority rejection")
        self.reason = reason
        super().__init__(reason)


class BackendEndpointData(Protocol):
    async def capture_subject(
        self,
        registry_id: UUID,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None,
    ) -> str | dict[str, Any]: ...

    async def open_subject(
        self,
        subject: str,
        registry_id: UUID,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        authority: BackendEndpointAuthority | None,
    ) -> str: ...

    async def readiness(self) -> bool: ...


class BackendEndpointSource(Protocol):
    """Shared source ABI: capture in the origin UOW; open only in its owner."""

    async def capture(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None = None,
    ) -> BackendEndpointSubject: ...

    async def open(
        self,
        subject: BackendEndpointSubject,
        *,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        authority: BackendEndpointAuthority | None = None,
    ) -> BackendEndpointSnapshot: ...


@dataclass(frozen=True, slots=True, repr=False)
class BackendEndpointSnapshot:
    """Private immutable transport input. Its representation never contains secrets."""

    registry_id: UUID
    request_id: UUID
    purpose: BackendEndpointPurpose
    authority: BackendEndpointAuthority | None
    endpoint_sha256: str = field(repr=False)
    url: str = field(repr=False)
    auth_method: str | None = field(repr=False)
    auth_token: str | None = field(repr=False)
    gpu_resource_id: str | None = field(repr=False)
    vram_budget_mb: int | None = field(repr=False)
    eviction_priority: int = field(repr=False)
    extra_params: Mapping[str, Any] | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "BackendEndpointSnapshot("
            f"registry_id={self.registry_id!r}, request_id={self.request_id!r}, "
            f"purpose={self.purpose!r})"
        )

    def transport_configuration(self) -> _TransportConfiguration:
        """Materialize one transport-only configuration inside credential-owning code."""
        return _TransportConfiguration(
            id=self.registry_id,
            url=self.url,
            auth_method=self.auth_method,
            auth_token=self.auth_token,
            gpu_resource_id=self.gpu_resource_id,
            vram_budget_mb=self.vram_budget_mb,
            extra_params=_thaw_json(self.extra_params),
            health_meta=None,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _TransportConfiguration:
    id: UUID
    url: str
    auth_method: str | None
    auth_token: str | None
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    extra_params: dict[str, Any] | None
    health_meta: dict[str, Any] | None


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise BackendEndpointUnavailable()
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise BackendEndpointUnavailable()


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _snapshot(
    raw_json: str,
    request: BackendEndpointSubject,
) -> BackendEndpointSnapshot:
    try:
        value = json.loads(raw_json, parse_constant=_reject_json_constant)
        if type(value) is not dict or set(value) != _SNAPSHOT_FIELDS:
            raise ValueError("invalid endpoint snapshot shape")
        if (
            value["schema"] != "aap-inference-backend-endpoint-snapshot/v1"
            or type(value["registry_id"]) is not str
            or UUID(value["registry_id"]) != request.registry_id
            or type(value["request_id"]) is not str
            or UUID(value["request_id"]) != request.request_id
            or value["purpose"] != request.purpose
            or value["authority"]
            != (
                request.authority.model_dump(mode="json")
                if request.authority is not None
                else None
            )
        ):
            raise ValueError("endpoint snapshot is not bound to the request")
        digest = value["endpoint_sha256"]
        if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("invalid endpoint snapshot identity")
        if type(value["url"]) is not str:
            raise ValueError("invalid endpoint URL")
        auth_method = value["auth_method"]
        if auth_method is not None and type(auth_method) is not str:
            raise ValueError("invalid endpoint authentication mode")
        token = value["auth_token"]
        resource = value["gpu_resource_id"]
        vram = value["vram_budget_mb"]
        priority = value["eviction_priority"]
        extra = value["extra_params"]
        if token is not None and type(token) is not str:
            raise ValueError("invalid endpoint credential")
        if resource is not None and type(resource) is not str:
            raise ValueError("invalid GPU resource identity")
        if vram is not None and type(vram) is not int:
            raise ValueError("invalid endpoint VRAM budget")
        if type(priority) is not int:
            raise ValueError("invalid endpoint eviction priority")
        if extra is not None and type(extra) is not dict:
            raise ValueError("invalid endpoint extra parameters")
        frozen_extra = None if extra is None else _freeze_json(extra)
        return BackendEndpointSnapshot(
            registry_id=request.registry_id,
            request_id=request.request_id,
            purpose=request.purpose,
            authority=request.authority,
            endpoint_sha256=digest,
            url=value["url"],
            auth_method=auth_method,
            auth_token=token,
            gpu_resource_id=resource,
            vram_budget_mb=vram,
            eviction_priority=priority,
            extra_params=frozen_extra,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise BackendEndpointUnavailable() from None


class BackendEndpointManager:
    """Capture exact snapshots in caller-owned data and open them only in owner code."""

    def __init__(self, data: BackendEndpointData) -> None:
        self.data = data

    async def readiness(self) -> bool:
        try:
            return await self.data.readiness()
        except BackendEndpointUnavailable:
            return False

    async def capture(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None = None,
    ) -> BackendEndpointSubject:
        if type(registry_id) is not UUID or type(request_id) is not UUID:
            raise BackendEndpointUnavailable()
        try:
            typed_binding = (
                None
                if binding is None
                else binding
                if isinstance(binding, BackendEndpointBinding)
                else BackendEndpointBinding.model_validate(binding)
            )
            raw = await self.data.capture_subject(
                registry_id, request_id, purpose, typed_binding
            )
            captured = (
                json.loads(raw, parse_constant=_reject_json_constant)
                if type(raw) is str
                else raw
            )
            if type(captured) is not dict:
                raise BackendEndpointUnavailable()
            if (
                set(captured) == {"status", "reason"}
                and captured["status"] == "rejected"
            ):
                raise BackendEndpointAuthorityRejected(captured["reason"])
            if (
                set(captured) != _CAPTURE_FIELDS
                or captured["status"] != "ok"
                or type(captured["subject"]) is not str
            ):
                raise BackendEndpointUnavailable()
            return make_backend_endpoint_subject(
                registry_id,
                request_id=request_id,
                purpose=purpose,
                binding=typed_binding,
                subject=captured["subject"],
            )
        except (BackendEndpointUnavailable, BackendEndpointAuthorityRejected):
            raise
        except Exception:
            raise BackendEndpointUnavailable() from None

    async def open(
        self,
        subject: BackendEndpointSubject,
        *,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        authority: BackendEndpointAuthority | None = None,
    ) -> BackendEndpointSnapshot:
        request = decode_backend_endpoint_subject(subject)
        typed_authority = (
            authority
            if authority is None or isinstance(authority, BackendEndpointAuthority)
            else BackendEndpointAuthority.model_validate(authority)
        )
        if (
            request.request_id != request_id
            or request.purpose != purpose
            or request.authority != typed_authority
        ):
            raise BackendEndpointUnavailable()
        try:
            raw = await self.data.open_subject(
                request.subject,
                request.registry_id,
                request.request_id,
                request.purpose,
                request.authority,
            )
        except BackendEndpointUnavailable:
            raise
        except Exception:
            raise BackendEndpointUnavailable() from None
        if type(raw) is not str:
            raise BackendEndpointUnavailable()
        return _snapshot(raw, request)


__all__ = [
    "BackendEndpointAuthorityRejected",
    "BackendEndpointData",
    "BackendEndpointManager",
    "BackendEndpointSnapshot",
    "BackendEndpointSource",
    "BackendEndpointUnavailable",
]
