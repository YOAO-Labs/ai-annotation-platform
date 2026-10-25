from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import dialect

from app.application.project_task_cleanup import _task_ids_query


def test_bug_report_task_relation_is_a_fixed_typed_uuid_set():
    task_ids = [uuid.uuid4(), uuid.uuid4()]
    relation = _task_ids_query(task_ids)
    compiled = select(relation.c.id).compile(dialect=dialect())

    assert "unnest" in str(compiled).lower()
    assert "uuid[]" in str(compiled).lower()
    assert compiled.params["task_ids"] == task_ids
