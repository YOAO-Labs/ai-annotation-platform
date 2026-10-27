"""Database-free SQLAlchemy characterization for Jobs column refresh routing."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, attributes, loading, with_loader_criteria

from app.db.jobs_lifecycle import _retarget_job_column_load
from app.db.models.async_job import AsyncJob


class _RefreshResult:
    def __init__(self, row: AsyncJob) -> None:
        self._row = row

    def unique(self):
        return self

    def scalars(self):
        return self

    def one(self):
        return self._row


class _CaptureSession:
    def __init__(self, row: AsyncJob) -> None:
        self.row = row
        self.statement = None
        self.parameters = None
        self.execution_options = None

    def execute(
        self,
        statement,
        params=None,
        *,
        execution_options=None,
        bind_arguments=None,
    ):
        self.statement = statement
        self.parameters = params
        self.execution_options = execution_options
        return _RefreshResult(self.row)


def _refresh_query(*, only_load_props=None, with_for_update=None):
    job_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    row = AsyncJob(kind="refresh-probe")
    attributes.set_committed_value(row, "id", job_id)
    state = inspect(row)
    state.key = inspect(AsyncJob).identity_key_from_primary_key((job_id,))
    session = _CaptureSession(row)

    # shortcut: characterize SQLAlchemy 2.0.49's refresh shape; rebaseline on lock upgrades.
    loading.load_on_pk_identity(
        session,
        select(AsyncJob),
        (job_id,),
        refresh_state=state,
        only_load_props=only_load_props,
        with_for_update=with_for_update,
        require_pk_cols=True,
    )
    assert session.statement is not None
    assert session.parameters is not None
    return row, session.statement, session.parameters


def _execute_state(statement, parameters, *, column_load=True, dialect="postgresql"):
    session = SimpleNamespace(
        get_bind=lambda mapper: SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    )
    return SimpleNamespace(
        is_column_load=column_load,
        is_select=True,
        all_mappers=[inspect(AsyncJob)],
        statement=statement,
        parameters=parameters,
        session=session,
    )


def test_jobs_column_loader_is_registered_for_sync_session():
    assert event.contains(Session, "do_orm_execute", _retarget_job_column_load)


@pytest.mark.parametrize(
    "only_load_props",
    [
        {"updated_at"},
        {"status", "payload"},
        None,
    ],
    ids=["one-expired-column", "partial-refresh-order", "whole-row-refresh"],
)
def test_column_refresh_uses_read_job_without_changing_projection(only_load_props):
    _row, original, parameters = _refresh_query(only_load_props=only_load_props)
    original_parameters = dict(parameters)
    execute_state = _execute_state(original, parameters)

    assert _retarget_job_column_load(execute_state) is None
    rewritten = execute_state.statement
    original_sql = str(original.compile(dialect=postgresql.dialect()))
    rewritten_sql = str(rewritten.compile(dialect=postgresql.dialect()))
    original_projection = original_sql.split("\nFROM ", 1)[0]
    rewritten_projection = rewritten_sql.split("\nFROM ", 1)[0]

    assert rewritten_projection == original_projection
    assert "FROM async_jobs" not in rewritten_sql
    assert "FROM aap_jobs_v1.read_job(%(pk_1)s::UUID) AS job" in rewritten_sql
    assert (
        "aap_jobs_v1.read_job_execution_snapshot(%(pk_1)s::UUID) "
        "AS execution_snapshot_id" in rewritten_sql
    )
    assert "aap_jobs_v1.read_job_progress(%(pk_1)s::UUID) AS progress" in rewritten_sql
    assert ") AS async_jobs" in rewritten_sql
    assert "WHERE async_jobs.id = %(pk_1)s::UUID" in rewritten_sql
    assert set(rewritten.compile(dialect=postgresql.dialect()).params) == set(
        parameters
    )
    assert execute_state.parameters == original_parameters
    assert (
        rewritten._compile_options._only_load_props
        == original._compile_options._only_load_props
    )
    assert rewritten._compile_options._for_refresh_state is True
    assert inspect(rewritten.column_descriptions[0]["entity"]).mapper is inspect(
        AsyncJob
    )

    asyncpg_compiled = rewritten.compile(dialect=postgresql.asyncpg.dialect())
    assert "read_job($1::UUID) AS job" in str(asyncpg_compiled)
    assert "read_job_execution_snapshot($1::UUID) AS execution_snapshot_id" in str(
        asyncpg_compiled
    )
    assert "WHERE async_jobs.id = $1::UUID" in str(asyncpg_compiled)
    assert asyncpg_compiled.positiontup == ["pk_1"]


def test_locked_refresh_stays_on_original_statement():
    _row, original, parameters = _refresh_query(
        only_load_props={"updated_at"}, with_for_update={"read": True}
    )
    execute_state = _execute_state(original, parameters)

    assert _retarget_job_column_load(execute_state) is None
    assert execute_state.statement is original


def test_non_column_load_stays_on_original_statement():
    _row, original, parameters = _refresh_query(only_load_props={"updated_at"})
    execute_state = _execute_state(original, parameters, column_load=False)

    assert _retarget_job_column_load(execute_state) is None
    assert execute_state.statement is original


def test_refresh_without_mapped_primary_key_bind_stays_on_original_statement():
    _row, original, _parameters = _refresh_query(only_load_props={"updated_at"})
    execute_state = _execute_state(original, {"unrelated": "value"})

    assert _retarget_job_column_load(execute_state) is None
    assert execute_state.statement is original


def test_partial_refresh_leaves_other_dirty_attributes_out_of_projection():
    row, original, parameters = _refresh_query(only_load_props={"updated_at"})
    row.status = "local-unflushed-state"
    execute_state = _execute_state(original, parameters)

    assert _retarget_job_column_load(execute_state) is None
    rewritten = execute_state.statement
    sql = str(rewritten.compile(dialect=postgresql.dialect()))
    assert sql.split("\nFROM ", 1)[0].strip() == "SELECT async_jobs.updated_at"
    assert inspect(row).attrs.status.history.has_changes()
    assert row.status == "local-unflushed-state"


def test_refresh_with_loader_criteria_stays_on_original_statement():
    _row, original, parameters = _refresh_query(only_load_props={"updated_at"})
    original = original.options(
        with_loader_criteria(AsyncJob, lambda model: model.status != "failed")
    )
    execute_state = _execute_state(original, parameters)

    assert _retarget_job_column_load(execute_state) is None
    assert execute_state.statement is original


def test_non_postgresql_refresh_stays_on_original_statement():
    _row, original, parameters = _refresh_query(only_load_props={"updated_at"})
    execute_state = _execute_state(original, parameters, dialect="sqlite")

    assert _retarget_job_column_load(execute_state) is None
    assert execute_state.statement is original
