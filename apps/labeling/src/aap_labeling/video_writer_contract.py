"""Portable contract for atomically publishing video-tracker candidates.

This module contains only closed value objects and wire codecs. It never
imports an application framework, database adapter, model runtime, or object
store implementation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal
from uuid import UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_assets_mask_content.contract import MASK_PREFIX, MaskReference
from aap_assets_video_inputs.tracker import (
    TrackerExecutionSpec,
    TrackerVideoInputEvidence,
)
from aap_preannotation_results.contract import CanonicalUUID, Digest, UtcDateTime
from aap_preannotation_results.video_execution import ExecutionProof
from aap_preannotation_results.video_execution_plan import (
    MAX_VIDEO_RESULT_ROWS,
    VideoExecutionContractFailure,
    VideoExecutionPlan,
    VideoFrameResultRow,
    normalize_video_frame_results,
)
from aap_preannotation_results.video_window_checkpoint import (
    VideoWindowCheckpointManifest,
    decode_video_window_checkpoint_manifest,
    decode_video_window_checkpoint_payload,
    snapshot_video_window_checkpoint,
)

LABELING_VIDEO_WRITER_PROTOCOL = "aap-labeling-video-writer/v1"
LABELING_VIDEO_WRITER_DATA_SCHEMA = "aap-labeling-video-writer-data/v1"
VIDEO_WRITER_FUNCTION = "aap_labeling_v1.video_writer_step(text,text)"
MAX_VIDEO_WRITER_REQUEST_BYTES = 68_157_440  # 65 MiB, decimal by protocol.
MAX_VIDEO_WRITER_STAGED_BYTES = 67_108_864  # Existing 64 MiB candidate cap.
MAX_VIDEO_WRITER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_VIDEO_WRITER_NATIVE_BYTES = 135_266_304  # 129 MiB for escaped Native JSON.
VIDEO_WRITER_OPERATION_NAME = "aap-labeling-video-writer-finalize/v1"
_RECEIPT_STATUSES = {"pending_review", "cancelled", "failed"}
_ERROR_CODES = {
    "invalid_request",
    "invalid_response",
    "storage_unavailable",
    "operation_conflict",
    "lease_lost",
    "realm_mismatch",
    "execution_busy",
    "recovery_required",
    "job_missing",
    "job_not_active",
    "job_cancelled",
    "input_unavailable",
    "input_busy",
    "permission_denied",
    "candidate_invalid",
    "source_map_invalid",
    "staged_result_too_large",
    "mask_content_deleting",
    "mask_content_epoch_changed",
    "plan_not_frozen",
    "checkpoint_incomplete",
    "finish_conflict",
}
_API_ERROR_CODES = _ERROR_CODES | {
    "account_not_active",
    "account_busy",
    "task_busy",
    "source_changed",
    "source_version_conflict",
    "input_scope_mismatch",
    "mask_content_invalid",
    "correction_empty_result",
}


class _WriterValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class VideoWriterFailure(Exception):
    """Closed writer protocol failure without retaining request contents."""

    def __init__(self, code: str):
        if type(code) is not str or code not in _API_ERROR_CODES:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


class VideoWriterInvalidRequest(VideoWriterFailure):
    def __init__(self):
        super().__init__("invalid_request")


class VideoWriterUnavailable(VideoWriterFailure):
    def __init__(self):
        super().__init__("invalid_response")


class VideoWriterRejected(VideoWriterFailure):
    def __init__(self, code: str):
        super().__init__(code)


class VideoWriterExpectedInput(_WriterValue):
    input_id: CanonicalUUID
    request_digest: Digest
    plan_digest: Digest
    source_version_id: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    source_sha256: Digest

    @field_validator("source_version_id")
    @classmethod
    def exact_source_version(cls, value: str) -> str:
        if not value.strip() or value == "null":
            raise ValueError("source version must be exact")
        return value


class VideoWriterCommand(_WriterValue):
    schema_: Literal["aap-labeling-video-writer/v1"] = Field(alias="schema")
    operation_id: CanonicalUUID
    execution: ExecutionProof
    outcome: Literal["completed", "cancelled", "failed"]
    expected_input: VideoWriterExpectedInput | None
    frame_results: tuple[VideoFrameResultRow, ...]
    error_message: Annotated[StrictStr, Field(max_length=2000)] | None

    @field_validator("frame_results", mode="before")
    @classmethod
    def results_are_ordered_array(cls, value: Any):
        if type(value) not in (list, tuple) or len(value) > MAX_VIDEO_RESULT_ROWS:
            raise ValueError("frame results must be a bounded array")
        # Keep the executor's direction/interleaving order intact. In
        # particular, a correction can contain backward and forward branches.
        return tuple(value)

    @model_validator(mode="after")
    def valid_finalization_shape(self) -> VideoWriterCommand:
        if self.operation_id != video_writer_operation_id(self.execution.run_id):
            raise ValueError("operation id is not stable for this execution")
        if self.outcome == "failed":
            if self.frame_results or self.error_message is None:
                raise ValueError("failure requires an error and no candidates")
        elif self.error_message is not None:
            raise ValueError("only a failed outcome carries an error message")
        if self.outcome == "completed" and self.expected_input is None:
            raise ValueError("completion requires the accepted input identity")
        if self.frame_results and self.expected_input is None:
            raise ValueError("nonempty candidates require the accepted input")
        return self


class VideoWriterPublicationRequest(_WriterValue):
    schema_: Literal["aap-labeling-video-writer/v1"] = Field(alias="schema")
    execution: ExecutionProof


class VideoWriterStatusRequest(_WriterValue):
    schema_: Literal["aap-labeling-video-writer/v1"] = Field(alias="schema")
    operation_id: CanonicalUUID
    execution: ExecutionProof
    request_digest: Digest

    @model_validator(mode="after")
    def stable_operation(self) -> VideoWriterStatusRequest:
        if self.operation_id != video_writer_operation_id(self.execution.run_id):
            raise ValueError("operation id is not stable for this execution")
        return self


class VideoWriterReceipt(_WriterValue):
    operation_id: CanonicalUUID
    job_id: CanonicalUUID
    run_id: CanonicalUUID
    request_digest: Digest
    outcome: Literal["completed", "cancelled", "failed"]
    status: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    completed_at: UtcDateTime
    staged_sha256: Digest | None
    staged_byte_size: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def receipt_consistency(self) -> VideoWriterReceipt:
        if self.status not in _RECEIPT_STATUSES:
            raise ValueError("receipt has an unsupported canonical Job status")
        if (self.staged_sha256 is None) != (self.staged_byte_size == 0):
            raise ValueError("staged digest and byte size disagree")
        return self


class VideoWriterJobFacts(_WriterValue):
    job_id: CanonicalUUID
    task_id: CanonicalUUID
    dataset_item_id: CanonicalUUID
    annotation_id: CanonicalUUID | None
    created_by: CanonicalUUID | None
    job_kind: Literal["tracking", "correction"]
    from_frame: Annotated[StrictInt, Field(ge=0)]
    to_frame: Annotated[StrictInt, Field(ge=0)]
    direction: Literal["forward", "backward", "bidirectional"]
    status: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    event_channel: Annotated[StrictStr, Field(min_length=1, max_length=255)]

    @model_validator(mode="after")
    def ordered_frames(self) -> VideoWriterJobFacts:
        if self.from_frame > self.to_frame:
            raise ValueError("Job frame range is reversed")
        return self


class VideoWriterAcceptance(_WriterValue):
    execution: TrackerExecutionSpec
    evidence: TrackerVideoInputEvidence


class VideoWriterFacts(_WriterValue):
    job: VideoWriterJobFacts
    effective_outcome: Literal["completed", "cancelled", "failed"]
    acceptance: VideoWriterAcceptance | None
    execution_plan: VideoExecutionPlan | None


class VideoWriterPolicyData(_WriterValue):
    functions: tuple[StrictStr, ...]
    max_request_bytes: Annotated[StrictInt, Field(ge=1)]
    max_staged_bytes: Annotated[StrictInt, Field(ge=1)]

    @field_validator("functions", mode="before")
    @classmethod
    def functions_are_array(cls, value: Any):
        if type(value) not in (list, tuple):
            raise ValueError("functions must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def exact_policy(self) -> VideoWriterPolicyData:
        if (
            self.functions != (VIDEO_WRITER_FUNCTION,)
            or self.max_request_bytes != MAX_VIDEO_WRITER_REQUEST_BYTES
            or self.max_staged_bytes != MAX_VIDEO_WRITER_STAGED_BYTES
        ):
            raise ValueError("Labeling writer policy changed")
        return self


class VideoWriterPrepareData(_WriterValue):
    receipt: VideoWriterReceipt | None
    facts: VideoWriterFacts | None

    @model_validator(mode="after")
    def exactly_replay_or_facts(self) -> VideoWriterPrepareData:
        if (self.receipt is None) == (self.facts is None):
            raise ValueError("prepare must return either a receipt or current facts")
        return self


class VideoWriterLockMasksData(_WriterValue):
    epochs: dict[StrictStr, Annotated[StrictInt, Field(ge=0)]]

    @field_validator("epochs", mode="before")
    @classmethod
    def epochs_are_object(cls, value: Any):
        if type(value) is not dict:
            raise ValueError("mask epochs must be an object")
        return value


class VideoWriterReceiptData(_WriterValue):
    receipt: VideoWriterReceipt


class VideoWriterStatusData(_WriterValue):
    receipt: VideoWriterReceipt | None


class VideoWriterExecuteReply(_WriterValue):
    schema_: Literal["aap-labeling-video-writer/v1"] = Field(alias="schema")
    receipt: VideoWriterReceipt


class VideoWriterStatusReply(_WriterValue):
    schema_: Literal["aap-labeling-video-writer/v1"] = Field(alias="schema")
    receipt: VideoWriterReceipt | None


class _VideoWriterDataMetadata(_WriterValue):
    operation_id: CanonicalUUID
    execution: ExecutionProof
    outcome: Literal["completed", "cancelled", "failed"]
    request_digest: Digest
    expected_input: VideoWriterExpectedInput | None
    error_message: Annotated[StrictStr, Field(max_length=2000)] | None


class VideoWriterPolicyArguments(_WriterValue):
    pass


class VideoWriterPrepareArguments(_VideoWriterDataMetadata):
    pass


class VideoWriterLockMasksArguments(_VideoWriterDataMetadata):
    object_keys: tuple[Annotated[StrictStr, Field(min_length=1, max_length=255)], ...]

    @field_validator("object_keys", mode="before")
    @classmethod
    def ordered_object_keys(cls, value: Any):
        if type(value) not in (list, tuple):
            raise ValueError("mask keys must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def sorted_unique_keys(self) -> VideoWriterLockMasksArguments:
        if self.object_keys != tuple(sorted(set(self.object_keys))):
            raise ValueError("mask keys must be sorted and unique")
        return self


class VideoWriterApplyArguments(_VideoWriterDataMetadata):
    staged_result_json: StrictStr | None
    staged_sha256: Digest | None
    staged_byte_size: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def exact_staged_bytes(self) -> VideoWriterApplyArguments:
        if self.staged_result_json is None:
            if self.staged_sha256 is not None or self.staged_byte_size != 0:
                raise ValueError(
                    "empty staged result must have zero size and no digest"
                )
            return self
        raw = self.staged_result_json.encode("utf-8", errors="strict")
        if len(raw) > MAX_VIDEO_WRITER_STAGED_BYTES:
            raise ValueError("staged result exceeds the 64 MiB limit")
        if self.staged_sha256 is None or self.staged_byte_size != len(raw):
            raise ValueError("staged result byte identity is incomplete")
        if hashlib.sha256(raw).hexdigest() != self.staged_sha256:
            raise ValueError("staged result digest changed")
        parsed = _strict_json_object(raw, maximum=MAX_VIDEO_WRITER_STAGED_BYTES)
        if _canonical_json(parsed) != raw:
            raise ValueError("staged result is not canonical JSON")
        return self


class VideoWriterPublicationArguments(_WriterValue):
    execution: ExecutionProof


class VideoWriterStatusArguments(_WriterValue):
    operation_id: CanonicalUUID
    execution: ExecutionProof
    request_digest: Digest


class VideoWriterReadWindowArguments(_WriterValue):
    operation_id: CanonicalUUID
    execution: ExecutionProof
    request_digest: Digest
    window_ordinal: Annotated[StrictInt, Field(ge=0, le=8191)]


class VideoWriterStoredCheckpoint(_WriterValue):
    manifest: VideoWindowCheckpointManifest
    payload_json: StrictStr


class VideoWriterWindowAttempt(_WriterValue):
    generation: Annotated[StrictInt, Field(ge=1)]
    window_ordinal: Annotated[StrictInt, Field(ge=0, le=8191)]
    mode: Literal["execute", "replay"]
    state: Literal["started", "capturing", "captured", "replay_verified", "abandoned"]


class VideoWriterReadWindowData(_WriterValue):
    checkpoint: VideoWriterStoredCheckpoint | None
    attempt: VideoWriterWindowAttempt | None


_DATA_ARGUMENT_MODELS = {
    "policy": VideoWriterPolicyArguments,
    "prepare": VideoWriterPrepareArguments,
    "lock_masks": VideoWriterLockMasksArguments,
    "apply": VideoWriterApplyArguments,
    "status": VideoWriterStatusArguments,
    "read_publication": VideoWriterPublicationArguments,
    "read_window": VideoWriterReadWindowArguments,
}


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if type(value) in (list, tuple):
        return [_json_ready(child) for child in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("value is not strict JSON")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            _json_ready(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise VideoWriterInvalidRequest() from None


def _input_bytes(value: Any, *, maximum: int) -> tuple[bytes, Any]:
    try:
        if type(value) is bytes:
            raw = value
        elif type(value) is str:
            raw = value.encode("utf-8", errors="strict")
        elif isinstance(value, BaseModel) or isinstance(value, Mapping):
            raw = _canonical_json(value)
        else:
            raise ValueError("invalid wire input")
        if len(raw) > maximum:
            raise ValueError("wire input is too large")
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        return raw, parsed
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise VideoWriterInvalidRequest() from None


def _strict_json_object(raw: bytes, *, maximum: int) -> dict[str, Any]:
    try:
        if len(raw) > maximum:
            raise ValueError("message is too large")
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if type(value) is not dict:
            raise ValueError("message must be a JSON object")
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise VideoWriterInvalidRequest() from None


def _model_from_wire(model: type[_WriterValue], value: Any, *, maximum: int):
    _raw, parsed = _input_bytes(value, maximum=maximum)
    try:
        return model.model_validate(parsed)
    except (ValidationError, TypeError, ValueError, RecursionError, OverflowError):
        raise VideoWriterInvalidRequest() from None


def video_writer_operation_id(run_id: UUID) -> UUID:
    try:
        return uuid5(run_id, VIDEO_WRITER_OPERATION_NAME)
    except (TypeError, ValueError, AttributeError):
        raise VideoWriterInvalidRequest() from None


def make_video_writer_command(
    execution: ExecutionProof | Mapping[str, Any],
    *,
    outcome: Literal["completed", "cancelled", "failed"],
    expected_input: VideoWriterExpectedInput | Mapping[str, Any] | None,
    frame_results: Sequence[VideoFrameResultRow | Mapping[str, Any]],
    error_message: str | None = None,
) -> VideoWriterCommand:
    """Build the stable finalization command while preserving result order."""
    try:
        proof = (
            execution
            if type(execution) is ExecutionProof
            else ExecutionProof.model_validate(execution)
        )
        if (
            type(frame_results) not in (tuple, list)
            or len(frame_results) > MAX_VIDEO_RESULT_ROWS
        ):
            raise ValueError("frame results must be a bounded array")
        normalized: list[VideoFrameResultRow] = []
        for row in frame_results:
            raw_row = (
                row.model_dump(mode="json", by_alias=True)
                if type(row) is VideoFrameResultRow
                else row
            )
            # The public normalizer adds and verifies the existing candidate
            # identity. A one-row batch avoids its branch sort changing the
            # executor's whole-job order.
            normalized.append(
                normalize_video_frame_results([raw_row], branch="forward")[0]
            )
        expected = (
            None
            if expected_input is None
            else expected_input
            if type(expected_input) is VideoWriterExpectedInput
            else VideoWriterExpectedInput.model_validate(expected_input)
        )
        command = VideoWriterCommand(
            schema=LABELING_VIDEO_WRITER_PROTOCOL,
            operation_id=video_writer_operation_id(proof.run_id),
            execution=proof,
            outcome=outcome,
            expected_input=expected,
            frame_results=tuple(normalized),
            error_message=error_message,
        )
        if len(video_writer_command_bytes(command)) > MAX_VIDEO_WRITER_REQUEST_BYTES:
            raise ValueError("request exceeds the protocol limit")
        return command
    except VideoWriterFailure:
        raise
    except (
        TypeError,
        ValueError,
        ValidationError,
        VideoExecutionContractFailure,
        RecursionError,
        OverflowError,
    ):
        raise VideoWriterInvalidRequest() from None


def decode_video_writer_command(value: Any) -> VideoWriterCommand:
    command = _model_from_wire(
        VideoWriterCommand, value, maximum=MAX_VIDEO_WRITER_REQUEST_BYTES
    )
    try:
        if len(video_writer_command_bytes(command)) > MAX_VIDEO_WRITER_REQUEST_BYTES:
            raise ValueError("request exceeds the protocol limit")
    except VideoWriterFailure:
        raise
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise VideoWriterInvalidRequest() from None
    return command


def video_writer_command_bytes(value: VideoWriterCommand | Any) -> bytes:
    command = (
        value
        if type(value) is VideoWriterCommand
        else decode_video_writer_command(value)
    )
    raw = _canonical_json(command)
    if len(raw) > MAX_VIDEO_WRITER_REQUEST_BYTES:
        raise VideoWriterInvalidRequest()
    return raw


def video_writer_command_digest(value: VideoWriterCommand | Any) -> str:
    return hashlib.sha256(video_writer_command_bytes(value)).hexdigest()


def make_video_writer_status_request(
    operation_id: UUID,
    execution: ExecutionProof | Mapping[str, Any],
    request_digest: str,
) -> VideoWriterStatusRequest:
    try:
        proof = (
            execution
            if type(execution) is ExecutionProof
            else ExecutionProof.model_validate(execution)
        )
        request = VideoWriterStatusRequest(
            schema=LABELING_VIDEO_WRITER_PROTOCOL,
            operation_id=operation_id,
            execution=proof,
            request_digest=request_digest,
        )
        return request
    except (TypeError, ValueError, ValidationError):
        raise VideoWriterInvalidRequest() from None


def decode_video_writer_status_request(value: Any) -> VideoWriterStatusRequest:
    return _model_from_wire(VideoWriterStatusRequest, value, maximum=16 * 1024)


def decode_video_writer_publication_request(
    value: Any,
) -> VideoWriterPublicationRequest:
    return _model_from_wire(VideoWriterPublicationRequest, value, maximum=16 * 1024)


def video_writer_publication_request_bytes(execution: Any) -> bytes:
    try:
        return _canonical_json(
            VideoWriterPublicationRequest(
                schema=LABELING_VIDEO_WRITER_PROTOCOL, execution=execution
            )
        )
    except (TypeError, ValueError, ValidationError):
        raise VideoWriterInvalidRequest() from None


def video_writer_status_request_bytes(value: VideoWriterStatusRequest | Any) -> bytes:
    request = (
        value
        if type(value) is VideoWriterStatusRequest
        else decode_video_writer_status_request(value)
    )
    raw = _canonical_json(request)
    if len(raw) > 16 * 1024:
        raise VideoWriterInvalidRequest()
    return raw


def video_writer_data_arguments(action: str, value: Any) -> _WriterValue:
    model = _DATA_ARGUMENT_MODELS.get(action)
    if model is None:
        raise VideoWriterInvalidRequest()
    return _model_from_wire(model, value, maximum=MAX_VIDEO_WRITER_NATIVE_BYTES)


def video_writer_data_arguments_bytes(action: str, value: Any) -> bytes:
    raw = _canonical_json(video_writer_data_arguments(action, value))
    if len(raw) > MAX_VIDEO_WRITER_NATIVE_BYTES:
        raise VideoWriterInvalidRequest()
    return raw


def _typed_data_arguments(action: str, value: Any) -> _WriterValue:
    model = _DATA_ARGUMENT_MODELS.get(action)
    if model is None:
        raise VideoWriterUnavailable()
    try:
        return (
            value if type(value) is model else model.model_validate(_json_ready(value))
        )
    except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise VideoWriterUnavailable() from None


def _response_data(action: str, value: Any) -> _WriterValue:
    data_models = {
        "policy": VideoWriterPolicyData,
        "prepare": VideoWriterPrepareData,
        "lock_masks": VideoWriterLockMasksData,
        "apply": VideoWriterReceiptData,
        "status": VideoWriterStatusData,
        "read_publication": VideoWriterStatusData,
        "read_window": VideoWriterReadWindowData,
    }
    model = data_models.get(action)
    if model is None:
        raise VideoWriterUnavailable()
    try:
        return value if type(value) is model else model.model_validate(value)
    except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise VideoWriterUnavailable() from None


def _receipt_matches(
    receipt: VideoWriterReceipt,
    *,
    operation_id: UUID,
    execution: ExecutionProof,
    request_digest: str,
) -> bool:
    return (
        receipt.operation_id == operation_id
        and receipt.job_id == execution.job_id
        and receipt.run_id == execution.run_id
        and receipt.request_digest == request_digest
    )


def decode_video_writer_data_reply(
    action: str, request: Any, value: Any
) -> _WriterValue:
    """Decode one exact Native envelope and bind its data to the call."""
    arguments = _typed_data_arguments(action, request)
    _raw, parsed = _input_bytes(value, maximum=MAX_VIDEO_WRITER_NATIVE_BYTES)
    if (
        type(parsed) is not dict
        or parsed.get("schema") != LABELING_VIDEO_WRITER_DATA_SCHEMA
    ):
        raise VideoWriterUnavailable()
    if parsed.get("ok") is False:
        if set(parsed) != {"schema", "ok", "error"}:
            raise VideoWriterUnavailable()
        error = parsed.get("error")
        if type(error) is not dict or set(error) != {"code"}:
            raise VideoWriterUnavailable()
        code = error.get("code")
        if type(code) is not str or code not in _ERROR_CODES:
            raise VideoWriterUnavailable()
        raise VideoWriterRejected(code)
    if parsed.get("ok") is not True or set(parsed) != {"schema", "ok", "data"}:
        raise VideoWriterUnavailable()
    data = _response_data(action, parsed["data"])
    if action == "policy":
        decode_video_writer_policy(data)
        return data
    if action == "prepare":
        assert isinstance(arguments, VideoWriterPrepareArguments)
        assert isinstance(data, VideoWriterPrepareData)
        metadata = arguments
        if data.receipt is not None:
            if data.facts is not None or not _receipt_matches(
                data.receipt,
                operation_id=metadata.operation_id,
                execution=metadata.execution,
                request_digest=metadata.request_digest,
            ):
                raise VideoWriterUnavailable()
        elif data.facts is None or data.facts.job.job_id != metadata.execution.job_id:
            raise VideoWriterUnavailable()
    elif action == "lock_masks":
        assert isinstance(arguments, VideoWriterLockMasksArguments)
        assert isinstance(data, VideoWriterLockMasksData)
        if tuple(sorted(data.epochs)) != arguments.object_keys:
            raise VideoWriterUnavailable()
    elif action == "apply":
        assert isinstance(arguments, VideoWriterApplyArguments)
        assert isinstance(data, VideoWriterReceiptData)
        if not _receipt_matches(
            data.receipt,
            operation_id=arguments.operation_id,
            execution=arguments.execution,
            request_digest=arguments.request_digest,
        ):
            raise VideoWriterUnavailable()
        if (
            data.receipt.staged_sha256 != arguments.staged_sha256
            or data.receipt.staged_byte_size != arguments.staged_byte_size
        ):
            raise VideoWriterUnavailable()
    elif action == "status":
        assert isinstance(arguments, VideoWriterStatusArguments)
        assert isinstance(data, VideoWriterStatusData)
        if data.receipt is not None and not _receipt_matches(
            data.receipt,
            operation_id=arguments.operation_id,
            execution=arguments.execution,
            request_digest=arguments.request_digest,
        ):
            raise VideoWriterUnavailable()
    elif action == "read_publication":
        assert isinstance(arguments, VideoWriterPublicationArguments)
        assert isinstance(data, VideoWriterStatusData)
        receipt = data.receipt
        if receipt is not None and (
            receipt.job_id != arguments.execution.job_id
            or receipt.run_id != arguments.execution.run_id
            or receipt.operation_id
            != video_writer_operation_id(arguments.execution.run_id)
        ):
            raise VideoWriterUnavailable()
    elif action == "read_window":
        assert isinstance(arguments, VideoWriterReadWindowArguments)
        assert isinstance(data, VideoWriterReadWindowData)
        if data.attempt is not None and (
            data.attempt.generation != arguments.execution.generation
            or data.attempt.window_ordinal != arguments.window_ordinal
        ):
            raise VideoWriterUnavailable()
        if (
            data.checkpoint is not None
            and data.checkpoint.manifest.window_ordinal != arguments.window_ordinal
        ):
            raise VideoWriterUnavailable()
        if data.attempt is not None:
            if data.checkpoint is None and (
                data.attempt.mode == "replay"
                or data.attempt.state in {"captured", "replay_verified"}
            ):
                raise VideoWriterUnavailable()
            if data.checkpoint is not None and (
                (data.attempt.mode == "execute" and data.attempt.state != "captured")
                or (
                    data.attempt.mode == "replay"
                    and data.attempt.state != "replay_verified"
                )
            ):
                raise VideoWriterUnavailable()
    return data


def decode_video_writer_policy(value: Any) -> VideoWriterPolicyData:
    if type(value) is VideoWriterPolicyData:
        return value
    try:
        if type(value) is dict and set(value) == {"schema", "ok", "data"}:
            envelope = value
            if (
                envelope.get("schema") != LABELING_VIDEO_WRITER_DATA_SCHEMA
                or envelope.get("ok") is not True
            ):
                raise ValueError("invalid policy envelope")
            value = envelope["data"]
        return VideoWriterPolicyData.model_validate(value)
    except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise VideoWriterUnavailable() from None


def decode_video_writer_policy_reply(value: Any) -> VideoWriterPolicyData:
    return decode_video_writer_data_reply("policy", {}, value)  # type: ignore[return-value]


def _validate_facts_for_command(
    command: VideoWriterCommand,
    facts: VideoWriterFacts,
) -> VideoWriterFacts:
    job = facts.job
    if job.job_id != command.execution.job_id:
        raise VideoWriterUnavailable()
    if facts.effective_outcome not in {"completed", "cancelled", "failed"}:
        raise VideoWriterUnavailable()
    if command.outcome == "completed" and facts.effective_outcome not in {
        "completed",
        "cancelled",
    }:
        raise VideoWriterUnavailable()
    if command.outcome == "cancelled" and facts.effective_outcome != "cancelled":
        raise VideoWriterUnavailable()
    if command.outcome == "failed" and facts.effective_outcome not in {
        "failed",
        "cancelled",
    }:
        raise VideoWriterUnavailable()
    accepted = facts.acceptance
    plan = facts.execution_plan
    if plan is None or accepted is None:
        if _needs_staging(command, facts):
            raise VideoWriterUnavailable()
        return facts
    evidence = accepted.evidence
    execution = accepted.execution
    expected = command.expected_input
    if expected is not None and (
        evidence.input_id != expected.input_id
        or evidence.request_digest != expected.request_digest
        or evidence.plan_digest != expected.plan_digest
        or evidence.source_version_id != expected.source_version_id
        or evidence.source_sha256 != expected.source_sha256
    ):
        raise VideoWriterUnavailable()
    if (
        evidence.tracker_job_id != job.job_id
        or evidence.task_id != job.task_id
        or evidence.project_id != plan.project_id
        or evidence.input_id != plan.input_id
        or evidence.request_digest != plan.request_digest
        or evidence.plan_digest != plan.source_plan_digest
        or evidence.prompt_digest != plan.prompt_digest
        or evidence.timebase_digest != plan.timebase.timebase_digest
        or evidence.source_version_id != plan.source.source_version_id
        or evidence.source_sha256 != plan.source.source_sha256
        or evidence.from_frame != job.from_frame
        or evidence.to_frame != job.to_frame
        or evidence.direction != job.direction
        or evidence.grid_step != plan.timebase.grid_step
        or execution.job_kind != job.job_kind
        or execution.grid_step != evidence.grid_step
        or execution.from_frame != job.from_frame
        or execution.to_frame != job.to_frame
        or execution.direction != job.direction
        or execution.correction_frame != plan.correction_frame
        or execution.segment_id != plan.segment_id
        or execution.model_key != plan.requested_model_key
        or plan.task_id != job.task_id
        or plan.job_kind != job.job_kind
        or plan.from_frame != job.from_frame
        or plan.to_frame != job.to_frame
        or plan.direction != job.direction
    ):
        raise VideoWriterUnavailable()
    return facts


def _needs_staging(command: VideoWriterCommand, facts: VideoWriterFacts) -> bool:
    if command.outcome == "completed" and not (
        facts.effective_outcome == "cancelled" and facts.job.job_kind == "correction"
    ):
        return True
    if command.outcome != "cancelled" or facts.job.job_kind != "tracking":
        return False
    return facts.acceptance is not None or bool(command.frame_results)


def _source_instance_map(
    facts: VideoWriterFacts,
    frame_results: Sequence[VideoFrameResultRow],
) -> dict[str, str]:
    accepted = facts.acceptance
    if accepted is None:
        raise VideoWriterUnavailable()
    execution = accepted.execution
    source_ids = {snapshot.annotation_id for snapshot in execution.source_annotations}
    prompt_snapshot = execution.prompt_snapshot
    seeds = prompt_snapshot.get("seeds") or []
    if type(seeds) is not list:
        raise VideoWriterUnavailable()
    mapping: dict[str, UUID] = {}
    for seed in seeds:
        if type(seed) is not dict:
            raise VideoWriterUnavailable()
        source = seed.get("source_annotation_id")
        instance = seed.get("obj_id")
        if source is None:
            continue
        if instance is None:
            raise VideoWriterUnavailable()
        try:
            annotation_id = source if isinstance(source, UUID) else UUID(str(source))
        except (ValueError, TypeError, AttributeError):
            raise VideoWriterUnavailable() from None
        instance_id = str(instance)
        if instance_id in mapping and mapping[instance_id] != annotation_id:
            raise VideoWriterRejected("source_map_invalid")
        mapping[instance_id] = annotation_id

    annotation_id = facts.job.annotation_id
    if annotation_id is not None and annotation_id not in mapping.values():
        if annotation_id not in source_ids:
            raise VideoWriterRejected("source_map_invalid")
        primary_id = _primary_instance_id(frame_results) or "1"
        if primary_id in mapping:
            raise VideoWriterRejected("source_map_invalid")
        mapping[primary_id] = annotation_id
    if not set(mapping.values()).issubset(source_ids):
        raise VideoWriterRejected("source_map_invalid")
    return {instance: str(source) for instance, source in mapping.items()}


def _primary_instance_id(rows: Sequence[VideoFrameResultRow]) -> str | None:
    if all(row.instance_id is None for row in rows):
        return None
    marked = next(
        (
            row.instance_id
            for row in rows
            if row.primary and row.instance_id is not None
        ),
        None,
    )
    ids = [row.instance_id for row in rows if row.instance_id is not None]
    if not ids:
        return None
    primary_id = marked or min(
        ids, key=lambda value: (0, int(value)) if value.isdigit() else (1, value)
    )
    # Unlabelled rows join the primary partition in the existing acceptance
    # policy; its first row can therefore still select the singleton source.
    return next(
        row.instance_id
        for row in rows
        if row.instance_id is None or row.instance_id == primary_id
    )


def _staged_result(
    command: VideoWriterCommand,
    facts: VideoWriterFacts,
    frame_results: Sequence[VideoFrameResultRow],
) -> dict[str, Any] | None:
    if not _needs_staging(command, facts):
        return None
    accepted = facts.acceptance
    plan = facts.execution_plan
    if accepted is None or plan is None:
        raise VideoWriterUnavailable()
    if facts.effective_outcome == "cancelled" and facts.job.job_kind == "correction":
        return None
    if facts.effective_outcome == "cancelled" and not frame_results:
        return None
    evidence = accepted.evidence
    staged = {
        "results": [
            row.model_dump(mode="json", by_alias=True) for row in frame_results
        ],
        "grid_step": evidence.grid_step,
        "output_geometry": plan.output.geometry,
        "source_instance_map": _source_instance_map(facts, frame_results),
        "tracker_input_evidence": evidence.model_dump(mode="json", by_alias=True),
    }
    if command.outcome == "completed":
        track_windows = [window for window in plan.windows if window.phase == "track"]
        boundaries = [
            window.from_frame if window.branch == "forward" else window.to_frame
            for index, window in enumerate(track_windows)
            if index
            and window.context_span.mode == "session"
            and window.ordinal == window.context_span.start_ordinal
        ]
        if boundaries:
            staged["tracker_context_boundaries"] = boundaries
    raw = json.dumps(
        staged, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(raw) > MAX_VIDEO_WRITER_STAGED_BYTES:
        raise VideoWriterRejected("staged_result_too_large")
    return staged


def video_writer_staged_json(
    value: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, int]:
    if value is None:
        return None, None, 0
    raw = _canonical_json(value)
    if len(raw) > MAX_VIDEO_WRITER_STAGED_BYTES:
        raise VideoWriterRejected("staged_result_too_large")
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest(), len(raw)


def collect_video_writer_mask_references(value: Any) -> tuple[MaskReference, ...]:
    """Collect actual nested RLE refs and validate their existing value shape."""
    found: dict[tuple[str, bytes], MaskReference] = {}

    def visit(node: Any) -> None:
        if type(node) is dict:
            object_key = node.get("object_key")
            encoding = node.get("encoding")
            candidate = (
                type(object_key) is str and object_key.startswith(f"{MASK_PREFIX}/")
            ) or encoding in {"coco_rle_ref", "coco_rle_gzip"}
            if candidate:
                try:
                    reference = MaskReference.model_validate(node)
                except (TypeError, ValueError, ValidationError):
                    raise VideoWriterRejected("candidate_invalid") from None
                identity = (reference.object_key, _canonical_json(reference))
                found[identity] = reference
                return
            for child in node.values():
                visit(child)
        elif type(node) is list:
            for child in node:
                visit(child)

    try:
        visit(value)
        return tuple(found[key] for key in sorted(found))
    except VideoWriterFailure:
        raise
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise VideoWriterRejected("candidate_invalid") from None


def validate_video_writer_checkpoint(
    plan: VideoExecutionPlan,
    checkpoint: VideoWriterStoredCheckpoint,
    *,
    window_ordinal: int,
    predecessor: VideoWindowCheckpointManifest | None,
):
    """Verify exact canonical Jobs payload bytes and its frozen plan chain."""
    try:
        manifest = decode_video_window_checkpoint_manifest(checkpoint.manifest)
        if manifest.window_ordinal != window_ordinal:
            raise ValueError("checkpoint is for another plan window")
        raw = checkpoint.payload_json.encode("utf-8", errors="strict")
        payload_document = _strict_json_object(
            raw, maximum=MAX_VIDEO_WRITER_STAGED_BYTES
        )
        if _canonical_json(payload_document) != raw:
            raise ValueError("stored payload is not canonical JSON")
        if (
            len(raw) != manifest.payload_byte_size
            or hashlib.sha256(raw).hexdigest() != manifest.payload_digest
        ):
            raise ValueError("stored payload bytes differ from the manifest")
        payload = decode_video_window_checkpoint_payload(raw)
        with snapshot_video_window_checkpoint(
            plan,
            payload,
            predecessor_checkpoint=predecessor,
        ) as snapshot:
            if snapshot.manifest != manifest:
                raise ValueError("stored manifest differs from its canonical payload")
        return manifest, payload
    except VideoWriterInvalidRequest:
        raise VideoWriterUnavailable() from None
    except VideoWriterFailure:
        raise
    except (
        TypeError,
        ValueError,
        ValidationError,
        VideoExecutionContractFailure,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise VideoWriterUnavailable() from None


video_writer_request_bytes = video_writer_command_bytes
decode_native_reply = decode_video_writer_data_reply


def decode_video_writer_api_reply(
    value: Any,
    *,
    status: bool = False,
    request: VideoWriterCommand | VideoWriterStatusRequest | Any | None = None,
) -> VideoWriterReceipt | None:
    model = VideoWriterStatusReply if status else VideoWriterExecuteReply
    _raw, parsed = _input_bytes(value, maximum=MAX_VIDEO_WRITER_RESPONSE_BYTES)
    if (
        type(parsed) is not dict
        or parsed.get("schema") != LABELING_VIDEO_WRITER_PROTOCOL
    ):
        raise VideoWriterUnavailable()
    if set(parsed) == {"schema", "error"}:
        error = parsed["error"]
        if type(error) is not dict or set(error) != {"code"}:
            raise VideoWriterUnavailable()
        code = error.get("code")
        if type(code) is not str or code not in _API_ERROR_CODES:
            raise VideoWriterUnavailable()
        raise VideoWriterRejected(code)
    try:
        decoded = model.model_validate(parsed)
    except (TypeError, ValueError, ValidationError, RecursionError, OverflowError):
        raise VideoWriterUnavailable() from None
    receipt = decoded.receipt
    if request is None or receipt is None:
        return receipt
    if status:
        expected = (
            request
            if type(request) is VideoWriterStatusRequest
            else decode_video_writer_status_request(request)
        )
        matches = _receipt_matches(
            receipt,
            operation_id=expected.operation_id,
            execution=expected.execution,
            request_digest=expected.request_digest,
        )
    else:
        expected = (
            request
            if type(request) is VideoWriterCommand
            else decode_video_writer_command(request)
        )
        matches = _receipt_matches(
            receipt,
            operation_id=expected.operation_id,
            execution=expected.execution,
            request_digest=video_writer_command_digest(expected),
        )
    if not matches:
        raise VideoWriterUnavailable()
    return receipt


def video_writer_schema() -> dict[str, Any]:
    """Return the checked-in private HTTP schema, kept language-neutral."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": LABELING_VIDEO_WRITER_PROTOCOL,
        "title": "Labeling video writer protocol",
        "oneOf": [
            {"$ref": "#/$defs/VideoWriterCommand"},
            {"$ref": "#/$defs/VideoWriterStatusRequest"},
            {"$ref": "#/$defs/VideoWriterPublicationRequest"},
        ],
        "$defs": {
            "ExecutionProof": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "job_id",
                    "run_id",
                    "worker_id",
                    "generation",
                    "lease_token",
                ],
                "properties": {
                    "job_id": {"type": "string", "format": "uuid"},
                    "run_id": {"type": "string", "format": "uuid"},
                    "worker_id": {"type": "string", "format": "uuid"},
                    "generation": {"type": "integer", "minimum": 1},
                    "lease_token": {
                        "type": "string",
                        "format": "uuid",
                        "writeOnly": True,
                    },
                },
            },
            "ExpectedInput": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "input_id",
                    "request_digest",
                    "plan_digest",
                    "source_version_id",
                    "source_sha256",
                ],
                "properties": {
                    "input_id": {"type": "string", "format": "uuid"},
                    "request_digest": {"$ref": "#/$defs/Digest"},
                    "plan_digest": {"$ref": "#/$defs/Digest"},
                    "source_version_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 255,
                    },
                    "source_sha256": {"$ref": "#/$defs/Digest"},
                },
            },
            "Digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "FrameResult": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "frame_index",
                    "geometry",
                    "confidence",
                    "outside",
                    "instance_id",
                    "primary",
                    "geometry_digest",
                    "candidate_key",
                ],
                "properties": {
                    "frame_index": {"type": "integer", "minimum": 0},
                    "geometry": {"type": "object"},
                    "confidence": {"type": ["number", "null"]},
                    "outside": {"type": "boolean"},
                    "instance_id": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 255,
                    },
                    "primary": {"type": "boolean"},
                    "geometry_digest": {
                        "type": "string",
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                    "candidate_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 512,
                    },
                },
            },
            "VideoWriterCommand": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "schema",
                    "operation_id",
                    "execution",
                    "outcome",
                    "expected_input",
                    "frame_results",
                    "error_message",
                ],
                "properties": {
                    "schema": {"const": LABELING_VIDEO_WRITER_PROTOCOL},
                    "operation_id": {
                        "type": "string",
                        "format": "uuid",
                        "x-uuid5": {
                            "namespace": "execution.run_id",
                            "name": VIDEO_WRITER_OPERATION_NAME,
                        },
                    },
                    "execution": {"$ref": "#/$defs/ExecutionProof"},
                    "outcome": {"enum": ["completed", "cancelled", "failed"]},
                    "expected_input": {
                        "anyOf": [{"$ref": "#/$defs/ExpectedInput"}, {"type": "null"}]
                    },
                    "frame_results": {
                        "type": "array",
                        "maxItems": MAX_VIDEO_RESULT_ROWS,
                        "items": {"$ref": "#/$defs/FrameResult"},
                    },
                    "error_message": {"type": ["string", "null"], "maxLength": 2000},
                },
            },
            "VideoWriterPublicationRequest": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "execution"],
                "properties": {
                    "schema": {"const": LABELING_VIDEO_WRITER_PROTOCOL},
                    "execution": {"$ref": "#/$defs/ExecutionProof"},
                },
            },
            "VideoWriterStatusRequest": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "operation_id", "execution", "request_digest"],
                "properties": {
                    "schema": {"const": LABELING_VIDEO_WRITER_PROTOCOL},
                    "operation_id": {
                        "type": "string",
                        "format": "uuid",
                        "x-uuid5": {
                            "namespace": "execution.run_id",
                            "name": VIDEO_WRITER_OPERATION_NAME,
                        },
                    },
                    "execution": {"$ref": "#/$defs/ExecutionProof"},
                    "request_digest": {"$ref": "#/$defs/Digest"},
                },
            },
            "VideoWriterReceipt": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "operation_id",
                    "job_id",
                    "run_id",
                    "request_digest",
                    "outcome",
                    "status",
                    "completed_at",
                    "staged_sha256",
                    "staged_byte_size",
                ],
                "properties": {
                    "operation_id": {"type": "string", "format": "uuid"},
                    "job_id": {"type": "string", "format": "uuid"},
                    "run_id": {"type": "string", "format": "uuid"},
                    "request_digest": {"$ref": "#/$defs/Digest"},
                    "outcome": {"enum": ["completed", "cancelled", "failed"]},
                    "status": {"enum": ["pending_review", "cancelled", "failed"]},
                    "completed_at": {"type": "string", "format": "date-time"},
                    "staged_sha256": {
                        "anyOf": [{"$ref": "#/$defs/Digest"}, {"type": "null"}]
                    },
                    "staged_byte_size": {"type": "integer", "minimum": 0},
                },
            },
            "VideoWriterExecuteReply": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "receipt"],
                "properties": {
                    "schema": {"const": LABELING_VIDEO_WRITER_PROTOCOL},
                    "receipt": {"$ref": "#/$defs/VideoWriterReceipt"},
                },
            },
            "VideoWriterStatusReply": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "receipt"],
                "properties": {
                    "schema": {"const": LABELING_VIDEO_WRITER_PROTOCOL},
                    "receipt": {
                        "anyOf": [
                            {"$ref": "#/$defs/VideoWriterReceipt"},
                            {"type": "null"},
                        ]
                    },
                },
            },
        },
        "x-budgets": {
            "request_bytes": MAX_VIDEO_WRITER_REQUEST_BYTES,
            "staged_bytes": MAX_VIDEO_WRITER_STAGED_BYTES,
            "native_bytes": MAX_VIDEO_WRITER_NATIVE_BYTES,
        },
        "x-http-error-codes": sorted(_API_ERROR_CODES),
        "x-native-data-protocol": {
            "schema": LABELING_VIDEO_WRITER_DATA_SCHEMA,
            "function": VIDEO_WRITER_FUNCTION,
            "actions": [
                "policy",
                "prepare",
                "read_window",
                "lock_masks",
                "apply",
                "status",
                "read_publication",
            ],
            "max_request_bytes": MAX_VIDEO_WRITER_REQUEST_BYTES,
            "max_staged_bytes": MAX_VIDEO_WRITER_STAGED_BYTES,
            "max_native_bytes": MAX_VIDEO_WRITER_NATIVE_BYTES,
            "receipt_fields": [
                "operation_id",
                "job_id",
                "run_id",
                "request_digest",
                "outcome",
                "status",
                "completed_at",
                "staged_sha256",
                "staged_byte_size",
            ],
        },
    }


