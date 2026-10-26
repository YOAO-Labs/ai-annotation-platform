"""Bind test-database Labeling fixture writes to the caller's Session."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_labeling.e2e_fixture_contract import (
    AnnotationFixtureRow,
    FixtureCommand,
    InsertFixtureAnnotations,
    InsertFixtureBatches,
    InsertFixtureTaskDatasetItemLinks,
    InsertFixtureTasks,
    InsertFixtureVideoTrackerJobs,
    LABELING_E2E_FILTERING_RENDER_FUNCTION,
    LabelingE2EFixtureUnavailable,
    PatchFixtureBatches,
    PatchFixtureFeedbackAnchor,
    PatchFixtureTasks,
    PrepareFilteringRendering,
    TaskBatchFixturePatch,
    TaskBatchFixtureRow,
    TaskDatasetItemLinkFixtureRow,
    TaskFixturePatch,
    TaskFixtureRow,
    VideoTrackerJobFixtureRow,
    LABELING_E2E_FIXTURE_PROTOCOL,
)
from aap_labeling.e2e_fixture_data import LabelingE2EFixtureData
from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.labeling_core_read_relations import (
    annotation_row,
    batch_row,
    execute_core_read,
    task_dataset_item_link_row,
    task_row,
    video_tracker_job_row,
)

_COMMAND = text("SELECT aap_labeling_v1.e2e_fixture_command(CAST(:request AS jsonb))")
_FILTERING_RENDER = text(
    f"SELECT {LABELING_E2E_FILTERING_RENDER_FUNCTION}(CAST(:request AS jsonb))"
)


class LabelingE2EFixtureWrites:
    """Execute fixed fixture operations without committing or staging ORM DML."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _command(self, request: FixtureCommand):
        # Match the prior explicit flush point for pending Workspace/Assets rows.
        # Fixture model instances passed to this class are transient value carriers,
        # never Session.add() targets.
        await self.db.flush()

        async def execute(payload: str):
            with self.db.no_autoflush:
                return await self.db.scalar(_COMMAND, {"request": payload})

        try:
            return await LabelingE2EFixtureData(execute).command(request)
        except LabelingE2EFixtureUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "labeling_e2e_fixture_unavailable"},
            ) from exc
        except DBAPIError as exc:
            if _sqlstate(exc) == "42883":
                raise HTTPException(
                    status_code=503,
                    detail={"reason": "labeling_e2e_fixture_unavailable"},
                ) from exc
            raise

    @staticmethod
    def _prepare_rows(rows, model, row_contract):
        prepared = []
        for row in rows:
            if not isinstance(row, model):
                raise TypeError(f"expected transient {model.__name__} fixture value")
            state = inspect(row)
            if not state.transient:
                raise ValueError("fixture inserts require transient ORM value objects")
            if state.dict.get("id") is None:
                row.id = uuid.uuid4()
                state = inspect(row)
            values = {}
            for name in row_contract.model_fields:
                if name not in state.dict:
                    continue
                value = state.dict[name]
                column = model.__table__.c[name]
                if (
                    value is None
                    and (
                        column.default is not None or column.server_default is not None
                    )
                    and not column.type.should_evaluate_none
                ):
                    continue
                values[name] = value
            prepared.append(row_contract.model_validate(values))
        return prepared

    async def _rows(self, row_alias, ids, *, populate_existing: bool = False):
        if not ids:
            return []
        statement = (
            select(row_alias).where(row_alias.id.in_(ids)).order_by(row_alias.id)
        )
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        result = await execute_core_read(self.db, statement)
        by_id = {row.id: row for row in result.scalars().all()}
        if set(by_id) != set(ids):
            raise HTTPException(
                status_code=503,
                detail={"reason": "labeling_e2e_fixture_unavailable"},
            )
        return [by_id[row_id] for row_id in ids]

    async def insert_batches(self, rows: list[TaskBatch]) -> list[TaskBatch]:
        prepared = self._prepare_rows(rows, TaskBatch, TaskBatchFixtureRow)
        if not prepared:
            return []
        project_ids = {row.project_id for row in prepared}
        if len(project_ids) != 1:
            raise ValueError("a fixture batch insert must use one project")
        request = InsertFixtureBatches(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_batches",
            project_id=next(iter(project_ids)),
            rows=prepared,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(batch_row, data.ids)

    async def insert_tasks(self, rows: list[Task]) -> list[Task]:
        prepared = self._prepare_rows(rows, Task, TaskFixtureRow)
        if not prepared:
            return []
        project_ids = {row.project_id for row in prepared}
        if len(project_ids) != 1:
            raise ValueError("a fixture task insert must use one project")
        request = InsertFixtureTasks(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_tasks",
            project_id=next(iter(project_ids)),
            rows=prepared,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(task_row, data.ids)

    async def patch_tasks(self, patches: list[TaskFixturePatch]) -> list[Task]:
        if not patches:
            return []
        project_ids = {patch.project_id for patch in patches}
        if len(project_ids) != 1:
            raise ValueError("a fixture task patch must use one project")
        request = PatchFixtureTasks(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="patch_tasks",
            project_id=next(iter(project_ids)),
            rows=patches,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(task_row, data.ids, populate_existing=True)

    async def patch_task(self, task: Task, **changes: Any) -> Task:
        state = inspect(task)
        if not state.persistent:
            raise ValueError("fixture Task updates require a Session-loaded Task")
        allowed = set(TaskFixturePatch.model_fields) - {"id", "project_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported fixture Task fields: {sorted(unknown)}")
        if any(attr.history.has_changes() for attr in state.attrs):
            raise ValueError("fixture Task is dirty; stage updates through this owner")
        changed = {
            name: value
            for name, value in changes.items()
            if getattr(task, name) != value
        }
        if not changed:
            return task
        patch = TaskFixturePatch(id=task.id, project_id=task.project_id, **changed)
        return (await self.patch_tasks([patch]))[0]

    async def patch_batches(
        self, patches: list[TaskBatchFixturePatch]
    ) -> list[TaskBatch]:
        if not patches:
            return []
        project_ids = {patch.project_id for patch in patches}
        if len(project_ids) != 1:
            raise ValueError("a fixture batch patch must use one project")
        request = PatchFixtureBatches(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="patch_batches",
            project_id=next(iter(project_ids)),
            rows=patches,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(batch_row, data.ids, populate_existing=True)

    async def patch_batch(self, batch: TaskBatch, **changes: Any) -> TaskBatch:
        state = inspect(batch)
        if not state.persistent:
            raise ValueError("fixture Batch updates require a Session-loaded Batch")
        allowed = set(TaskBatchFixturePatch.model_fields) - {"id", "project_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported fixture Batch fields: {sorted(unknown)}")
        if any(attr.history.has_changes() for attr in state.attrs):
            raise ValueError("fixture Batch is dirty; stage updates through this owner")
        changed = {
            name: value
            for name, value in changes.items()
            if getattr(batch, name) != value
        }
        if not changed:
            return batch
        patch = TaskBatchFixturePatch(
            id=batch.id, project_id=batch.project_id, **changed
        )
        return (await self.patch_batches([patch]))[0]

    async def insert_annotations(self, rows: list[Annotation]) -> list[Annotation]:
        prepared = self._prepare_rows(rows, Annotation, AnnotationFixtureRow)
        if not prepared:
            return []
        project_ids = {row.project_id for row in prepared}
        if len(project_ids) != 1:
            raise ValueError("a fixture Annotation insert must use one project")
        request = InsertFixtureAnnotations(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_annotations",
            project_id=next(iter(project_ids)),
            rows=prepared,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(annotation_row, data.ids)

    async def insert_video_tracker_jobs(
        self, rows: list[VideoTrackerJob]
    ) -> list[VideoTrackerJob]:
        prepared = self._prepare_rows(rows, VideoTrackerJob, VideoTrackerJobFixtureRow)
        if not prepared:
            return []
        task_ids = list(dict.fromkeys(row.task_id for row in prepared))
        tasks = await self._rows(task_row, task_ids)
        project_ids = {task.project_id for task in tasks}
        if len(project_ids) != 1:
            raise ValueError("fixture tracker jobs must use one project")
        request = InsertFixtureVideoTrackerJobs(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_video_tracker_jobs",
            project_id=next(iter(project_ids)),
            rows=prepared,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(video_tracker_job_row, data.ids)

    async def insert_task_dataset_item_links(
        self,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID,
        rows: list[TaskDatasetItemLink],
    ) -> list[TaskDatasetItemLink]:
        prepared = self._prepare_rows(
            rows, TaskDatasetItemLink, TaskDatasetItemLinkFixtureRow
        )
        if not prepared:
            return []
        if any(row.task_id != task_id for row in prepared):
            raise ValueError("fixture links must target the scoped Task")
        request = InsertFixtureTaskDatasetItemLinks(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_task_dataset_item_links",
            project_id=project_id,
            task_id=task_id,
            rows=prepared,
        )
        data = await self._command(request)
        assert data.ids is not None
        return await self._rows(task_dataset_item_link_row, data.ids)

    async def patch_feedback_anchor(
        self,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID,
        feedback_id: uuid.UUID,
        anchor_position: dict[str, Any],
    ) -> dict[str, Any]:
        request = PatchFixtureFeedbackAnchor(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="patch_feedback_anchor",
            project_id=project_id,
            task_id=task_id,
            feedback_id=feedback_id,
            anchor_position=anchor_position,
        )
        data = await self._command(request)
        if data.anchor_position is None:
            raise HTTPException(
                status_code=503,
                detail={"reason": "labeling_e2e_fixture_unavailable"},
            )
        return data.anchor_position

    async def prepare_filtering_rendering(
        self,
        *,
        project_id: uuid.UUID,
        low_prediction_id: uuid.UUID,
        at_prediction_id: uuid.UUID,
        above_prediction_id: uuid.UUID,
    ) -> None:
        request = PrepareFilteringRendering(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="prepare_filtering_rendering",
            project_id=project_id,
            low_prediction_id=low_prediction_id,
            at_prediction_id=at_prediction_id,
            above_prediction_id=above_prediction_id,
        )

        async def execute(payload: str):
            with self.db.no_autoflush:
                return await self.db.scalar(_FILTERING_RENDER, {"request": payload})

        try:
            data = await LabelingE2EFixtureData(
                execute_filtering_rendering=execute
            ).command(request)
        except LabelingE2EFixtureUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "labeling_e2e_fixture_unavailable"},
            ) from exc
        except DBAPIError as exc:
            if _sqlstate(exc) == "42883":
                raise HTTPException(
                    status_code=503,
                    detail={"reason": "labeling_e2e_fixture_unavailable"},
                ) from exc
            raise

        if data.rendered_predictions is None:
            raise HTTPException(
                status_code=503,
                detail={"reason": "labeling_e2e_fixture_unavailable"},
            )
        rendered = {row.id: row.result for row in data.rendered_predictions}
        for prediction in self.db.identity_map.values():
            if isinstance(prediction, Prediction):
                identity = inspect(prediction).identity
                if identity is not None and identity[0] in rendered:
                    set_committed_value(
                        prediction, "result", rendered[identity[0]]
                    )


def _sqlstate(exc: DBAPIError) -> str | None:
    original = getattr(exc, "orig", exc)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


__all__ = ["LabelingE2EFixtureWrites"]
