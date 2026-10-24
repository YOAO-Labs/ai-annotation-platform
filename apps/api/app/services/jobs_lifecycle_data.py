"""Borrowed-session adapter for the Jobs lifecycle and row capabilities."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import delete, inspect, or_, select, text, update
from sqlalchemy.orm.bulk_persistence import (
    BulkORMDelete,
    BulkORMUpdate,
    BulkUDCompileState,
)

from aap_jobs.row_lock_contract import JOBS_ROW_LOCK_FUNCTIONS

from app.db.jobs_lifecycle import encode_jobs_json, observe_job_changes
from app.db.models.async_job import AsyncJob, AsyncJobKind


def job_lock_statement(job_id, *, mode, populate_existing=False):
    """Keep the caller's ORM result, refresh flag and autoflush around a fixed lock."""
    function = JOBS_ROW_LOCK_FUNCTIONS[mode]
    return (
        select(AsyncJob)
        .from_statement(
            text(f"SELECT * FROM {function}(CAST(:job_id AS pg_catalog.uuid))")
        )
        .params(job_id=job_id)
        .execution_options(populate_existing=populate_existing)
    )


class JobsLifecyclePostgresData:
    def __init__(self, db):
        self.db = db

    changes = staticmethod(observe_job_changes)

    async def create_job(self, **values):
        row = AsyncJob(**values)
        self.db.add(row)
        await self.db.flush()
        return row

    async def flush(self):
        await self.db.flush()

    async def get_job(self, job_id, *, populate_existing=False):
        session = self.db.sync_session
        identity = inspect(AsyncJob).identity_key_from_primary_key((job_id,))
        cached = session.identity_map.get(identity)
        state = inspect(cached) if cached is not None else None
        if not populate_existing and state is not None and not state.expired:
            return cached
        result = await self.db.execute(
            select(AsyncJob)
            .from_statement(
                text(
                    "SELECT * FROM aap_jobs_v1.read_job("
                    "CAST(:job_id AS pg_catalog.uuid))"
                )
            )
            .execution_options(populate_existing=True),
            {"job_id": job_id},
        )
        row = result.scalar_one_or_none()
        if row is None and state is not None and state.expired:
            session._remove_newly_deleted([state])
        return row

    async def find_by_execution_id(self, execution_id):
        if not execution_id:
            return None
        result = await self.db.execute(
            select(AsyncJob).from_statement(
                text(
                    "SELECT * FROM aap_jobs_v1.jobs_by_execution_id("
                    "CAST(:execution_id AS pg_catalog.text))"
                )
            ),
            {"execution_id": execution_id},
        )
        return result.scalar_one_or_none()

    async def insert_video_summary(self, **values):
        await self._autoflush()
        await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.insert_video_summary("
                "CAST(:id AS pg_catalog.uuid), CAST(:kind AS pg_catalog.text), "
                "CAST(:user_id AS pg_catalog.uuid), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:payload AS pg_catalog.jsonb), "
                "CAST(:celery_task_id AS pg_catalog.text))"
            ),
            {**values, "payload": encode_jobs_json(values["payload"])},
        )

    async def _autoflush(self):
        if self.db.autoflush:
            await self.db.flush()

    def _synchronize(self, statement, affected_ids, *, deleting=False):
        # shortcut: Pinned SQLAlchemy cache hooks; rebaseline on upgrades.
        compiled = statement.compile()
        options = BulkUDCompileState.default_update_options + {
            "_subject_mapper": inspect(AsyncJob),
            "_identity_token": None,
            "_matched_rows": [(job_id, None) for job_id in affected_ids],
        }
        result = SimpleNamespace(
            returned_defaults_rows=None,
            context=SimpleNamespace(compiled_parameters=[compiled.construct_params()]),
            prefetch_cols=lambda: compiled.prefetch,
            postfetch_cols=lambda: compiled.postfetch,
        )
        owner = BulkORMDelete if deleting else BulkORMUpdate
        owner._do_post_synchronize_fetch(
            self.db.sync_session, statement, result, options
        )

    async def mark_dispatch_failed(self, job_id, *, failed_at, error):
        await self._autoflush()
        changed = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.mark_dispatch_failed("
                "CAST(:job_id AS pg_catalog.uuid), "
                "CAST(:failed_at AS pg_catalog.timestamptz), "
                "CAST(:error AS pg_catalog.text))"
            ),
            {"job_id": job_id, "failed_at": failed_at, "error": error},
        )
        statement = (
            update(AsyncJob)
            .where(AsyncJob.id == job_id, AsyncJob.status == "pending")
            .values(status="failed", completed_at=failed_at, error_message=error)
        )
        self._synchronize(statement, [job_id] if changed else [])
        return bool(changed)

    async def delete_batch_predict_jobs(self, batch_id, *, completed_at):
        filters = (
            AsyncJob.kind == AsyncJobKind.BATCH_PREDICT.value,
            AsyncJob.payload["batch_id"].astext == str(batch_id),
        )
        await self._autoflush()
        cancelled = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.cancel_batch_predict_jobs("
                "CAST(:batch_id AS pg_catalog.uuid), "
                "CAST(:completed_at AS pg_catalog.timestamptz))"
            ),
            {"batch_id": batch_id, "completed_at": completed_at},
        )
        self._synchronize(
            update(AsyncJob)
            .where(*filters, AsyncJob.status.in_(("pending", "running")))
            .values(status="cancelled", completed_at=completed_at),
            cancelled,
        )
        await self._autoflush()
        deleted = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.delete_batch_predict_jobs("
                "CAST(:batch_id AS pg_catalog.uuid))"
            ),
            {"batch_id": batch_id},
        )
        self._synchronize(delete(AsyncJob).where(*filters), deleted, deleting=True)
        return len(deleted)

    async def delete_terminal_jobs_before(self, cutoff, *, statuses):
        await self._autoflush()
        deleted = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.delete_terminal_jobs_before("
                "CAST(:cutoff AS pg_catalog.timestamptz), "
                "CAST(:statuses AS pg_catalog.text[]))"
            ),
            {"cutoff": cutoff, "statuses": list(statuses)},
        )
        statement = delete(AsyncJob).where(
            AsyncJob.status.in_(statuses),
            AsyncJob.created_at < cutoff,
            or_(
                AsyncJob.kind != AsyncJobKind.DATASET_IMPORT.value,
                AsyncJob.payload["import_protocol"].astext.is_distinct_from(
                    "aap-dataset-import/v1"
                ),
            ),
        )
        self._synchronize(statement, deleted, deleting=True)
        return list(deleted)
