"""Bind compact-video bbox commands to the caller's API transaction."""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_labeling.video_bbox_editing_contract import (
    VideoBBoxEditingCommand,
    VideoBBoxEditingRejected,
    VideoBBoxEditingUnavailable,
)
from aap_labeling.video_bbox_editing_data import VideoBBoxEditingDataClient
from aap_labeling.video_bbox_editing_policy import (
    VideoBBoxAnnotationFacts,
    VideoBBoxTaskFacts,
    compose_video_tracks_command,
    convert_video_track_to_bboxes_command,
)
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.annotation_evidence import record_annotation_actor
from app.services.labeling_annotation_statistics import update_annotation_statistics
from app.services.labeling_core_read_relations import annotation_row


def _native_error_fields(error: BaseException):
    pending = [error]
    seen: set[int] = set()
    fallback = (None, None, None)
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostic = getattr(current, "diag", None)
        message = getattr(current, "message", None) or getattr(
            diagnostic, "message_primary", None
        )
        detail = getattr(current, "detail", None) or getattr(
            diagnostic, "message_detail", None
        )
        sqlstate = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if message == "aap_video_bbox_editing_error":
            return message, detail, sqlstate
        if sqlstate in {"55P03", "40P01", "40001"}:
            return message, detail, sqlstate
        if message is not None or sqlstate is not None:
            fallback = (message, detail, sqlstate)
        for name in ("orig", "__cause__", "__context__"):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return fallback


def _task_facts(task: Task) -> VideoBBoxTaskFacts:
    return VideoBBoxTaskFacts(
        id=task.id,
        project_id=task.project_id,
        batch_id=task.batch_id,
        status=task.status,
        review_round_id=task.review_round_id,
    )


def _annotation_facts(annotation: Annotation) -> VideoBBoxAnnotationFacts:
    return VideoBBoxAnnotationFacts(
        id=annotation.id,
        task_id=annotation.task_id,
        project_id=annotation.project_id,
        version=annotation.version,
        is_active=annotation.is_active,
        was_cancelled=annotation.was_cancelled,
        annotation_type=annotation.annotation_type,
        class_name=annotation.class_name,
        geometry=annotation.geometry or {},
        video_segment_id=annotation.video_segment_id,
        source=annotation.source,
        confidence=annotation.confidence,
        attributes=annotation.attributes or {},
        track_id=annotation.track_id,
    )


