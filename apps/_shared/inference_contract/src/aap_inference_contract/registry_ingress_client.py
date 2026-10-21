"""Authenticated one-shot HTTP client for Registry environment synchronization."""

from __future__ import annotations

import math
from urllib.parse import urlsplit

from .registry_ingress import (
    REGISTRY_ENV_SYNC_REPLY_MAX_BYTES,
    RegistryEnvSyncRequest,
    RegistryEnvSyncResult,
    RegistryEnvSyncStatusRequest,
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_request,
    decode_registry_env_sync_result,
    decode_registry_env_sync_status_request,
    decode_registry_env_sync_status_result,
    registry_env_sync_request_bytes,
    registry_env_sync_status_request,
    registry_env_sync_status_request_bytes,
)


class _AmbiguousRegistryEnvSyncResponse(Exception):
    """A command may have committed but its acknowledgement was not trustworthy."""


class RegistryEnvSyncHttpClient:
    """Submit a command once and recover ambiguous outcomes through one status read."""

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
            raise RegistryEnvSyncUnavailable()
        try:
            if timeout_seconds <= 0 or not math.isfinite(float(timeout_seconds)):
                raise RegistryEnvSyncUnavailable()
            parsed = urlsplit(base_url)
            _ = parsed.port
        except (OverflowError, ValueError):
            raise RegistryEnvSyncUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RegistryEnvSyncUnavailable()
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = float(timeout_seconds)

    async def _post(self, path: str, body: bytes) -> bytes:
        try:
            import httpx
        except ImportError:
            raise RegistryEnvSyncUnavailable() from None

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}{path}",
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise _AmbiguousRegistryEnvSyncResponse()
                    if (
                        response.headers.get("content-encoding", "identity")
                        .strip()
                        .lower()
                        != "identity"
                    ):
                        raise _AmbiguousRegistryEnvSyncResponse()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise _AmbiguousRegistryEnvSyncResponse()
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        if (
                            not declared_length.isascii()
                            or not declared_length.isdigit()
                        ):
                            raise _AmbiguousRegistryEnvSyncResponse()
                        try:
                            if int(declared_length) > REGISTRY_ENV_SYNC_REPLY_MAX_BYTES:
                                raise _AmbiguousRegistryEnvSyncResponse()
                        except ValueError:
                            raise _AmbiguousRegistryEnvSyncResponse() from None
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > REGISTRY_ENV_SYNC_REPLY_MAX_BYTES:
                            raise _AmbiguousRegistryEnvSyncResponse()
                        chunks.append(chunk)
                    return b"".join(chunks)
        except _AmbiguousRegistryEnvSyncResponse:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise _AmbiguousRegistryEnvSyncResponse() from None

    async def sync(
        self,
        request: RegistryEnvSyncRequest | dict | bytes | str,
    ) -> RegistryEnvSyncResult:
        typed_request = decode_registry_env_sync_request(request)
        body = registry_env_sync_request_bytes(typed_request)
        try:
            raw = await self._post("/v1/registry/env-sync", body)
            try:
                result = decode_registry_env_sync_result(typed_request, raw)
            except RegistryEnvSyncUnavailable:
                raise _AmbiguousRegistryEnvSyncResponse() from None
            if result.state != "committed":
                raise _AmbiguousRegistryEnvSyncResponse()
            return result
        except _AmbiguousRegistryEnvSyncResponse:
            return await self._recover_status(typed_request)

    async def _recover_status(
        self,
        request: RegistryEnvSyncRequest,
    ) -> RegistryEnvSyncResult:
        status_request = registry_env_sync_status_request(request)
        try:
            result = await self.status(status_request)
        except RegistryEnvSyncUnavailable:
            raise RegistryEnvSyncUnavailable() from None
        if result.state != "committed":
            raise RegistryEnvSyncUnavailable()
        return result

    async def status(
        self,
        request: RegistryEnvSyncStatusRequest | dict | bytes | str,
    ) -> RegistryEnvSyncResult:
        typed_request = decode_registry_env_sync_status_request(request)
        body = registry_env_sync_status_request_bytes(typed_request)
        try:
            raw = await self._post("/v1/registry/env-sync-status", body)
            return decode_registry_env_sync_status_result(typed_request, raw)
        except _AmbiguousRegistryEnvSyncResponse:
            raise RegistryEnvSyncUnavailable() from None


__all__ = ["RegistryEnvSyncHttpClient"]
