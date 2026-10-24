"""Keep draft and schema-cleanup calls on their original API transaction."""

from __future__ import annotations

import json
import uuid
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, MultipleResultsFound
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm.util import identity_key

from aap_labeling.annotation_maintenance_contract import (
    ANNOTATION_MAINTENANCE_PROTOCOL,
    AnnotationMaintenanceUnavailable,
    CleanupAnnotationReference,
    CleanupSchema,
    ObserveCleanupTasks,
    ReadCleanupCandidates,
    ReadDraft,
    SaveDraft,
)
from aap_labeling.annotation_maintenance_data import AnnotationMaintenanceDataClient
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import AnnotationDraft
from app.services.annotation_evidence import record_annotation_actor
from app.services.labeling_annotation_data import _native_error_fields
from app.services.labeling_annotation_statistics import update_annotation_statistics


def _orphan_user_attribute_keys(attributes, allowed_keys: set[str]) -> list[str]:
    if not isinstance(attributes, dict):
        return []
    return [
        key for key in attributes if key not in allowed_keys and not key.startswith("_")
    ]


def _prune_orphan_user_attributes(attributes, allowed_keys: set[str]) -> dict:
    if not isinstance(attributes, dict):
        return {}
    return {
        key: value
        for key, value in attributes.items()
        if key in allowed_keys or key.startswith("_")
    }


