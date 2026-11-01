"""Track independently committed RLE owners belonging to one test engine."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import delete, event, select, text
from sqlalchemy.dialects.postgresql.dml import OnConflictDoUpdate
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
    """Track inserts committed by this engine, including independent allocations."""
    created = RasterOwners()
    pending = {}

    def current(connection):
        return pending.setdefault(connection, [RasterOwners()])[-1]

    def merge(target, source):
        target.objects.update(source.objects)
        target.publications.update(source.publications)
        target.operations.update(source.operations)

    def register(session, _flush_context):
        bind = session.get_bind()
        if bind.engine is not engine.sync_engine:
            return
        owned = current(session.connection())
        for row in session.new:
            if isinstance(row, RasterMaskObject):
                owned.objects.add(row.id)
            elif isinstance(row, RasterMaskPublication):
                owned.publications.add(row.id)

    def register_operation(connection, cursor, _statement, _parameters, context, _many):
        compiled = context.compiled
        if compiled is None or not getattr(compiled.statement, "is_insert", False):
            return
        statement = compiled.statement
        if statement.table._deannotate() is not StorageOperation.__table__:
            return
        # The permit path inserts one explicit ID. A no-op conflict inserts none;
        # an upsert update and a mixed multi-row insert cannot establish ownership.
        if (
            cursor.rowcount != 1
            or len(context.compiled_parameters) != 1
            or statement._multi_values
            or isinstance(statement._post_values_clause, OnConflictDoUpdate)
        ):
            return
        values = context.compiled_parameters[0]
        if str(values.get("purpose", "")).startswith("raster_object_"):
            current(connection).operations.add(UUID(str(values["id"])))

    def savepoint(connection, _name):
        pending.setdefault(connection, [RasterOwners()]).append(RasterOwners())

    def release_savepoint(connection, _name, _context):
        stack = pending[connection]
        merge(stack[-2], stack.pop())

    def rollback_savepoint(connection, _name, _context):
        pending[connection].pop()

    def commit(connection):
        stack = pending.pop(connection, [])
        for owned in stack:
            merge(created, owned)

    def rollback(connection):
        pending.pop(connection, None)

    listeners = (
        ("after_cursor_execute", register_operation),
        ("savepoint", savepoint),
        ("release_savepoint", release_savepoint),
        ("rollback_savepoint", rollback_savepoint),
        ("commit", commit),
        ("rollback", rollback),
    )
    event.listen(Session, "after_flush", register)
    for name, listener in listeners:
        event.listen(engine.sync_engine, name, listener)
    try:
        yield created
    finally:
        for name, listener in listeners:
            event.remove(engine.sync_engine, name, listener)
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
