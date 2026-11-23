"""Bind Insights' saved-view operations to the caller's transaction."""

from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.dataset_view import DatasetSavedViewOut, DatasetSavedViewWrite


class SavedTaskView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    owner_id: UUID | None
    name: str
    visibility: str
    entity_scope: str
    filter_json: dict[str, Any]
    sort_json: list[dict[str, Any]]
    columns_json: list[str]
    created_at: datetime
    updated_at: datetime


_LIST_TASK = text(
    "SELECT yoao_insights_v1.list_saved_task_views("
    "CAST(:actor AS uuid), CAST(:project AS uuid), CAST(:scope AS text))"
)
_READ_TASK = text(
    "SELECT yoao_insights_v1.read_saved_task_view("
    "CAST(:actor AS uuid), CAST(:project AS uuid), CAST(:view AS uuid))"
)
_CREATE_TASK = text(
    "SELECT yoao_insights_v1.create_saved_task_view("
    "CAST(:actor AS uuid), CAST(:project AS uuid), CAST(:name AS text), "
    "CAST(:visibility AS text), CAST(:scope AS text), :filters, :sort, :columns)"
).bindparams(
    bindparam("filters", type_=JSONB),
    bindparam("sort", type_=JSONB),
    bindparam("columns", type_=JSONB),
)
_UPDATE_TASK = text(
    "SELECT yoao_insights_v1.update_saved_task_view("
    "CAST(:actor AS uuid), CAST(:project AS uuid), CAST(:view AS uuid), "
    "CAST(:name AS text), CAST(:visibility AS text), :filters, :sort, :columns)"
).bindparams(
    bindparam("filters", type_=JSONB(none_as_null=True)),
    bindparam("sort", type_=JSONB(none_as_null=True)),
    bindparam("columns", type_=JSONB(none_as_null=True)),
)
_DELETE_TASK = text(
    "SELECT yoao_insights_v1.delete_saved_task_view("
    "CAST(:actor AS uuid), CAST(:project AS uuid), CAST(:view AS uuid))"
)
_LIST_DATASET = text(
    "SELECT yoao_insights_v1.list_personal_dataset_views("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid))"
)
_HAS_DATASET = text(
    "SELECT yoao_insights_v1.has_personal_dataset_view("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:view AS uuid))"
)
_PREPARE_DATASET = text(
    "SELECT yoao_insights_v1.prepare_personal_dataset_view("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid))"
)
_CREATE_DATASET = text(
    "SELECT yoao_insights_v1.create_personal_dataset_view("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:name AS text), :query)"
).bindparams(bindparam("query", type_=JSONB))
_UPDATE_DATASET = text(
    "SELECT yoao_insights_v1.update_personal_dataset_view("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:view AS uuid), "
    "CAST(:name AS text), :query)"
).bindparams(bindparam("query", type_=JSONB))
_DELETE_DATASET = text(
    "SELECT yoao_insights_v1.delete_personal_dataset_view("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:view AS uuid))"
)

_ERRORS = {
    "IT401": (401, "账号已停用或不存在"),
    "IT403": (403, "项目职责无效，访问被拒绝"),
    "IT404": (404, "项目不存在"),
    "IT409": (409, "Task view project access is busy"),
    "IT413": (403, "Only project admins can create shared views"),
    "IT414": (404, "Task view not found"),
    "IT423": (403, "Only project admins can edit shared views"),
    "IT433": (403, "Only project admins can share views"),
    "ID404": (404, "Dataset not found"),
    "ID409": (409, "At most 100 personal views per dataset"),
    "ID414": (404, "Personal dataset view not found"),
}
_DUPLICATES = {
    "uq_project_task_views_private_owner_name": "Task view name already exists",
    "uq_project_task_views_project_name": "Task view name already exists",
    "uq_dataset_saved_views_owner_name": "A personal view with this name already exists",
}


@contextmanager
def _translate_view_errors():
    try:
        yield
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None)
        if state in _ERRORS:
            status, detail = _ERRORS[state]
            headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            raise HTTPException(status, detail=detail, headers=headers) from exc
        if state == "23505":
            constraint = getattr(exc.orig.__cause__, "constraint_name", None)
            if constraint in _DUPLICATES:
                raise HTTPException(409, detail=_DUPLICATES[constraint]) from exc
        raise


