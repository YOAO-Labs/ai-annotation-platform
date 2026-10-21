"""Credential-owning setup probes over encrypted, transaction-bound snapshots."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BACKEND_PROBE_PROTOCOL,
    BackendProbeFailure,
    BackendProbeRequest,
    BackendProbeSuccess,
    BackendProbeUnavailable,
    decode_backend_probe_request,
    make_backend_probe_request,
    reply_modalities,
)

from .capabilities import derive_modalities
from .probe import BackendSetupLocator, probe_backend_capabilities


class BackendProbeData(Protocol):
    async def open_subject(self, probe_subject: str, registry_id: UUID) -> str: ...

    async def readiness(self) -> bool: ...


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _materialization_failure(request: BackendProbeRequest) -> BackendProbeFailure:
    return BackendProbeFailure(
        schema=BACKEND_PROBE_PROTOCOL,
        ok=False,
        request_id=request.request_id,
        registry_id=request.registry_id,
        subject_sha256=hashlib.sha256(
            request.probe_subject.encode("ascii")
        ).hexdigest(),
        error="legacy_materialization_failure",
    )


def _setup_locator(request: BackendProbeRequest, raw_json: str) -> BackendSetupLocator:
    endpoint: Any = json.loads(raw_json, parse_constant=_reject_json_constant)
    required = {"id", "url", "auth_method", "auth_token", "extra_params"}
    if not isinstance(endpoint, dict) or set(endpoint) != required:
        raise ValueError("backend snapshot has an invalid shape")
    raw_id = endpoint["id"]
    if type(raw_id) is not str:
        raise ValueError("backend snapshot identity is invalid")
    backend_id = UUID(raw_id)
    if backend_id != request.registry_id:
        raise ValueError("backend snapshot identity does not match the request")
    if type(endpoint["url"]) is not str or (
        endpoint["auth_method"] is not None and type(endpoint["auth_method"]) is not str
    ):
        raise ValueError("backend snapshot endpoint fields are invalid")
    if endpoint["auth_token"] is not None and type(endpoint["auth_token"]) is not str:
        raise ValueError("backend snapshot credential is invalid")
    # Keep JSONB values as stored; the existing probe boundary owns their legacy
    # coercion and fail-open behavior.
    return BackendSetupLocator(
        id=backend_id,
        url=endpoint["url"],
        auth_method=endpoint["auth_method"],
        auth_token=endpoint["auth_token"],
        extra_params=endpoint["extra_params"],
    )


class BackendProbeManager:
    """Open the exact Native snapshot, then reuse the existing capability probe."""

    def __init__(self, data: BackendProbeData) -> None:
        self.data = data

    async def readiness(self) -> bool:
        try:
            return await self.data.readiness()
        except BackendProbeUnavailable:
            return False

    async def execute(
        self, request: BackendProbeRequest
    ) -> BackendProbeSuccess | BackendProbeFailure:
        request = decode_backend_probe_request(request)
        raw_json = await self.data.open_subject(
            request.probe_subject,
            request.registry_id,
        )
        try:
            locator = _setup_locator(request, raw_json)
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            return _materialization_failure(request)

        capabilities = await probe_backend_capabilities(
            locator,
            timeout=request.timeout_seconds,
            limits=CapabilityLimits(**request.limits.model_dump()),
        )
        try:
            modalities = (
                derive_modalities(capabilities) if capabilities is not None else None
            )
        except Exception:
            # Legacy setup/capability projection failures remain fail-open.
            modalities = None
        return BackendProbeSuccess(
            schema=BACKEND_PROBE_PROTOCOL,
            ok=True,
            request_id=request.request_id,
            registry_id=request.registry_id,
            subject_sha256=hashlib.sha256(
                request.probe_subject.encode("ascii")
            ).hexdigest(),
            modalities=modalities,
        )

    async def probe_modalities(
        self,
        registry_id: UUID,
        probe_subject: str,
        *,
        timeout: float,
        limits: CapabilityLimits,
    ) -> list[str] | None:
        request = make_backend_probe_request(
            registry_id,
            probe_subject,
            timeout=timeout,
            limits=limits,
        )
        reply = await self.execute(request)
        return reply_modalities(request, reply)


__all__ = ["BackendProbeManager", "BackendProbeData"]
