"""Persistence-free commands for manual prediction candidate decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID

from app.schemas.annotation import AnnotationOut
from app.services.labeling_write_scope import LabelingWriteScope


@dataclass(frozen=True, slots=True)
class CandidateAcceptCommand:
    write_scope: LabelingWriteScope
    prediction_id: UUID
    shape_index: int | None
    override_class_name: str | None
    attribute_overrides: dict | None
    video_segment_id: UUID | None
    reference_receipt: Any | None = None
    repair_original_receipt: Any | None = None
    video_acceptance_receipt: Any | None = None


@dataclass(frozen=True, slots=True)
class CandidateRejectCommand:
    write_scope: LabelingWriteScope
    prediction_id: UUID
    shape_index: int | None
    video_rejection_receipt: Any | None = None


class CandidateDecisionError(Exception):
    """Expected HTTP rejection facts projected by the transport boundary."""

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


class CandidateDecisionPort(Protocol):
    """Accept or reject candidates in the request's existing transaction."""

    async def accept(
        self, command: CandidateAcceptCommand
    ) -> list[AnnotationOut] | None:
        """Return newly created annotations, or None for the existing 404 path."""
        ...

    async def reject(self, command: CandidateRejectCommand) -> None:
        """Persist the selected rejection and commit the caller's transaction."""
        ...
