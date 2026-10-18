"""Retry exact committed Assets storage deletions without provider table access."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.asset_storage_cleanup_contract import AssetStorageDeletion
from app.services.storage import StorageService

log = logging.getLogger(__name__)


class AssetStorageDeletionData:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def pending(
        self, *, operation_id: UUID | None, limit: int
    ) -> tuple[AssetStorageDeletion, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Storage cleanup limit must be 1-1000")
        rows = await self.db.execute(
            text(
                "SELECT operation_id, asset_id, bucket, prefix FROM aap_assets_v1.pending_storage_deletions(CAST(:operation AS uuid), CAST(:limit AS integer))"
            ),
            {"operation": operation_id, "limit": limit},
        )
        return tuple(
            AssetStorageDeletion.model_validate(dict(row)) for row in rows.mappings()
        )

    async def complete(self, reference: AssetStorageDeletion) -> None:
        done = await self.db.scalar(
            text(
                "SELECT aap_assets_v1.complete_storage_deletion(CAST(:operation AS uuid), CAST(:asset AS uuid))"
            ),
            {"operation": reference.operation_id, "asset": reference.asset_id},
        )
        if done is not True:
            raise ValueError(
                "Storage deletion acknowledgement did not match pending work"
            )


async def drain_asset_storage_deletions(
    db: AsyncSession,
    storage: StorageService,
    *,
    operation_id: UUID | None = None,
    limit: int = 100,
) -> dict[str, int]:
    if db.in_transaction():
        raise RuntimeError(
            "Storage cleanup must start after the business transaction commits"
        )
    data = AssetStorageDeletionData(db)
    try:
        pending = await data.pending(operation_id=operation_id, limit=limit)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    result = {"completed": 0, "failed": 0}
    for reference in pending:
        if reference.bucket != storage.media_cache_bucket:
            log.error(
                "Asset cleanup scope mismatch for operation %s asset %s",
                reference.operation_id,
                reference.asset_id,
            )
            result["failed"] += 1
            continue
        try:
            await asyncio.to_thread(
                storage.delete_prefix, reference.prefix, bucket=reference.bucket
            )
            await data.complete(reference)
            await db.commit()
            result["completed"] += 1
        except Exception:
            await db.rollback()
            result["failed"] += 1
            log.exception(
                "Asset cleanup remains pending for operation %s asset %s",
                reference.operation_id,
                reference.asset_id,
            )
    return result
