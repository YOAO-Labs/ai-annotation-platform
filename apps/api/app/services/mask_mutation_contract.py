"""Persistence-free request and error contract for Mask mutations."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from app.schemas.mask_mutation import (
    MaskMutationCommitRequest,
    MaskMutationCommitResponse,
)
from app.services.audit_origin_contract import AuditOriginContext


class MaskMutationError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        **detail: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **detail}


class MaskMutationPort(Protocol):
    """Commit a typed Mask request within the caller's transaction."""

    async def commit(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        payload: MaskMutationCommitRequest,
        audit_origin: AuditOriginContext,
    ) -> MaskMutationCommitResponse: ...
