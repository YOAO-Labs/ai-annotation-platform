"""Single-attempt authenticated HTTP client for the backend probe capability."""

from __future__ import annotations

import math
import uuid
from urllib.parse import urlsplit

from .capabilities import CapabilityLimits
from .probe import (
    BACKEND_PROBE_REPLY_MAX_BYTES,
    BackendProbeInvalidRequest,
    BackendProbeReply,
    BackendProbeRequest,
    BackendProbeUnavailable,
    backend_probe_request_bytes,
    decode_backend_probe_reply,
    decode_backend_probe_request,
    make_backend_probe_request,
    reply_modalities,
)
from .health_observation import (
    HEALTH_OBSERVATION_REPLY_MAX_BYTES,
    BackendHealthObservationInvalidRequest,
    BackendHealthObservationReply,
    BackendHealthObservationRequest,
    BackendHealthObservationUnavailable,
    decode_health_observation_reply,
    decode_health_observation_request,
    health_observation_request_bytes,
)


class BackendProbeHttpClient:
    """Call exactly one selected Inference provider, without redirect or fallback."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int | float = 30,
    ) -> None:
        if (
            type(base_url) is not str
            or not base_url
            or base_url.strip() != base_url
            or any(character.isspace() for character in base_url)
            or type(token) is not str
            or not token
            or not token.isascii()
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
            or type(timeout_seconds) not in (int, float)
        ):
            raise BackendProbeUnavailable()
        try:
            if timeout_seconds <= 0 or not math.isfinite(float(timeout_seconds)):
                raise BackendProbeUnavailable()
            parsed = urlsplit(base_url)
            _ = parsed.port
        except (OverflowError, ValueError):
            raise BackendProbeUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BackendProbeUnavailable()
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = float(timeout_seconds)

    async def execute(
        self, request: BackendProbeRequest | dict | bytes | str
    ) -> BackendProbeReply:
        typed_request = decode_backend_probe_request(request)
        body = backend_probe_request_bytes(typed_request)
        try:
            import httpx
        except ImportError:
            raise BackendProbeUnavailable() from None

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/v1/backends/probe",
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise BackendProbeUnavailable()
                    if (
                        response.headers.get("content-encoding", "identity")
                        .strip()
                        .lower()
                        != "identity"
                    ):
                        raise BackendProbeUnavailable()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise BackendProbeUnavailable()
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        if (
                            not declared_length.isascii()
                            or not declared_length.isdigit()
                        ):
                            raise BackendProbeUnavailable()
                        try:
                            if int(declared_length) > BACKEND_PROBE_REPLY_MAX_BYTES:
                                raise BackendProbeUnavailable()
                        except ValueError:
                            raise BackendProbeUnavailable() from None
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > BACKEND_PROBE_REPLY_MAX_BYTES:
                            raise BackendProbeUnavailable()
                        chunks.append(chunk)
                    response_body = b"".join(chunks)
        except BackendProbeUnavailable:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise BackendProbeUnavailable() from None

        try:
            return decode_backend_probe_reply(typed_request, response_body)
        except BackendProbeInvalidRequest:
            raise BackendProbeUnavailable() from None

    async def probe_modalities(
        self,
        registry_id: uuid.UUID,
        probe_subject: str,
        *,
        timeout: int | float,
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


class BackendHealthObservationHttpClient:
    """Make one authenticated request to the selected observation owner."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int | float = 30,
    ) -> None:
        if (
            type(base_url) is not str
            or not base_url
            or base_url.strip() != base_url
            or any(character.isspace() for character in base_url)
            or type(token) is not str
            or not token
            or not token.isascii()
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
            or type(timeout_seconds) not in (int, float)
        ):
            raise BackendHealthObservationUnavailable()
        try:
            if timeout_seconds <= 0 or not math.isfinite(float(timeout_seconds)):
                raise BackendHealthObservationUnavailable()
            parsed = urlsplit(base_url)
            _ = parsed.port
        except (OverflowError, ValueError):
            raise BackendHealthObservationUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BackendHealthObservationUnavailable()
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = float(timeout_seconds)

    async def execute(
        self,
        request: BackendHealthObservationRequest | dict | bytes | str,
    ) -> BackendHealthObservationReply:
        typed_request = decode_health_observation_request(request)
        body = health_observation_request_bytes(typed_request)
        try:
            import httpx
        except ImportError:
            raise BackendHealthObservationUnavailable() from None

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/v1/backends/health-observation",
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise BackendHealthObservationUnavailable()
                    if (
                        response.headers.get("content-encoding", "identity")
                        .strip()
                        .lower()
                        != "identity"
                    ):
                        raise BackendHealthObservationUnavailable()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise BackendHealthObservationUnavailable()
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        if (
                            not declared_length.isascii()
                            or not declared_length.isdigit()
                        ):
                            raise BackendHealthObservationUnavailable()
                        try:
                            if (
                                int(declared_length)
                                > HEALTH_OBSERVATION_REPLY_MAX_BYTES
                            ):
                                raise BackendHealthObservationUnavailable()
                        except ValueError:
                            raise BackendHealthObservationUnavailable() from None
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > HEALTH_OBSERVATION_REPLY_MAX_BYTES:
                            raise BackendHealthObservationUnavailable()
                        chunks.append(chunk)
                    response_body = b"".join(chunks)
        except BackendHealthObservationUnavailable:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise BackendHealthObservationUnavailable() from None

        try:
            return decode_health_observation_reply(typed_request, response_body)
        except BackendHealthObservationInvalidRequest:
            raise BackendHealthObservationUnavailable() from None


__all__ = ["BackendHealthObservationHttpClient", "BackendProbeHttpClient"]
