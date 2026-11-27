from __future__ import annotations

from typing import Any


class TaskWorkflowError(Exception):
    """A task-workflow failure with the public HTTP status and detail preserved."""

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))
