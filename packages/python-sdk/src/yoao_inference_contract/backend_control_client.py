"""One fixed authenticated Inference control endpoint, with closed operations."""

from __future__ import annotations

import math
from urllib.parse import urlsplit

from .backend_control import (
    BACKEND_CONTROL_REPLY_MAX_BYTES,
    BackendControlReply,
    BackendControlRequest,
    BackendRequestOnlyProbeRequest,
    BackendSmokeRequest,
    BackendStatsSnapshotsRequest,
    backend_control_request_bytes,
    decode_backend_control_reply,
    decode_backend_control_request,
)


class BackendControlUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("Inference backend control unavailable")


class BackendControlHttpClient:
    """Call exactly one selected owner endpoint, without redirects or failover."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int | float | None = None,
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
            or (
                timeout_seconds is not None
                and type(timeout_seconds) not in (int, float)
            )
        ):
            raise BackendControlUnavailable()
        try:
            if timeout_seconds is not None and (
                timeout_seconds <= 0 or not math.isfinite(float(timeout_seconds))
            ):
                raise BackendControlUnavailable()
            parsed = urlsplit(base_url)
            _ = parsed.port
        except (OverflowError, ValueError):
            raise BackendControlUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BackendControlUnavailable()
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = (
            None if timeout_seconds is None else float(timeout_seconds)
        )

    async def execute(
        self,
        request: BackendControlRequest
        | BackendRequestOnlyProbeRequest
        | BackendSmokeRequest
        | BackendStatsSnapshotsRequest
        | dict
        | bytes
        | str,
    ) -> BackendControlReply:
        try:
            typed_request = decode_backend_control_request(request)
            body = backend_control_request_bytes(typed_request)
            import httpx
        except (ImportError, TypeError, ValueError):
            raise BackendControlUnavailable() from None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/v1/backends/control",
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise BackendControlUnavailable()
                    if (
                        response.headers.get("content-encoding", "identity")
                        .strip()
                        .lower()
                        != "identity"
                    ):
                        raise BackendControlUnavailable()
                    if (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                        != "application/json"
                    ):
                        raise BackendControlUnavailable()
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None and (
                        not declared_length.isascii()
                        or not declared_length.isdigit()
                        or int(declared_length) > BACKEND_CONTROL_REPLY_MAX_BYTES
                    ):
                        raise BackendControlUnavailable()
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > BACKEND_CONTROL_REPLY_MAX_BYTES:
                            raise BackendControlUnavailable()
                        chunks.append(chunk)
                    raw_reply = b"".join(chunks)
        except BackendControlUnavailable:
            raise
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, OverflowError):
            raise BackendControlUnavailable() from None
        try:
            return decode_backend_control_reply(typed_request, raw_reply)
        except ValueError:
            raise BackendControlUnavailable() from None


__all__ = ["BackendControlHttpClient", "BackendControlUnavailable"]