class LabelingAnnotationMaintenanceService:
    def __init__(self, db):
        self.db = db

    async def _command(self, request):
        async def execute(payload: str):
            return await self.db.scalar(
                text(
                    "SELECT aap_labeling_v1.annotation_maintenance_command(CAST(:request AS jsonb))"
                ),
                {"request": payload},
            )

        try:
            return await AnnotationMaintenanceDataClient(execute).command(request)
        except AnnotationMaintenanceUnavailable as exc:
            raise HTTPException(
                503, detail={"reason": "annotation_maintenance_unavailable"}
            ) from exc
        except DBAPIError as exc:
            message, detail, sqlstate = _native_error_fields(exc)
            if sqlstate == "21000" and request.operation == "read_draft":
                raise MultipleResultsFound(
                    "Multiple rows were found when one or none was required"
                ) from exc
            if message != "aap_annotation_maintenance_error":
                raise
            try:
                value = json.loads(detail or "{}")
                reason = value["reason"]
                if not isinstance(reason, str):
                    raise ValueError("Invalid maintenance error reason")
            except (ValueError, KeyError, TypeError):
                reason = "annotation_maintenance_conflict"
            if reason == "draft_target_changed":
                raise StaleDataError(
                    "Annotation draft no longer matches the selected row"
                ) from exc
            if reason in {"batch_busy", "task_busy", "annotation_busy"}:
                await self.db.rollback()
            raise HTTPException(409, detail={"reason": reason}) from exc

    async def _attach(self, model, snapshot, *, refresh: bool = False):
        loaded = self.db.identity_map.get(identity_key(model, ident=snapshot.id))
        values = snapshot.model_dump(mode="python")
        if loaded is not None:
            unloaded = inspect(loaded).unloaded
            for field, value in values.items():
                if refresh or field in unloaded:
                    set_committed_value(loaded, field, value)
            return loaded
        loaded = model(**values)
        make_transient_to_detached(loaded)
        return await self.db.merge(loaded, load=False)

    async def get_draft(self, task_id: uuid.UUID, user_id: uuid.UUID):
        if self.db.autoflush:
            await self.db.flush()
        data = await self._command(
            ReadDraft(
                schema=ANNOTATION_MAINTENANCE_PROTOCOL,
                operation="read_draft",
                task_id=task_id,
                user_id=user_id,
            )
        )
        return (
            None
            if data.draft is None
            else await self._attach(AnnotationDraft, data.draft)
        )

    async def save_draft(self, task_id: uuid.UUID, user_id: uuid.UUID, result: dict):
        draft = await self.get_draft(task_id, user_id)
        existing = draft is not None
        original = (
            inspect(draft).committed_state.get("result", draft.result)
            if existing
            else None
        )
        write_result = not existing or original != result
        data = await self._command(
            SaveDraft(
                schema=ANNOTATION_MAINTENANCE_PROTOCOL,
                operation="save_draft",
                task_id=task_id,
                user_id=user_id,
                draft_id=draft.id if existing else uuid.uuid4(),
                existing=existing,
                write_result=write_result,
                result=result,
            )
        )
        if not existing:
            draft = await self._attach(AnnotationDraft, data.draft)
            set_committed_value(draft, "result", result)
        else:
            set_committed_value(draft, "result", result)
            if write_result:
                set_committed_value(draft, "updated_at", data.draft.updated_at)
        await self.db.flush()
        return draft

    async def cleanup_schema_orphan_annotations(
        self,
        *,
        project_id: uuid.UUID,
        class_names: set[str],
        attribute_keys: set[str],
        actor_id: uuid.UUID,
        dry_run: bool,
    ) -> dict[str, int | dict[str, int]]:
        with self.db.no_autoflush:
            data = await self._command(
                ReadCleanupCandidates(
                    schema=ANNOTATION_MAINTENANCE_PROTOCOL,
                    operation="read_cleanup_candidates",
                    project_id=project_id,
                )
            )
            annotations = [
                await self._attach(Annotation, snapshot)
                for snapshot in data.annotations
            ]

        orphan_annotations = 0
        orphan_attribute_keys: Counter[str] = Counter()
        candidates = []
        task_ids = set()
        for annotation in annotations:
            if annotation.class_name not in class_names:
                orphan_annotations += 1
                orphan_keys = []
            else:
                orphan_keys = _orphan_user_attribute_keys(
                    annotation.attributes, attribute_keys
                )
                orphan_attribute_keys.update(orphan_keys)
            if not dry_run and (
                annotation.class_name not in class_names or orphan_keys
            ):
                candidates.append(
                    CleanupAnnotationReference(
                        id=annotation.id, task_id=annotation.task_id
                    )
                )
                task_ids.add(annotation.task_id)
        if dry_run:
            return {
                "orphan_annotations": orphan_annotations,
                "orphan_attribute_keys": dict(orphan_attribute_keys),
            }
        if not candidates:
            return {"orphan_annotations": 0, "orphan_attribute_keys": {}}

        with self.db.no_autoflush:
            observed = await self._command(
                ObserveCleanupTasks(
                    schema=ANNOTATION_MAINTENANCE_PROTOCOL,
                    operation="observe_cleanup_tasks",
                    task_ids=sorted(task_ids, key=str),
                )
            )
            if {row.id for row in observed.tasks} != task_ids or any(
                row.project_id != project_id for row in observed.tasks
            ):
                raise HTTPException(409, detail={"reason": "task_target_changed"})
            written = await self._command(
                CleanupSchema(
                    schema=ANNOTATION_MAINTENANCE_PROTOCOL,
                    operation="cleanup_schema",
                    project_id=project_id,
                    class_names=sorted(class_names),
                    attribute_keys=sorted(attribute_keys),
                    annotations=candidates,
                    tasks=observed.tasks,
                )
            )
            batches = [
                await self._attach(TaskBatch, row, refresh=True)
                for row in written.batches
            ]
            tasks = {
                row.id: await self._attach(Task, row, refresh=True)
                for row in written.tasks
            }
            locked_annotations = [
                await self._attach(Annotation, row, refresh=True)
                for row in written.annotations
            ]
        await self.db.flush()
        for task_id in sorted(written.affected_task_ids, key=str):
            await record_annotation_actor(self.db, tasks[task_id], actor_id, lock=False)
        for task_id in sorted(written.deactivated_task_ids, key=str):
            await update_annotation_statistics(self.db, task_id)
        # Keep the same loaded rows alive until evidence and statistics finish.
        del batches, locked_annotations
        return written.report.model_dump()