__all__ = [
    "LABELING_VIDEO_WRITER_DATA_SCHEMA",
    "LABELING_VIDEO_WRITER_PROTOCOL",
    "MAX_VIDEO_WRITER_REQUEST_BYTES",
    "MAX_VIDEO_WRITER_NATIVE_BYTES",
    "MAX_VIDEO_WRITER_RESPONSE_BYTES",
    "MAX_VIDEO_WRITER_STAGED_BYTES",
    "VIDEO_WRITER_FUNCTION",
    "VideoWriterAcceptance",
    "VideoWriterApplyArguments",
    "VideoWriterCommand",
    "VideoWriterExecuteReply",
    "VideoWriterExpectedInput",
    "VideoWriterFacts",
    "VideoWriterFailure",
    "VideoWriterInvalidRequest",
    "VideoWriterJobFacts",
    "VideoWriterLockMasksArguments",
    "VideoWriterLockMasksData",
    "VideoWriterPolicyArguments",
    "VideoWriterPolicyData",
    "VideoWriterPrepareArguments",
    "VideoWriterPrepareData",
    "VideoWriterReceipt",
    "VideoWriterReceiptData",
    "VideoWriterReadWindowArguments",
    "VideoWriterReadWindowData",
    "VideoWriterRejected",
    "VideoWriterStatusArguments",
    "VideoWriterStatusData",
    "VideoWriterStatusReply",
    "VideoWriterStatusRequest",
    "VideoWriterPublicationRequest",
    "VideoWriterPublicationArguments",
    "VideoWriterUnavailable",
    "VideoWriterStoredCheckpoint",
    "VideoWriterWindowAttempt",
    "collect_video_writer_mask_references",
    "decode_video_writer_api_reply",
    "decode_video_writer_command",
    "decode_video_writer_data_reply",
    "decode_video_writer_policy",
    "decode_video_writer_policy_reply",
    "decode_video_writer_status_request",
    "decode_native_reply",
    "make_video_writer_command",
    "make_video_writer_status_request",
    "video_writer_command_bytes",
    "video_writer_command_digest",
    "video_writer_data_arguments",
    "video_writer_data_arguments_bytes",
    "video_writer_operation_id",
    "video_writer_request_bytes",
    "video_writer_schema",
    "video_writer_staged_json",
    "video_writer_status_request_bytes",
    "video_writer_publication_request_bytes",
    "decode_video_writer_publication_request",
    "_needs_staging",
    "_source_instance_map",
    "_staged_result",
    "_validate_facts_for_command",
    "validate_video_writer_checkpoint",
]
