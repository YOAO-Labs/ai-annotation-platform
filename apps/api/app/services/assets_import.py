"""SQLAlchemy bindings for the versioned Dataset import capabilities."""

from __future__ import annotations

import json
from uuid import UUID

from aap_assets_source.import_contract import (
    ImportAcceptance,
    ImportContext,
    ImportFailure,
    ImportLimits,
    ImportState,
    decode_import_reply,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

DATASET_IMPORT_PROTOCOL = "aap-dataset-import/v1"

_FAILURES: dict[str, tuple[int, object, dict[str, str] | None]] = {
    "invalid_request": (422, "导入参数无效", None),
    "invalid_source_object": (422, "来源文件名或路径超出支持范围", None),
    "account_not_active": (
        401,
        "账号已停用或不存在",
        {"WWW-Authenticate": "Bearer"},
    ),
    "account_busy": (409, {"reason": "account_busy"}, None),
    "manager_required": (403, "仅素材库管理员可导入素材", None),
    "dataset_missing": (404, "Dataset not found", None),
    "connection_missing": (404, "连接器不存在", None),
    "connection_changed": (409, {"reason": "connection_changed"}, None),
    "dataset_busy": (409, {"reason": "dataset_busy"}, None),
    "operation_conflict": (409, {"reason": "operation_conflict"}, None),
    "job_missing": (404, "async_job not found", None),
    "job_terminal": (409, "cannot cancel terminal job", None),
    "lease_lost": (409, {"reason": "lease_lost"}, None),
    "import_cancelled": (409, {"reason": "import_cancelled"}, None),
    "invalid_response": (502, "Dataset import returned an invalid response", None),
    "storage_unavailable": (503, "Dataset import service is unavailable", None),
    "storage_maintenance": (409, {"reason": "storage_maintenance"}, None),
    "source_changed": (409, {"reason": "source_changed"}, None),
}


def _raise_failure(exc: ImportFailure) -> None:
    status_code, detail, headers = _FAILURES[exc.code]
    raise HTTPException(status_code, detail, headers=headers) from None


class DatasetImportCapabilities:
    """Use versioned Assets import functions inside the caller's transaction."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def prepare(
        self, *, actor_id: UUID, dataset_id: UUID, connection_id: UUID
    ) -> ImportContext:
        return await self._call(
            """
            SELECT aap_assets_v1.prepare_dataset_import(
                CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid),
                CAST(:connection_id AS uuid)
            )
            """,
            {
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "connection_id": connection_id,
            },
            ImportContext,
            scope=(actor_id, dataset_id, connection_id),
        )

    async def accept(
        self,
        *,
        operation_id: UUID,
        actor_id: UUID,
        dataset_id: UUID,
        connection_id: UUID,
        request: dict,
        limits: ImportLimits,
        audit_context: dict,
    ) -> ImportAcceptance:
        return await self._call(
            """
            SELECT aap_assets_v1.accept_dataset_import(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:connection_id AS uuid),
                CAST(:request AS jsonb), CAST(:limits AS jsonb),
                CAST(:audit_context AS jsonb)
            )
            """,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "connection_id": connection_id,
                "request": json.dumps(request, separators=(",", ":")),
                "limits": json.dumps(
                    limits.model_dump(mode="json"), separators=(",", ":")
                ),
                "audit_context": json.dumps(audit_context, separators=(",", ":")),
            },
            ImportAcceptance,
            scope=(operation_id, actor_id, dataset_id, connection_id),
        )

    async def cancel(self, *, actor_id: UUID, job_id: UUID) -> ImportState:
        state = await self._call(
            """
            SELECT aap_assets_v1.cancel_dataset_import(
                CAST(:actor_id AS uuid), CAST(:job_id AS uuid)
            )
            """,
            {"actor_id": actor_id, "job_id": job_id},
            ImportState,
            # actor_id is the authenticated canceller. The returned actor_id is
            # the original accepted-job owner, so only job identity is scoped
            # against the result here. Native code authorizes the current actor.
            scope=(job_id,),
        )
        if state.stage != "cancelled":
            raise HTTPException(502, "Dataset import returned an invalid response")
        return state

    async def _call(self, statement, parameters, result_type, *, scope=None):
        try:
            payload = await self._db.scalar(text(statement), parameters)
        except SQLAlchemyError:
            raise HTTPException(503, "Dataset import service is unavailable") from None
        try:
            result = decode_import_reply(payload, result_type)
        except ImportFailure as exc:
            _raise_failure(exc)
        except (ValidationError, ValueError, TypeError, KeyError):
            raise HTTPException(
                502, "Dataset import returned an invalid response"
            ) from None
        if result is None:
            raise HTTPException(502, "Dataset import returned an invalid response")
        if scope is not None:
            expected = tuple(scope)
            if isinstance(result, ImportContext):
                actual = (result.actor_id, result.dataset_id, result.connection_id)
            elif isinstance(result, ImportAcceptance):
                actual = (
                    result.job_id,
                    result.actor_id,
                    result.dataset_id,
                    result.connection_id,
                )
            elif isinstance(result, ImportState):
                actual = (result.job_id,)
            else:
                actual = ()
            if actual != expected:
                raise HTTPException(502, "Dataset import returned an invalid response")
        return result
