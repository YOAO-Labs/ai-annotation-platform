"""HTTP values and complete-page readers shared by every media consumer."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator

from .contract import (
    MediaAssets,
    MediaFailure,
    MediaSource,
    Count,
    Digest,
    Revision,
    ReadSourceRequest,
    InternalSourceRequest,
    PacketSample,
    FrameEntry,
    SamplesPage,
    SamplesRequest,
    TimetablePage,
    TimetableRequest,
    Value,
    require_scope,
)


class SourceReference(Value):
    source: MediaSource
    original_url: StrictStr
    video_url: StrictStr
    poster_url: StrictStr | None
    expires_in: StrictInt = Field(ge=1, le=604800)

    @field_validator("original_url", "video_url", "poster_url")
    @classmethod
    def valid_url(cls, value):
        return _http_url(value)


class MediaReferences(Value):
    state: MediaAssets
    video_url: StrictStr
    expires_in: StrictInt = Field(ge=1, le=86400)

    @field_validator("video_url")
    @classmethod
    def valid_url(cls, value):
        return _http_url(value)


class SourceReferencesRequest(Value):
    requests: list[ReadSourceRequest] = Field(max_length=500)


class SourceReferences(Value):
    references: list[SourceReference] = Field(max_length=500)


class InternalSourceReferenceRequest(Value):
    source: InternalSourceRequest
    expires_in: StrictInt = Field(default=3600, ge=1, le=604800)


class SamplesDocument(Value):
    dataset_item_id: UUID
    chunk_id: Count
    source_generation: Revision
    timetable_revision: Count
    snapshot_digest: Digest
    codec_string: StrictStr
    description: StrictStr | None
    width: Count
    height: Count
    total: Count
    samples: list[PacketSample]


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
        raise ValueError("media reference is not an HTTP(S) URL")
    parsed.port
    return value


class MediaCalls(Protocol):
    async def call(self, operation: str, request: BaseModel) -> BaseModel: ...


async def read_all_timetable(
    gateway: MediaCalls, request: TimetableRequest
) -> list[FrameEntry]:
    if request.after_frame is not None:
        raise MediaFailure("invalid_request")
    frames: list[FrameEntry] = []
    snapshot = None
    while True:
        page = await gateway.call("read_video_timetable", request)
        if not isinstance(page, TimetablePage):
            raise MediaFailure("invalid_response")
        try:
            require_scope(request, page)
        except ValueError:
            raise MediaFailure("invalid_response") from None
        current = (
            page.source.source_snapshot,
            page.source.source_generation,
            page.source.timetable_revision,
            page.snapshot_digest,
            page.total,
        )
        if snapshot is not None and current != snapshot:
            raise MediaFailure("source_changed")
        snapshot = current
        if (
            frames
            and page.frames
            and page.frames[0].frame_index <= frames[-1].frame_index
        ):
            raise MediaFailure("invalid_response")
        frames.extend(page.frames)
        if len(frames) > page.total:
            raise MediaFailure("invalid_response")
        if page.complete:
            if len(frames) != page.total:
                raise MediaFailure("invalid_response")
            return frames
        request = request.model_copy(update={"after_frame": page.next_frame})


async def read_all_samples(
    gateway: MediaCalls, request: SamplesRequest
) -> list[PacketSample]:
    return (await read_samples_document(gateway, request)).samples


async def read_samples_document(
    gateway: MediaCalls, request: SamplesRequest
) -> SamplesDocument:
    """Keep codec headers and packet rows on one verified source snapshot."""
    if request.after_sample is not None:
        raise MediaFailure("invalid_request")
    samples: list[PacketSample] = []
    snapshot = None
    first: SamplesPage | None = None
    while True:
        page = await gateway.call("read_video_samples", request)
        if not isinstance(page, SamplesPage):
            raise MediaFailure("invalid_response")
        try:
            require_scope(request, page)
        except ValueError:
            raise MediaFailure("invalid_response") from None
        current = (
            page.dataset_item_id,
            page.chunk_id,
            page.source_generation,
            page.timetable_revision,
            page.snapshot_digest,
            page.codec_string,
            page.description,
            page.width,
            page.height,
            page.total,
        )
        if snapshot is not None and current != snapshot:
            raise MediaFailure("source_changed")
        snapshot = current
        first = page if first is None else first
        if (
            samples
            and page.samples
            and (
                page.samples[0].frame_index <= samples[-1].frame_index
                or page.samples[0].offset_in_chunk < samples[-1].offset_in_chunk
            )
        ):
            raise MediaFailure("invalid_response")
        samples.extend(page.samples)
        if page.complete:
            if len(samples) != page.total:
                raise MediaFailure("invalid_response")
            return SamplesDocument(
                **first.model_dump(
                    exclude={"samples", "after_sample", "next_sample", "complete"}
                ),
                samples=samples,
            )
        request = request.model_copy(update={"after_sample": page.next_sample})
