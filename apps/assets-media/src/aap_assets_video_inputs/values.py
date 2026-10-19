"""Video input values; native admission and HTTP bindings are separate work."""

from __future__ import annotations

import hashlib
from typing import Annotated, Iterable, Literal
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

VIDEO_INPUT_SCHEMA = "aap-assets-video-inputs/v1"
REQUEST_BYTES = 64 * 1024
REPLY_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 100
SAFE_INTEGER = 9_007_199_254_740_991
Count = Annotated[StrictInt, Field(ge=0, le=SAFE_INTEGER)]
Revision = Annotated[StrictInt, Field(ge=1, le=SAFE_INTEGER)]
Index = Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
SignedTime = Annotated[StrictInt, Field(ge=-SAFE_INTEGER, le=SAFE_INTEGER)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Format = Literal["jpeg", "webp"]
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PREFIX = rf"accepted-video-inputs/{_UUID}/g[1-9][0-9]*/a{_UUID}/"
ATTEMPT_PREFIX_PATTERN = rf"^{_PREFIX}$"
VIDEO_BLOB_KEY_PATTERN = rf"^{_PREFIX}source$"
FRAME_BLOB_KEY_PATTERN = rf"^{_PREFIX}frames/(0|[1-9][0-9]*)/w[1-9][0-9]*-(jpeg|webp)$"


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    @field_validator("*", mode="after")
    @classmethod
    def no_nul(cls, value):
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("NUL is not supported by PostgreSQL values")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("video input strings require valid UTF-8") from None
        return value


class TaskVideoBinding(Value):
    task_id: UUID
    project_id: UUID
    task_version: Revision
    task_source_generation: Revision
    dataset_item_id: UUID
    task_source_snapshot: Digest
    file_path: Annotated[StrictStr, Field(max_length=1024)]
    file_type: Literal["video"]


class ObjectIdentity(Value):
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Count
    version_id: Annotated[StrictStr, Field(max_length=255)] | None


class VideoSourceSnapshot(Value):
    dataset_item_id: UUID
    dataset_id: UUID
    source_generation: Revision
    source_snapshot: Digest
    timetable_revision: Count
    object_identity: ObjectIdentity


class BlobIdentity(Value):
    key: Annotated[StrictStr, Field(max_length=2048)]
    version_id: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Revision
    sha256: Digest

    @model_validator(mode="after")
    def real_version(self):
        if not self.version_id.strip() or self.version_id == "null":
            raise ValueError("retained video requires a real object version")
        return self


class VideoBlob(BlobIdentity):
    key: Annotated[StrictStr, Field(max_length=2048, pattern=VIDEO_BLOB_KEY_PATTERN)]


class FrameBlob(BlobIdentity):
    key: Annotated[
        StrictStr,
        Field(
            max_length=2048,
            pattern=FRAME_BLOB_KEY_PATTERN,
        ),
    ]


class VideoFacts(Value):
    """Measured from the retained bytes, not copied from mutable item metadata."""

    frame_count: Revision
    width: Revision
    height: Revision
    duration_ms: Count
    video_stream: Annotated[StrictInt, Field(ge=0, le=0)] = 0


class FrozenVideoInput(Value):
    input_id: UUID
    job_id: UUID
    actor_id: UUID
    task: TaskVideoBinding
    source: VideoSourceSnapshot
    blob: VideoBlob
    video: VideoFacts
    created_at: AwareDatetime

    @model_validator(mode="after")
    def accepted_source(self):
        if self.task.dataset_item_id != self.source.dataset_item_id:
            raise ValueError("video input changed the captured Task link")
        if not self.blob.key.startswith(f"accepted-video-inputs/{self.input_id}/"):
            raise ValueError("video blob escaped its input")
        if self.blob.byte_size != self.source.object_identity.byte_size:
            raise ValueError("video copy changed the observed byte size")
        return self


class FrameSampling(Value):
    max_frames: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]
    step: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)] = 1
    width: Annotated[StrictInt, Field(ge=1, le=4096)] = 1280
    format: Format = "jpeg"

    def count(self, frame_count: int) -> int:
        return min(self.max_frames, (frame_count + self.step - 1) // self.step)


class FrameTimeEntry(Value):
    ordinal: Revision
    frame_index: Index
    pts_ms: SignedTime
    is_keyframe: StrictBool


def _seed(video: FrozenVideoInput, sampling: FrameSampling) -> bytes:
    fields = (
        VIDEO_INPUT_SCHEMA,
        video.input_id,
        video.job_id,
        video.actor_id,
        video.task.task_id,
        video.task.project_id,
        video.task.task_version,
        video.task.task_source_generation,
        video.task.task_source_snapshot,
        video.source.dataset_id,
        video.source.dataset_item_id,
        video.source.source_generation,
        video.source.source_snapshot,
        video.source.timetable_revision,
        video.blob.sha256,
        video.blob.byte_size,
        video.video.frame_count,
        video.video.width,
        video.video.height,
        video.video.duration_ms,
        video.video.video_stream,
        sampling.max_frames,
        sampling.step,
        sampling.width,
        sampling.format,
    )
    return ("\n".join(map(str, fields)) + "\n").encode("ascii")


def _entry_matches(sampling: FrameSampling, entry: FrameTimeEntry, ordinal: int):
    if entry.ordinal != ordinal or entry.frame_index != (ordinal - 1) * sampling.step:
        raise ValueError("frame entry changed the captured decoded ordinal")


def _selected_count(video: FrozenVideoInput, sampling: FrameSampling) -> int:
    count = sampling.count(video.video.frame_count)
    if (count - 1) * sampling.step > 2_147_483_647:
        raise ValueError("selected frame cannot be represented by the video protocol")
    return count


def timebase_digest(
    video: FrozenVideoInput,
    sampling: FrameSampling,
    entries: Iterable[FrameTimeEntry],
) -> str:
    """Hash selected time facts in order without retaining a whole frame table."""
    count = _selected_count(video, sampling)
    digest = hashlib.sha256(b"timebase\n" + _seed(video, sampling))
    seen = 0
    for seen, entry in enumerate(entries, start=1):
        if seen > count:
            raise ValueError("timebase contains unselected frames")
        _entry_matches(sampling, entry, seen)
        digest.update(
            f"{seen}\n{entry.frame_index}\n{entry.pts_ms}\n{int(entry.is_keyframe)}\n".encode(
                "ascii"
            )
        )
    if seen != count:
        raise ValueError("timebase omitted selected frames")
    return digest.hexdigest()


class FrozenFramePlan(Value):
    input: FrozenVideoInput
    sampling: FrameSampling
    selected_count: Revision
    timebase_digest: Digest
    plan_digest: Digest

    @model_validator(mode="after")
    def complete_plan(self):
        if self.selected_count != _selected_count(self.input, self.sampling):
            raise ValueError("frame plan changed selected count")
        expected = hashlib.sha256(
            b"plan\n"
            + _seed(self.input, self.sampling)
            + self.timebase_digest.encode("ascii")
            + b"\n"
        ).hexdigest()
        if self.plan_digest != expected:
            raise ValueError("frame plan digest changed")
        return self

    @classmethod
    def from_entries(cls, video, sampling, entries):
        timebase = timebase_digest(video, sampling, entries)
        digest = hashlib.sha256(
            b"plan\n" + _seed(video, sampling) + timebase.encode("ascii") + b"\n"
        ).hexdigest()
        return cls(
            input=video,
            sampling=sampling,
            selected_count=sampling.count(video.video.frame_count),
            timebase_digest=timebase,
            plan_digest=digest,
        )


class FramePageRequest(Value):
    actor_id: UUID
    job_id: UUID
    task_id: UUID
    input_id: UUID
    plan_digest: Digest
    cursor: Count = 0
    page_size: Annotated[StrictInt, Field(ge=1, le=PAGE_SIZE)] = PAGE_SIZE


class FrameTimePage(Value):
    plan: FrozenFramePlan
    cursor: Count
    next_cursor: Count | None
    entries: Annotated[tuple[FrameTimeEntry, ...], Field(max_length=PAGE_SIZE)]

    @model_validator(mode="after")
    def contiguous_page(self):
        if self.cursor > self.plan.selected_count:
            raise ValueError("frame cursor exceeds the sealed plan")
        if self.cursor < self.plan.selected_count and not self.entries:
            raise ValueError("page omitted remaining frame entries")
        for offset, entry in enumerate(self.entries, start=self.cursor + 1):
            _entry_matches(self.plan.sampling, entry, offset)
        end = self.cursor + len(self.entries)
        if end > self.plan.selected_count:
            raise ValueError("page exceeds the sealed plan")
        if self.next_cursor != (end if end < self.plan.selected_count else None):
            raise ValueError("frame cursor does not describe page progress")
        return self


def require_page_scope(request: FramePageRequest, page: FrameTimePage) -> None:
    video = page.plan.input
    if (
        request.job_id != video.job_id
        or request.task_id != video.task.task_id
        or request.input_id != video.input_id
        or request.plan_digest != page.plan.plan_digest
        or request.cursor != page.cursor
        or len(page.entries) > request.page_size
    ):
        raise ValueError("frame page escaped its requested input")


def require_timebase(plan: FrozenFramePlan, pages: Iterable[FrameTimePage]) -> None:
    def ordered_entries():
        cursor = 0
        complete = False
        for page in pages:
            if complete or page.plan != plan or page.cursor != cursor:
                raise ValueError("timebase pages changed scope or progress")
            yield from page.entries
            cursor += len(page.entries)
            complete = page.next_cursor is None
        if not complete:
            raise ValueError("timebase pages ended before the sealed selection")

    if (
        timebase_digest(plan.input, plan.sampling, ordered_entries())
        != plan.timebase_digest
    ):
        raise ValueError("timebase changed after the frame plan was sealed")


class FrameReference(Value):
    plan: FrozenFramePlan
    entry: FrameTimeEntry
    blob: FrameBlob
    url: Annotated[StrictStr, Field(min_length=1, max_length=16384)]
    expires_in: Annotated[StrictInt, Field(ge=1, le=3600)]

    @model_validator(mode="after")
    def exact_frame(self):
        _entry_matches(self.plan.sampling, self.entry, self.entry.ordinal)
        if self.entry.ordinal > self.plan.selected_count:
            raise ValueError("frame reference is outside the sealed selection")
        key = self.blob.key
        suffix = (
            f"frames/{self.entry.frame_index}/w{self.plan.sampling.width}"
            f"-{self.plan.sampling.format}"
        )
        if not key.startswith(
            f"accepted-video-inputs/{self.plan.input.input_id}/"
        ) or not key.endswith(suffix):
            raise ValueError("frame artifact changed input or rendition")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.fragment
            or any(c.isspace() for c in self.url)
            or parse_qs(parsed.query).get("versionId") != [self.blob.version_id]
            or not unquote(parsed.path).endswith("/" + self.blob.key)
        ):
            raise ValueError("frame URL is not bound to its exact object version")
        return self


def value_document() -> dict:
    definitions = {}
    models = [
        FrozenVideoInput,
        FrozenFramePlan,
        FramePageRequest,
        FrameTimePage,
        FrameReference,
    ]
    for model in models:
        definition = model.model_json_schema()
        for name, child in definition.pop("$defs", {}).items():
            if name in definitions and definitions[name] != child:
                raise ValueError("conflicting video input value definition")
            definitions[name] = child
        if model.__name__ in definitions and definitions[model.__name__] != definition:
            raise ValueError("conflicting video input value model")
        definitions[model.__name__] = definition
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": VIDEO_INPUT_SCHEMA,
        "$defs": definitions,
        "x-stage": "values-only; native and HTTP admission not activated",
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "page_size": PAGE_SIZE,
        },
        "x-digest": "SHA-256 of ordered ASCII fields separated and terminated by LF; see ADR-0109",
    }