class LabelingVideoBBoxEditingService:
    """Keep legacy ORM results while Labeling owns the compact-video writes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _command(self, request: VideoBBoxEditingCommand):
        async def execute(request_json: str):
            return await self.db.scalar(
                text(
                    "SELECT aap_labeling_v1.video_bbox_editing_command("
                    "CAST(:request AS jsonb))"
                ),
                {"request": request_json},
            )

        try:
            return await VideoBBoxEditingDataClient(execute).command(request)
        except VideoBBoxEditingUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "video_bbox_editing_unavailable"},
            ) from exc
        except VideoBBoxEditingRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except DBAPIError as exc:
            message, detail, sqlstate = _native_error_fields(exc)
            if message == "aap_video_bbox_editing_error":
                try:
                    payload = json.loads(detail or "{}")
                    status_code = int(payload.get("status_code", 409))
                    response_detail = payload.get(
                        "detail", {"reason": "video_bbox_edit_conflict"}
                    )
                except (TypeError, ValueError):
                    status_code, response_detail = (
                        409,
                        {"reason": "video_bbox_edit_conflict"},
                    )
                raise HTTPException(
                    status_code=status_code, detail=response_detail
                ) from exc
            if sqlstate in {"55P03", "40P01", "40001"}:
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "task_busy"},
                ) from exc
            raise

    async def _sync_existing_annotations(
        self, annotation_ids: list[uuid.UUID], fields: tuple[str, ...]
    ) -> None:
        if not annotation_ids:
            return
        unique_ids = list(dict.fromkeys(annotation_ids))
        columns = [
            annotation_row.id,
            *(getattr(annotation_row, field) for field in fields),
        ]
        with self.db.no_autoflush:
            result = await self.db.execute(
                select(*columns).where(annotation_row.id.in_(unique_ids))
            )
        rows = {row.id: row for row in result}
        loaded = {
            item.id: item
            for item in self.db.sync_session.identity_map.values()
            if isinstance(item, Annotation) and item.id in unique_ids
        }
        for annotation_id, values in rows.items():
            annotation = loaded.get(annotation_id)
            if annotation is None:
                continue
            for field in fields:
                set_committed_value(annotation, field, getattr(values, field))

    async def _load_created_annotations(
        self, annotation_ids: list[uuid.UUID]
    ) -> list[Annotation]:
        if not annotation_ids:
            return []
        with self.db.no_autoflush:
            rows = (
                (
                    await self.db.execute(
                        select(annotation_row).where(
                            annotation_row.id.in_(annotation_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_id = {annotation.id: annotation for annotation in rows}
        if len(by_id) != len(set(annotation_ids)):
            raise HTTPException(
                status_code=409,
                detail={"reason": "video_bbox_edit_conflict"},
            )
        return [by_id[annotation_id] for annotation_id in annotation_ids]

    async def convert_video_track_to_bboxes(
        self,
        *,
        task: Task,
        annotation: Annotation,
        user_id: uuid.UUID,
        operation: str,
        scope: str,
        frame_index: int | None = None,
        frame_mode: str = "keyframes",
        frame_count: int | None = None,
        max_created: int = 5000,
    ) -> tuple[Annotation | None, list[Annotation], bool, list[int]]:
        await record_annotation_actor(self.db, task, user_id)
        command = convert_video_track_to_bboxes_command(
            task=_task_facts(task),
            annotation=_annotation_facts(annotation),
            actor_id=user_id,
            operation=operation,  # type: ignore[arg-type]
            scope=scope,  # type: ignore[arg-type]
            frame_index=frame_index,
            frame_mode=frame_mode,  # type: ignore[arg-type]
            frame_count=frame_count,
            max_created=max_created,
        )
        if self.db.autoflush:
            await self.db.flush()
        result = await self._command(command)
        if result.deleted_source:
            await self._sync_existing_annotations(
                result.deleted_annotation_ids, ("is_active", "updated_at")
            )
        elif operation == "split" and scope == "frame":
            await self._sync_existing_annotations(
                result.updated_annotation_ids, ("geometry", "version", "updated_at")
            )
        created = await self._load_created_annotations(result.created_annotation_ids)
        await update_annotation_statistics(self.db, task.id)
        source = annotation if not result.deleted_source else None
        return source, created, result.deleted_source, result.removed_frame_indexes

    async def compose_video_tracks(
        self,
        *,
        task: Task,
        annotations: list[Annotation],
        user_id: uuid.UUID,
        operation: str,
        frame_index: int | None = None,
        delete_sources: bool = True,
        gap_mode: str = "interpolate",
    ) -> tuple[list[Annotation], list[Annotation], list[uuid.UUID]]:
        await record_annotation_actor(self.db, task, user_id)
        command = compose_video_tracks_command(
            task=_task_facts(task),
            annotations=[_annotation_facts(annotation) for annotation in annotations],
            actor_id=user_id,
            operation=operation,  # type: ignore[arg-type]
            frame_index=frame_index,
            delete_sources=delete_sources,
            gap_mode=gap_mode,  # type: ignore[arg-type]
        )
        if self.db.autoflush:
            await self.db.flush()
        result = await self._command(command)
        if operation == "aggregate_bboxes" and delete_sources:
            await self._sync_existing_annotations(
                result.deleted_annotation_ids, ("is_active", "updated_at")
            )
        elif operation == "split_track":
            await self._sync_existing_annotations(
                result.updated_annotation_ids, ("geometry", "version", "updated_at")
            )
        elif operation in {"merge_tracks", "join_tracks"}:
            await self._sync_existing_annotations(
                result.updated_annotation_ids,
                ("geometry", "track_id", "version", "updated_at"),
            )
            await self._sync_existing_annotations(
                result.deleted_annotation_ids, ("is_active", "updated_at")
            )
        created = await self._load_created_annotations(result.created_annotation_ids)
        await update_annotation_statistics(self.db, task.id)
        return (
            [
                annotation
                for annotation in annotations
                if annotation.id in set(result.updated_annotation_ids)
            ],
            created,
            result.deleted_annotation_ids,
        )


__all__ = ["LabelingVideoBBoxEditingService"]
