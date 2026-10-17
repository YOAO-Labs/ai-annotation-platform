"""Port for async-job lifecycle operations applied by the quality workflow."""

from __future__ import annotations

import uuid
from typing import Any, Protocol


class QualityJobLifecycle(Protocol):
    async def mark_running(
        self, job_id: uuid.UUID, *, celery_task_id: str | None
    ) -> None: ...

    async def update_progress(self, job_id: uuid.UUID, progress_pct: int) -> None: ...

    async def mark_complete(
        self, job_id: uuid.UUID, *, result: dict[str, Any] | None
    ) -> None: ...

    async def mark_failed(self, job_id: uuid.UUID, *, error: str) -> None: ...

    async def mark_cancelled(
        self, job_id: uuid.UUID, *, result: dict[str, Any] | None
    ) -> None: ...

    async def notify_terminal(self, *, job_id: uuid.UUID) -> None: ...
