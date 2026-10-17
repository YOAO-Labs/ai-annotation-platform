"""Shared contracts for annotation slice commits and restores."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from app.schemas.annotation_slice import (
    AnnotationSliceResponse,
    AnnotationSliceRestoreRequest,
    PolygonSliceCommitRequest,
)
from app.services.audit_origin_contract import AuditOriginContext

SLICE_RESTORE_TTL = timedelta(days=30)


class AnnotationSliceError(ValueError):
    def __init__(self, status_code: int, reason: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message}


def annotation_slice_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class AnnotationSlicePort(Protocol):
    """Execute slice requests inside the caller's transaction."""

    async def commit_polygon(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        payload: PolygonSliceCommitRequest,
        audit_origin: AuditOriginContext,
    ) -> AnnotationSliceResponse: ...

    async def restore(
        self,
        *,
        task_id: UUID,
        operation_id: UUID,
        actor_id: UUID,
        payload: AnnotationSliceRestoreRequest,
        audit_origin: AuditOriginContext,
    ) -> AnnotationSliceResponse: ...
