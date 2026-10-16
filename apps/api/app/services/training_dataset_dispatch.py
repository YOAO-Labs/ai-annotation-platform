"""Execution port for training dataset jobs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class TrainingDatasetDispatcher(Protocol):
    async def dispatch_dataset(
        self,
        version_id: UUID,
        job_id: UUID,
        attempt_id: UUID,
        *,
        execution_id: str,
    ) -> None:
        """Queue a dataset execution with its persisted identity."""
        ...
