"""Bounded bearer client for private tracker-input control."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel

from . import tracker as t
from . import tracker_http as h


class TrackerVideoInputClient:
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
            raise ValueError("tracker input control requires an absolute HTTP base URL")
        if (
            len(token) < 32
            or not token.isascii()
            or any(char.isspace() for char in token)
        ):
            raise ValueError(
                "tracker input control requires a 32+ character ASCII token"
            )
        if not 0 < timeout_seconds <= 120:
            raise ValueError("invalid tracker input control timeout")
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
            raise t.TrackerFailure("invalid_request")
        try:
            body = h.json_bytes(request)
            if len(body) > t.REQUEST_BYTES:
                raise t.TrackerFailure("resource_budget_exceeded")
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
                    if len(raw) + len(chunk) > t.REPLY_BYTES:
                        raise t.TrackerFailure("resource_budget_exceeded")
                    raw.extend(chunk)
                result = h.decode_reply(bytes(raw), definition[1])
                if response.status_code != 200:
                    raise t.TrackerFailure("invalid_response")
                h.require_scope(operation, request, result)
                return result
        except t.TrackerFailure:
            raise
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            raise t.TrackerFailure("invalid_response") from None
        except Exception:
            # Never include authorization, request bodies or signed source URLs.
            raise t.TrackerFailure("storage_unavailable") from None

    async def input(
        self, request: t.TrackerInputReadRequest
    ) -> t.TrackerVideoInputStatus:
        return await self._invoke("input", request)

    async def prepare(
        self, request: t.TrackerInputRequest
    ) -> t.TrackerVideoInputStatus:
        return await self._invoke("prepare", request)

    async def reference(
        self, request: t.TrackerSourceReferenceRequest
    ) -> t.TrackerVideoSourceReference:
        return await self._invoke("reference", request)
