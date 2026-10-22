"""Authenticated HTTP client for the private Labeling video-writer owner."""

from __future__ import annotations

import math
from urllib.parse import urlsplit

from .video_writer_contract import (
    MAX_VIDEO_WRITER_REQUEST_BYTES,
    MAX_VIDEO_WRITER_RESPONSE_BYTES,
    VideoWriterCommand,
    VideoWriterInvalidRequest,
    VideoWriterReceipt,
    VideoWriterRejected,
    VideoWriterStatusRequest,
    VideoWriterUnavailable,
    decode_video_writer_api_reply,
    decode_video_writer_command,
    make_video_writer_status_request,
    video_writer_command_bytes,
    video_writer_command_digest,
    video_writer_status_request_bytes,
    video_writer_publication_request_bytes,
    video_writer_operation_id,
)

_REPLY_LIMIT_BYTES = MAX_VIDEO_WRITER_RESPONSE_BYTES


class _AmbiguousVideoWriterResponse(Exception):
    """The owner may have committed but did not return a trustworthy receipt."""


class VideoWriterHttpClient:
    """Submit one stable command and recover uncertain outcomes by exact status."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int | float = 30,
        client=None,
    ) -> None:
        if (
            type(base_url) is not str
            or not base_url
            or base_url.strip() != base_url
            or any(character.isspace() for character in base_url)
            or type(token) is not str
            or len(token) < 32
            or not token.isascii()
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
            or type(timeout_seconds) not in (int, float)
        ):
            raise VideoWriterUnavailable()
        try:
            parsed = urlsplit(base_url)
            parsed.port
            valid_timeout = (
                math.isfinite(float(timeout_seconds)) and 0 < timeout_seconds <= 3600
            )
        except (OverflowError, ValueError):
            raise VideoWriterUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not valid_timeout
        ):
            raise VideoWriterUnavailable()
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = float(timeout_seconds)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> VideoWriterHttpClient:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http_client(self):
        if self._client is None:
            await self.__aenter__()
        return self._client

    async def _post(self, path: str, body: bytes) -> tuple[int, bytes]:
        if type(body) is not bytes or len(body) > MAX_VIDEO_WRITER_REQUEST_BYTES:
            raise VideoWriterInvalidRequest()
        try:
            import httpx
        except ImportError:
            raise VideoWriterUnavailable() from None

        try:
            client = await self._http_client()
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
                if (
                    response.headers.get("content-encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise _AmbiguousVideoWriterResponse()
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type != "application/json":
                    raise _AmbiguousVideoWriterResponse()
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    if not declared_length.isascii() or not declared_length.isdigit():
                        raise _AmbiguousVideoWriterResponse()
                    if int(declared_length) > _REPLY_LIMIT_BYTES:
                        raise _AmbiguousVideoWriterResponse()
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > _REPLY_LIMIT_BYTES:
                        raise _AmbiguousVideoWriterResponse()
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks)
        except _AmbiguousVideoWriterResponse:
            raise
        except VideoWriterInvalidRequest:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise _AmbiguousVideoWriterResponse() from None
        except Exception:
            raise _AmbiguousVideoWriterResponse() from None

    @staticmethod
    def _same_receipt(
        receipt: VideoWriterReceipt,
        command: VideoWriterCommand,
        request_digest: str,
    ) -> bool:
        return (
            receipt.operation_id == command.operation_id
            and receipt.job_id == command.execution.job_id
            and receipt.run_id == command.execution.run_id
            and receipt.request_digest == request_digest
        )

    @staticmethod
    def _same_status_receipt(
        receipt: VideoWriterReceipt, request: VideoWriterStatusRequest
    ) -> bool:
        return (
            receipt.operation_id == request.operation_id
            and receipt.job_id == request.execution.job_id
            and receipt.run_id == request.execution.run_id
            and receipt.request_digest == request.request_digest
        )

    def _decode_execute(self, status_code: int, raw: bytes) -> VideoWriterReceipt:
        if status_code == 200:
            try:
                result = decode_video_writer_api_reply(raw, status=False)
            except VideoWriterRejected:
                raise
            except Exception:
                raise _AmbiguousVideoWriterResponse() from None
            if not isinstance(result, VideoWriterReceipt):
                raise _AmbiguousVideoWriterResponse()
            return result
        if status_code in {401, 409, 413, 422}:
            # A well-formed closed rejection is final; malformed errors are not
            # evidence that the transaction failed to commit.
            try:
                result = decode_video_writer_api_reply(raw, status=False)
            except (VideoWriterInvalidRequest, VideoWriterRejected):
                raise
            except Exception:
                raise _AmbiguousVideoWriterResponse() from None
            if isinstance(result, VideoWriterReceipt):
                raise _AmbiguousVideoWriterResponse()
            raise VideoWriterUnavailable()
        raise _AmbiguousVideoWriterResponse()

    async def execute(
        self, command: VideoWriterCommand | dict | bytes | str
    ) -> VideoWriterReceipt:
        typed = decode_video_writer_command(command)
        body = video_writer_command_bytes(typed)
        digest = video_writer_command_digest(typed)
        if len(body) > MAX_VIDEO_WRITER_REQUEST_BYTES:
            raise VideoWriterInvalidRequest()

        for attempt in range(2):
            try:
                status_code, raw = await self._post(
                    "/v1/labeling/video-writer/execute", body
                )
                receipt = self._decode_execute(status_code, raw)
                if not self._same_receipt(receipt, typed, digest):
                    raise _AmbiguousVideoWriterResponse()
                return receipt
            except _AmbiguousVideoWriterResponse:
                receipt = await self._recover_status(typed, digest)
                if receipt is not None:
                    return receipt
                if attempt == 1:
                    raise VideoWriterUnavailable() from None
        raise VideoWriterUnavailable()

    async def _recover_status(
        self, command: VideoWriterCommand, request_digest: str
    ) -> VideoWriterReceipt | None:
        status_request = make_video_writer_status_request(
            command.operation_id, command.execution, request_digest
        )
        body = video_writer_status_request_bytes(status_request)
        try:
            status_code, raw = await self._post(
                "/v1/labeling/video-writer/status", body
            )
            if status_code != 200:
                decode_video_writer_api_reply(raw, status=True)
                raise VideoWriterUnavailable()
            receipt = decode_video_writer_api_reply(raw, status=True)
        except (
            VideoWriterInvalidRequest,
            VideoWriterRejected,
            VideoWriterUnavailable,
        ):
            raise
        except Exception:
            raise VideoWriterUnavailable() from None
        if receipt is not None and not self._same_receipt(
            receipt, command, request_digest
        ):
            raise VideoWriterUnavailable()
        return receipt

    async def publication(self, execution) -> VideoWriterReceipt | None:
        from .video_writer_contract import decode_video_writer_publication_request

        body = video_writer_publication_request_bytes(execution)
        request = decode_video_writer_publication_request(body)
        try:
            status_code, raw = await self._post(
                "/v1/labeling/video-writer/status", body
            )
            receipt = decode_video_writer_api_reply(raw, status=True)
            if status_code != 200 or (
                receipt is not None
                and (
                    receipt.job_id != request.execution.job_id
                    or receipt.run_id != request.execution.run_id
                    or receipt.operation_id
                    != video_writer_operation_id(request.execution.run_id)
                )
            ):
                raise VideoWriterUnavailable()
            return receipt
        except (VideoWriterInvalidRequest, VideoWriterRejected, VideoWriterUnavailable):
            raise
        except Exception:
            raise VideoWriterUnavailable() from None

    async def status(
        self,
        operation_id,
        execution,
        request_digest: str,
    ) -> VideoWriterReceipt | None:
        status_request = make_video_writer_status_request(
            operation_id, execution, request_digest
        )
        body = video_writer_status_request_bytes(status_request)
        try:
            status_code, raw = await self._post(
                "/v1/labeling/video-writer/status", body
            )
            if status_code != 200:
                decode_video_writer_api_reply(raw, status=True)
                raise VideoWriterUnavailable()
            receipt = decode_video_writer_api_reply(raw, status=True)
            if receipt is not None and not self._same_status_receipt(
                receipt, status_request
            ):
                raise VideoWriterUnavailable()
            return receipt
        except (
            VideoWriterInvalidRequest,
            VideoWriterRejected,
            VideoWriterUnavailable,
        ):
            raise
        except Exception:
            raise VideoWriterUnavailable() from None


__all__ = ["VideoWriterHttpClient"]