class ProjectSavedViews:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list(
        self, project_id: UUID, actor_id: UUID, scope: str
    ) -> list[SavedTaskView]:
        with _translate_view_errors():
            rows = await self.db.scalars(
                _LIST_TASK, {"actor": actor_id, "project": project_id, "scope": scope}
            )
        return [SavedTaskView.model_validate(row) for row in rows]

    async def read(
        self, project_id: UUID, view_id: UUID, actor_id: UUID
    ) -> SavedTaskView:
        with _translate_view_errors():
            row = await self.db.scalar(
                _READ_TASK, {"actor": actor_id, "project": project_id, "view": view_id}
            )
        if row is None:
            raise HTTPException(404, detail="Task view not found")
        return SavedTaskView.model_validate(row)

    async def create(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        name: str,
        visibility: str,
        entity_scope: str,
        filter_json: dict[str, Any],
        sort_json: list[dict[str, Any]],
        columns_json: list[str],
    ) -> SavedTaskView:
        with _translate_view_errors():
            await self.db.flush()
            row = await self.db.scalar(
                _CREATE_TASK,
                {
                    "actor": actor_id,
                    "project": project_id,
                    "name": name,
                    "visibility": visibility,
                    "scope": entity_scope,
                    "filters": filter_json,
                    "sort": sort_json,
                    "columns": columns_json,
                },
            )
        return SavedTaskView.model_validate(row)

    async def update(
        self,
        view: SavedTaskView,
        *,
        actor_id: UUID,
        name: str | None,
        visibility: str | None,
        filter_json: dict[str, Any] | None,
        sort_json: list[dict[str, Any]] | None,
        columns_json: list[str] | None,
    ) -> SavedTaskView:
        with _translate_view_errors():
            await self.db.flush()
            row = await self.db.scalar(
                _UPDATE_TASK,
                {
                    "actor": actor_id,
                    "project": view.project_id,
                    "view": view.id,
                    "name": name,
                    "visibility": visibility,
                    "filters": filter_json,
                    "sort": sort_json,
                    "columns": columns_json,
                },
            )
        return SavedTaskView.model_validate(row)

    async def delete(self, view: SavedTaskView, actor_id: UUID) -> None:
        with _translate_view_errors():
            await self.db.flush()
            await self.db.execute(
                _DELETE_TASK,
                {"actor": actor_id, "project": view.project_id, "view": view.id},
            )


class PersonalDatasetViews:
    def __init__(self, db: AsyncSession, dataset_id: UUID, actor_id: UUID):
        self.db = db
        self.scope = {"actor": actor_id, "dataset": dataset_id}

    async def list(self) -> list[DatasetSavedViewOut]:
        rows = await self.db.scalars(_LIST_DATASET, self.scope)
        return [DatasetSavedViewOut.model_validate(row) for row in rows]

    async def require_view(self, view_id: UUID) -> None:
        if not await self.db.scalar(_HAS_DATASET, {**self.scope, "view": view_id}):
            raise HTTPException(404, detail="Personal dataset view not found")

    async def prepare_create(self) -> None:
        with _translate_view_errors():
            await self.db.execute(_PREPARE_DATASET, self.scope)

    async def create(self, payload: DatasetSavedViewWrite) -> DatasetSavedViewOut:
        with _translate_view_errors():
            row = await self.db.scalar(
                _CREATE_DATASET,
                {
                    **self.scope,
                    "name": payload.name,
                    "query": payload.query.model_dump(mode="json", exclude_none=True),
                },
            )
        return DatasetSavedViewOut.model_validate(row)

    async def update(
        self, view_id: UUID, payload: DatasetSavedViewWrite
    ) -> DatasetSavedViewOut:
        with _translate_view_errors():
            row = await self.db.scalar(
                _UPDATE_DATASET,
                {
                    **self.scope,
                    "view": view_id,
                    "name": payload.name,
                    "query": payload.query.model_dump(mode="json", exclude_none=True),
                },
            )
        return DatasetSavedViewOut.model_validate(row)

    async def delete(self, view_id: UUID) -> None:
        with _translate_view_errors():
            await self.db.execute(_DELETE_DATASET, {**self.scope, "view": view_id})
