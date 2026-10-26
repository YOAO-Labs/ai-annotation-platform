"""Execution port for video tracker jobs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class VideoTrackerJobDispatcher(Protocol):
    def dispatch_tracker_job(self, job_id: UUID, *, execution_id: str) -> str:
        """Queue a tracker job with its persisted execution identity."""
        ...
