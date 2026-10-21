"""Bind Mask and slice contracts to the caller's existing write transaction."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.schemas.annotation_slice import (
    AnnotationSliceResponse,
    AnnotationSliceRestoreRequest,
    PolygonSliceCommitRequest,
)
from app.schemas.mask_mutation import (
    MaskMutationCommitRequest,
    MaskMutationCommitResponse,
)
from app.services.annotation_slice import AnnotationSliceService
from app.services.annotation_slice_contracts import AnnotationSlicePort
from app.services.audit_origin_contract import AuditOriginContext
from app.services.mask_mutation import MaskMutationService
from app.services.mask_mutation_contract import MaskMutationPort
from aap_workspace_contract.access import ProjectAccess
from app.services.identity_contracts import IdentityActorFacts


class _MaskMutationSqlAdapter(MaskMutationPort):
    def __init__(
        self,
        db: AsyncSession,
        *,
        task_id: UUID,
        actor: User | IdentityActorFacts,
        access: ProjectAccess | None,
    ) -> None:
        self._task_id = task_id
        self._actor = actor
        self._access = access
        self._service = MaskMutationService(db)

    async def commit(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        payload: MaskMutationCommitRequest,
        audit_origin: AuditOriginContext,
    ) -> MaskMutationCommitResponse:
        if (
            task_id != self._task_id
            or actor_id != self._actor.id
            or (self._access is not None and self._access.user_id != actor_id)
        ):
            raise ValueError("Mask mutation command identity does not match binding")
        return await self._service._commit(
            task_id,
            payload,
            self._actor,
            origin=audit_origin,
            access=self._access,
        )


class _AnnotationSliceSqlAdapter(AnnotationSlicePort):
    def __init__(
        self,
        db: AsyncSession,
        *,
        task_id: UUID,
        actor: User | IdentityActorFacts,
        access: ProjectAccess | None,
    ) -> None:
        self._task_id = task_id
        self._actor = actor
        self._access = access
        self._service = AnnotationSliceService(db)

    def _assert_bound(self, task_id: UUID, actor_id: UUID) -> None:
        if (
            task_id != self._task_id
            or actor_id != self._actor.id
            or (self._access is not None and self._access.user_id != actor_id)
        ):
            raise ValueError("Annotation slice command identity does not match binding")

    async def commit_polygon(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        payload: PolygonSliceCommitRequest,
        audit_origin: AuditOriginContext,
    ) -> AnnotationSliceResponse:
        self._assert_bound(task_id, actor_id)
        return await self._service._commit(
            task_id,
            payload,
            self._actor,
            origin=audit_origin,
            access=self._access,
        )

    async def restore(
        self,
        *,
        task_id: UUID,
        operation_id: UUID,
        actor_id: UUID,
        payload: AnnotationSliceRestoreRequest,
        audit_origin: AuditOriginContext,
    ) -> AnnotationSliceResponse:
        self._assert_bound(task_id, actor_id)
        return await self._service._restore(
            task_id,
            operation_id,
            payload,
            self._actor,
            origin=audit_origin,
            access=self._access,
        )
