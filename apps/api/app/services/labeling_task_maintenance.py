"""Call the closed Labeling Task maintenance owner on the current transaction."""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key
from sqlalchemy.orm.exc import StaleDataError

from aap_labeling.task_maintenance_contract import (
    TaskMaintenanceCommand,
    TaskMaintenanceData,
    TaskMaintenanceUnavailable,
)
from aap_labeling.task_maintenance_data import TaskMaintenanceDataClient
from app.services.labeling_annotation_data import _native_error_fields


class LabelingTaskMaintenanceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def attach_batch(self, model, snapshot):
        loaded = self.db.identity_map.get(identity_key(model, ident=snapshot.id))
        values = snapshot.model_dump(mode="python")
        if loaded is not None:
            for field, value in values.items():
                if field in inspect(loaded).unloaded:
                    set_committed_value(loaded, field, value)
            return loaded
        loaded = model(**values)
        make_transient_to_detached(loaded)
        return await self.db.merge(loaded, load=False)

    async def command(
        self, request: TaskMaintenanceCommand, *, flush_before: bool = False
    ) -> TaskMaintenanceData:
        if flush_before and self.db.autoflush:
            await self.db.flush()

        async def execute(payload: str):
            return await self.db.scalar(
                text(
                    "SELECT aap_labeling_v1.task_maintenance_command("
                    "CAST(:request AS jsonb))"
                ),
                {"request": payload},
            )

        try:
            return await TaskMaintenanceDataClient(execute).command(request)
        except DBAPIError as exc:
            owner_message, detail, sqlstate = _native_error_fields(exc)
            if sqlstate in {"55P03", "40P01", "40001"}:
                raise HTTPException(409, detail={"reason": "task_busy"}) from exc
            if owner_message != "aap_labeling_task_maintenance_error":
                raise
            try:
                reason = json.loads(detail or "{}").get("reason")
            except (ValueError, TypeError):
                reason = None
            if reason == "batch_target_changed":
                raise StaleDataError(
                    "UPDATE statement on table 'task_batches' expected to update 1 row(s); 0 were matched."
                ) from exc
            if reason in {"task_busy", "batch_busy"}:
                await self.db.rollback()
                raise HTTPException(409, detail={"reason": reason}) from exc
            if reason == "task_target_changed":
                await self.db.rollback()
                raise HTTPException(409, detail={"reason": reason}) from exc
            raise HTTPException(
                503, detail={"reason": "task_maintenance_unavailable"}
            ) from exc
        except TaskMaintenanceUnavailable as exc:
            raise HTTPException(
                503, detail={"reason": "task_maintenance_unavailable"}
            ) from exc
