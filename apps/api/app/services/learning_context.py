"""Pure structural protocols for non-Learning entities consumed by Learning services."""

from __future__ import annotations

import uuid
from typing import Any, Protocol


class LearningActorContext(Protocol):
    id: uuid.UUID


class LearningProjectContext(Protocol):
    id: uuid.UUID
    data_type: str | None
    status: str
    tool_bindings: dict[str, Any] | None
