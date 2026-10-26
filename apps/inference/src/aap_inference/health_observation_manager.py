"""Credential-owning, transaction-bound backend health observations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import httpx
from contextlib import asynccontextmanager
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID


from aap_inference_contract.health_observation import (
    HEALTH_OBSERVATION_PROTOCOL,
    BackendHealthObservationReply,
    BackendHealthObservationRequest,
    BackendHealthObservationUnavailable,
    decode_health_observation_request,
)

from aap_inference_contract.health_observation_outcome import (
    health_observation_outcome_text,
)

from .backend_transport import BackendTransport

_SNAPSHOT_FIELDS = {
    "backend_json",
    "gpu_health_challenge",
    "timeout_seconds",
}
_BACKEND_FIELDS = {
    "id",
    "url",
    "auth_method",
    "auth_token",
    "gpu_resource_id",
    "vram_budget_mb",
    "extra_params",
}
_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_CONSTRUCTOR_ERRORS = {ValueError, TypeError, AttributeError, OverflowError}


class BackendHealthObservationData(Protocol):
    async def open_subject(
        self,
        subject: str,
        registry_id: UUID,
        request_id: UUID,
    ) -> str: ...

    async def seal_observation(
        self,
        subject: str,
        registry_id: UUID,
        request_id: UUID,
        observation_json: str,
    ) -> str: ...

    async def readiness(self) -> bool: ...


class HealthObservationClient(Protocol):
    async def health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict | None]: ...

    async def setup(self) -> Any: ...


ClientFactory = Callable[[Any], HealthObservationClient]


@asynccontextmanager
async def _forbid_gpu_dispatch(_operation, _scope):
    raise RuntimeError("health observation cannot enter GPU dispatch")
    yield None


def _ignore_observation(_backend_id, _outcome, _seconds) -> None:
    return None


def _default_client_factory(backend, timeout_seconds: float) -> BackendTransport:
    options = SimpleNamespace(
        ml_health_timeout=timeout_seconds,
        ml_predict_timeout=timeout_seconds,
    )
    return BackendTransport(
        backend,
        options=options,
        dispatch_context=_forbid_gpu_dispatch,
        observe=_ignore_observation,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _subject_sha256(subject: str) -> str:
    return hashlib.sha256(subject.encode("ascii")).hexdigest()


def _load_subject_snapshot(raw_text: str) -> tuple[str, str | None, float]:
    if type(raw_text) is not str:
        raise BackendHealthObservationUnavailable()
    try:
        snapshot = json.loads(raw_text, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise BackendHealthObservationUnavailable() from None
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise BackendHealthObservationUnavailable()
    backend_json = snapshot["backend_json"]
    challenge = snapshot["gpu_health_challenge"]
    timeout = snapshot["timeout_seconds"]
    if type(backend_json) is not str:
        raise BackendHealthObservationUnavailable()
    if challenge is not None and (
        type(challenge) is not str or _CHALLENGE_RE.fullmatch(challenge) is None
    ):
        raise BackendHealthObservationUnavailable()
    if type(timeout) not in {int, float}:
        raise BackendHealthObservationUnavailable()
    try:
        numeric_timeout = float(timeout)
    except (OverflowError, TypeError, ValueError):
        raise BackendHealthObservationUnavailable() from None
    if not math.isfinite(numeric_timeout):
        raise BackendHealthObservationUnavailable()
    # Zero and negative settings intentionally reach the existing httpx transport:
    # its legacy behavior owns how those configurations fail.
    return backend_json, challenge, numeric_timeout


def _legacy_constructor_error(error: BaseException) -> dict | None:
    if type(error) not in _LEGACY_CONSTRUCTOR_ERRORS:
        return None
    return {
        "kind": "legacy_constructor_error",
        "error_type": type(error).__name__,
        "error_message": str(error),
    }


def _backend_from_json(backend_json: str):
    try:
        endpoint = json.loads(backend_json, parse_constant=_reject_json_constant)
    except ValueError as error:
        # Python's JSON integer conversion errors were historically raised before
        # the health I/O phase; preserve them as the same generic constructor error.
        raise ValueError(str(error)) from None
    if not isinstance(endpoint, dict) or set(endpoint) != _BACKEND_FIELDS:
        raise BackendHealthObservationUnavailable()
    return SimpleNamespace(**endpoint, health_meta=None)


class BackendHealthObservationManager:
    """Observe one backend endpoint snapshot and seal its raw result."""

    def __init__(
        self,
        data: BackendHealthObservationData,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.data = data
        self.client_factory = client_factory

    async def readiness(self) -> bool:
        try:
            return await self.data.readiness()
        except BackendHealthObservationUnavailable:
            return False

    async def execute(
        self,
        request: BackendHealthObservationRequest,
    ) -> BackendHealthObservationReply:
        request = decode_health_observation_request(request)
        raw_subject = await self.data.open_subject(
            request.subject,
            request.registry_id,
            request.request_id,
        )
        backend_json, challenge, timeout_seconds = _load_subject_snapshot(raw_subject)
        try:
            backend = _backend_from_json(backend_json)
            client = (
                _default_client_factory(backend, timeout_seconds)
                if self.client_factory is None
                else self.client_factory(backend)
            )
        except BackendHealthObservationUnavailable:
            raise
        except Exception as error:
            failure = _legacy_constructor_error(error)
            if failure is None:
                raise BackendHealthObservationUnavailable() from None
            return await self._seal_reply(request, failure)

        try:
            if challenge is None:
                healthy, meta = await client.health_meta()
            else:
                healthy, meta = await client.health_meta(gpu_health_challenge=challenge)
        except Exception as error:
            failure = _legacy_constructor_error(error)
            if failure is None:
                failure = {
                    "kind": "legacy_health_error",
                    "error_type": "InvalidURL"
                    if isinstance(error, httpx.InvalidURL)
                    else "RuntimeError",
                    "error_message": str(error),
                }
            else:
                failure["kind"] = "legacy_health_error"
            return await self._seal_reply(request, failure)
        if type(healthy) is not bool or (meta is not None and type(meta) is not dict):
            raise BackendHealthObservationUnavailable()
        setup_failed = False
        setup = None
        if healthy and meta is not None:
            try:
                setup = await client.setup()
            except Exception:
                setup_failed = True
                setup = None
        return await self._seal_reply(
            request,
            {
                "kind": "observed",
                "healthy": healthy,
                "meta": meta,
                "setup_failed": setup_failed,
                "setup": setup,
            },
        )

    async def _seal_reply(
        self,
        request: BackendHealthObservationRequest,
        outcome: dict,
    ) -> BackendHealthObservationReply:
        evidence = await self.data.seal_observation(
            request.subject,
            request.registry_id,
            request.request_id,
            health_observation_outcome_text(outcome),
        )
        if type(evidence) is not str:
            raise BackendHealthObservationUnavailable()
        try:
            return BackendHealthObservationReply(
                schema=HEALTH_OBSERVATION_PROTOCOL,
                request_id=request.request_id,
                registry_id=request.registry_id,
                subject_sha256=_subject_sha256(request.subject),
                evidence=evidence,
            )
        except (TypeError, ValueError):
            raise BackendHealthObservationUnavailable() from None


__all__ = ["BackendHealthObservationManager", "BackendHealthObservationData"]
