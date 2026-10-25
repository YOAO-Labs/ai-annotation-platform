"""Authenticated HTTP client for the independent Inference pipeline owner."""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

from pydantic import ValidationError

from .pipeline_contract import (
    PIPELINE_REQUEST_BODY_MAX_BYTES,
    PIPELINE_RESPONSE_BODY_MAX_BYTES,
    PIPELINE_PROTOCOL,
    PipelineCommandRequest,
    PipelineFailure,
    PipelineInvalidRequest,
    PipelinePostCommitFailure,
    PipelineQueryReply,
    PipelineQueryRequest,
    PipelineReply,
    PipelineStatusFailure,
    PipelineStatusReply,
    PipelineStatusRequest,
    PipelineSuccess,
    PipelineUnavailable,
    decode_pipeline_command_request,
    decode_pipeline_query_reply,
    decode_pipeline_query_request,
    decode_pipeline_reply,
    decode_pipeline_status_reply,
    decode_pipeline_status_request,
    pipeline_status_request,
)


class _AmbiguousOwnerResponse(Exception):
    """The command may have committed but did not receive a trustworthy ack."""


class PipelineHttpClient:
    """Small client whose command path never retries the mutation itself."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 5.0,
        max_request_bytes: int = PIPELINE_REQUEST_BODY_MAX_BYTES,
        max_response_bytes: int = PIPELINE_RESPONSE_BODY_MAX_BYTES,
        transport=None,
    ) -> None:
        if (
            type(base_url) is not str
            or not base_url
            or any(character.isspace() for character in base_url)
            or type(token) is not str
            or not token
            or not token.isascii()
            or any(character.isspace() for character in token)
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or type(max_request_bytes) is not int
            or type(max_response_bytes) is not int
            or not 1 <= max_request_bytes <= PIPELINE_REQUEST_BODY_MAX_BYTES
            or not 1 <= max_response_bytes <= PIPELINE_RESPONSE_BODY_MAX_BYTES
        ):
            raise PipelineUnavailable()
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise PipelineUnavailable()
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._transport = transport
        self._client = None

    async def _http_client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError:
                raise PipelineUnavailable() from None
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    @staticmethod
    def _request_bytes(request) -> bytes:
        try:
            return json.dumps(
                request.model_dump(mode="json", by_alias=True),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
        except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError):
            raise PipelineInvalidRequest() from None

    async def _post(self, path: str, request) -> tuple[int, bytes]:
        body = self._request_bytes(request)
        if len(body) > self.max_request_bytes:
            raise PipelineInvalidRequest()
        try:
            import httpx
        except ImportError:
            raise PipelineUnavailable() from None
        client = await self._http_client()
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}{path}",
                content=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as response:
                if response.status_code == 200:
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise _AmbiguousOwnerResponse()
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    if not declared_length.isascii() or not declared_length.isdigit():
                        raise _AmbiguousOwnerResponse()
                    try:
                        length = int(declared_length)
                        if length < 0 or length > self.max_response_bytes:
                            raise _AmbiguousOwnerResponse()
                    except ValueError:
                        raise _AmbiguousOwnerResponse() from None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise _AmbiguousOwnerResponse()
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks)
        except _AmbiguousOwnerResponse:
            raise
        except httpx.RequestError:
            raise _AmbiguousOwnerResponse() from None
        except httpx.HTTPError:
            raise _AmbiguousOwnerResponse() from None

    async def execute(
        self, command: PipelineCommandRequest | dict | bytes | str
    ) -> PipelineReply:
        request = decode_pipeline_command_request(command)
        try:
            status_code, raw = await self._post("/v1/pipelines/execute", request)
            if status_code >= 500:
                raise _AmbiguousOwnerResponse()
            if status_code != 200:
                raise PipelineUnavailable()
            try:
                reply = decode_pipeline_reply(request, raw, require_committed=True)
            except (
                PipelineUnavailable,
                ValidationError,
                RecursionError,
                ValueError,
                TypeError,
                UnicodeError,
            ):
                raise _AmbiguousOwnerResponse() from None
            if isinstance(reply, PipelineFailure):
                if reply.commit_state in {"unknown", "caller_pending"}:
                    raise _AmbiguousOwnerResponse()
                return reply
            if reply.commit_state != "committed":
                raise _AmbiguousOwnerResponse()
            return reply
        except _AmbiguousOwnerResponse:
            return await self._recover_command_status(request)

    async def _recover_command_status(
        self, command: PipelineCommandRequest
    ) -> PipelineSuccess:
        request = pipeline_status_request(command)
        try:
            status_code, raw = await self._post("/v1/pipelines/status", request)
            if status_code != 200:
                raise PipelineUnavailable()
            reply = decode_pipeline_status_reply(request, raw)
        except (
            _AmbiguousOwnerResponse,
            PipelineUnavailable,
            ValidationError,
            RecursionError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise PipelineUnavailable() from None
        if isinstance(reply, PipelineStatusFailure) or reply.state != "committed":
            raise PipelineUnavailable()
        if reply.resource_id is None:
            raise PipelineUnavailable()
        recovered = {
            "schema": PIPELINE_PROTOCOL,
            "ok": True,
            "operation_id": str(command.operation_id),
            "operation": command.operation,
            "actor_id": str(command.admitted_actor.id),
            "target_id": str(command.target_id)
            if command.target_id is not None
            else None,
            "result": {"resource_id": str(reply.resource_id)},
            "commit_state": "committed",
        }
        try:
            return decode_pipeline_reply(command, recovered, require_committed=True)
        except (
            PipelineInvalidRequest,
            PipelineUnavailable,
            ValidationError,
            RecursionError,
            ValueError,
            TypeError,
        ):
            raise PipelineUnavailable() from None

    async def query(
        self, query: PipelineQueryRequest | dict | bytes | str
    ) -> PipelineQueryReply:
        request = decode_pipeline_query_request(query)
        try:
            status_code, raw = await self._post("/v1/pipelines/query", request)
            if status_code == 500:
                raise PipelinePostCommitFailure()
            if status_code != 200:
                raise PipelineUnavailable()
            return decode_pipeline_query_reply(request, raw)
        except (
            _AmbiguousOwnerResponse,
            PipelineUnavailable,
            ValidationError,
            RecursionError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise PipelineUnavailable() from None

    async def status(
        self, status: PipelineStatusRequest | dict | bytes | str
    ) -> PipelineStatusReply:
        request = decode_pipeline_status_request(status)
        try:
            status_code, raw = await self._post("/v1/pipelines/status", request)
            if status_code != 200:
                raise PipelineUnavailable()
            return decode_pipeline_status_reply(request, raw)
        except (
            _AmbiguousOwnerResponse,
            PipelineUnavailable,
            ValidationError,
            RecursionError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise PipelineUnavailable() from None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                # Cleanup must not replace an operation result or its failure.
                return


__all__ = ["PipelineHttpClient"]
