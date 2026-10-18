"""Bounded HTTP transport for an explicitly selected media owner."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from . import contract as c
from .transport import (
    MediaReferences,
    SourceReference,
    SourceReferences,
    SourceReferencesRequest,
    InternalSourceReferenceRequest,
    _http_url,
    read_all_samples,
    read_all_timetable,
    read_samples_document,
)


class MediaClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlsplit(url)
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
            raise c.MediaFailure("storage_unavailable")
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    async def _request(self, path: str, request: BaseModel, result_type):
        try:
            body = c.json_bytes(request.model_dump(mode="json"))
            if len(body) > c.REQUEST_BYTES:
                raise c.MediaFailure("resource_budget_exceeded")
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
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
                            raise c.MediaFailure("invalid_response")
                    result = c.decode_reply(bytes(data), result_type)
                    if not response.is_success:
                        raise c.MediaFailure("storage_unavailable")
                    return result
        except c.MediaFailure:
            raise
        except httpx.HTTPError:
            raise c.MediaFailure("storage_unavailable") from None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise c.MediaFailure("invalid_response") from None

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if (
            definition is None
            or definition.role == "worker"
            or type(request) is not definition.request
        ):
            raise c.MediaFailure("invalid_request")
        result = await self._request(
            "/v1/commands/" + operation, request, definition.result
        )
        try:
            c.require_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.MediaFailure("invalid_response") from None
        return result

    async def source_reference(self, request: c.ReadSourceRequest) -> SourceReference:
        result = await self._request("/v1/source-reference", request, SourceReference)
        try:
            c.require_scope(request, result.source)
        except ValueError:
            raise c.MediaFailure("invalid_response") from None
        return result

    async def raw_source_reference(
        self, request: c.ReadSourceRequest
    ) -> SourceReference:
        result = await self._request(
            "/v1/raw-source-reference", request, SourceReference
        )
        try:
            c.require_scope(request, result.source)
            if result.video_url != result.original_url:
                raise ValueError("raw source reference selected a derived video")
        except ValueError:
            raise c.MediaFailure("invalid_response") from None
        return result

    async def internal_source_reference(
        self, request: c.InternalSourceRequest, *, expires_in: int = 3600
    ) -> SourceReference:
        result = await self._request(
            "/v1/internal-source-reference",
            InternalSourceReferenceRequest(source=request, expires_in=expires_in),
            SourceReference,
        )
        try:
            c.require_scope(request, result.source)
            if result.expires_in != expires_in:
                raise ValueError("source reference changed requested lifetime")
        except ValueError:
            raise c.MediaFailure("invalid_response") from None
        return result

    async def source_references(
        self, requests: list[c.ReadSourceRequest], *, raw_only: bool = False
    ) -> list[SourceReference]:
        async def read_batch(batch):
            try:
                result = await self._request(
                    "/v1/source-references",
                    SourceReferencesRequest(requests=batch, raw_only=raw_only),
                    SourceReferences,
                )
            except c.MediaFailure as exc:
                if exc.code != "resource_budget_exceeded" or len(batch) <= 1:
                    raise
                middle = len(batch) // 2
                return await read_batch(batch[:middle]) + await read_batch(
                    batch[middle:]
                )
            if len(result.references) != len(batch):
                raise c.MediaFailure("invalid_response")
            try:
                for request, reference in zip(batch, result.references, strict=True):
                    c.require_scope(request, reference.source)
            except ValueError:
                raise c.MediaFailure("invalid_response") from None
            return result.references

        references = []
        for start in range(0, len(requests), 50):
            references.extend(await read_batch(requests[start : start + 50]))
        return references

    async def _assets(self, path: str, request: BaseModel) -> MediaReferences:
        result = await self._request(path, request, MediaReferences)
        try:
            c.require_scope(request, result.state)
            for asset in result.state.assets:
                if asset.status == "ready":
                    if asset.url is None or asset.expires_in != result.expires_in:
                        raise ValueError("ready reference lacks a signed URL")
                    _http_url(asset.url)
        except ValueError:
            raise c.MediaFailure("invalid_response") from None
        return result

    async def assets_reference(self, request: c.AssetsRequest) -> MediaReferences:
        return await self._assets("/v1/assets-reference", request)

    async def internal_frame_reference(
        self, request: c.InternalFrameRequest
    ) -> MediaReferences:
        return await self._assets("/v1/internal-frame-reference", request)

    async def read_all_timetable(self, request: c.TimetableRequest):
        return await read_all_timetable(self, request)

    async def read_all_samples(self, request: c.SamplesRequest):
        return await read_all_samples(self, request)

    async def read_samples_document(self, request: c.SamplesRequest):
        return await read_samples_document(self, request)
