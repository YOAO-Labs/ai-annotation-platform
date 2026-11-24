"""Language-neutral preference policy loaded from its packaged JSON contract."""

from __future__ import annotations

import json
import re
from importlib.resources import files

_POLICY = json.loads(
    files(__package__).joinpath("preferences.policy.json").read_text(encoding="utf-8")
)
_types = _POLICY["known_notification_types"]
_toast_types = _POLICY["default_toast_types"]
_reserved_channel_ids = _POLICY["reserved_channel_ids"]
_external_default = _POLICY["external_channel_default_enabled"]
_channel_pattern = _POLICY["external_channel_id_pattern"]
_external_deliverable = _POLICY["external_deliverable_types"]

if (
    type(_types) is not list
    or any(type(value) is not str for value in _types)
    or len(_types) != len(set(_types))
    or type(_toast_types) is not list
    or any(type(value) is not str for value in _toast_types)
    or not set(_toast_types).issubset(_types)
    or len(_toast_types) != len(set(_toast_types))
    or type(_reserved_channel_ids) is not list
    or any(type(value) is not str for value in _reserved_channel_ids)
    or len(_reserved_channel_ids) != len(set(_reserved_channel_ids))
    or type(_external_default) is not bool
    or type(_channel_pattern) is not str
    or type(_external_deliverable) is not list
    or any(type(value) is not str for value in _external_deliverable)
    or not set(_external_deliverable).issubset(_types)
    or len(_external_deliverable) != len(set(_external_deliverable))
):
    raise RuntimeError("Invalid notification preference policy")

_CHANNEL_RE = re.compile(_channel_pattern)

KNOWN_NOTIFICATION_TYPES: tuple[str, ...] = tuple(_types)
DEFAULT_TOAST_TYPES: frozenset[str] = frozenset(_toast_types)
RESERVED_CHANNEL_IDS: frozenset[str] = frozenset(_reserved_channel_ids)
EXTERNAL_CHANNEL_DEFAULT_ENABLED: bool = _external_default
EXTERNAL_CHANNEL_ID_PATTERN: str = _channel_pattern
EXTERNAL_DELIVERABLE_TYPES: frozenset[str] = frozenset(_external_deliverable)


def default_toast_for(notification_type: str) -> bool:
    return notification_type in DEFAULT_TOAST_TYPES


def is_external_deliverable(notification_type: str) -> bool:
    return notification_type in EXTERNAL_DELIVERABLE_TYPES


def is_valid_external_channel_id(channel_id: str) -> bool:
    return (
        type(channel_id) is str
        and channel_id not in RESERVED_CHANNEL_IDS
        and bool(_CHANNEL_RE.fullmatch(channel_id))
    )
