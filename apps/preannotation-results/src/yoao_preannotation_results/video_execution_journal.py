"""Private, proof-bound storage contract for immutable video window results."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from . import codec, contract as c
from .video_execution import ExecutionProof
from .video_execution_plan import (
    MAX_VIDEO_CHECKPOINT_BYTES,
    MAX_VIDEO_WINDOWS,
    VideoExecutionContractFailure,
    VideoExecutionPlan,
    decode_video_execution_plan,
)
from .video_execution_planner import VideoExecutionInputFacts
from .video_window_checkpoint import (
    VideoWindowCheckpointManifest,
    decode_video_window_checkpoint_manifest,
    decode_video_window_checkpoint_part,
)

SCHEMA = "aap-video-checkpoint-data/v1"
MAX_BYTES = 2 * 1024 * 1024
NATIVE_FUNCTIONS = (
    "aap_jobs_v1.video_execution_step(text,text)",
    "aap_jobs_v1.video_execution_checkpoint_step(text,text)",
)
_AttemptState = Literal[
    "started", "capturing", "captured", "replay_verified", "abandoned"
]


class VideoJournalFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PolicyRequest(c.Value):
    pass


class ReadRequest(ExecutionProof):
    pass


class FreezePlanRequest(ExecutionProof):
    plan: VideoExecutionPlan


class ResumePageRequest(ExecutionProof):
    after_ordinal: Annotated[StrictInt, Field(ge=-1, le=c.MAX_INT8)] = -1
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 100


class BeginWindowRequest(ExecutionProof):
    plan_digest: c.Digest
    window_ordinal: c.NonnegativeInt
    window_digest: c.Digest
    seed_input_digest: c.Digest | None
    predecessor_checkpoint_digest: c.Digest | None


class CaptureWindowRequest(ExecutionProof):
    plan_digest: c.Digest
    window_ordinal: c.NonnegativeInt
    capture_id: c.CanonicalUUID
    manifest: VideoWindowCheckpointManifest


class PutPartRequest(ExecutionProof):
    capture_id: c.CanonicalUUID
    index: Annotated[StrictInt, Field(ge=0, lt=128)]
    sha256: c.Digest
    data_base64: c.Base64Part = Field(repr=False)

    @model_validator(mode="after")
    def content_digest(self):
        if (
            hashlib.sha256(codec.decode_part(self.data_base64)).hexdigest()
            != self.sha256
        ):
            raise ValueError("part content digest mismatch")
        return self


class SealWindowRequest(ExecutionProof):
    capture_id: c.CanonicalUUID


class ReadPartRequest(ExecutionProof):
    checkpoint_id: c.Digest
    index: Annotated[StrictInt, Field(ge=0, lt=128)]


class VerifyReplayRequest(ExecutionProof):
    plan_digest: c.Digest
    window_ordinal: c.NonnegativeInt
    capture_id: c.CanonicalUUID
    checkpoint_id: c.Digest
    result_digest: c.Digest
    continuation_digest: c.Digest | None
    discovery_seeds_digest: c.Digest | None
    seed_input_digest: c.Digest | None
    predecessor_checkpoint_digest: c.Digest | None
    raw_response_digest: c.Digest


class CloseSpanRequest(ExecutionProof):
    plan_digest: c.Digest
    context_span_digest: c.Digest


class FinishRequest(ExecutionProof):
    outcome: Literal["completed", "cancelled", "failed"]


_REQUESTS = {
    "policy": PolicyRequest,
    "input_facts": ReadRequest,
    "freeze_plan": FreezePlanRequest,
    "read_plan": ReadRequest,
    "read_resume": ResumePageRequest,
    "begin_window": BeginWindowRequest,
    "capture_window": CaptureWindowRequest,
    "put_part": PutPartRequest,
    "seal_window": SealWindowRequest,
    "read_part": ReadPartRequest,
    "verify_replay": VerifyReplayRequest,
    "close_span": CloseSpanRequest,
    "recovery_ready": ReadRequest,
    "finish": FinishRequest,
    "retire": ReadRequest,
}
ACTIONS = frozenset(_REQUESTS)

# Native errors are deliberately closed; arbitrary database/provider strings
# cannot become a persisted public Job error or disclose a private proof.
REJECTIONS = frozenset(
    {
        "capture_closed",
        "capture_conflict",
        "capture_mismatch",
        "checkpoint_conflict",
        "checkpoint_identity_incomplete",
        "checkpoint_incomplete",
        "checkpoint_invalid",
        "checkpoint_missing",
        "execution_busy",
        "finish_conflict",
        "input_busy",
        "input_unavailable",
        "invalid_request",
        "invalid_response",
        "job_cancelled",
        "job_missing",
        "job_not_active",
        "lease_lost",
        "manifest_invalid",
        "manifest_mismatch",
        "mask_retention_unavailable",
        "part_conflict",
        "part_invalid",
        "part_missing",
        "plan_conflict",
        "plan_mismatch",
        "plan_not_frozen",
        "realm_mismatch",
        "recovery_required",
        "replay_conflict",
        "replay_mismatch",
        "scope_unavailable",
        "span_incomplete",
        "span_mismatch",
        "storage_unavailable",
        "window_mismatch",
        "window_out_of_order",
    }
)


def validate_request(action: str, value: c.Value):
    model = _REQUESTS.get(action)
    try:
        if model is None or type(value) is not model:
            raise ValueError("wrong action request")
        # Revalidate nested mutable geometry/model copies before serialization.
        raw = c.json_bytes(value)
        if len(raw) > MAX_BYTES:
            raise ValueError("request too large")
        request = model.model_validate_json(raw)
        if isinstance(request, FreezePlanRequest):
            plan = decode_video_execution_plan(request.plan)
            if plan.request_id != request.job_id:
                raise ValueError("plan belongs to another Job")
        if isinstance(request, CaptureWindowRequest):
            manifest = decode_video_window_checkpoint_manifest(request.manifest)
            if (
                manifest.request_id != request.job_id
                or manifest.plan_digest != request.plan_digest
                or manifest.window_ordinal != request.window_ordinal
            ):
                raise ValueError("capture identity mismatch")
        return request
    except (
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        OverflowError,
        VideoExecutionContractFailure,
    ):
        raise VideoJournalFailure("invalid_request") from None


def request_bytes(action: str, value: c.Value) -> bytes:
    return c.json_bytes(validate_request(action, value))


class ReadAttempt(c.Value):
    generation: c.PositiveInt
    mode: Literal["execute", "replay"]
    state: _AttemptState
    capture_id: c.CanonicalUUID
    seed_input_digest: c.Digest | None
    predecessor_checkpoint_digest: c.Digest | None


class WindowAttempt(ReadAttempt):
    job_id: c.CanonicalUUID
    run_id: c.CanonicalUUID
    window_ordinal: c.NonnegativeInt
    window_digest: c.Digest
    checkpoint: VideoWindowCheckpointManifest | None


class WindowSlot(c.Value):
    ordinal: c.NonnegativeInt
    window_digest: c.Digest
    context_span_digest: c.Digest
    phase: Literal["discovery", "track"]
    branch: Literal["forward", "backward"]
    seed_source: Literal[
        "none",
        "prompt",
        "source_geometry",
        "correction",
        "discovery",
        "continuation",
        "session",
    ]
    seed_digest: c.Digest | None
    completed: StrictBool
    checkpoint: VideoWindowCheckpointManifest | None
    attempt: ReadAttempt | None

    @model_validator(mode="after")
    def canonical_completed_window(self):
        if self.completed != (self.checkpoint is not None):
            raise ValueError("window completion identity mismatch")
        if self.checkpoint is not None and (
            self.checkpoint.window_ordinal != self.ordinal
            or self.checkpoint.window_digest != self.window_digest
            or self.checkpoint.context_span_digest != self.context_span_digest
            or self.checkpoint.phase != self.phase
        ):
            raise ValueError("window checkpoint metadata mismatch")
        return self


class ClosedSpan(c.Value):
    context_span_digest: c.Digest
    closed: StrictBool


class ResumePage(c.Value):
    windows: Annotated[list[WindowSlot], Field(max_length=100)]
    closed_spans: Annotated[list[ClosedSpan], Field(max_length=100)]
    next_ordinal: c.NonnegativeInt | None


class WindowCapture(c.Value):
    capture_id: c.CanonicalUUID
    window_ordinal: c.NonnegativeInt
    state: Literal["receiving", "sealed", "abandoned"]
    manifest: VideoWindowCheckpointManifest


class PartReceipt(c.Value):
    capture_id: c.CanonicalUUID
    checkpoint_id: c.Digest
    manifest_digest: c.Digest
    part_index: c.NonnegativeInt
    part_count: Annotated[StrictInt, Field(ge=1, le=128)]
    payload_digest: c.Digest
    payload_byte_size: Annotated[StrictInt, Field(ge=1, le=MAX_VIDEO_CHECKPOINT_BYTES)]
    part_digest: c.Digest

    @model_validator(mode="after")
    def bounded_part(self):
        if (
            not 0 <= self.part_index < self.part_count
            or self.part_count
            != (self.payload_byte_size + c.MAX_PART_BYTES - 1) // c.MAX_PART_BYTES
        ):
            raise ValueError("part receipt size mismatch")
        return self


class SealedWindow(c.Value):
    capture_id: c.CanonicalUUID
    ordinal: c.NonnegativeInt
    checkpoint_id: c.Digest
    manifest: VideoWindowCheckpointManifest


def _model(model, value):
    return model.model_validate_json(c.json_bytes(value))


def _manifest_identity(manifest, request):
    if manifest.request_id != request.job_id:
        raise ValueError("checkpoint belongs to another Job")


def decode_native_reply(action: str, request: c.Value, value: Any) -> dict:
    """Validate all reply identities before the caller commits its transaction."""
    request = validate_request(action, request)
    try:
        raw = (
            value.encode("utf-8")
            if type(value) is str
            else c.json_bytes(value)
            if type(value) is dict
            else value
        )
        if type(raw) is not bytes or len(raw) > MAX_BYTES:
            raise ValueError("invalid reply bytes")
        parsed = c.parse_json(raw)
        if type(parsed) is not dict or parsed.get("schema") != SCHEMA:
            raise ValueError("invalid reply schema")
        if parsed.get("status") == "rejected":
            if (
                set(parsed) != {"schema", "status", "reason"}
                or parsed["reason"] not in REJECTIONS
            ):
                raise ValueError("invalid rejection")
            raise VideoJournalFailure(parsed["reason"])
        if parsed.get("status") != "ok":
            raise ValueError("invalid status")
        data = {
            key: value
            for key, value in parsed.items()
            if key not in {"schema", "status"}
        }
        if action == "policy":
            if (
                data != {"ready": True, "functions": list(NATIVE_FUNCTIONS)}
                or data["ready"] is not True
            ):
                raise ValueError("invalid Native profile")
            return data
        field = {
            "input_facts": "facts",
            "freeze_plan": "plan",
            "read_plan": "plan",
            "read_resume": None,
            "begin_window": "attempt",
            "capture_window": "capture",
            "put_part": "part",
            "seal_window": "window",
            "read_part": "part",
            "verify_replay": "verified",
            "close_span": "closed",
            "recovery_ready": "ready",
            "finish": "finished",
            "retire": "retired",
        }[action]
        if action == "read_resume":
            page = _model(ResumePage, data)
            ordinals = [item.ordinal for item in page.windows]
            if (
                len(ordinals) > request.limit
                or ordinals != sorted(set(ordinals))
                or any(
                    index <= request.after_ordinal or index >= MAX_VIDEO_WINDOWS
                    for index in ordinals
                )
                or (
                    page.next_ordinal is not None
                    and (not ordinals or page.next_ordinal != ordinals[-1])
                )
            ):
                raise ValueError("invalid resume page")
            seen = {}
            for span in page.closed_spans:
                if (
                    span.context_span_digest in seen
                    and seen[span.context_span_digest] != span.closed
                ):
                    raise ValueError("conflicting context closure")
                seen[span.context_span_digest] = span.closed
            for window in page.windows:
                if window.checkpoint is not None:
                    _manifest_identity(window.checkpoint, request)
                if (
                    window.attempt is not None
                    and window.attempt.generation != request.generation
                ):
                    raise ValueError("stale attempt in resume page")
            return {"page": page}
        if set(data) != {field}:
            raise ValueError("invalid reply fields")
        result = data[field]
        if action == "input_facts":
            facts = _model(VideoExecutionInputFacts, result)
            if facts.job_id != request.job_id:
                raise ValueError("input facts belong to another Job")
            return {"facts": facts}
        if action in {"freeze_plan", "read_plan"}:
            if result is None:
                if action != "read_plan":
                    raise ValueError("missing frozen plan")
                return {"plan": None}
            plan = decode_video_execution_plan(result)
            if plan.request_id != request.job_id or (
                action == "freeze_plan" and plan != request.plan
            ):
                raise ValueError("frozen plan identity changed")
            return {"plan": plan}
        if action == "begin_window":
            attempt = _model(WindowAttempt, result)
            if any(
                getattr(attempt, key) != getattr(request, key)
                for key in (
                    "job_id",
                    "run_id",
                    "generation",
                    "window_ordinal",
                    "window_digest",
                    "seed_input_digest",
                    "predecessor_checkpoint_digest",
                )
            ):
                raise ValueError("window begin identity changed")
            if attempt.mode == "replay" and attempt.checkpoint is None:
                raise ValueError("replay is missing canonical output")
            if attempt.checkpoint is not None:
                _manifest_identity(attempt.checkpoint, request)
                if (
                    attempt.checkpoint.plan_digest != request.plan_digest
                    or attempt.checkpoint.window_ordinal != request.window_ordinal
                ):
                    raise ValueError("window begin checkpoint changed")
            return {"attempt": attempt}
        if action == "capture_window":
            capture = _model(WindowCapture, result)
            if (
                capture.capture_id != request.capture_id
                or capture.window_ordinal != request.window_ordinal
                or capture.manifest != request.manifest
            ):
                raise ValueError("capture identity changed")
            return {"capture": capture}
        if action == "put_part":
            part = _model(PartReceipt, result)
            if (
                part.capture_id != request.capture_id
                or part.part_index != request.index
                or part.part_digest != request.sha256
            ):
                raise ValueError("part receipt identity changed")
            return {"part": part}
        if action == "seal_window":
            window = _model(SealedWindow, result)
            _manifest_identity(window.manifest, request)
            if (
                window.capture_id != request.capture_id
                or window.ordinal != window.manifest.window_ordinal
                or window.checkpoint_id != window.manifest.checkpoint_id
            ):
                raise ValueError("sealed window identity changed")
            return {"window": window}
        if action == "read_part":
            part = decode_video_window_checkpoint_part(result)
            if (
                part.checkpoint_id != request.checkpoint_id
                or part.part_index != request.index
            ):
                raise ValueError("part read identity changed")
            return {"part": part}
        if result is not True:
            raise ValueError("invalid completion marker")
        return {field: True}
    except VideoJournalFailure:
        raise
    except (
        TypeError,
        ValueError,
        KeyError,
        UnicodeError,
        RecursionError,
        OverflowError,
        VideoExecutionContractFailure,
    ):
        raise VideoJournalFailure("invalid_response") from None
