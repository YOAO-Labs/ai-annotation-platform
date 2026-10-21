"""Lossless Project row transport with the legacy driver decoding boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime
import json
import math
import re
import uuid

from .contract import _reject_constant, _unique_object
from .project_query_contract import (
    PROJECT_ENRICHMENT_COLUMNS,
    PROJECT_JSON_COLUMNS,
    PROJECT_ROW_COLUMNS,
    ProjectQueryUnavailable,
)

_FLOAT_COLUMNS = ("iou_dedup_threshold", "box_threshold", "text_threshold")
_UUID_COLUMNS = ("id", "organization_id", "owner_id", "ml_backend_pool_id")
_DATE_TEXT = re.compile(r"^[0-9]{4,7}-[0-9]{2}-[0-9]{2}(?: BC)?$")
_TIMESTAMP_TEXT = re.compile(
    r"^[0-9]{4,6}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00(?::00)?)(?: BC)?$"
)


def _date_value(value):
    if value is None:
        return None
    if value in ("infinity", "-infinity"):
        return date.max if value == "infinity" else date.min
    if type(value) is not str or _DATE_TEXT.fullmatch(value) is None:
        raise ProjectQueryUnavailable()
    # A finite out-of-range value fails during row decoding, before foreign reads.
    if value.endswith(" BC"):
        raise ValueError("ordinal must be >= 1")
    year = int(value.split("-", 1)[0])
    if year > 9999:
        raise ValueError(f"year {year} is out of range")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ProjectQueryUnavailable() from None


def _timestamp_value(value):
    if value is None:
        return None
    if value in ("infinity", "-infinity"):
        # asyncpg's timestamp extrema are deliberately naive.
        return datetime.max if value == "infinity" else datetime.min
    if type(value) is not str or _TIMESTAMP_TEXT.fullmatch(value) is None:
        raise ProjectQueryUnavailable()
    if value.endswith(" BC") or int(value.split("-", 1)[0]) > 9999:
        raise OverflowError("date value out of range")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("Timestamp is missing its UTC offset")
        return parsed.astimezone(UTC)
    except ValueError:
        raise ProjectQueryUnavailable() from None


def raw_project_row(value, *, target_id=None):
    """Validate the fixed transport shape without applying public ProjectOut rules."""
    if type(value) is not dict or set(value) != set(PROJECT_ROW_COLUMNS):
        raise ProjectQueryUnavailable()
    for field in _UUID_COLUMNS:
        if value[field] is None and field != "id":
            continue
        if type(value[field]) is not str:
            raise ProjectQueryUnavailable()
        try:
            uuid.UUID(value[field])
        except ValueError:
            raise ProjectQueryUnavailable() from None
    if target_id is not None and uuid.UUID(value["id"]) != target_id:
        raise ProjectQueryUnavailable()
    for field in PROJECT_JSON_COLUMNS:
        if value[field] is not None:
            if type(value[field]) is not str:
                raise ProjectQueryUnavailable()
            # Match the driver parser, retaining original text for lossless forwarding.
            _json_column(value[field])
    for field in _FLOAT_COLUMNS:
        if value[field] is not None:
            if type(value[field]) is not str:
                raise ProjectQueryUnavailable()
            try:
                float(value[field])
            except ValueError:
                raise ProjectQueryUnavailable() from None
    _date_value(value["due_date"])
    _timestamp_value(value["created_at"])
    _timestamp_value(value["updated_at"])
    return value


def decode_project_row(row_json, *, target_id=None):
    if row_json is None:
        return None
    try:
        value = json.loads(
            row_json, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, UnicodeError):
        raise ProjectQueryUnavailable() from None
    return raw_project_row(value, target_id=target_id)


def decode_project_rows(rows_json):
    try:
        rows = json.loads(
            rows_json, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, UnicodeError):
        raise ProjectQueryUnavailable() from None
    if type(rows) is not list:
        raise ProjectQueryUnavailable()
    return [raw_project_row(row) for row in rows]


def project_json_value(row, field):
    value = row[field]
    return _json_column(value) if value is not None else None


def _json_column(value):
    try:
        return json.loads(
            value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError:
        raise ProjectQueryUnavailable() from None


def materialize_project_row(row, *, with_my_role=False):
    expected = set(PROJECT_ROW_COLUMNS) | set(PROJECT_ENRICHMENT_COLUMNS)
    if with_my_role:
        expected.add("my_project_role")
    if type(row) is not dict or set(row) != expected:
        raise ProjectQueryUnavailable()
    data = dict(row)
    raw_project_row({key: data[key] for key in PROJECT_ROW_COLUMNS})
    for field in PROJECT_JSON_COLUMNS:
        data[field] = project_json_value(row, field)
    for field in _FLOAT_COLUMNS:
        data[field] = float(data[field]) if data[field] is not None else None
    for field in (*_UUID_COLUMNS, "ml_backend_id"):
        data[field] = uuid.UUID(data[field]) if data[field] is not None else None
    data["due_date"] = _date_value(data["due_date"])
    for field in ("created_at", "updated_at"):
        data[field] = _timestamp_value(data[field])
    for field in ("member_count", "ai_completed_tasks"):
        if (
            type(data[field]) is not str
            or re.fullmatch(r"0|[1-9][0-9]*", data[field]) is None
        ):
            raise ProjectQueryUnavailable()
        data[field] = int(data[field])
    return data


_STATS_COUNTS = (
    "total_data",
    "completed",
    "pending_review",
    "total_annotations",
    "ai_derived_annotations",
)
_STATS_SERIES = ("total_data_series", "completed_series", "pending_review_series")
_READINESS_COUNTS = (
    "linked_dataset_count",
    "dataset_item_count",
    "task_count",
    "linked_task_count",
    "task_creation_active_jobs",
    "task_creation_failed_jobs",
    "batch_count",
    "nonempty_batch_count",
    "executable_batch_count",
    "assigned_batch_count",
    "active_annotator_count",
    "active_reviewer_count",
    "activated_batch_count",
)


def _integer(value, *, signed=False):
    pattern = r"0|-?[1-9][0-9]*" if signed else r"0|[1-9][0-9]*"
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise ProjectQueryUnavailable()
    try:
        return int(value)
    except ValueError:
        raise ProjectQueryUnavailable() from None


def _rate(value):
    if type(value) not in (int, float):
        raise ProjectQueryUnavailable()
    try:
        if not math.isfinite(value):
            raise ProjectQueryUnavailable()
    except OverflowError:
        raise ProjectQueryUnavailable() from None


def _response_row(value, *, target_id=None, with_my_role=False):
    expected = set(PROJECT_ROW_COLUMNS) | set(PROJECT_ENRICHMENT_COLUMNS)
    if with_my_role:
        expected.add("my_project_role")
    if type(value) is not dict or set(value) != expected:
        raise ProjectQueryUnavailable()
    raw_project_row(
        {key: value[key] for key in PROJECT_ROW_COLUMNS}, target_id=target_id
    )
    for field in (
        "owner_name",
        "owner_avatar_ref",
        *(("my_project_role",) if with_my_role else ()),
    ):
        if value[field] is not None and type(value[field]) is not str:
            raise ProjectQueryUnavailable()
    if value["ml_backend_id"] is not None:
        if type(value["ml_backend_id"]) is not str:
            raise ProjectQueryUnavailable()
        try:
            uuid.UUID(value["ml_backend_id"])
        except ValueError:
            raise ProjectQueryUnavailable() from None
    for field in ("member_count", "ai_completed_tasks"):
        _integer(value[field])


def validate_project_query_data(request, data_json):
    try:
        value = json.loads(
            data_json, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectQueryUnavailable() from None
    operation = request.operation
    if operation in ("list_projects", "query_projects"):
        if operation == "query_projects":
            if type(value) is not dict or set(value) != {
                "items",
                "total",
                "page",
                "page_size",
                "pages",
            }:
                raise ProjectQueryUnavailable()
            for field in ("total", "pages"):
                _integer(value[field])
            if (
                value["page"] != request.pagination.page
                or type(value["page_size"]) is not int
                or value["page_size"] != request.pagination.page_size
            ):
                raise ProjectQueryUnavailable()
            rows = value["items"]
            if type(rows) is not list or len(rows) > request.pagination.page_size:
                raise ProjectQueryUnavailable()
        else:
            rows = value
        if type(rows) is not list:
            raise ProjectQueryUnavailable()
        for row in rows:
            _response_row(row, with_my_role=True)
    elif operation in ("get_project", "get_project_projection"):
        _response_row(
            value, target_id=request.target_id, with_my_role=operation == "get_project"
        )
    elif operation == "project_stats":
        if type(value) is not dict or set(value) != set(_STATS_COUNTS) | set(
            _STATS_SERIES
        ) | {"ai_rate", "ai_rate_series"}:
            raise ProjectQueryUnavailable()
        for field in _STATS_COUNTS:
            _integer(
                value[field],
                signed=field in ("total_data", "completed", "pending_review"),
            )
        _rate(value["ai_rate"])
        for field in (*_STATS_SERIES, "ai_rate_series"):
            if type(value[field]) is not list or len(value[field]) != 12:
                raise ProjectQueryUnavailable()
            for item in value[field]:
                _rate(item) if field == "ai_rate_series" else _integer(item)
    elif operation == "project_readiness":
        expected = set(_READINESS_COUNTS) | {
            "project_id",
            "guide_ready",
            "classes_ready",
            "latest_task_creation_status",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or value["project_id"] != str(request.target_id)
        ):
            raise ProjectQueryUnavailable()
        if any(
            type(value[field]) is not bool for field in ("guide_ready", "classes_ready")
        ):
            raise ProjectQueryUnavailable()
        if (
            value["latest_task_creation_status"] is not None
            and type(value["latest_task_creation_status"]) is not str
        ):
            raise ProjectQueryUnavailable()
        for field in _READINESS_COUNTS:
            _integer(value[field])
    elif operation == "search_projects":
        from .project_query_contract import decode_project_query_filters

        if (
            type(value) is not list
            or len(value) > decode_project_query_filters(request).limit
        ):
            raise ProjectQueryUnavailable()
        for item in value:
            if type(item) is not dict or set(item) != {
                "id",
                "display_id",
                "name",
                "type_key",
                "data_type",
                "type_label",
            }:
                raise ProjectQueryUnavailable()
            if type(item["id"]) is not str:
                raise ProjectQueryUnavailable()
            if any(
                type(item[field]) is not str
                for field in (
                    "display_id",
                    "name",
                    "type_key",
                    "data_type",
                    "type_label",
                )
            ):
                raise ProjectQueryUnavailable()
            try:
                uuid.UUID(item["id"])
            except ValueError:
                raise ProjectQueryUnavailable() from None
    else:
        raise ProjectQueryUnavailable()
    return value


def materialize_project_query_data(reply):
    """Restore public Python scalars after the selected owner's successful reply."""
    from .project_query_contract import ProjectQuerySuccess, decode_project_query_reply

    reply = decode_project_query_reply(reply.request, reply)
    if not isinstance(reply, ProjectQuerySuccess):
        raise ProjectQueryUnavailable()
    request = reply.request
    value = validate_project_query_data(request, reply.data_json)
    if request.operation == "list_projects":
        return [materialize_project_row(row, with_my_role=True) for row in value]
    if request.operation == "query_projects":
        return {
            **value,
            "items": [
                materialize_project_row(row, with_my_role=True)
                for row in value["items"]
            ],
            **{key: int(value[key]) for key in ("page", "total", "pages")},
        }
    if request.operation in ("get_project", "get_project_projection"):
        return (
            materialize_project_row(
                value, with_my_role=request.operation == "get_project"
            )
            if value is not None
            else None
        )
    if request.operation == "project_stats":
        return {
            **value,
            **{key: int(value[key]) for key in _STATS_COUNTS},
            **{key: [int(item) for item in value[key]] for key in _STATS_SERIES},
        }
    if request.operation == "project_readiness":
        return {
            **value,
            "project_id": uuid.UUID(value["project_id"]),
            **{key: int(value[key]) for key in _READINESS_COUNTS},
        }
    return value
