"""Bounded authenticated HTTP client for the private image-media owner."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from . import contract as c
from .transport import (
    ImageAssetReferences,
    ImageAssetReferencesRequest,
    ImageGenerationReference,
    ImageGenerationReferenceRequest,
    ImageSourceReference,
    ImageSourceReferences,
    ImageSourceReferencesRequest,
    InternalImageSourceReferenceRequest,
    require_asset_reference_scope,
    require_generation_reference_scope,
    require_internal_source_reference_scope,
    require_source_reference_scope,
    require_source_references_scope,
)


class ImageClient:
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
            raise c.ImageFailure("storage_unavailable")
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    async def _request(
        self,
        path: str,
        request: BaseModel,
        result_type: type[BaseModel],
        *,
        reply_limit: int | None = None,
    ) -> BaseModel:
        try:
            if reply_limit is None:
                reply_limit = c.REPLY_BYTES
            body = c.json_bytes(request)
            if len(body) > c.REQUEST_BYTES:
                raise c.ImageFailure("resource_budget_exceeded")
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
                        if len(data) > reply_limit:
                            raise c.ImageFailure("resource_budget_exceeded")
                    if not response.is_success:
                        # A well-formed failure envelope preserves its typed error;
                        # success data on a failure status is a protocol violation.
                        c.decode_reply(bytes(data), result_type)
                        raise c.ImageFailure("invalid_response")
                    return c.decode_reply(bytes(data), result_type)
        except c.ImageFailure:
            raise
        except httpx.HTTPError:
            raise c.ImageFailure("storage_unavailable") from None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise c.ImageFailure("invalid_response") from None

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if (
            definition is None
            or definition.role in {"worker", "cleanup"}
            or type(request) is not definition.request
        ):
            raise c.ImageFailure("invalid_request")
        result = await self._request(
            "/v1/images/commands/" + operation,
            request,
            definition.result,
        )
        try:
            c.require_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def source_reference(
        self, request: c.ReadImageSourceRequest
    ) -> ImageSourceReference:
        result = await self._request(
            "/v1/images/source-reference", request, ImageSourceReference
        )
        try:
            require_source_reference_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def raw_source_reference(
        self, request: c.ReadImageSourceRequest
    ) -> ImageSourceReference:
        result = await self._request(
            "/v1/images/raw-source-reference", request, ImageSourceReference
        )
        try:
            require_source_reference_scope(request, result, raw_only=True)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def source_references(
        self, request: ImageSourceReferencesRequest
    ) -> ImageSourceReferences:
        async def read_batch(batch):
            batch_request = request.model_copy(update={"requests": batch})
            try:
                result = await self._request(
                    "/v1/images/source-references",
                    batch_request,
                    ImageSourceReferences,
                    reply_limit=c.PAGE_BYTES,
                )
            except c.ImageFailure as exc:
                if exc.code != "resource_budget_exceeded" or len(batch) <= 1:
                    raise
                middle = len(batch) // 2
                return await read_batch(batch[:middle]) + await read_batch(
                    batch[middle:]
                )
            try:
                require_source_references_scope(batch_request, result)
                if len(result.references) != len(batch):
                    raise ValueError("image provider omitted a requested source")
                indexed = {}
                for reference in result.references:
                    key = _source_reference_key(reference)
                    if key in indexed:
                        raise ValueError("image provider duplicated a source")
                    indexed[key] = reference
                requested = [_selector_key(item) for item in batch]
                if set(indexed) != set(requested):
                    raise ValueError("image provider substituted a source selector")
                ordered = [indexed[key] for key in requested]
                ordered_result = ImageSourceReferences(references=ordered)
                if len(c.json_bytes(ordered_result)) > c.PAGE_BYTES:
                    raise c.ImageFailure("resource_budget_exceeded")
                return ordered
            except c.ImageFailure as exc:
                if exc.code != "resource_budget_exceeded" or len(batch) <= 1:
                    raise
                middle = len(batch) // 2
                return await read_batch(batch[:middle]) + await read_batch(
                    batch[middle:]
                )
            except (ValueError, TypeError, AttributeError):
                raise c.ImageFailure("invalid_response") from None

        references = []
        encoded_size = 0
        for start in range(0, len(request.requests), 50):
            page = await read_batch(request.requests[start : start + 50])
            for reference in page:
                encoded_size += len(c.json_bytes(reference)) + 2
                if encoded_size > c.PAGE_BYTES:
                    raise c.ImageFailure("resource_budget_exceeded")
                references.append(reference)
        try:
            return ImageSourceReferences(references=references)
        except ValueError:
            raise c.ImageFailure("invalid_response") from None

    async def internal_source_reference(
        self, request: InternalImageSourceReferenceRequest
    ) -> ImageSourceReference:
        result = await self._request(
            "/v1/images/internal-source-reference",
            request,
            ImageSourceReference,
        )
        try:
            require_internal_source_reference_scope(request, result)
            if result.expires_in != request.expires_in:
                raise ValueError("image reference changed requested lifetime")
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def generation_reference(
        self, request: ImageGenerationReferenceRequest
    ) -> ImageGenerationReference:
        result = await self._request(
            "/v1/images/generation-reference",
            request,
            ImageGenerationReference,
        )
        try:
            require_generation_reference_scope(request, result)
            if result.expires_in != request.expires_in:
                raise ValueError("image reference changed requested lifetime")
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result

    async def asset_references(
        self, request: ImageAssetReferencesRequest
    ) -> ImageAssetReferences:
        result = await self._request(
            "/v1/images/asset-references", request, ImageAssetReferences
        )
        try:
            require_asset_reference_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ImageFailure("invalid_response") from None
        return result


def _selector_key(request: c.ActorImageSourceRequest) -> tuple:
    return request.actor_id, request.task_id, request.dataset_item_id


def _source_reference_key(reference: ImageSourceReference) -> tuple:
    if reference.actor_id is None:
        raise ValueError("actor source reference omitted its actor")
    source = reference.source
    return (
        reference.actor_id,
        source.task_id,
        None if source.task_id is not None else source.item_id,
    )
