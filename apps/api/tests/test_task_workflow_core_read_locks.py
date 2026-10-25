from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy import select

from app.application import task_workflow_locks as workflow_locks
from app.application.task_workflow_errors import TaskWorkflowError
from app.services.feedback import unresolved_issue_count_sq
from app.services.labeling_core_read_relations import task_row
from app.services.task_discussion import task_comment_count_sq


class _Session:
    no_autoflush = nullcontext()


@pytest.mark.asyncio
async def test_task_target_keeps_project_batch_task_lock_order_and_scope(monkeypatch):
    task_id, project_id, batch_id, actor_id = (uuid.uuid4() for _ in range(4))
    actor = SimpleNamespace(id=actor_id)
    project = SimpleNamespace(id=project_id)
    access = SimpleNamespace()
    task = SimpleNamespace(id=task_id, project_id=project_id, batch_id=batch_id)
    events = []

    async def read_target(_db, statement):
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        assert "aap_labeling_v1.task_rows" in compiled
        assert "FROM public.tasks" not in compiled
        events.append("target")
        return SimpleNamespace(one_or_none=lambda: (project_id, batch_id))

    async def lock_project(_db, **_kwargs):
        events.append("project")
        return actor, project, access

    async def lock_batch(_db, batch_ids, **kwargs):
        events.append("batch")
        assert batch_ids == [batch_id]
        assert kwargs == {
            "project_id": project_id,
            "lock_mode": "update",
            "nowait": True,
            "populate_existing": True,
        }
        return [SimpleNamespace(id=batch_id)]

    async def lock_task(_db, requested_task_id, **kwargs):
        events.append("task")
        assert requested_task_id == task_id
        assert kwargs == {
            "project_id": project_id,
            "lock_mode": "update",
            "nowait": True,
            "populate_existing": True,
        }
        return task

    monkeypatch.setattr(workflow_locks, "execute_core_read", read_target)
    monkeypatch.setattr(workflow_locks, "lock_current_project_access", lock_project)
    monkeypatch.setattr(
        workflow_locks.task_access, "assert_access_binding", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(workflow_locks, "lock_batch_rows_for_core_read", lock_batch)
    monkeypatch.setattr(workflow_locks, "lock_task_for_core_read", lock_task)

    (
        locked,
        locked_actor,
        _locked_project,
        locked_access,
    ) = await workflow_locks.lock_current_task_target(
        _Session(),
        task_id=task_id,
        actor_id=actor_id,
        expected_access=access,
    )

    assert events == ["target", "project", "batch", "task"]
    assert locked is task
    assert locked_actor is actor
    assert locked_access is access


@pytest.mark.asyncio
async def test_task_target_rejects_batch_change_after_fixed_locks(monkeypatch):
    task_id, project_id, batch_id, actor_id = (uuid.uuid4() for _ in range(4))
    actor = SimpleNamespace(id=actor_id)
    project = SimpleNamespace(id=project_id)
    access = SimpleNamespace()
    monkeypatch.setattr(
        workflow_locks, "execute_core_read", _result((project_id, batch_id))
    )
    monkeypatch.setattr(
        workflow_locks,
        "lock_current_project_access",
        _project_lock(actor, project, access),
    )
    monkeypatch.setattr(
        workflow_locks.task_access,
        "assert_access_binding",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        workflow_locks,
        "lock_batch_rows_for_core_read",
        _batch_lock(batch_id),
    )
    monkeypatch.setattr(
        workflow_locks,
        "lock_task_for_core_read",
        _task_lock(SimpleNamespace(id=task_id, project_id=project_id, batch_id=None)),
    )

    with pytest.raises(TaskWorkflowError) as raised:
        await workflow_locks.lock_current_task_target(
            _Session(),
            task_id=task_id,
            actor_id=actor_id,
            expected_access=access,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == {"reason": "task_target_changed"}


def _result(value):
    async def read(_db, _statement):
        return SimpleNamespace(one_or_none=lambda: value)

    return read


def _project_lock(actor, project, access):
    async def lock(_db, **_kwargs):
        return actor, project, access

    return lock


def _batch_lock(batch_id):
    async def lock(_db, batch_ids, **_kwargs):
        return [SimpleNamespace(id=batch_id)] if batch_ids == [batch_id] else []

    return lock


def _task_lock(task):
    async def lock(_db, _task_id, **_kwargs):
        return task

    return lock


def test_discussion_counts_correlate_only_to_public_labeling_rows():
    statement = select(
        task_row.id,
        unresolved_issue_count_sq(),
        task_comment_count_sq(),
    ).select_from(task_row)
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "aap_labeling_v1.task_rows" in sql
    assert "aap_labeling_v1.annotation_rows" in sql
    assert "FROM aap_labeling_v1.task_rows AS root_task" in sql
    assert "FROM aap_labeling_v1.annotation_rows AS root_annotation" in sql
    assert "FROM tasks AS root_task" not in sql
    assert "FROM annotations AS root_annotation" not in sql
