"""Track independently committed RLE owners belonging to one test engine."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from app.db.models.raster_mask_object import RasterMaskObject, RasterMaskPublication
from app.db.models.storage_maintenance import StorageOperation


@dataclass
class RasterOwners:
    objects: set[UUID] = field(default_factory=set)
    publications: set[UUID] = field(default_factory=set)
    operations: set[UUID] = field(default_factory=set)


@contextmanager
def track_raster_owners(engine):
    """Register allocations flushed through this function-scoped test engine.

    Capture before commit, so a committed allocation is still owned if its
    subsequent upload fails. Existing owners reused by the test are not new.
    Other tests use their own engines and cannot enter this registry.
    """
    created = RasterOwners()

    def register(session, _flush_context):
        if session.get_bind() is engine.sync_engine:
            for row in session.new:
                if isinstance(row, RasterMaskObject):
                    created.objects.add(row.id)
                elif isinstance(row, RasterMaskPublication):
                    created.publications.add(row.id)

    def register_operation(
        _connection, _cursor, _statement, _parameters, context, _many
    ):
        compiled = context.compiled
        if compiled is None or not getattr(compiled.statement, "is_insert", False):
            return
        if compiled.statement.table is not StorageOperation.__table__:
            return
        for values in context.compiled_parameters:
            if str(values.get("purpose", "")).startswith("raster_object_"):
                created.operations.add(UUID(str(values["id"])))

    event.listen(Session, "after_flush", register)
    event.listen(engine.sync_engine, "after_cursor_execute", register_operation)
    try:
        yield created
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", register_operation)
        event.remove(Session, "after_flush", register)


async def remove_test_raster_owners(engine, created):
    # Delete only registered operation and publication IDs, including guards
    # attached to reused owners. Never remove another test's guard.
    async with async_sessionmaker(engine)() as db:
        await db.execute(
            delete(StorageOperation).where(StorageOperation.id.in_(created.operations))
        )
        await db.execute(
            delete(RasterMaskPublication).where(
                RasterMaskPublication.id.in_(created.publications)
            )
        )
        foreign_guard = await db.scalar(
            select(RasterMaskPublication.id)
            .where(RasterMaskPublication.object_id.in_(created.objects))
            .limit(1)
        )
        if foreign_guard is not None:
            raise RuntimeError("raster owner still has an unowned publication")
        await db.execute(
            text(
                "UPDATE raster_mask_objects SET state='retired', "
                "retired_at=greatest(now(),created_at,published_at,last_used_at) "
                "WHERE id=ANY(:ids) AND state<>'retired'"
            ),
            {"ids": list(created.objects)},
        )
        await db.execute(
            delete(RasterMaskObject).where(RasterMaskObject.id.in_(created.objects))
        )
        await db.commit()
