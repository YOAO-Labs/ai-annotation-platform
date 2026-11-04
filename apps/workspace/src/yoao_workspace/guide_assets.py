"""Fixed JSON handoff helpers for Workspace guide publication."""

from __future__ import annotations

import json
from typing import Any


def guide_assets_ascii(value: Any) -> str:
    """Serialize the staged full guide_assets list for the fixed Native action."""
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
