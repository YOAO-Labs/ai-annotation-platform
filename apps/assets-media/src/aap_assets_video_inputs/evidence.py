"""Typed video prediction evidence and Labeling retention capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, StrictBool, ValidationError, model_validator

from .execution import ErrorCode, VideoFrameRecord
from .values import Count, Digest, FrameTimeEntry, FrozenFramePlan, Revision, Value

EVIDENCE_SCHEMA = "aap-assets-video-input-evidence/v1"
FAILURE_SCHEMA = "aap-assets-video-input-failure/v1"
LABELING_SCHEMA = "aap-labeling-video-input-sources/v1"
RETENTION_SCHEMA = "aap-labeling-video-input-retention/v1"
REQUEST_BYTES = 64 * 1024
REPLY_BYTES = 2 * 1024 * 1024


class VideoInputEvidence(Value):
    """Immutable provenance for a successfully retained decoded frame."""

    protocol_schema: Literal["aap-assets-video-input-evidence/v1"] = Field(
        default=EVIDENCE_SCHEMA, alias="schema", serialization_alias="schema"
    )
    input_id: UUID
    job_id: UUID
    task_id: UUID
    project_id: UUID
    plan_digest: Digest
    timebase_digest: Digest
    frame_id: UUID
    entry: FrameTimeEntry
    source_sha256: Digest
    frame_sha256: Digest

    @model_validator(mode="after")
    def entry_belongs_to_plan(self):
        if self.entry.ordinal < 1:
            raise ValueError("video evidence requires a selected frame ordinal")
        return self


class VideoInputFailureEvidence(Value):
    """Original-video and selected-time provenance when frame output failed."""

    protocol_schema: Literal["aap-assets-video-input-failure/v1"] = Field(
        default=FAILURE_SCHEMA, alias="schema", serialization_alias="schema"
    )
    input_id: UUID
    job_id: UUID
    task_id: UUID
    project_id: UUID
    plan_digest: Digest
    timebase_digest: Digest
    frame_id: UUID | None
    entry: FrameTimeEntry
    source_sha256: Digest
    frame_sha256: Digest | None

    @model_validator(mode="after")
    def failed_output_identity(self):
        if self.frame_sha256 is not None and self.frame_id is None:
            raise ValueError("video failure hash requires a canonical frame id")
        return self


VideoEvidence = VideoInputEvidence | VideoInputFailureEvidence


def prediction_extra(evidence: VideoEvidence) -> dict[str, Any]:
    """Serialize the typed DTO under the single prediction metadata key."""
    if not isinstance(evidence, (VideoInputEvidence, VideoInputFailureEvidence)):
        raise TypeError(
            "prediction video evidence must use a typed ready or failure DTO"
        )
    return {"accepted_video_input": evidence.model_dump(mode="json", by_alias=True)}


def ready_evidence(
    plan: FrozenFramePlan, frame: VideoFrameRecord
) -> VideoInputEvidence:
    """Build a ready provenance DTO only from the exact retained frame record."""
    if frame.status != "ready" or frame.blob is None:
        raise ValueError("ready video evidence requires a retained frame blob")
    video = plan.input
    if (
        frame.plan != plan
        or frame.entry.ordinal > plan.selected_count
        or frame.entry.frame_index != (frame.entry.ordinal - 1) * plan.sampling.step
    ):
        raise ValueError("video frame belongs to another plan or timebase")
    return VideoInputEvidence(
        input_id=video.input_id,
        job_id=video.job_id,
        task_id=video.task.task_id,
        project_id=video.task.project_id,
        plan_digest=plan.plan_digest,
        timebase_digest=plan.timebase_digest,
        frame_id=frame.frame_id,
        entry=frame.entry,
        source_sha256=video.blob.sha256,
        frame_sha256=frame.blob.sha256,
    )


def failure_evidence(
    plan: FrozenFramePlan,
    entry: FrameTimeEntry,
    *,
    frame_id: UUID | None = None,
    frame_sha256: str | None = None,
) -> VideoInputFailureEvidence:
    """Build failure provenance without inventing a frame artifact."""
    if entry.ordinal > plan.selected_count or entry.frame_index != (
        (entry.ordinal - 1) * plan.sampling.step
    ):
        raise ValueError("failed frame is outside its retained input plan")
    video = plan.input
    return VideoInputFailureEvidence(
        input_id=video.input_id,
        job_id=video.job_id,
        task_id=video.task.task_id,
        project_id=video.task.project_id,
        plan_digest=plan.plan_digest,
        timebase_digest=plan.timebase_digest,
        frame_id=frame_id,
        entry=entry,
        source_sha256=video.blob.sha256,
        frame_sha256=frame_sha256,
    )


def parse_prediction_evidence(value: Any) -> VideoEvidence:
    """Parse the only accepted video evidence shapes carried in prediction extra."""
    try:
        if isinstance(value, (str, bytes)):
            if (
                len(value.encode("utf-8") if isinstance(value, str) else value)
                > REQUEST_BYTES
            ):
                raise ValueError("oversized video evidence")
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("video evidence must be an object")
        schema = value.get("schema")
        if schema == EVIDENCE_SCHEMA:
            result = VideoInputEvidence.model_validate(value)
        elif schema == FAILURE_SCHEMA:
            result = VideoInputFailureEvidence.model_validate(value)
        else:
            raise ValueError("unknown video evidence schema")
        if value != result.model_dump(mode="json", by_alias=True):
            raise ValueError("video evidence is not canonical JSON")
        return result
    except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
        raise VideoEvidenceFailure("invalid_response") from None


class VideoEvidenceFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class LockVideoInputPinRequest(Value):
    task_id: UUID
    project_id: UUID
    evidence: VideoInputEvidence
    actor_id: UUID | None


class VideoInputPinRecord(Value):
    frame: VideoFrameRecord

    @model_validator(mode="after")
    def ready_canonical_frame(self):
        if self.frame.status != "ready" or self.frame.blob is None:
            raise ValueError("video pin must return a ready retained frame")
        return self


class AttachAnnotationVideoInputSourceRequest(Value):
    annotation_id: UUID
    actor_id: UUID
    evidence: VideoInputEvidence


class CopyAnnotationVideoInputSourcesRequest(Value):
    source_annotation_id: UUID
    target_annotation_id: UUID


class VideoInputReferenceRequest(Value):
    input_id: UUID


class VideoInputReference(Value):
    input_id: UUID
    referenced: StrictBool


class AttachVideoInputEvidenceAck(Value):
    annotation_id: UUID
    input_id: UUID
    plan_digest: Digest
    ordinal: Revision
    attached: StrictBool


class CopyVideoInputEvidenceAck(Value):
    source_annotation_id: UUID
    target_annotation_id: UUID
    copied: Count


class _NativeError(Value):
    code: ErrorCode


@dataclass(frozen=True)
class NativeOperation:
    request: type[Value]
    result: type[Value]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["labeling"]
    signature: str


ASSETS_PIN_OPERATION = NativeOperation(
    LockVideoInputPinRequest,
    VideoInputPinRecord,
    ("task_id", "project_id", "evidence", "actor_id"),
    ("uuid", "uuid", "jsonb", "uuid"),
    "labeling",
    "aap_assets_v1._lock_accepted_video_input_pin(uuid,uuid,jsonb,uuid)",
)

LABELING_OPERATIONS = {
    "attach_annotation_video_input_source": NativeOperation(
        AttachAnnotationVideoInputSourceRequest,
        AttachVideoInputEvidenceAck,
        ("annotation_id", "actor_id", "evidence"),
        ("uuid", "uuid", "jsonb"),
        "labeling",
        "aap_labeling_v1.attach_annotation_video_input_source(uuid,uuid,jsonb)",
    ),
    "copy_annotation_video_input_sources": NativeOperation(
        CopyAnnotationVideoInputSourcesRequest,
        CopyVideoInputEvidenceAck,
        ("source_annotation_id", "target_annotation_id"),
        ("uuid", "uuid"),
        "labeling",
        "aap_labeling_v1.copy_annotation_video_input_sources(uuid,uuid)",
    ),
    "accepted_video_input_referenced": NativeOperation(
        VideoInputReferenceRequest,
        VideoInputReference,
        ("input_id",),
        ("uuid",),
        "labeling",
        "aap_labeling_v1.accepted_video_input_referenced(uuid)",
    ),
}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def contract_document() -> dict[str, Any]:
    """Portable evidence DTOs and exact private/public native function contracts."""
    models = (
        VideoInputEvidence,
        VideoInputFailureEvidence,
        LockVideoInputPinRequest,
        VideoInputPinRecord,
        AttachAnnotationVideoInputSourceRequest,
        CopyAnnotationVideoInputSourcesRequest,
        VideoInputReferenceRequest,
        VideoInputReference,
        AttachVideoInputEvidenceAck,
        CopyVideoInputEvidenceAck,
    )
    definitions: dict[str, Any] = {}
    for model in models:
        shape = model.model_json_schema()
        nested = shape.pop("$defs", {})
        for name, child in nested.items():
            if name in definitions and definitions[name] != child:
                raise ValueError(
                    f"conflicting video evidence schema definition: {name}"
                )
            definitions[name] = child
        if model.__name__ in definitions and definitions[model.__name__] != shape:
            raise ValueError(f"conflicting video evidence model: {model.__name__}")
        definitions[model.__name__] = shape
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": EVIDENCE_SCHEMA,
        "$defs": definitions,
        "oneOf": [
            {"$ref": "#/$defs/VideoInputEvidence"},
            {"$ref": "#/$defs/VideoInputFailureEvidence"},
        ],
        "x-prediction-extra-key": "accepted_video_input",
        "x-native": {
            "assets_pin": {
                "signature": ASSETS_PIN_OPERATION.signature,
                "arguments": list(ASSETS_PIN_OPERATION.arguments),
                "request": ASSETS_PIN_OPERATION.request.__name__,
                "result": ASSETS_PIN_OPERATION.result.__name__,
                "role": ASSETS_PIN_OPERATION.role,
                "owner": "assets",
                "private": True,
            },
            **{
                name: {
                    "signature": operation.signature,
                    "arguments": list(operation.arguments),
                    "request": operation.request.__name__,
                    "result": operation.result.__name__,
                    "role": operation.role,
                }
                for name, operation in LABELING_OPERATIONS.items()
            },
        },
        "x-budgets": {"request_bytes": REQUEST_BYTES, "reply_bytes": REPLY_BYTES},
    }


def parse_labeling_native(operation: str, request: Value, raw: Any) -> Value:
    definition = LABELING_OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise VideoEvidenceFailure("invalid_request")
    try:
        request_data = request.model_dump(mode="json", by_alias=True)
        if len(_canonical_json_bytes(request_data)) > REQUEST_BYTES:
            raise VideoEvidenceFailure("resource_budget_exceeded")
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized video evidence reply")
            raw = json.loads(raw)
        elif len(_canonical_json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized video evidence reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != LABELING_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid video evidence envelope")
        if raw["ok"] is False and set(raw) == {"schema", "ok", "error"}:
            raise VideoEvidenceFailure(_NativeError.model_validate(raw["error"]).code)
        if raw["ok"] is not True or set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid video evidence success")
        result = definition.result.model_validate(raw["data"])
        if isinstance(request, AttachAnnotationVideoInputSourceRequest):
            if (
                result.annotation_id != request.annotation_id
                or result.input_id != request.evidence.input_id
                or result.plan_digest != request.evidence.plan_digest
                or result.ordinal != request.evidence.entry.ordinal
            ):
                raise ValueError("video evidence attach reply changed its identity")
        elif isinstance(request, CopyAnnotationVideoInputSourcesRequest):
            if (
                result.source_annotation_id != request.source_annotation_id
                or result.target_annotation_id != request.target_annotation_id
            ):
                raise ValueError("video evidence copy reply changed its annotations")
        elif isinstance(request, VideoInputReferenceRequest):
            if result.input_id != request.input_id:
                raise ValueError("video retention reply changed its input identity")
        return result
    except VideoEvidenceFailure:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise VideoEvidenceFailure("invalid_response") from None


def parse_assets_pin(
    request: LockVideoInputPinRequest, raw: Any
) -> VideoInputPinRecord:
    """Validate the private Assets pin reply against the exact evidence DTO."""
    try:
        request_data = request.model_dump(mode="json", by_alias=True)
        if len(_canonical_json_bytes(request_data)) > REQUEST_BYTES:
            raise VideoEvidenceFailure("resource_budget_exceeded")
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized video pin reply")
            raw = json.loads(raw)
        elif len(_canonical_json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized video pin reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != "aap-assets-video-input-execution/v1"
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid video pin envelope")
        if raw["ok"] is False and set(raw) == {"schema", "ok", "error"}:
            raise VideoEvidenceFailure(_NativeError.model_validate(raw["error"]).code)
        if raw["ok"] is not True or set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid video pin success")
        if not isinstance(raw["data"], dict) or set(raw["data"]) != {"frame"}:
            raise ValueError("invalid video pin data")
        result = VideoInputPinRecord.model_validate(raw["data"])
        frame_record = result.frame
        evidence = request.evidence
        plan = frame_record.plan
        if (
            frame_record.frame_id != evidence.frame_id
            or frame_record.entry != evidence.entry
            or frame_record.blob is None
            or frame_record.blob.sha256 != evidence.frame_sha256
            or plan.input.input_id != evidence.input_id
            or plan.input.job_id != evidence.job_id
            or plan.input.task.task_id != evidence.task_id
            or plan.input.task.project_id != evidence.project_id
            or plan.plan_digest != evidence.plan_digest
            or plan.timebase_digest != evidence.timebase_digest
            or plan.input.blob.sha256 != evidence.source_sha256
            or request.task_id != evidence.task_id
            or request.project_id != evidence.project_id
        ):
            raise ValueError("video pin reply changed canonical evidence")
        return result
    except VideoEvidenceFailure:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise VideoEvidenceFailure("invalid_response") from None
