"""Database-free checks for paged dashboard batch queries and their role scope."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.dashboard.annotator import my_batches
from app.api.v1.dashboard.reviewer import _reviewing_batches, reviewer_batches
from app.db.models.user import User


@pytest.mark.parametrize("endpoint", [my_batches, reviewer_batches])
@pytest.mark.parametrize("role", ["super_admin", "employee"])
async def test_batch_pages_apply_stable_order_and_preserve_scope(endpoint, role):
    result = MagicMock()
    result.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    user = User(id=uuid.uuid4(), role=role, is_active=True)

    assert await endpoint(limit=10, offset=120, db=db, current_user=user) == []

    query = db.execute.call_args.args[0]
    sql = str(query.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 10 OFFSET 120" in sql
    assert "task_batches.id" in str(query._order_by_clause)
    if role == "employee":
        assert "project_members" in sql
        assert "users.is_active" in sql
        assert "annotator" in sql if endpoint is my_batches else "reviewer" in sql


async def test_reviewer_snapshot_retains_compatible_first_hundred_default():
    result = MagicMock()
    result.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    user = User(id=uuid.uuid4(), role="super_admin", is_active=True)
    assert await _reviewing_batches(db, user) == []
    sql = str(
        db.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "LIMIT 100 OFFSET 0" in sql


@pytest.mark.parametrize("kind", ["datasets", "dataset_items", "bug_reports"])
async def test_created_time_offset_lists_use_id_to_break_equal_timestamps(kind):
    from app.services.bug_report import BugReportService
    from app.services.dataset import DatasetService

    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar.return_value = 0
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    if kind == "datasets":
        assert await DatasetService(db).list(limit=10, offset=20) == ([], 0)
    elif kind == "dataset_items":
        assert await DatasetService(db).list_items(
            uuid.uuid4(), limit=10, offset=20
        ) == ([], 0)
    else:
        assert await BugReportService(db).list(limit=10, offset=20) == ([], 0)
    query = next(
        call.args[0]
        for call in db.execute.call_args_list
        if len(call.args[0]._order_by_clause)
    )
    order = str(query._order_by_clause)
    assert order == f"{kind}.created_at DESC, {kind}.id"
    sql = str(query.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 10 OFFSET 20" in sql
