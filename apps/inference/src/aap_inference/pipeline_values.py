"""Fixed ProjectPipeline row codec and old ORM dirty-value comparison."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
import json
import math
import uuid

from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable
from aap_workspace_contract.project_query_values import decode_postgres_timestamp
from aap_workspace_contract.wire import (
    _assert_json_value,
    _reject_constant,
    _unique_object,
)
from pydantic import ValidationError

from .pipeline_contract import (
    PIPELINE_INSERT_COLUMNS,
    PIPELINE_ROW_COLUMNS,
    PIPELINE_UPDATE_COLUMNS,
    PipelineInvalidRequest,
    PipelineRow,
    PipelineUnavailable,
)

_UUID_COLUMNS = ("id", "project_id", "organization_id", "created_by")
_NULLABLE_UUID_COLUMNS = frozenset({"project_id", "organization_id"})


def _raw_json(payload) -> object:
    if type(payload) not in (str, bytes):
        raise PipelineUnavailable()
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError:
        raise PipelineUnavailable() from None
    except ValueError as error:
        message = str(error)
        if message == "Duplicate JSON field" or message.startswith("Invalid JSON number:"):
            raise PipelineUnavailable() from None
        # Preserve driver-compatible ValueError phases such as Python's
        # excessive-integer-digit failure while decoding stored JSONB.
        raise
    try:
        _assert_json_value(value)
    except ValueError:
        raise PipelineUnavailable() from None
    return value


def _uuid(value, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, uuid.UUID):
        return value
    if type(value) is not str:
        raise PipelineUnavailable()
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise PipelineUnavailable() from None


def _timestamp(value):
    if value is None or isinstance(value, datetime):
        return value
    if type(value) is not str:
        raise PipelineUnavailable()
    # A DTO may already contain the naive extrema produced by the shared
    # PostgreSQL infinity decoder; accept that exact, normalized ISO spelling.
    if value == datetime.max.isoformat():
        return datetime.max
    if value == datetime.min.isoformat():
        return datetime.min
    try:
        return decode_postgres_timestamp(value)
    except ProjectQueryUnavailable:
        raise PipelineUnavailable() from None


def _decode_row_value(value, *, target_id=None) -> PipelineRow:
    if type(value) is not dict or set(value) != set(PIPELINE_ROW_COLUMNS):
        raise PipelineUnavailable()
    if type(value["scope"]) is not str or type(value["name"]) is not str:
        raise PipelineUnavailable()
    if type(value["is_default"]) is not bool or type(value["usage_count"]) is not int:
        raise PipelineUnavailable()
    row = dict(value)
    for field in _UUID_COLUMNS:
        row[field] = _uuid(row[field], nullable=field in _NULLABLE_UUID_COLUMNS)
    row["created_at"] = _timestamp(row["created_at"])
    row["updated_at"] = _timestamp(row["updated_at"])
    if target_id is not None and row["id"] != target_id:
        raise PipelineUnavailable()
    try:
        return PipelineRow.model_validate(row)
    except (ValidationError, ValueError, TypeError):
        raise PipelineUnavailable() from None


def decode_pipeline_row(row_json, *, target_id=None) -> PipelineRow | None:
    """Decode exactly one full stored ProjectPipeline row before policy runs."""
    if row_json is None:
        return None
    if type(target_id) is str:
        try:
            target_id = uuid.UUID(target_id)
        except ValueError:
            raise PipelineUnavailable() from None
    if isinstance(row_json, PipelineRow):
        if target_id is not None and row_json.id != target_id:
            raise PipelineUnavailable()
        return row_json
    try:
        value = _raw_json(row_json)
    except (TypeError, UnicodeError, RecursionError):
        raise PipelineUnavailable() from None
    if type(value) is not dict:
        raise PipelineUnavailable()
    return _decode_row_value(value, target_id=target_id)


def decode_pipeline_rows(rows_json) -> list[PipelineRow]:
    """Decode the complete ordered result set without truncation or filtering."""
    try:
        rows = _raw_json(rows_json)
    except (TypeError, UnicodeError, RecursionError):
        raise PipelineUnavailable() from None
    if type(rows) is not list:
        raise PipelineUnavailable()
    return [_decode_row_value(row) for row in rows]


def _stages_json(value) -> str:
    try:
        _assert_json_value(value)
        return json.dumps(
            value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PipelineInvalidRequest() from None


def _sql_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        value = value.value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is str:
        return value
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise PipelineInvalidRequest()
        return json.dumps(value, ensure_ascii=True, allow_nan=False)
    raise PipelineInvalidRequest()


def encode_pipeline_columns(payload) -> tuple[list[str], list[str | None]]:
    """Encode the ordered native write projection as columns and SQL text values."""
    if not isinstance(payload, Mapping) or any(type(key) is not str for key in payload):
        raise PipelineInvalidRequest()
    allowed = set(PIPELINE_INSERT_COLUMNS) | set(PIPELINE_UPDATE_COLUMNS)
    columns = list(payload)
    if any(column not in allowed for column in columns):
        raise PipelineInvalidRequest()
    values = []
    for column, value in payload.items():
        if column == "stages":
            values.append(_stages_json(value))
        else:
            if type(value) is float and not math.isfinite(value):
                raise PipelineInvalidRequest()
            values.append(_sql_text(value))
    return columns, values


def _uuid_equal(left, right) -> bool:
    if left is None or right is None:
        return left is right
    try:
        left_uuid = left if isinstance(left, uuid.UUID) else uuid.UUID(str(left))
        right_uuid = right if isinstance(right, uuid.UUID) else uuid.UUID(str(right))
        return left_uuid == right_uuid
    except (ValueError, TypeError, AttributeError):
        return left == right


def dirty_pipeline_payload(current, payload) -> dict:
    """Return only old-ORM-dirty update values, preserving insertion order."""
    if isinstance(current, PipelineRow):
        def get(name):
            return getattr(current, name)

    elif isinstance(current, Mapping):
        def get(name):
            return current[name]

    else:
        raise PipelineUnavailable()
    if not isinstance(payload, Mapping) or any(type(key) is not str for key in payload):
        raise PipelineInvalidRequest()
    if any(key not in PIPELINE_UPDATE_COLUMNS for key in payload):
        raise PipelineInvalidRequest()

    dirty = {}
    for key, value in payload.items():
        try:
            previous = get(key)
        except (KeyError, AttributeError):
            raise PipelineUnavailable() from None
        equal = (
            _uuid_equal(previous, value)
            if key in _NULLABLE_UUID_COLUMNS
            else previous == value
        )
        if not equal:
            dirty[key] = value
    return dirty


__all__ = [
    "decode_pipeline_row",
    "decode_pipeline_rows",
    "dirty_pipeline_payload",
    "encode_pipeline_columns",
]
