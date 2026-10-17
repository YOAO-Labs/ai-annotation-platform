"""Persistence-free request and port for manual annotation creation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID

from app.schemas.annotation import AnnotationCreate, AnnotationOut
from app.services.audit_origin_contract import AuditOriginContext


@dataclass(frozen=True, slots=True)
class AnnotationCreateCommand:
    task_id: UUID
    actor_id: UUID
    data: AnnotationCreate
    idempotency_key: str | None
    audit_origin: AuditOriginContext


class AnnotationCreateError(Exception):
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


class AnnotationCreatePort(Protocol):
    async def create(self, command: AnnotationCreateCommand) -> AnnotationOut: ...
