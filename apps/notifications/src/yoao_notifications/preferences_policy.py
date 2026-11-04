"""Language-neutral preference policy loaded from its packaged JSON contract."""

from __future__ import annotations

import json
from importlib.resources import files

_POLICY = json.loads(
    files(__package__).joinpath("preferences.policy.json").read_text(encoding="utf-8")
)
_types = _POLICY["known_notification_types"]
_toast_types = _POLICY["default_toast_types"]
if (
    type(_types) is not list
    or any(type(value) is not str for value in _types)
    or len(_types) != len(set(_types))
    or type(_toast_types) is not list
    or any(type(value) is not str for value in _toast_types)
    or not set(_toast_types).issubset(_types)
    or len(_toast_types) != len(set(_toast_types))
):
    raise RuntimeError("Invalid notification preference policy")

KNOWN_NOTIFICATION_TYPES: tuple[str, ...] = tuple(_types)
DEFAULT_TOAST_TYPES: frozenset[str] = frozenset(_toast_types)


def default_toast_for(notification_type: str) -> bool:
    return notification_type in DEFAULT_TOAST_TYPES
