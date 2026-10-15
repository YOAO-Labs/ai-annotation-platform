"""Prepared auxiliary asset source and publication boundary."""

from .sources import (
    AuxiliaryError,
    FrozenInputs,
    FrozenItem,
    FrozenCamera,
    resolve_task_inputs,
)
from .publication import (
    AUXILIARY_PREFIX,
    assert_enabled,
    fail_generation,
    find_asset,
    generation_prefix,
    get_ready_generation,
    publish_auxiliary,
    reserve_generation,
)

__all__ = [
    "AUXILIARY_PREFIX",
    "AuxiliaryError",
    "FrozenInputs",
    "FrozenItem",
    "FrozenCamera",
    "assert_enabled",
    "fail_generation",
    "find_asset",
    "generation_prefix",
    "publish_auxiliary",
    "reserve_generation",
    "resolve_task_inputs",
    "get_ready_generation",
]
