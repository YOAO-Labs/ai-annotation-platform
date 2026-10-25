"""One-attempt HTTP binding for the private guide-object capability."""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

import httpx

from .guide_object_contract import (
    GuideObjectFailure,
    GuideObjectRequest,
    GuideObjectSuccess,
    decode_guide_object_reply,
    decode_guide_object_request,
)


class GuideObjectHttpClient:
    def __init__(
        self,
        origin: str,
        token: str,
        *,
        timeout_seconds: float = 15.0,
        max_request_bytes: int = 65_536,
        max_response_bytes: int = 65_536,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            parsed = urlsplit(origin)
            parsed.port
            valid_origin = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() or ord(char) < 32 for char in origin)
            )
        except (TypeError, ValueError):
            valid_origin = False
        if not valid_origin:
            raise ValueError("Guide object origin must be an HTTP(S) origin")
        if (
            type(token) is not str
            or not token.isascii()
            or len(token) < 32
            or any(char.isspace() for char in token)
        ):
            raise ValueError("Guide object service token is invalid")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0.1 <= timeout_seconds <= 120
        ):
            raise ValueError("Guide object timeout is invalid")
        for budget in (max_request_bytes, max_response_bytes):
            if type(budget) is not int or not 1 <= budget <= 1_048_576:
                raise ValueError("Guide object body budget is invalid")
        self._url = origin.rstrip("/") + "/v1/guide-objects/execute"
        self._token = token
        self._timeout = timeout_seconds
        self._request_budget = max_request_bytes
        self._response_budget = max_response_bytes
        self._transport = transport

    async def execute(self, request: GuideObjectRequest) -> GuideObjectSuccess:
        request = decode_guide_object_request(request)
        body = json.dumps(
            request.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        if len(body) > self._request_budget:
            raise GuideObjectFailure("unavailable")
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                # A timeout can leave an object effect uncertain. Never replay it.
                async with client.stream(
                    "POST",
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                    content=body,
                ) as response:
                    if response.status_code == 500:
                        raise GuideObjectFailure("storage_error")
                    if response.status_code != 200:
                        raise GuideObjectFailure("unavailable")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(payload) + len(chunk) > self._response_budget:
                            raise GuideObjectFailure("unavailable")
                        payload.extend(chunk)
                    return decode_guide_object_reply(request, bytes(payload))
        except GuideObjectFailure:
            raise
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            OSError,
            TimeoutError,
            ValueError,
            UnicodeError,
            RecursionError,
        ):
            raise GuideObjectFailure("unavailable") from None
