from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from types import MappingProxyType


def freeze_json(value: Any) -> Any:
    """Copy JSON-shaped inference data into read-only mappings and tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Copy immutable inference facts into the mutable values ORM JSON fields expect."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SecondaryInferenceResult:
    write_target: Literal["attributes", "geometry"]
    model_ref: tuple[tuple[str, str | None], ...]
    attributes: tuple[tuple[str, Any], ...]
    geometry_shapes: tuple[Any, ...]
