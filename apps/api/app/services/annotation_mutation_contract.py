"""Persistence-free requests for task-scoped manual annotation mutations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID

from app.schemas.annotation import AnnotationOut, AnnotationUpdate
from app.services.audit_origin_contract import AuditOriginContext


@dataclass(frozen=True, slots=True)
class AnnotationUpdateCommand:
    task_id: UUID
    annotation_id: UUID
    actor_id: UUID
    data: AnnotationUpdate
    if_match: str | None
    audit_origin: AuditOriginContext


@dataclass(frozen=True, slots=True)
class AnnotationDeleteCommand:
    task_id: UUID
    annotation_id: UUID
    actor_id: UUID
    audit_origin: AuditOriginContext


class AnnotationMutationError(Exception):
    """Expected HTTP rejection facts returned to the transport boundary."""

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


class AnnotationMutationPort(Protocol):
    async def update(self, command: AnnotationUpdateCommand) -> AnnotationOut: ...

    async def delete(self, command: AnnotationDeleteCommand) -> bool:
        """Return the existing deletion success receipt; commit on success."""
        ...
