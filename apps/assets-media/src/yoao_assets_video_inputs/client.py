"""Authenticated, bounded HTTP client for private retained-video control."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel

from . import execution as c
from . import http_contract as h
from . import values as v


class VideoInputClient:
    """Call only the ten read/preparation/reference video control operations."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client=None,
        timeout_seconds: float = 30,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("video input control requires an absolute HTTP base URL")
        if (
            len(token) < 32
            or not token.isascii()
            or any(char.isspace() for char in token)
        ):
            raise ValueError("video input control requires a 32+ character ASCII token")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("invalid video input control timeout")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owned = client is None
        if client is None:
            import httpx

            client = httpx.AsyncClient(
                timeout=timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            )
        self._client = client

    async def close(self):
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def _invoke(self, operation: str, request: BaseModel) -> BaseModel:
        definition = h.HTTP_OPERATIONS.get(operation)
        if definition is None or type(request) is not definition[0]:
            raise c.VideoInputFailure("invalid_request")
        try:
            body = h.json_bytes(request)
            if len(body) > v.REQUEST_BYTES:
                raise c.VideoInputFailure("resource_budget_exceeded")
            async with self._client.stream(
                "POST",
                f"{self._base_url}{h.HTTP_PREFIX}/{operation}",
                content=body,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
                auth=None,
                follow_redirects=False,
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > v.REPLY_BYTES:
                        raise c.VideoInputFailure("resource_budget_exceeded")
                    raw.extend(chunk)
                result = h.decode_reply(bytes(raw), definition[1])
                if response.status_code != 200:
                    raise c.VideoInputFailure("invalid_response")
                h.require_scope(operation, request, result)
                return result
        except c.VideoInputFailure:
            raise
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            raise c.VideoInputFailure("invalid_response") from None
        except Exception:
            # Do not include request URLs, authorization headers or body data in
            # the public exception or any log emitted by this client.
            raise c.VideoInputFailure("storage_unavailable") from None

    async def manifest(self, request: c.VideoManifestScope) -> c.VideoManifestHeader:
        return await self._invoke("manifest", request)

    async def members(self, request: c.VideoManifestPageRequest) -> c.VideoManifestPage:
        return await self._invoke("members", request)

    async def prepare(
        self, request: c.PrepareVideoMemberRequest
    ) -> c.VideoMemberReceipt:
        return await self._invoke("prepare", request)

    async def seal(self, request: c.SealVideoManifestRequest) -> c.VideoManifestHeader:
        return await self._invoke("seal", request)

    async def input(self, request: c.ReadVideoInputRequest) -> c.VideoInputRecord:
        return await self._invoke("input", request)

    async def verify(self, request: c.LockVideoInputRequest) -> c.VideoInputRecord:
        return await self._invoke("verify", request)

    async def times(self, request: v.FramePageRequest) -> v.FrameTimePage:
        return await self._invoke("times", request)

    async def require_frame(self, request: c.VideoFrameRequest) -> c.VideoFrameRecord:
        return await self._invoke("require-frame", request)

    async def frame(self, request: c.VideoFrameRequest) -> c.VideoFrameRecord:
        return await self._invoke("frame", request)

    async def reference(self, request: c.VideoFrameRequest) -> v.FrameReference:
        return await self._invoke("reference", request)
