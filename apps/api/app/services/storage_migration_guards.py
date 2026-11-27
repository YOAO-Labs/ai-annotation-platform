"""Read and lock the persisted state that fences storage migration transitions."""

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.storage_migration_cutovers import StorageMigrationCutover as Cutover
from app.db.models.storage_migration_read_publication import (
    StorageMigrationReadPublication as Publication,
)
from app.schemas.storage_migration_window import StorageMigrationFinalizationItem
from app.services import storage_access

ACTIVE_PUBLICATIONS = ("active", "withdrawing")


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


async def public_for_window(db, window_id):
    if db.autoflush:
        await db.flush()
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.migration_finalization_for_window(:id)"
                ),
                {"id": window_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return (
        StorageMigrationFinalizationItem.model_validate(row)
        if row is not None
        else None
    )


async def require_window_open(db, window_id, *, closing=False):
    if db.autoflush:
        await db.flush()
    if await db.scalar(
        text(
            "SELECT yoao_assets_v1.migration_finalization_blocks_window(:id, :closing)"
        ),
        {"id": window_id, "closing": closing},
    ):
        reject("finalization_active" if closing else "finalization_closing")


async def require_resumable(db, location_id):
    if db.autoflush:
        await db.flush()
    if await db.scalar(
        text("SELECT yoao_assets_v1.migration_finalization_blocks_source_resume(:id)"),
        {"id": location_id},
    ):
        reject("finalization_active")


async def frozen_copy(db, copy_id):
    if db.autoflush:
        await db.flush()
    return await db.scalar(
        text("SELECT yoao_assets_v1.migration_copy_is_frozen(:id)"), {"id": copy_id}
    )


async def require_unpublished(db, finalization_id):
    if await db.scalar(
        select(Publication.id).where(
            Publication.id == finalization_id,
            Publication.state.in_(ACTIVE_PUBLICATIONS),
        )
    ):
        reject("read_publication_active")


async def cutover_owner(db, cutover_id, *, lock=False, required=True):
    query = select(Cutover).where(Cutover.id == cutover_id)
    if lock:
        query = query.with_for_update(nowait=True)
    try:
        row = await db.scalar(query.execution_options(populate_existing=True))
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("cutover_busy")
        raise
    if row is None and required:
        reject("cutover_unknown", 404)
    return row


async def require_target_resumable(db, location_id):
    if db.autoflush:
        await db.flush()
    if await db.scalar(
        text("SELECT yoao_assets_v1.migration_cutover_blocks_target_resume(:id)"),
        {"id": location_id},
    ):
        reject("cutover_active")


async def require_finalization_invalidatable(db, finalization_id):
    row = await cutover_owner(db, finalization_id, lock=True, required=False)
    if row is not None and row.state not in ("rollback_restoring", "rolled_back"):
        reject("cutover_active")
