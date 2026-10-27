"""Bind manual prediction candidate decisions to the request's SQL transaction."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.annotation import AnnotationOut
from app.services.annotation import AnnotationService
from app.services.content_snapshot import ContentError
from app.services.candidate_decision_contract import (
    CandidateAcceptCommand,
    CandidateDecisionError,
    CandidateDecisionPort,
    CandidateRejectCommand,
)
from app.services.labeling_write_scope import LabelingWriteScope
from app.services.raster_mask_storage import RasterMaskContractError
from app.services.video_collaboration import (
    assert_video_annotation_write_scope,
    heartbeat_task_lock_for_legacy_video,
)
from app.services.identity_contracts import IdentityActorFacts


class _CandidateDecisionSqlAdapter(CandidateDecisionPort):
    """Keep ORM rows and commit details inside one bound request transaction."""

    def __init__(
        self, db: AsyncSession, *, task: Task, actor: User | IdentityActorFacts
    ) -> None:
        self._db = db
        self._task = task
        self._actor = actor
        self._annotations = AnnotationService(db)

    def _assert_bound(self, write_scope: LabelingWriteScope, *, operation: str) -> None:
        if (
            write_scope.task_id != self._task.id
            or write_scope.project_id != self._task.project_id
            or write_scope.actor_id != self._actor.id
            or write_scope.operation != operation
        ):
            raise ValueError(
                "candidate decision command identity does not match binding"
            )

    async def accept(
        self, command: CandidateAcceptCommand
    ) -> list[AnnotationOut] | None:
        self._assert_bound(command.write_scope, operation="candidate_accept")
        try:
            annotations = await self._annotations._accept_prediction(
                command.prediction_id,
                shape_index=command.shape_index,
                override_class_name=command.override_class_name,
                attribute_overrides=command.attribute_overrides,
                video_segment_id=command.video_segment_id,
                write_scope=command.write_scope,
                reference_receipt=command.reference_receipt,
                repair_original_receipt=command.repair_original_receipt,
                video_acceptance_receipt=command.video_acceptance_receipt,
            )
            if annotations is None:
                return None
            for annotation in annotations:
                await assert_video_annotation_write_scope(
                    self._db,
                    task=self._task,
                    user=self._actor,
                    segment_id=annotation.video_segment_id,
                    geometry=annotation.geometry,
                )
            await heartbeat_task_lock_for_legacy_video(
                self._db, self._task, self._actor.id
            )
            if command.repair_original_receipt is not None:
                from app.services.managed_mask_repair import recheck_credentials

                await recheck_credentials(self._db, command.repair_original_receipt)
            if command.video_acceptance_receipt is not None:
                from app.services.video_prediction_acceptance import (
                    recheck_credentials,
                )

                await recheck_credentials(self._db, command.video_acceptance_receipt)
            await self._db.commit()
            return [
                AnnotationOut.model_validate(annotation) for annotation in annotations
            ]
        except RasterMaskContractError as exc:
            raise CandidateDecisionError(
                status_code=exc.status_code, detail=exc.detail
            ) from exc
        except ContentError as exc:
            from app.services.dataset_content import content_http_error

            error = content_http_error(exc)
            raise CandidateDecisionError(
                status_code=error.status_code, detail=error.detail
            ) from None
        except HTTPException as exc:
            raise CandidateDecisionError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc

    async def reject(self, command: CandidateRejectCommand) -> None:
        self._assert_bound(command.write_scope, operation="candidate_reject")
        try:
            await self._annotations._reject_prediction(
                command.prediction_id,
                shape_index=command.shape_index,
                write_scope=command.write_scope,
            )
            if command.video_rejection_receipt is not None:
                from app.services.video_prediction_acceptance import (
                    recheck_credentials,
                )

                await recheck_credentials(self._db, command.video_rejection_receipt)
            await self._db.commit()
        except HTTPException as exc:
            raise CandidateDecisionError(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc


def bind_candidate_decision_port(
    db: AsyncSession, *, task: Task, actor: User | IdentityActorFacts
) -> CandidateDecisionPort:
    """Bind candidate decisions without crossing the API boundary with ORM rows."""
    return _CandidateDecisionSqlAdapter(db, task=task, actor=actor)
