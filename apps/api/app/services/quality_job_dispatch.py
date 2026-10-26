"""Execution port for quality runs and mask repair/import operations.

Domain services own eligibility, durable state and dispatch-failure handling.
The supplied dispatcher owns transport, task names and queue selection.
"""

from __future__ import annotations

import uuid
from typing import Literal, Protocol

QualityRunKind = Literal["mask_qc", "point_cloud_quality", "video_track_quality"]


class QualityJobDispatcher(Protocol):
    def dispatch_run(
        self, kind: QualityRunKind, run_id: uuid.UUID, *, execution_id: str
    ) -> None:
        """Queue a quality run with its already-persisted execution identity."""
        ...

    def dispatch_mask_repair(
        self, batch_id: uuid.UUID, *, rollback: bool, execution_id: str
    ) -> str:
        """Queue repair or rollback with its persisted execution identity."""
        ...

    def dispatch_mask_import(self, batch_id: uuid.UUID, *, execution_id: str) -> str:
        """Queue a staged format import with its persisted execution identity."""
        ...
