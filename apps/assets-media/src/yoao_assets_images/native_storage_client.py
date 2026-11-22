"""Bounded client for the five finite Native pyramid signing routes."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from . import contract as c
from .native_storage_contract import (
    NATIVE_READ_REFERENCES_PATH,
    NATIVE_RETIREMENT_PREFIX_REFERENCE_PATH,
    NATIVE_RETIREMENT_REFERENCES_PATH,
    NATIVE_VERIFY_REFERENCE_PATH,
    NATIVE_WRITE_REFERENCE_PATH,
    NativePyramidIoReferences,
    NativePyramidPrefixIoReference,
    NativePyramidReadIoRequest,
    NativePyramidRetirementIoRequest,
    NativePyramidRetirementPrefixIoRequest,
    NativePyramidWriteIoRequest,
    require_read_reference_scope,
    require_retirement_prefix_reference_scope,
    require_retirement_reference_scope,
    require_write_reference_scope,
)


class NativePyramidStorageClient:
    """Request only the exact Native scopes issued by the Assets Source owner."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30,
        http: httpx.AsyncBaseTransport | httpx.AsyncClient | None = None,
    ):
        parsed = urlsplit(base_url)
        try:
            parsed.port
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and len(token) >= 32
                and token.isascii()
                and not any(character.isspace() for character in token)
                and 0 < timeout_seconds <= 3600
            )
        except ValueError:
            valid = False
        if not valid:
            raise c.ImageFailure("storage_unavailable")
        if http is not None and not isinstance(
            http, (httpx.AsyncBaseTransport, httpx.AsyncClient)
        ):
            raise c.ImageFailure("storage_unavailable")
        self._url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._injected = http
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if isinstance(self._injected, httpx.AsyncClient):
            return self._injected
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._injected,
            )
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def _request(
        self,
        path: str,
        request: BaseModel,
        result_type: type[BaseModel],
    ) -> BaseModel:
        try:
            body = c.json_bytes(request)
            if len(body) > c.REQUEST_BYTES:
                raise c.ImageFailure("resource_budget_exceeded")
            client = self._http()
            async with client.stream(
                "POST",
                self._url + path,
                content=body,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
            ) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    data.extend(chunk)
                    if len(data) > c.REPLY_BYTES:
                        raise c.ImageFailure("resource_budget_exceeded")
                if not response.is_success:
                    c.decode_reply(bytes(data), result_type)
                    raise c.ImageFailure("invalid_response")
                return c.decode_reply(bytes(data), result_type)
        except c.ImageFailure:
            raise
        except httpx.HTTPError:
            raise c.ImageFailure("storage_unavailable") from None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise c.ImageFailure("invalid_response") from None

    async def write_reference(
        self, request: NativePyramidWriteIoRequest
    ) -> NativePyramidIoReferences:
        result = await self._request(
            NATIVE_WRITE_REFERENCE_PATH, request, NativePyramidIoReferences
        )
        try:
            require_write_reference_scope(request, result, "put")
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def verify_reference(
        self, request: NativePyramidWriteIoRequest
    ) -> NativePyramidIoReferences:
        result = await self._request(
            NATIVE_VERIFY_REFERENCE_PATH, request, NativePyramidIoReferences
        )
        try:
            require_write_reference_scope(request, result, "verify")
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def read_references(
        self, request: NativePyramidReadIoRequest
    ) -> NativePyramidIoReferences:
        result = await self._request(
            NATIVE_READ_REFERENCES_PATH, request, NativePyramidIoReferences
        )
        try:
            require_read_reference_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def retirement_references(
        self, request: NativePyramidRetirementIoRequest
    ) -> NativePyramidIoReferences:
        result = await self._request(
            NATIVE_RETIREMENT_REFERENCES_PATH, request, NativePyramidIoReferences
        )
        try:
            require_retirement_reference_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def retirement_prefix_reference(
        self, request: NativePyramidRetirementPrefixIoRequest
    ) -> NativePyramidPrefixIoReference:
        result = await self._request(
            NATIVE_RETIREMENT_PREFIX_REFERENCE_PATH,
            request,
            NativePyramidPrefixIoReference,
        )
        try:
            require_retirement_prefix_reference_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result
