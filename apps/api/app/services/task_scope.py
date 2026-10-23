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
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.services.content_snapshot import ContentError

    query = (
        select(Task.id, Task.batch_id)
        .where(ids_clause(Task.id, task_ids))
        .order_by(Task.id)
    )
    before = dict((await db.execute(query)).all())
    batch_ids = sorted({batch_id for batch_id in before.values() if batch_id}, key=str)
    if batch_ids:
        await db.execute(
            select(TaskBatch.id)
            .where(ids_clause(TaskBatch.id, batch_ids))
            .order_by(TaskBatch.id)
            .with_for_update(read=True, nowait=True)
        )
    after = dict(
        (await db.execute(query.with_for_update(read=True, nowait=True))).all()
    )
    if before != after or set(after) != set(task_ids):
        raise ContentError(error_code)
