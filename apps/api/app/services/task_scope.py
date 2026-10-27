"""Bounded SQL parameters and batch-before-Task locks for frozen selections."""

from sqlalchemy import any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID


def ids_clause(column, identities):
    identities = list(identities)
    if len(identities) <= 1000:
        return column.in_(identities)
    # A whole-project selection may exceed asyncpg's bind-parameter limit.
    return column == any_(bindparam(None, identities, type_=ARRAY(UUID(as_uuid=True))))


async def lock_task_scope(db, task_ids, *, error_code="task_scope_changed"):
    """Lock batch visibility before Tasks, and reject a reassignment in between."""
    from app.services.content_snapshot import ContentError
    from app.services.labeling_core_read_relations import (
        lock_batch_rows_for_core_read,
        lock_task_rows_for_core_read_share,
        task_row,
    )

    query = (
        select(task_row.id, task_row.batch_id)
        .where(ids_clause(task_row.id, task_ids))
        .order_by(task_row.id)
    )
    before = dict((await db.execute(query)).all())
    batch_ids = sorted({batch_id for batch_id in before.values() if batch_id}, key=str)
    if batch_ids:
        await lock_batch_rows_for_core_read(
            db, batch_ids, lock_mode="share", nowait=True
        )
    after = {
        row.id: row.batch_id
        for row in await lock_task_rows_for_core_read_share(db, list(task_ids))
    }
    if before != after or set(after) != set(task_ids):
        raise ContentError(error_code)
