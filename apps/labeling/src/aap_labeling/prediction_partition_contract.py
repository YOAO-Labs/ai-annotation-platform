"""Closed commands for monthly Labeling prediction partition maintenance."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)


PREDICTION_PARTITIONS_PROTOCOL = "aap-labeling-prediction-partitions/v1"
PREDICTION_PARTITIONS_FUNCTION = "aap_labeling_v1.prediction_partition_command(jsonb)"
PredictionPartitionName = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^predictions_y[0-9]{4}m(0[1-9]|1[0-2])$"),
]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EnsurePredictionPartition(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-partitions/v1"] = Field(alias="schema")
    operation: Literal["ensure"]
    month: date = Field(json_schema_extra={"pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])-01$"})

    @field_validator("month")
    @classmethod
    def _require_first_day(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("prediction partition month must start on day one")
        return value


class EnsurePredictionPartitionData(_ClosedModel):
    operation: Literal["ensure"]
    month: date
    created: PredictionPartitionName | None


class PredictionPartitionReply(_ClosedModel):
    schema_: Literal["aap-labeling-prediction-partitions/v1"] = Field(alias="schema")
    ok: StrictBool
    request: EnsurePredictionPartition
    data: EnsurePredictionPartitionData


class PredictionPartitionUnavailable(RuntimeError):
    """The native Labeling prediction partition owner returned invalid data."""


_REPLY_ADAPTER = TypeAdapter(PredictionPartitionReply)


def prediction_partition_command_json(
    request: EnsurePredictionPartition,
) -> str:
    return request.model_dump_json(by_alias=True)


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def decode_prediction_partition_reply(
    request: EnsurePredictionPartition, value: object
) -> EnsurePredictionPartitionData:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise PredictionPartitionUnavailable() from None
    if not reply.ok or reply.request != request or reply.data.month != request.month:
        raise PredictionPartitionUnavailable()
    return reply.data


def prediction_partition_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PREDICTION_PARTITIONS_PROTOCOL
    schema["x-postgresql-functions"] = [PREDICTION_PARTITIONS_FUNCTION]
    return schema


__all__ = [
    "EnsurePredictionPartition",
    "EnsurePredictionPartitionData",
    "PREDICTION_PARTITIONS_FUNCTION",
    "PREDICTION_PARTITIONS_PROTOCOL",
    "PredictionPartitionUnavailable",
    "decode_prediction_partition_reply",
    "prediction_partition_command_json",
    "prediction_partition_schema",
]
