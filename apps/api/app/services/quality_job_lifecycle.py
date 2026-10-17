"""Port for async-job lifecycle operations applied by the quality workflow."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class QualityJobLifecycle(Protocol):
    async def mark_running(
        self, db: AsyncSession, job_id: uuid.UUID, *, celery_task_id: str | None
    ) -> None: ...

    async def update_progress(
        self, db: AsyncSession, job_id: uuid.UUID, progress_pct: int
    ) -> None: ...

    async def mark_complete(
        self, db: AsyncSession, job_id: uuid.UUID, *, result: dict[str, Any] | None
    ) -> None: ...

    async def mark_failed(
        self, db: AsyncSession, job_id: uuid.UUID, *, error: str
    ) -> None: ...

    async def mark_cancelled(
        self, db: AsyncSession, job_id: uuid.UUID, *, result: dict[str, Any] | None
    ) -> None: ...

    async def notify_terminal(self, db: AsyncSession, *, job_id: uuid.UUID) -> None: ...
