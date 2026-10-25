"""Authenticated HTTP client for the single-command routing owner wire."""

from __future__ import annotations

import math
from urllib.parse import urlsplit

from .routing import (
    ROUTING_REPLY_MAX_BYTES,
    RoutingCommand,
    RoutingResult,
    RoutingRuntimeRequest,
    RoutingRuntimeResult,
    RoutingStatusRequest,
    RoutingUnavailable,
    decode_routing_command,
    decode_routing_result,
    decode_routing_runtime_request,
    decode_routing_runtime_result,
    decode_routing_status_request,
    decode_routing_status_result,
    make_routing_status_request,
    routing_command_bytes,
    routing_runtime_request_bytes,
    routing_status_request_bytes,
)
from .scope_retirement import (
    SCOPE_RETIREMENT_REPLY_MAX_BYTES,
    ScopeRetirementRequest,
    ScopeRetirementResult,
    ScopeRetirementUnavailable,
    decode_scope_retirement_request,
    decode_scope_retirement_result,
    scope_retirement_request_bytes,
)
from .video_execution import (
    VIDEO_EXECUTION_STATUS_PATH,
    VideoScopeRejected,
    decode_video_execution_status_request,
    decode_video_execution_status_result,
    video_execution_status_request_bytes,
)


class _AmbiguousRoutingResponse(Exception):
    """The command may have committed but its acknowledgement was untrusted."""


class RoutingHttpClient:
    """Submit a routing command once and recover ambiguity through one status read."""

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
            raise RoutingUnavailable()
        try:
            if timeout_seconds <= 0 or not math.isfinite(float(timeout_seconds)):
                raise RoutingUnavailable()
            parsed = urlsplit(base_url)
            _ = parsed.port
        except (OverflowError, ValueError):
            raise RoutingUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RoutingUnavailable()
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = float(timeout_seconds)

    async def _post(
        self,
        path: str,
        body: bytes,
        *,
        max_reply_bytes: int = ROUTING_REPLY_MAX_BYTES,
    ) -> bytes:
        try:
            import httpx
        except ImportError:
            raise RoutingUnavailable() from None

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
                        raise _AmbiguousRoutingResponse()
                    if (
                        response.headers.get("content-encoding", "identity")
                        .strip()
                        .lower()
                        != "identity"
                    ):
                        raise _AmbiguousRoutingResponse()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise _AmbiguousRoutingResponse()
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        if (
                            not declared_length.isascii()
                            or not declared_length.isdigit()
                        ):
                            raise _AmbiguousRoutingResponse()
                        try:
                            if int(declared_length) > max_reply_bytes:
                                raise _AmbiguousRoutingResponse()
                        except ValueError:
                            raise _AmbiguousRoutingResponse() from None
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > max_reply_bytes:
                            raise _AmbiguousRoutingResponse()
                        chunks.append(chunk)
                    return b"".join(chunks)
        except _AmbiguousRoutingResponse:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise _AmbiguousRoutingResponse() from None

    async def execute(
        self,
        command: RoutingCommand | dict | bytes | str,
    ) -> RoutingResult:
        typed_command = decode_routing_command(command)
        body = routing_command_bytes(typed_command)
        try:
            raw = await self._post("/v1/routes/command", body)
            try:
                result = decode_routing_result(typed_command, raw)
            except RoutingUnavailable:
                raise _AmbiguousRoutingResponse() from None
            if result.state != "committed":
                raise _AmbiguousRoutingResponse()
            return result
        except _AmbiguousRoutingResponse:
            return await self._recover_status(typed_command)

    async def _recover_status(self, command: RoutingCommand) -> RoutingResult:
        status_request = make_routing_status_request(command)
        try:
            result = await self.status(status_request)
        except RoutingUnavailable:
            raise RoutingUnavailable() from None
        if result.state != "committed":
            raise RoutingUnavailable()
        return result

    async def status(
        self,
        request: RoutingStatusRequest | dict | bytes | str,
    ) -> RoutingResult:
        typed_request = decode_routing_status_request(request)
        body = routing_status_request_bytes(typed_request)
        try:
            raw = await self._post("/v1/routes/status", body)
            return decode_routing_status_result(typed_request, raw)
        except _AmbiguousRoutingResponse:
            raise RoutingUnavailable() from None

    async def runtime(
        self,
        request: RoutingRuntimeRequest | dict | bytes | str,
    ) -> RoutingRuntimeResult:
        typed_request = decode_routing_runtime_request(request)
        body = routing_runtime_request_bytes(typed_request)
        try:
            raw = await self._post("/v1/routes/runtime", body)
            return decode_routing_runtime_result(typed_request, raw)
        except _AmbiguousRoutingResponse:
            # This is an observation only; unlike command receipts it has no
            # write to recover, so malformed or lost replies fail immediately.
            raise RoutingUnavailable() from None

    async def retire_scope(
        self,
        request: ScopeRetirementRequest | dict | bytes | str,
    ) -> ScopeRetirementResult:
        typed_request = decode_scope_retirement_request(request)
        body = scope_retirement_request_bytes(typed_request)
        try:
            raw = await self._post(
                "/v1/routes/retire",
                body,
                max_reply_bytes=SCOPE_RETIREMENT_REPLY_MAX_BYTES,
            )
            return decode_scope_retirement_result(typed_request, raw)
        except ScopeRetirementUnavailable:
            # The commit acknowledgement may have been lost. One read recovers
            # the durable retirement by its immutable identity; the control is
            # never reposted by this client.
            return await self.scope_retirement_status(typed_request)
        except _AmbiguousRoutingResponse:
            return await self.scope_retirement_status(typed_request)

    async def scope_retirement_status(
        self,
        request: ScopeRetirementRequest | dict | bytes | str,
    ) -> ScopeRetirementResult:
        typed_request = decode_scope_retirement_request(request)
        body = scope_retirement_request_bytes(typed_request)
        try:
            raw = await self._post(
                "/v1/routes/retirement-status",
                body,
                max_reply_bytes=SCOPE_RETIREMENT_REPLY_MAX_BYTES,
            )
            return decode_scope_retirement_result(typed_request, raw)
        except (_AmbiguousRoutingResponse, ScopeRetirementUnavailable):
            raise RoutingUnavailable() from None

    async def video_execution_status(self, request):
        proof = decode_video_execution_status_request(request)
        body = video_execution_status_request_bytes(proof)
        try:
            raw = await self._post(
                VIDEO_EXECUTION_STATUS_PATH,
                body,
                max_reply_bytes=ROUTING_REPLY_MAX_BYTES,
            )
            return decode_video_execution_status_result(proof, raw)
        except (_AmbiguousRoutingResponse, VideoScopeRejected):
            # This is a proof-bound read. Invalid/unknown owner responses never
            # expose Native details to the caller or trigger a write retry.
            raise RoutingUnavailable() from None


__all__ = ["RoutingHttpClient"]
