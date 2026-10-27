"""Bind Labeling Prediction record commands to the caller's transaction."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from aap_labeling.prediction_record_contract import (
    DeletePredictionData,
    DeletePredictionsForBatch,
    DeletePredictionsForTasks,
    CreateFailedPrediction,
    CreateFailedPredictionData,
    DismissFailedPrediction,
    FailedPredictionStateData,
    PredictionRecordCommand,
    PredictionRecordData,
    PredictionIdentity,
    PredictionRecordRejected,
    PredictionRecordUnavailable,
    PurgeImportedPredictions,
    PurgeImportedPredictionsData,
    RejectPredictionCandidate,
    RejectPredictionCandidateData,
    RestoreFailedPrediction,
    PREDICTION_RECORDS_PROTOCOL,
)
from aap_labeling.prediction_record_data import PredictionRecordDataClient

from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta


def _native_sqlstate(error: BaseException) -> str | None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostic = getattr(current, "diag", None)
        sqlstate = (
            getattr(current, "sqlstate", None)
            or getattr(current, "pgcode", None)
            or getattr(diagnostic, "sqlstate", None)
        )
        if sqlstate is not None:
            return sqlstate
        for name in ("orig", "__cause__", "__context__"):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return None


def _raise_rejected(error: PredictionRecordRejected) -> None:
    if error.code == "task_not_found":
        raise HTTPException(status_code=404, detail="Task not found") from error
    if error.code == "prediction_not_found":
        raise HTTPException(status_code=404, detail="Prediction not found") from error
    if error.code == "shape_index_out_of_range":
        shape_index = error.details.get("shape_index")
        shape_count = error.details.get("shape_count")
        raise HTTPException(
            status_code=400,
            detail=f"shape_index {shape_index} out of range (0..{shape_count - 1})",
        ) from error
    raise HTTPException(
        status_code=409,
        detail={"reason": error.code, **error.details},
    ) from error


async def prediction_record_command(
    db: AsyncSession, request: PredictionRecordCommand
) -> PredictionRecordData:
    """Execute the closed command on the original AsyncSession transaction."""

    async def execute(request_json: str):
        return await db.scalar(
            text(
                "SELECT aap_labeling_v1.prediction_record_command(CAST(:request AS jsonb))"
            ),
            {"request": request_json},
        )

    try:
        return await PredictionRecordDataClient(execute).command(request)
    except PredictionRecordRejected as exc:
        _raise_rejected(exc)
    except PredictionRecordUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "prediction_records_unavailable"},
        ) from exc
    except DBAPIError as exc:
        if _native_sqlstate(exc) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "task_busy"}
            ) from exc
        raise


def _sync_cached_prediction_rejections(
    db: AsyncSession,
    *,
    prediction_id: uuid.UUID,
    prediction_created_at: datetime,
    rejected_shape_indexes: list[int],
) -> None:
    cached = db.identity_map.get(
        identity_key(
            Prediction,
            ident=(prediction_id, prediction_created_at),
        )
    )
    if isinstance(cached, Prediction):
        set_committed_value(
            cached, "rejected_shape_indexes", list(rejected_shape_indexes)
        )


async def reject_prediction_candidate(
    db: AsyncSession,
    *,
    prediction_id: uuid.UUID,
    prediction_created_at: datetime,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    shape_index: int | None,
) -> list[int]:
    """Merge a locked candidate rejection and refresh only its owned cache field."""
    data = await prediction_record_command(
        db,
        RejectPredictionCandidate(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="reject_candidate",
            prediction_id=prediction_id,
            prediction_created_at=prediction_created_at,
            task_id=task_id,
            project_id=project_id,
            shape_index=shape_index,
        ),
    )
    assert isinstance(data, RejectPredictionCandidateData)
    _sync_cached_prediction_rejections(
        db,
        prediction_id=prediction_id,
        prediction_created_at=prediction_created_at,
        rejected_shape_indexes=data.rejected_shape_indexes,
    )
    return data.rejected_shape_indexes


async def set_failed_prediction_dismissed(
    db: AsyncSession,
    *,
    failure_id: uuid.UUID,
    dismissed_at: datetime | None,
) -> bool:
    request: PredictionRecordCommand
    if dismissed_at is None:
        request = RestoreFailedPrediction(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="restore_failed",
            failure_id=failure_id,
        )
    else:
        request = DismissFailedPrediction(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="dismiss_failed",
            failure_id=failure_id,
            dismissed_at=dismissed_at,
        )
    data = await prediction_record_command(db, request)
    assert isinstance(data, FailedPredictionStateData)
    cached = db.identity_map.get(identity_key(FailedPrediction, ident=failure_id))
    if isinstance(cached, FailedPrediction):
        set_committed_value(cached, "dismissed_at", data.dismissed_at)
    return data.changed


def sync_deleted_prediction_records(
    db: AsyncSession, data: DeletePredictionData
) -> None:
    """Remove only identity-map objects whose rows the owner just deleted."""
    _expunge_deleted_rows(
        db,
        deleted_predictions=data.deleted_predictions,
        deleted_failed_prediction_ids=data.deleted_failed_prediction_ids,
        deleted_prediction_meta_ids=data.deleted_prediction_meta_ids,
    )


def _expunge_deleted_rows(
    db: AsyncSession,
    *,
    deleted_predictions: list[PredictionIdentity],
    deleted_failed_prediction_ids: list[uuid.UUID],
    deleted_prediction_meta_ids: list[uuid.UUID],
) -> None:
    deleted_prediction_keys = {
        identity_key(Prediction, ident=(row.id, row.created_at))
        for row in deleted_predictions
    }
    deleted_failure_keys = {
        identity_key(FailedPrediction, ident=failure_id)
        for failure_id in deleted_failed_prediction_ids
    }
    deleted_meta_keys = {
        identity_key(PredictionMeta, ident=meta_id)
        for meta_id in deleted_prediction_meta_ids
    }
    for key in deleted_prediction_keys | deleted_failure_keys | deleted_meta_keys:
        cached = db.identity_map.get(key)
        if cached is not None:
            db.expunge(cached)


async def purge_imported_prediction_records(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_ids: list[uuid.UUID] | None,
    source_scope: Literal["ml_backend", "external_import", "all"],
) -> PurgeImportedPredictionsData:
    data = await prediction_record_command(
        db,
        PurgeImportedPredictions(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="purge_import_predictions",
            project_id=project_id,
            task_ids=task_ids,
            source_scope=source_scope,
        ),
    )
    assert isinstance(data, PurgeImportedPredictionsData)
    _expunge_deleted_rows(
        db,
        deleted_predictions=data.deleted_predictions,
        deleted_failed_prediction_ids=[],
        deleted_prediction_meta_ids=data.deleted_prediction_meta_ids,
    )
    if data.total:
        await db.flush()
    return data


async def delete_prediction_records_for_tasks(
    db: AsyncSession, *, task_ids: list[uuid.UUID]
) -> DeletePredictionData:
    data = await prediction_record_command(
        db,
        DeletePredictionsForTasks(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="delete_for_tasks",
            task_ids=task_ids,
        ),
    )
    assert isinstance(data, DeletePredictionData)
    sync_deleted_prediction_records(db, data)
    return data


async def delete_prediction_records_for_batch(
    db: AsyncSession, *, batch_id: uuid.UUID
) -> DeletePredictionData:
    data = await prediction_record_command(
        db,
        DeletePredictionsForBatch(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="delete_for_batch",
            batch_id=batch_id,
        ),
    )
    assert isinstance(data, DeletePredictionData)
    sync_deleted_prediction_records(db, data)
    return data


async def create_prediction_record_failed_state(
    db: AsyncSession,
    *,
    failure_id: uuid.UUID,
    task_id: uuid.UUID | None,
    project_id: uuid.UUID,
    ml_backend_id: uuid.UUID | None,
    ml_backend_pool_id: uuid.UUID | None,
    model_version: str | None,
    error_type: str,
    message: str,
    extra: dict,
    execution_snapshot_id: uuid.UUID | None = None,
) -> datetime:
    data = await prediction_record_command(
        db,
        CreateFailedPrediction(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="create_failed",
            failure_id=failure_id,
            task_id=task_id,
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            ml_backend_pool_id=ml_backend_pool_id,
            model_version=model_version,
            error_type=error_type,
            message=message,
            extra=extra,
            execution_snapshot_id=execution_snapshot_id,
        ),
    )
    assert isinstance(data, CreateFailedPredictionData)
    return data.created_at


__all__ = [
    "create_prediction_record_failed_state",
    "delete_prediction_records_for_batch",
    "delete_prediction_records_for_tasks",
    "prediction_record_command",
    "purge_imported_prediction_records",
    "reject_prediction_candidate",
    "set_failed_prediction_dismissed",
    "sync_deleted_prediction_records",
]
