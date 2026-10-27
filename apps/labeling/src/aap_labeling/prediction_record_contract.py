"""Closed, portable commands for Labeling prediction records."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

PREDICTION_RECORDS_PROTOCOL = "aap-labeling-prediction-records/v1"
PREDICTION_RECORDS_FUNCTION = "aap_labeling_v1.prediction_record_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InsertPredictionResult(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["insert_result"]
    prediction_id: uuid.UUID
    prediction_created_at: datetime | None
    prediction_meta_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    ml_backend_id: uuid.UUID | None
    ml_backend_pool_id: uuid.UUID | None
    model_version: StrictStr | None
    score: float | None
    tool_unit_id: StrictStr
    result: list[dict[str, object]]
    source: StrictStr
    inference_time_ms: StrictInt | None
    prompt_tokens: StrictInt | None
    completion_tokens: StrictInt | None
    total_tokens: StrictInt | None
    prompt_cost: float | None
    completion_cost: float | None
    total_cost: float | None
    extra: dict[str, object]


class InsertTestSeedPrediction(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["insert_test_seed"]
    prediction_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    result: list[dict[str, object]]
    score: float | None
    model_version: StrictStr | None
    tool_unit_id: StrictStr | None
    rejected_shape_indexes: list[StrictInt] | None


class CreateFailedPrediction(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["create_failed"]
    failure_id: uuid.UUID
    task_id: uuid.UUID | None
    project_id: uuid.UUID
    ml_backend_id: uuid.UUID | None
    ml_backend_pool_id: uuid.UUID | None
    model_version: StrictStr | None
    error_type: StrictStr
    message: StrictStr
    extra: dict[str, object]
    execution_snapshot_id: uuid.UUID | None = None


class DismissFailedPrediction(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["dismiss_failed"]
    failure_id: uuid.UUID
    dismissed_at: datetime


class RestoreFailedPrediction(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["restore_failed"]
    failure_id: uuid.UUID


class DeletePredictionsForTasks(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["delete_for_tasks"]
    task_ids: list[uuid.UUID]


class DeletePredictionsForBatch(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["delete_for_batch"]
    batch_id: uuid.UUID


class PurgeImportedPredictions(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["purge_import_predictions"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID] | None
    source_scope: Literal["ml_backend", "external_import", "all"]


class RejectPredictionCandidate(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    operation: Literal["reject_candidate"]
    prediction_id: uuid.UUID
    prediction_created_at: datetime
    task_id: uuid.UUID
    project_id: uuid.UUID
    shape_index: StrictInt | None


PredictionRecordCommand: TypeAlias = Annotated[
    InsertPredictionResult
    | InsertTestSeedPrediction
    | CreateFailedPrediction
    | DismissFailedPrediction
    | RestoreFailedPrediction
    | DeletePredictionsForTasks
    | DeletePredictionsForBatch
    | PurgeImportedPredictions
    | RejectPredictionCandidate,
    Field(discriminator="operation"),
]


class PredictionRecordError(_ClosedModel):
    code: StrictStr
    details: dict[str, object]


class PredictionIdentity(_ClosedModel):
    id: uuid.UUID
    created_at: datetime


class InsertPredictionResultData(_ClosedModel):
    operation: Literal["insert_result"]
    prediction_id: uuid.UUID
    created_at: datetime
    tool_unit_id: StrictStr
    total_predictions: StrictInt
    task_updated_at: datetime


class InsertTestSeedPredictionData(_ClosedModel):
    operation: Literal["insert_test_seed"]
    prediction_id: uuid.UUID
    created_at: datetime
    tool_unit_id: StrictStr


class CreateFailedPredictionData(_ClosedModel):
    operation: Literal["create_failed"]
    failure_id: uuid.UUID
    created_at: datetime


class FailedPredictionStateData(_ClosedModel):
    operation: Literal["dismiss_failed", "restore_failed"]
    failure_id: uuid.UUID
    changed: StrictBool
    dismissed_at: datetime | None


class DeletePredictionData(_ClosedModel):
    operation: Literal["delete_for_tasks", "delete_for_batch"]
    predictions: StrictInt
    failed_predictions: StrictInt
    deleted_predictions: list[PredictionIdentity]
    deleted_failed_prediction_ids: list[uuid.UUID]
    deleted_prediction_meta_ids: list[uuid.UUID]


class PurgeImportedPredictionsData(_ClosedModel):
    operation: Literal["purge_import_predictions"]
    ml_backend: StrictInt
    external_import: StrictInt
    unknown: StrictInt
    total: StrictInt
    deleted_predictions: list[PredictionIdentity]
    deleted_prediction_meta_ids: list[uuid.UUID]


class RejectPredictionCandidateData(_ClosedModel):
    operation: Literal["reject_candidate"]
    prediction_id: uuid.UUID
    created_at: datetime
    task_id: uuid.UUID
    project_id: uuid.UUID
    rejected_shape_indexes: list[StrictInt]


PredictionRecordData: TypeAlias = Annotated[
    InsertPredictionResultData
    | InsertTestSeedPredictionData
    | CreateFailedPredictionData
    | FailedPredictionStateData
    | DeletePredictionData
    | PurgeImportedPredictionsData
    | RejectPredictionCandidateData,
    Field(discriminator="operation"),
]


class PredictionRecordReply(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-records/v1"] = Field(alias="schema")
    ok: StrictBool
    request: PredictionRecordCommand
    data: PredictionRecordData | None
    error: PredictionRecordError | None


class PredictionRecordUnavailable(RuntimeError):
    """The native Prediction record owner returned an invalid reply."""


class PredictionRecordRejected(RuntimeError):
    """The native Prediction record owner rejected a valid command."""

    def __init__(self, error: PredictionRecordError) -> None:
        self.code = error.code
        self.details = error.details
        super().__init__(error.code)


_COMMAND_ADAPTER = TypeAdapter(PredictionRecordCommand)
_REPLY_ADAPTER = TypeAdapter(PredictionRecordReply)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def decode_prediction_record_command(value: object) -> PredictionRecordCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PredictionRecordUnavailable() from None


def decode_prediction_record_reply(
    request: PredictionRecordCommand, value: object
) -> PredictionRecordData:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PredictionRecordUnavailable() from None
    if reply.request != request:
        raise PredictionRecordUnavailable()
    if not reply.ok:
        if reply.data is not None or reply.error is None:
            raise PredictionRecordUnavailable()
        raise PredictionRecordRejected(reply.error)
    if (
        reply.error is not None
        or reply.data is None
        or reply.data.operation != request.operation
    ):
        raise PredictionRecordUnavailable()
    return reply.data


def prediction_record_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PREDICTION_RECORDS_PROTOCOL
    schema["x-postgresql-functions"] = [PREDICTION_RECORDS_FUNCTION]
    return schema


def prediction_record_command_json(command: PredictionRecordCommand) -> str:
    validated = decode_prediction_record_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    )
    return validated.model_dump_json(by_alias=True, exclude_unset=True)


__all__ = [
    "PREDICTION_RECORDS_FUNCTION",
    "PREDICTION_RECORDS_PROTOCOL",
    "CreateFailedPrediction",
    "CreateFailedPredictionData",
    "DeletePredictionData",
    "DeletePredictionsForBatch",
    "DeletePredictionsForTasks",
    "DismissFailedPrediction",
    "FailedPredictionStateData",
    "InsertPredictionResult",
    "InsertPredictionResultData",
    "InsertTestSeedPrediction",
    "InsertTestSeedPredictionData",
    "PredictionIdentity",
    "PredictionRecordCommand",
    "PredictionRecordData",
    "PredictionRecordRejected",
    "PredictionRecordUnavailable",
    "PurgeImportedPredictions",
    "PurgeImportedPredictionsData",
    "RejectPredictionCandidate",
    "RejectPredictionCandidateData",
    "RestoreFailedPrediction",
    "decode_prediction_record_command",
    "decode_prediction_record_reply",
    "prediction_record_command_json",
    "prediction_record_schema",
]
