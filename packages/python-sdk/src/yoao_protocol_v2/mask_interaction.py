"""Native Raster Mask interaction wire models shared by first-party backends.

These models freeze the additive protocol used by single-frame Mask candidates,
Mask-as-prompt, scribble prompts, and video correction seeds. They validate wire
shape and bounded payload semantics; model-specific prompt adaptation remains in
each backend.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MAX_MASK_DIMENSION = 4096
MAX_MASK_PIXELS = MAX_MASK_DIMENSION * MAX_MASK_DIMENSION
MAX_MASK_RUNS = 1_000_000
MAX_RLE_OBJECT_BYTES = 4 * 1024 * 1024
MAX_MASK_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_NATIVE_MASK_PREVIEW_POINTS = 8_192
MAX_SCRIBBLE_STROKES = 64
MAX_SCRIBBLE_POINTS = 8_192
MAX_SCRIBBLE_JSON_BYTES = 512 * 1024
MAX_SCRIBBLE_RASTERIZED_PIXELS = 2_000_000

OutputGeometry = Literal["polygon", "mask"]
CorrectionDirection = Literal["forward", "backward", "bidirectional"]
MaskInteractionReason = Literal[
    "empty_mask",
    "unsupported_output_geometry",
    "unsupported_prompt",
    "unknown_model",
    "ambiguous_model",
    "capability_unavailable",
    "invalid_backend_response",
    "invalid_mask_payload",
    "mask_payload_too_large",
    "mask_response_too_large",
]
MaskFallbackReason = Literal["mask_prompt_unsupported"]


class CocoRlePayload(BaseModel):
    """Bounded uncompressed COCO RLE payload (column-major runs)."""

    model_config = ConfigDict(extra="forbid")

    encoding: Literal["coco_rle"] = "coco_rle"
    size: list[StrictInt] = Field(min_length=2, max_length=2)
    counts: list[StrictInt] = Field(min_length=1, max_length=MAX_MASK_RUNS)

    @model_validator(mode="after")
    def _validate_dimensions_and_runs(self) -> "CocoRlePayload":
        height, width = self.size
        if height <= 0 or width <= 0:
            raise ValueError("size values must be positive integers")
        if height > MAX_MASK_DIMENSION or width > MAX_MASK_DIMENSION:
            raise ValueError(f"mask dimensions must be <= {MAX_MASK_DIMENSION}")
        pixels = height * width
        if pixels > MAX_MASK_PIXELS:
            raise ValueError(f"mask pixels must be <= {MAX_MASK_PIXELS}")
        total = 0
        for index, count in enumerate(self.counts):
            if count < 0:
                raise ValueError(f"counts[{index}] must be non-negative")
            total += count
            if total > pixels:
                raise ValueError("sum(counts) exceeds height * width")
        if total != pixels:
            raise ValueError("sum(counts) must equal height * width")
        if len(_canonical_rle_bytes(self.size, self.counts)) > MAX_RLE_OBJECT_BYTES:
            raise ValueError("mask RLE canonical JSON must be <= 4 MiB")
        return self


def _canonical_rle_bytes(size: list[int], counts: list[int]) -> bytes:
    return json.dumps(
        {"encoding": "coco_rle", "size": list(size), "counts": list(counts)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def canonical_rle_bytes(rle: CocoRlePayload) -> bytes:
    """Return the same canonical JSON byte layout used by platform storage."""

    return _canonical_rle_bytes(rle.size, rle.counts)


def native_mask_candidate_id(
    rle: CocoRlePayload,
    *,
    prompt_revision: str,
    candidate_index: int,
) -> str:
    """Derive the session candidate id from pixels, prompt revision, and index."""

    if not prompt_revision or len(prompt_revision) > 256:
        raise ValueError("prompt_revision must contain 1..256 characters")
    if type(candidate_index) is not int or candidate_index < 0:
        raise ValueError("candidate_index must be non-negative")
    rle_digest = hashlib.sha256(canonical_rle_bytes(rle)).hexdigest()
    digest = hashlib.sha256()
    digest.update(rle_digest.encode())
    digest.update(b"\0")
    digest.update(prompt_revision.encode())
    digest.update(b"\0")
    digest.update(str(candidate_index).encode())
    return f"sha256:{digest.hexdigest()}"


class NativeMaskCandidatePreview(BaseModel):
    """Simplified display-only outline for a native Mask candidate."""

    model_config = ConfigDict(extra="forbid")

    points: list[tuple[float, float]] = Field(
        min_length=3,
        max_length=MAX_NATIVE_MASK_PREVIEW_POINTS,
    )

    @model_validator(mode="after")
    def _validate_points(self) -> "NativeMaskCandidatePreview":
        for point_index, (x, y) in enumerate(self.points):
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"points[{point_index}] must be finite")
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(f"points[{point_index}] must be normalized to [0,1]")
        return self


class NativeMaskCandidateValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rle: CocoRlePayload
    masklabels: list[str] = Field(min_length=1, max_length=1)
    preview: NativeMaskCandidatePreview | None = None

    @model_validator(mode="after")
    def _validate_non_empty_candidate(self) -> "NativeMaskCandidateValue":
        if not self.masklabels[0] or len(self.masklabels[0]) > 128:
            raise ValueError("masklabels must contain one non-empty label <= 128 chars")
        if sum(self.rle.counts[1::2]) <= 0:
            raise ValueError("native mask candidate must contain foreground pixels")
        return self


class NativeMaskCandidate(BaseModel):
    """A transient native Mask candidate returned by an interactive backend."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["mask"] = "mask"
    value: NativeMaskCandidateValue
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MaskPromptPayload(BaseModel):
    """An authorized Mask prompt after the platform resolves its content."""

    model_config = ConfigDict(extra="forbid")

    rle: CocoRlePayload
    source_annotation_id: str = Field(min_length=1, max_length=128)
    source_version: StrictInt = Field(ge=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScribbleStroke(BaseModel):
    """One normalized positive or negative scribble polyline.

    ``width`` is the full stroke diameter divided by the shorter original-image
    side. Consumers rasterize round caps and joins; viewport-only minimum widths
    do not alter this wire value.
    """

    model_config = ConfigDict(extra="forbid")

    polarity: StrictInt
    points: list[tuple[float, float]] = Field(min_length=2)
    width: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_points(self) -> "ScribbleStroke":
        if self.polarity not in (0, 1):
            raise ValueError("polarity must be 0 or 1")
        for point_index, (x, y) in enumerate(self.points):
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"points[{point_index}] must be finite")
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(f"points[{point_index}] must be normalized to [0,1]")
        return self


