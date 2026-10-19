"""Compose the Labeling Mask authority without transferring its transaction owner."""

from __future__ import annotations

from contextlib import nullcontext
from collections.abc import Iterable, Mapping

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.raster_mask_contract import RasterMaskContractError

_WRITE_OPERATIONS = frozenset(
    {
        "read_mask_write_epochs",
        "begin_mask_content_write",
        "lock_mask_write_epochs",
        "register_tracker_running_mask_root",
    }
)
_GC_OPERATIONS = frozenset(
    {
        "claim_mask_gc_sweep",
        "stage_mask_gc_page",
        "claim_mask_gc_deletes",
        "finish_mask_gc_delete",
        "read_mask_gc_progress",
    }
)


def mask_fence_error_code(exc: BaseException) -> str | None:
    pending = [exc]
    seen = set()
    fenced = False
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "AM409":
            fenced = True
            message = getattr(current, "message", None)
            if message in {"content_deleting", "content_epoch_changed"}:
                return message
        for name in ("orig", "__cause__", "__context__"):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return "content_deleting" if fenced else None


def mask_fence_error(exc: BaseException):
    code = mask_fence_error_code(exc)
    if code is None:
        return None
    return RasterMaskContractError(
        status_code=409,
        reason=f"mask_{code}",
        message="Mask content changed during verification or is being collected",
        retryable=True,
    )


class SqlAlchemyMaskGcDatabase:
    """One validated native operation; write permits stay in the caller transaction."""

    def __init__(self, db, *, commit: bool, write: bool = False):
        self.db = db
        self.commit = commit
        self.allowed = _WRITE_OPERATIONS if write else _GC_OPERATIONS

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from aap_labeling_mask_gc import contract as c
        from aap_labeling_mask_gc_runtime.database import (
            prepare_native_call,
            validate_native_result,
        )

        if operation not in self.allowed:
            raise c.MaskGcFailure("invalid_request")
        statement, arguments = prepare_native_call(operation, request)
        if len(arguments) != 1:
            raise c.MaskGcFailure("invalid_request")
        try:
            # A flush/savepoint could link pending ORM objects before their permit.
            with getattr(self.db, "no_autoflush", nullcontext()):
                raw = (
                    await self.db.execute(
                        text(statement.replace("$1::jsonb", "CAST(:payload AS jsonb)")),
                        {"payload": arguments[0]},
                    )
                ).scalar_one()
                result = validate_native_result(operation, request, raw)
            if self.commit:
                await self.db.commit()
            return result
        except c.MaskGcFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if self.commit:
                await self.db.rollback()
            raise c.MaskGcFailure(
                mask_fence_error_code(exc) or "storage_unavailable"
            ) from None


class IndependentMaskGcDatabase:
    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from app.db.base import async_session

        async with async_session() as db:
            return await SqlAlchemyMaskGcDatabase(db, commit=True).call(
                operation, request
            )


def _write_failure(exc):
    if exc.code in {"content_deleting", "content_epoch_changed"}:
        raise RasterMaskContractError(
            status_code=409,
            reason=f"mask_{exc.code}",
            message="Mask content changed during verification or is being collected",
            retryable=True,
        ) from None
    raise RasterMaskContractError(
        status_code=503,
        reason="mask_storage_unavailable",
        message="Mask content authority is unavailable",
        retryable=True,
    ) from None


async def _write_epoch_call(db, operation, request, expected):
    from aap_labeling_mask_gc.contract import MaskGcFailure

    try:
        result = await SqlAlchemyMaskGcDatabase(db, commit=False, write=True).call(
            operation, request
        )
        found = {item.object_key: item.epoch for item in result.items}
        if len(found) != len(result.items) or set(found) != set(expected):
            raise MaskGcFailure("invalid_response")
        return found
    except MaskGcFailure as exc:
        _write_failure(exc)


async def read_mask_write_epochs(db, object_keys: Iterable[str]) -> dict[str, int]:
    from aap_labeling_mask_gc.contract import ReadMaskWriteEpochsRequest

    keys = sorted(set(object_keys))
    found = {}
    for offset in range(0, len(keys), 1000):
        batch = keys[offset : offset + 1000]
        found.update(
            await _write_epoch_call(
                db,
                "read_mask_write_epochs",
                ReadMaskWriteEpochsRequest(object_keys=batch),
                batch,
            )
        )
    return found


async def begin_mask_content_write(db, object_keys: Iterable[str]) -> dict[str, int]:
    from aap_labeling_mask_gc.contract import BeginMaskContentWriteRequest

    keys = sorted(set(object_keys))
    found = {}
    for offset in range(0, len(keys), 1000):
        batch = keys[offset : offset + 1000]
        found.update(
            await _write_epoch_call(
                db,
                "begin_mask_content_write",
                BeginMaskContentWriteRequest(object_keys=batch),
                batch,
            )
        )
    return found


async def lock_mask_write_epochs(db, epochs: Mapping[str, int]) -> dict[str, int]:
    from aap_labeling_mask_gc.contract import LockMaskWriteEpochsRequest, MaskWriteEpoch

    keys = sorted(epochs)
    found = {}
    for offset in range(0, len(keys), 1000):
        batch = keys[offset : offset + 1000]
        found.update(
            await _write_epoch_call(
                db,
                "lock_mask_write_epochs",
                LockMaskWriteEpochsRequest(
                    items=[
                        MaskWriteEpoch(object_key=key, epoch=epochs[key])
                        for key in batch
                    ]
                ),
                batch,
            )
        )
    return found


async def register_tracker_running_mask_root(
    db, *, job_id, task_id, object_key: str
) -> bool:
    """Attach one just-written Mask to its live tracker Job in this transaction."""
    from aap_labeling_mask_gc import contract as c

    request = c.RegisterTrackerRunningMaskRootRequest(
        job_id=job_id,
        task_id=task_id,
        object_key=object_key,
    )
    try:
        await SqlAlchemyMaskGcDatabase(db, commit=False, write=True).call(
            "register_tracker_running_mask_root", request
        )
    except c.MaskGcFailure as exc:
        if exc.code == "lease_lost":
            return False
        _write_failure(exc)
    return True
