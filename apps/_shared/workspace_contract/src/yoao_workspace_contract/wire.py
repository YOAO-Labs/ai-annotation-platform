"""Strict JSON serialization and closed Pydantic wire helpers."""

from __future__ import annotations

import json
import math

from pydantic import BaseModel, ConfigDict


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _assert_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 128:
        raise ValueError("JSON value is too deeply nested")
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _assert_json_value(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _assert_json_value(child, depth=depth + 1)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    elif value is not None and type(value) not in (str, bool, int, float):
        raise ValueError("Invalid JSON value")


def _json_bytes(payload: dict | bytes | str | BaseModel) -> bytes:
    if isinstance(payload, bytes):
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    elif isinstance(payload, str):
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    elif isinstance(payload, BaseModel):
        value = payload.model_dump(mode="json", by_alias=True)
    else:
        value = payload
    _assert_json_value(value)
    if isinstance(value, dict) and "protocol" in value:
        raise ValueError("Use only the declared wire field schema")
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
