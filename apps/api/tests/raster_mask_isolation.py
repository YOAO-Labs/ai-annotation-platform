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
    committing = {}

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

    def register_insert(connection, cursor, sql, _parameters, context, _many):
        if sql.startswith("SELECT yoao_labeling_v1.stage_raster_object("):
            current(connection).objects.add(context.compiled_parameters[0]["id"])
            return
        if sql.startswith("SELECT yoao_labeling_v1.open_raster_publication("):
            current(connection).publications.add(context.compiled_parameters[0]["id"])
            return
        if sql.startswith("SELECT yoao_assets_storage_v1.begin_physical_io("):
            values = context.compiled_parameters[0]
            if values["purpose"].startswith("raster_object_"):
                current(connection).operations.add(values["operation"])
            return
        compiled = context.compiled
        if compiled is None or not getattr(compiled.statement, "is_insert", False):
            return
        statement = compiled.statement
        if statement.table._deannotate() is not StorageOperation.__table__:
            return
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

    def begin(connection):
        committing.pop(connection, None)

    def commit(connection):
        owned = RasterOwners()
        for scope in pending.pop(connection, []):
            merge(owned, scope)
        owned.objects.difference_update(created.objects)
        owned.publications.difference_update(created.publications)
        owned.operations.difference_update(created.operations)
        committing[connection] = owned
        merge(created, owned)

    def commit_failed(context):
        if context.execution_context is not None:
            return
        owned = committing.pop(context.connection, None)
        if owned is not None:
            created.objects.difference_update(owned.objects)
            created.publications.difference_update(owned.publications)
            created.operations.difference_update(owned.operations)

    def rollback(connection):
        pending.pop(connection, None)
        committing.pop(connection, None)

    listeners = (
        ("begin", begin),
        ("after_cursor_execute", register_insert),
        ("savepoint", savepoint),
        ("release_savepoint", release_savepoint),
        ("rollback_savepoint", rollback_savepoint),
        ("commit", commit),
        ("rollback", rollback),
        ("handle_error", commit_failed),
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
