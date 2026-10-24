"""Jobs-owned policy for caller-managed asynchronous-job lifecycle writes."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from uuid import UUID

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"


class LifecycleData(Protocol):
    """Small adapter over the caller's row, identity map, and unit of work."""

    def changes(
        self,
        row: Any,
        operation: str,
        arguments: Mapping[str, Any],
    ) -> AbstractContextManager[Any]:
        """Yield a transparent row proxy and retain its observed change trace."""
        ...

    async def create_job(
        self,
        *,
        kind: str,
        user_id: UUID | None,
        project_id: UUID | None,
        payload: Any,
        celery_task_id: str | None,
        status: str,
    ) -> Any:
        """Construct, add, and fully flush the caller's ORM row."""
        ...

    async def get_job(self, job_id: UUID) -> Any | None:
        """Use the caller's existing identity-map-aware get behavior."""
        ...

    async def flush(self) -> None:
        """Flush through the caller's existing unit of work."""
        ...


class JobsLifecycleManager:
    """Apply the original Python lifecycle rules to a borrowed row snapshot.

    The adapter records only attributes observed and assigned by each operation.
    These methods neither load rows nor commit; API facades retain lookup and
    transaction ownership.
    """

    def __init__(
        self,
        data: LifecycleData,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._data = data
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def create_job(
        self,
        *,
        kind: str,
        user_id: UUID | None = None,
        project_id: UUID | None = None,
        payload: dict | None = None,
        celery_task_id: str | None = None,
    ) -> Any:
        """Create through the caller's ORM view and preserve its full flush."""
        return await self._data.create_job(
            kind=kind,
            user_id=user_id,
            project_id=project_id,
            payload=payload or {},
            celery_task_id=celery_task_id,
            status=PENDING,
        )

    def mark_running(self, row: Any, *, celery_task_id: str | None = None) -> None:
        """Move cached pending rows to running and fill an empty execution ID."""
        arguments = {"celery_task_id": celery_task_id}
        with self._data.changes(row, "mark_running", arguments) as job:
            if job.status == PENDING:
                job.status = RUNNING
                job.started_at = self._clock()
            if celery_task_id and not job.celery_task_id:
                job.celery_task_id = celery_task_id

    def bind_execution_id(self, row: Any, execution_id: str | None) -> None:
        """Set or clear the execution ID without a status guard."""
        arguments = {"execution_id": execution_id}
        with self._data.changes(row, "bind_execution_id", arguments) as job:
            job.celery_task_id = execution_id

    @staticmethod
    def _merge_payload(job: Any, updates: dict) -> None:
        job.payload = {**(job.payload or {}), **updates}

    def update_payload(self, row: Any, updates: dict) -> None:
        """Shallow-merge caller values into the existing JSON payload."""
        arguments = {"updates": updates}
        with self._data.changes(row, "update_payload", arguments) as job:
            self._merge_payload(job, updates)

    def update_progress(
        self,
        row: Any,
        pct: int,
        *,
        extra_payload: dict | None = None,
        result: Any = None,
    ) -> None:
        """Clamp progress while preserving the original partial-write order."""
        arguments = {"pct": pct, "extra_payload": extra_payload, "result": result}
        with self._data.changes(row, "update_progress", arguments) as job:
            if result is not None:
                job.result = result
            job.progress_pct = max(0, min(100, int(pct)))
            if extra_payload:
                self._merge_payload(job, extra_payload)

    def replace_result(self, row: Any, *, result: Any) -> None:
        """Replace the native JSON result, including scalar and null values."""
        arguments = {"result": result}
        with self._data.changes(row, "replace_result", arguments) as job:
            job.result = result

    def mark_complete(self, row: Any, *, result: Any = None) -> None:
        """Complete unless the cached row is already cancelled or failed."""
        arguments = {"result": result}
        with self._data.changes(row, "mark_complete", arguments) as job:
            if job.status in {CANCELLED, FAILED}:
                return
            job.status = COMPLETED
            job.progress_pct = 100
            job.completed_at = self._clock()
            if result is not None:
                job.result = result

    def mark_failed(self, row: Any, *, error: str, result: Any = None) -> None:
        """Fail unless the cached row is already completed or cancelled."""
        arguments = {"error": error, "result": result}
        with self._data.changes(row, "mark_failed", arguments) as job:
            if job.status in {COMPLETED, CANCELLED}:
                return
            job.status = FAILED
            job.completed_at = self._clock()
            job.error_message = (error or "")[:4000]
            if result is not None:
                job.result = result

    def mark_cancelled(
        self,
        row: Any,
        *,
        result: Any = None,
        allowed_statuses: Collection[str] | None = (PENDING, RUNNING),
        progress_pct: int | None = None,
    ) -> Any:
        """Cancel when allowed by the caller's status policy; return the row."""
        arguments = {
            "result": result,
            "allowed_statuses": allowed_statuses,
            "progress_pct": progress_pct,
        }
        with self._data.changes(row, "mark_cancelled", arguments) as job:
            if allowed_statuses is not None and job.status not in allowed_statuses:
                return row
            job.status = CANCELLED
            job.completed_at = self._clock()
            if progress_pct is not None:
                job.progress_pct = max(0, min(100, int(progress_pct)))
            if result is not None:
                job.result = result
        return row

    def request_cancel(self, row: Any) -> None:
        """Set a cooperative-cancel marker only on cached live rows."""
        with self._data.changes(row, "request_cancel", {}) as job:
            if job.status not in {PENDING, RUNNING}:
                return
            job.payload = {
                **(job.payload or {}),
                "cancel_requested": True,
                "cancel_requested_at": self._clock().isoformat(),
            }

    @contextlib.asynccontextmanager
    async def track_job(
        self,
        *,
        kind: str,
        user_id: UUID | None = None,
        project_id: UUID | None = None,
        payload: dict | None = None,
        celery_task_id: str | None = None,
    ) -> AsyncIterator[Any]:
        """Retain create/run/flush/yield/terminal-flush ordering in one UOW."""
        row = await self.create_job(
            kind=kind,
            user_id=user_id,
            project_id=project_id,
            payload=payload,
            celery_task_id=celery_task_id,
        )
        running_row = await self._data.get_job(row.id)
        if running_row is not None:
            self.mark_running(running_row)
        await self._data.flush()
        try:
            yield row
        except Exception as error:
            error_message = str(error)
            failed_row = await self._data.get_job(row.id)
            if failed_row is not None:
                self.mark_failed(failed_row, error=error_message)
            await self._data.flush()
            raise
        else:
            completed_row = await self._data.get_job(row.id)
            if completed_row is not None:
                self.mark_complete(completed_row)
            await self._data.flush()
