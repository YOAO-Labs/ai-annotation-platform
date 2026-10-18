"""HTTP reference values and bounded image-backfill page iteration."""

from __future__ import annotations

from typing import Annotated, AsyncIterator, Literal, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .contract import (
    ActorImageSourceRequest,
    ImageBackfillPage,
    ImageFailure,
    Digest,
    InternalImageSourceRequest,
    ImagePyramidManifest,
    ImagePyramidSummary,
    ImageSource,
    ImageSourceSelector,
    ProfileVersion,
    PyramidGeneration,
    ReadImageGenerationRequest,
    Revision,
    ScanImageBackfillRequest,
    Value,
    require_scope,
)


class ImageSourceReference(Value):
    actor_id: UUID | None = None
    source: ImageSource
    original_url: StrictStr
    thumbnail_url: StrictStr | None
    expires_in: StrictInt = Field(ge=1, le=604800)

    @field_validator("original_url", "thumbnail_url")
    @classmethod
    def valid_url(cls, value):
        return _http_url(value)


class ImageSourceReferencesRequest(Value):
    requests: list[ActorImageSourceRequest] = Field(max_length=500)
    raw_only: StrictBool = False

    @model_validator(mode="after")
    def unique_sources(self):
        keys = [
            (row.actor_id, row.task_id, row.dataset_item_id) for row in self.requests
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate image source reference requests")
        return self


class ImageSourceReferences(Value):
    references: list[ImageSourceReference] = Field(max_length=500)


class InternalImageSourceReferenceRequest(Value):
    source: InternalImageSourceRequest
    expires_in: StrictInt = Field(default=3600, ge=1, le=604800)


class ImageGenerationReferenceRequest(ReadImageGenerationRequest):
    expected_source_snapshot: Digest | None = None
    expires_in: StrictInt = Field(default=3600, ge=1, le=604800)


class ImageGenerationReference(Value):
    source: ImageSource
    pyramid: ImagePyramidSummary
    pyramid_source_generation: Revision | None
    pyramid_source_snapshot: Digest | None
    manifest: ImagePyramidManifest | None
    overview_url: StrictStr | None
    expires_in: StrictInt = Field(ge=1, le=604800)

    @field_validator("overview_url")
    @classmethod
    def valid_url(cls, value):
        return _http_url(value)

    @model_validator(mode="after")
    def ready_state_has_manifest_and_overview(self):
        current_source = (
            self.pyramid_source_generation == self.source.source_generation
            and self.pyramid_source_snapshot == self.source.source_snapshot
        )
        if self.pyramid.status == "ready" and (
            self.manifest is None or self.overview_url is None or not current_source
        ):
            raise ValueError("ready image generation has no current signed overview")
        if self.overview_url is not None and (
            self.pyramid.status != "ready"
            or not current_source
            or self.source.source_drifted
            or not self.source.upload_ready
        ):
            raise ValueError("noncurrent image generation has a signed overview")
        if self.manifest is not None and (
            self.pyramid.generation != self.manifest.generation
        ):
            raise ValueError("image generation reference changed generation")
        if self.source.source_drifted and self.overview_url is not None:
            raise ValueError("drifted image source has a signed overview")
        return self


class ImageOverviewRequest(Value):
    kind: Literal["overview"] = "overview"
    generation: PyramidGeneration


class ImageTileRequest(Value):
    kind: Literal["tile"] = "tile"
    generation: PyramidGeneration
    level: StrictInt = Field(ge=0, le=31)
    x: StrictInt = Field(ge=0, le=65_535)
    y: StrictInt = Field(ge=0, le=65_535)


ImageAssetRequest = Annotated[
    ImageOverviewRequest | ImageTileRequest, Field(discriminator="kind")
]


class ImageAssetReferencesRequest(ActorImageSourceRequest):
    expected_source_snapshot: Digest | None = None
    profile_version: ProfileVersion | None = None
    items: list[ImageAssetRequest] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_coordinates(self):
        keys = [
            (
                row.kind,
                row.generation,
                getattr(row, "level", None),
                getattr(row, "x", None),
                getattr(row, "y", None),
            )
            for row in self.items
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate image asset coordinates")
        if len({row.generation for row in self.items}) != 1:
            raise ValueError("one image asset reference batch uses one generation")
        return self


class ImageAssetReference(Value):
    kind: Literal["overview", "tile"]
    generation: PyramidGeneration
    level: StrictInt | None
    x: StrictInt | None
    y: StrictInt | None
    url: StrictStr

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return _http_url(value)

    @model_validator(mode="after")
    def coordinate_shape(self):
        if self.kind == "overview":
            if any(value is not None for value in (self.level, self.x, self.y)):
                raise ValueError("overview reference has tile coordinates")
        elif self.kind == "tile":
            if any(value is None for value in (self.level, self.x, self.y)):
                raise ValueError("tile reference is missing a coordinate")
        else:
            raise ValueError("invalid image asset reference kind")
        return self


class ImageAssetReferences(Value):
    source: ImageSource
    generation: PyramidGeneration
    profile_version: ProfileVersion = "pyramid-v1"
    expires_in: StrictInt = Field(ge=1, le=86400)
    items: list[ImageAssetReference] = Field(max_length=128)


class ImageBackfillCalls(Protocol):
    async def call(self, operation: str, request: BaseModel) -> BaseModel: ...


def _http_url(value: str | None) -> str | None:
    from urllib.parse import urlsplit

    if value is None:
        return value
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("image reference is not an HTTP(S) URL")
    parsed.port
    return value


async def iter_image_backfill_sources(
    gateway: ImageBackfillCalls,
    request: ScanImageBackfillRequest,
) -> AsyncIterator[ImageSource]:
    """Yield one source at a time while enforcing stable snapshot and cursor progress."""
    if request.after_owner_id is not None:
        raise ImageFailure("invalid_request")
    cursor_kind = None
    cursor_id = None
    snapshot: tuple[str, object, int] | None = None
    count = 0
    while True:
        page = await gateway.call("scan_image_backfill", request)
        if not isinstance(page, ImageBackfillPage):
            raise ImageFailure("invalid_response")
        try:
            require_scope(request, page)
        except ValueError:
            raise ImageFailure("invalid_response") from None
        current = (page.snapshot_digest, page.accepted_before, page.total)
        if snapshot is not None and current != snapshot:
            raise ImageFailure("source_changed")
        snapshot = current
        for source in page.items:
            count += 1
            if count > page.total:
                raise ImageFailure("invalid_response")
            yield source
        if page.complete:
            if count != page.total:
                raise ImageFailure("invalid_response")
            return
        if page.next_owner_kind is None or page.next_owner_id is None:
            raise ImageFailure("invalid_response")
        if cursor_id is not None and (
            page.next_owner_kind != cursor_kind
            or page.next_owner_id.int <= cursor_id.int
        ):
            raise ImageFailure("invalid_response")
        cursor_kind = page.next_owner_kind
        cursor_id = page.next_owner_id
        request = request.model_copy(
            update={
                "after_owner_kind": cursor_kind,
                "after_owner_id": cursor_id,
                "selection": request.selection.model_copy(
                    update={"accepted_before": page.accepted_before}
                ),
            }
        )


def require_asset_reference_scope(
    request: ImageAssetReferencesRequest, result: ImageAssetReferences
) -> None:
    if (
        not _source_selector_matches(result.source, request)
        or result.source.source_drifted
        or not result.source.upload_ready
        or (
            request.expected_source_snapshot is not None
            and result.source.source_snapshot != request.expected_source_snapshot
        )
        or result.generation != request.items[0].generation
        or (
            request.profile_version is not None
            and result.profile_version != request.profile_version
        )
    ):
        raise ValueError("image asset references escaped source selector")
    expected = {
        (
            row.kind,
            row.generation,
            getattr(row, "level", None),
            getattr(row, "x", None),
            getattr(row, "y", None),
        )
        for row in request.items
    }
    actual = [
        (row.kind, row.generation, row.level, row.x, row.y) for row in result.items
    ]
    if (
        result.generation not in {row.generation for row in request.items}
        or len(actual) != len(set(actual))
        or set(actual) != expected
        or any(row.generation != result.generation for row in result.items)
    ):
        raise ValueError("image asset references escaped requested coordinates")


def require_source_references_scope(
    request: ImageSourceReferencesRequest, result: ImageSourceReferences
) -> None:
    from .contract import image_source_matches_selector

    requested = {
        (row.actor_id, row.task_id, row.dataset_item_id): row
        for row in request.requests
    }
    actual = []
    for reference in result.references:
        source = reference.source
        matches = [
            key
            for key, selector in requested.items()
            if key[0] == reference.actor_id
            and image_source_matches_selector(source, selector)
        ]
        if (
            len(matches) != 1
            or source.source_drifted
            or not source.upload_ready
            or source.object_identity is None
            or (request.raw_only and reference.thumbnail_url is not None)
            or (not source.thumbnail_current and reference.thumbnail_url is not None)
        ):
            raise ValueError("image source reference escaped its request or freshness")
        actual.append(matches[0])
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate image source references")


def require_source_reference_scope(
    request: ActorImageSourceRequest,
    result: ImageSourceReference,
    *,
    raw_only: bool = False,
) -> None:
    from .contract import image_source_matches_selector

    source = result.source
    if (
        result.actor_id != request.actor_id
        or not image_source_matches_selector(source, request)
        or source.source_drifted
        or not source.upload_ready
        or source.object_identity is None
        or (raw_only and result.thumbnail_url is not None)
        or (not source.thumbnail_current and result.thumbnail_url is not None)
    ):
        raise ValueError("image source reference escaped its request or freshness")


def require_internal_source_reference_scope(
    request: InternalImageSourceReferenceRequest, result: ImageSourceReference
) -> None:
    source_request = request.source
    source = result.source
    if (
        result.actor_id is not None
        or source.owner != source_request.owner
        or source.source_drifted
        or not source.upload_ready
        or source.object_identity is None
        or (
            source_request.expected_source_snapshot is not None
            and source.source_snapshot != source_request.expected_source_snapshot
        )
        or (
            source_request.object_identity is not None
            and source.object_identity != source_request.object_identity
        )
    ):
        raise ValueError("internal image reference escaped its observed source")


def require_generation_reference_scope(
    request: ImageGenerationReferenceRequest, result: ImageGenerationReference
) -> None:
    if (
        not _source_selector_matches(result.source, request)
        or result.source.source_drifted
        or not result.source.upload_ready
        or (
            result.pyramid.status == "ready"
            and (
                result.pyramid_source_generation != result.source.source_generation
                or result.pyramid_source_snapshot != result.source.source_snapshot
            )
        )
        or (
            request.expected_source_snapshot is not None
            and result.source.source_snapshot != request.expected_source_snapshot
        )
        or (
            request.generation is not None
            and result.pyramid.generation != request.generation
        )
        or (
            request.profile_version is not None
            and result.pyramid.profile_version != request.profile_version
        )
        or (
            result.manifest is not None
            and result.manifest.generation != result.pyramid.generation
        )
    ):
        raise ValueError(
            "image generation reference escaped source or generation scope"
        )


def _source_selector_matches(source: ImageSource, request: ImageSourceSelector) -> bool:
    from .contract import image_source_matches_selector

    return image_source_matches_selector(source, request)
