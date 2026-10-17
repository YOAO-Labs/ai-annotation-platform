"""Session-free contract for cancelling one training-dataset build."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID


class TrainingDatasetCancellationFailure(Exception):
    """Expected rejection facts preserved at the HTTP composition boundary."""

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


class TrainingDatasetCancellationPort(Protocol):
    """Cancel within the caller's transaction using only owner identifiers."""

    async def request_cancel(
        self,
        *,
        job_id: UUID,
        project_id: UUID | None,
        actor_id: UUID,
    ) -> None: ...
