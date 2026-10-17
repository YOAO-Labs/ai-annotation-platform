"""Value contracts for manual cross-frame annotation operations."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.schemas.annotation import (
    InterpolateRangeRequest,
    InterpolateRangeResponse,
    PropagateBatchRequest,
    PropagateBatchResponse,
    PropagateRequest,
    PropagateResponse,
)
from app.services.audit_origin_contract import AuditOriginContext


class AnnotationCrossFramePort(Protocol):
    """Execute a manual operation with its existing audit and commit boundary."""

    async def propagate(
        self,
        *,
        source_task_id: UUID,
        annotation_id: UUID,
        actor_id: UUID,
        payload: PropagateRequest,
        audit_origin: AuditOriginContext,
    ) -> PropagateResponse: ...

    async def propagate_batch(
        self,
        *,
        source_task_id: UUID,
        actor_id: UUID,
        payload: PropagateBatchRequest,
        audit_origin: AuditOriginContext,
    ) -> PropagateBatchResponse: ...

    async def interpolate_range(
        self,
        *,
        from_task_id: UUID,
        actor_id: UUID,
        payload: InterpolateRangeRequest,
        audit_origin: AuditOriginContext,
    ) -> InterpolateRangeResponse: ...


class CrossFrameBatchStager(Protocol):
    """Stage one worker target; the caller owns progress and commit."""

    async def stage_batch(
        self,
        *,
        source_task_id: UUID,
        target_task_id: UUID,
        annotation_ids: list[UUID],
        actor_id: UUID,
    ) -> int: ...
