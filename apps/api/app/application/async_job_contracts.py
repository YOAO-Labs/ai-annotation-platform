"""Session-free caller contracts for asynchronous job access and operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from app.schemas.async_job import (
    AsyncJobListResponse,
    AsyncJobOut,
    AsyncJobRetryFailedResponse,
)


class AsyncJobOperationFailure(Exception):
    """Expected async-job rejection facts restored by the HTTP boundary."""

    def __init__(
        self,
        status_code: int,
        detail: object,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class AsyncJobReadPort(Protocol):
    """Read async jobs without exposing SQL rows or authenticated Users."""

    async def list_jobs(
        self,
        *,
        actor_id: UUID,
        status: list[str] | None,
        kind: list[str] | None,
        project_id: UUID | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> AsyncJobListResponse: ...

    async def get_job(self, *, job_id: UUID, actor_id: UUID) -> AsyncJobOut | None: ...


class JobCancellationPort(Protocol):
    """Cancel one job using the caller-owned transaction and execution control."""

    async def cancel(self, *, job_id: UUID, actor_id: UUID) -> str: ...


class PredictionRetryPort(Protocol):
    """Admit and dispatch existing failed-prediction retry operations."""

    async def retry_prediction(self, *, failed_id: UUID, actor_id: UUID) -> None: ...

    async def retry_job_items(
        self, *, job_id: UUID, actor_id: UUID
    ) -> AsyncJobRetryFailedResponse: ...