class ScribblePrompt(BaseModel):
    """A bounded scribble interaction request with optional Mask context."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["scribble"] = "scribble"
    scribbles: list[ScribbleStroke] = Field(
        min_length=1,
        max_length=MAX_SCRIBBLE_STROKES,
    )
    output_geometry: OutputGeometry = "polygon"
    mask_prompt: MaskPromptPayload | None = None

    @model_validator(mode="after")
    def _validate_total_points(self) -> "ScribblePrompt":
        total = sum(len(stroke.points) for stroke in self.scribbles)
        if total > MAX_SCRIBBLE_POINTS:
            raise ValueError(f"scribble points must be <= {MAX_SCRIBBLE_POINTS}")
        scribble_bytes = json.dumps(
            {
                "type": self.type,
                "scribbles": [
                    stroke.model_dump(mode="json") for stroke in self.scribbles
                ],
                "output_geometry": self.output_geometry,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(scribble_bytes) > MAX_SCRIBBLE_JSON_BYTES:
            raise ValueError("scribble JSON must be <= 512 KiB")
        return self


class CorrectionFramePrompt(BaseModel):
    """A corrected video frame seed for bounded directional re-propagation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["correction_frame"] = "correction_frame"
    frame_index: StrictInt = Field(ge=0)
    direction: CorrectionDirection
    mask_prompt: MaskPromptPayload
    output_geometry: OutputGeometry = "mask"


class MaskInteractionDiagnostic(BaseModel):
    """Stable machine-readable diagnostic returned beside an empty/error result."""

    model_config = ConfigDict(extra="forbid")

    reason: MaskInteractionReason
    retryable: bool = False
    message: str | None = Field(default=None, max_length=500)
    supported_geometric_outputs: list[Literal["bbox", "polygon", "mask"]] | None = None


class MaskInteractionFallback(BaseModel):
    """Explicit correction fallback lineage; never inferred silently."""

    model_config = ConfigDict(extra="forbid")

    fallback_reason: MaskFallbackReason
    output_geometry: Literal["bbox", "polygon"]
    seed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


__all__ = [
    "CocoRlePayload",
    "CorrectionDirection",
    "CorrectionFramePrompt",
    "MAX_MASK_DIMENSION",
    "MAX_MASK_PIXELS",
    "MAX_MASK_RESPONSE_BYTES",
    "MAX_MASK_RUNS",
    "MAX_RLE_OBJECT_BYTES",
    "MAX_SCRIBBLE_JSON_BYTES",
    "MAX_SCRIBBLE_POINTS",
    "MAX_SCRIBBLE_RASTERIZED_PIXELS",
    "MAX_SCRIBBLE_STROKES",
    "MaskFallbackReason",
    "MaskInteractionDiagnostic",
    "MaskInteractionFallback",
    "MaskInteractionReason",
    "MaskPromptPayload",
    "NativeMaskCandidate",
    "NativeMaskCandidateValue",
    "OutputGeometry",
    "ScribblePrompt",
    "ScribbleStroke",
    "canonical_rle_bytes",
    "native_mask_candidate_id",
]
