"""Remove this test's independently committed RLE owners after caller rollback."""

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.raster_mask_object import RasterMaskObject, RasterMaskPublication
from app.db.models.storage_maintenance import StorageOperation


async def owned_raster_ids(engine):
    async with engine.connect() as connection:
        return set(await connection.scalars(select(RasterMaskObject.id)))


async def remove_test_raster_owners(engine, before):
    # The conftest target guard and the worktree launcher establish a disposable
    # database. Do not disable owner triggers or remove records predating a test.
    async with async_sessionmaker(engine)() as db:
        created = set(await db.scalars(select(RasterMaskObject.id))) - before
        if not created:
            return
        purposes = [
            f"raster_object_{operation}_{identifier.hex}"
            for identifier in created
            for operation in ("read", "write", "delete")
        ]
        await db.execute(
            delete(StorageOperation).where(StorageOperation.purpose.in_(purposes))
        )
        await db.execute(
            delete(RasterMaskPublication).where(
                RasterMaskPublication.object_id.in_(created)
            )
        )
        await db.execute(
            text(
                "UPDATE raster_mask_objects SET state='retired', "
                "retired_at=greatest(now(),created_at,published_at,last_used_at) "
                "WHERE id=ANY(:ids) AND state<>'retired'"
            ),
            {"ids": list(created)},
        )
        await db.execute(
            delete(RasterMaskObject).where(RasterMaskObject.id.in_(created))
        )
        await db.commit()
