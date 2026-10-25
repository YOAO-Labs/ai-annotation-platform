import uuid

import pytest

from sqlalchemy import func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm.util import identity_key

from aap_labeling.core_read_contract import CORE_READ_COLUMNS, CORE_READ_RELATIONS
from app.db.models.task import Task
from app.services.labeling_core_read_relations import (
    annotation_row,
    batch_row,
    failed_prediction_row,
    get_core_row,
    task_dataset_item_link_row,
    task_lock_row,
    prediction_meta_row,
    prediction_row,
    task_row,
    lock_annotation_rows_for_core_read,
    lock_batch_rows_for_core_read,
    lock_task_for_core_read,
)


def test_core_rows_bind_only_the_published_relation_columns():
    rows = {
        "task_rows": task_row,
        "batch_rows": batch_row,
        "annotation_rows": annotation_row,
        "prediction_rows": prediction_row,
        "prediction_meta_rows": prediction_meta_row,
        "failed_prediction_rows": failed_prediction_row,
        "task_lock_rows": task_lock_row,
        "task_dataset_item_link_rows": task_dataset_item_link_row,
    }

    assert tuple(rows) == CORE_READ_RELATIONS
    for relation, row in rows.items():
        selectable = inspect(row).selectable
        assert selectable.schema == "aap_labeling_v1"
        assert tuple(selectable.c.keys()) == CORE_READ_COLUMNS[relation]


def test_composed_task_annotation_read_stays_on_labeling_relations():
    statement = (
        select(task_row.id, task_row.sequence_order, func.count(annotation_row.id))
        .select_from(task_row)
        .outerjoin(batch_row, batch_row.id == task_row.batch_id)
        .outerjoin(annotation_row, annotation_row.task_id == task_row.id)
        .where(task_row.project_id == uuid.UUID("00000000-0000-0000-0000-000000000001"))
        .group_by(task_row.id)
        .order_by(func.coalesce(task_row.sequence_order, 2147483647), task_row.id)
        .limit(50)
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "aap_labeling_v1.task_rows" in sql
    assert "aap_labeling_v1.batch_rows" in sql
    assert "aap_labeling_v1.annotation_rows" in sql
    assert "FROM public.tasks" not in sql
    assert "FROM public.annotations" not in sql


class _ReadResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _ReadSession:
    autoflush = False

    def __init__(self):
        self.sql = []

    async def execute(self, statement):
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))
        return _ReadResult()


@pytest.mark.asyncio
async def test_row_locks_use_only_fixed_owner_lock_functions():
    session = _ReadSession()
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    await lock_task_for_core_read(session, row_id, lock_mode="share", nowait=True)
    await lock_batch_rows_for_core_read(
        session, [row_id], project_id=row_id, nowait=True
    )
    await lock_annotation_rows_for_core_read(
        session, [row_id], task_id=row_id, project_id=row_id, nowait=True
    )

    assert "aap_labeling_v1.lock_task_for_core_read" in session.sql[0]
    assert "aap_labeling_v1.lock_batch_rows_for_core_read" in session.sql[1]
    assert "aap_labeling_v1.lock_annotation_rows_for_core_read" in session.sql[2]
    assert all("FOR UPDATE" not in statement for statement in session.sql)


@pytest.mark.asyncio
async def test_get_core_row_keeps_an_unexpired_identity_map_hit():
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    task = Task(
        id=row_id,
        project_id=uuid.uuid4(),
        display_id="T-1",
        file_name="x",
        file_path="x",
    )

    class IdentityMapSession:
        identity_map = {identity_key(Task, ident=row_id): task}

    assert await get_core_row(IdentityMapSession(), task_row, row_id) is task


@pytest.mark.asyncio
async def test_get_core_row_queries_the_public_relation_when_uncached():
    session = _ReadSession()
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    session.identity_map = {}

    assert await get_core_row(session, task_row, row_id) is None
    assert "FROM aap_labeling_v1.task_rows" in session.sql[0]
    assert "FROM public.tasks" not in session.sql[0]
