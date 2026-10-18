"""Language-neutral HTTP models for the media-probe service."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_SAFE_INTEGER = (1 << 53) - 1
SafeInteger = Annotated[
    int, Field(strict=True, ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)
]
NonNegativeSafeInteger = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class VideoMetadata(ProtocolModel):
    duration_ms: SafeInteger | None
    fps: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    frame_count: NonNegativeSafeInteger | None
    width: NonNegativeSafeInteger | None
    height: NonNegativeSafeInteger | None
    codec: str | None


class VideoFrame(ProtocolModel):
    frame_index: NonNegativeSafeInteger
    pts_ms: SafeInteger
    is_keyframe: bool
    pict_type: str | None
    byte_offset: NonNegativeSafeInteger | None


class PacketSample(ProtocolModel):
    pts_ms: SafeInteger
    duration_ms: SafeInteger
    is_keyframe: bool
    size_bytes: NonNegativeSafeInteger
    offset_in_chunk: NonNegativeSafeInteger
    frame_index: NonNegativeSafeInteger


class StreamComplete(ProtocolModel):
    type: Literal["complete"]
    count: NonNegativeSafeInteger
    sha256: Sha256Digest


class VideoFrameRecord(ProtocolModel):
    type: Literal["frame"]
    data: VideoFrame


class PacketSampleRecord(ProtocolModel):
    type: Literal["packet"]
    data: PacketSample


class ProbeFailure(ProtocolModel):
    code: Literal[
        "probe_failed",
        "probe_timeout",
        "invalid_request",
        "file_too_large",
    ]
    message: str
