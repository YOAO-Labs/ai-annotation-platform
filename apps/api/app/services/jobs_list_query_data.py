"""Borrowed-session binding for the fixed Jobs list query functions."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select, text

from yoao_jobs.async_job_list_query_contract import AsyncJobListQuery


class JobsListQuerySession(Protocol):
    async def execute(self, statement, params=None) -> Any: ...


class JobsListQueryPostgresData:
    """Call page then total while retaining ORM autoflush and identity behavior."""

    def __init__(self, session: JobsListQuerySession, async_job_model):
        self.session = session
        self.async_job_model = async_job_model

    @staticmethod
    def _params(
        query: AsyncJobListQuery,
        *,
        include_page: bool,
        include_platform_storage: bool = False,
    ) -> dict[str, Any]:
        params = {
            "actor_id": query.actor_id,
            "platform_role": query.platform_role,
            "statuses": query.statuses,
            "kinds": query.kinds,
            "project_id": query.project_id,
            "search_pattern": query.search_pattern,
        }
        if include_page:
            params.update({"limit": query.limit, "offset": query.offset})
        if include_platform_storage:
            params["include_platform_storage"] = True
        return params

    async def page(
        self, query: AsyncJobListQuery, *, include_platform_storage: bool = False
    ) -> list[Any]:
        if include_platform_storage:
            page_call = (
                "yoao_jobs_v1.async_job_list_page_scoped("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text), "
                "CAST(:statuses AS pg_catalog.text[]), "
                "CAST(:kinds AS pg_catalog.text[]), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:search_pattern AS pg_catalog.text), "
                "CAST(:limit AS pg_catalog.int4), "
                "CAST(:offset AS pg_catalog.int4), "
                "CAST(:include_platform_storage AS pg_catalog.bool))"
            )
        else:
            page_call = (
                "yoao_jobs_v1.async_job_list_page("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text), "
                "CAST(:statuses AS pg_catalog.text[]), "
                "CAST(:kinds AS pg_catalog.text[]), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:search_pattern AS pg_catalog.text), "
                "CAST(:limit AS pg_catalog.int4), "
                "CAST(:offset AS pg_catalog.int4))"
            )
        statement = (
            select(self.async_job_model)
            .from_statement(
                text(
                    "SELECT job.*, "
                    "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                    "AS execution_snapshot_id, progress.last_progress_at, "
                    f"progress.progress_observation FROM {page_call} AS job "
                    "CROSS JOIN LATERAL "
                    "yoao_jobs_v1.read_job_progress(job.id) AS progress"
                )
            )
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(
            statement,
            self._params(
                query,
                include_page=True,
                include_platform_storage=include_platform_storage,
            ),
        )
        return list(result.scalars().all())

    async def total(
        self, query: AsyncJobListQuery, *, include_platform_storage: bool = False
    ) -> int:
        if include_platform_storage:
            total_call = (
                "SELECT yoao_jobs_v1.async_job_list_total_scoped("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text), "
                "CAST(:statuses AS pg_catalog.text[]), "
                "CAST(:kinds AS pg_catalog.text[]), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:search_pattern AS pg_catalog.text), "
                "CAST(:include_platform_storage AS pg_catalog.bool)) "
                "AS async_job_list_total"
            )
        else:
            total_call = (
                "SELECT yoao_jobs_v1.async_job_list_total("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text), "
                "CAST(:statuses AS pg_catalog.text[]), "
                "CAST(:kinds AS pg_catalog.text[]), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:search_pattern AS pg_catalog.text))"
            )
        statement = select(
            func.count(self.async_job_model.id).label("async_job_list_total")
        ).from_statement(text(total_call))
        result = await self.session.execute(
            statement,
            self._params(
                query,
                include_page=False,
                include_platform_storage=include_platform_storage,
            ),
        )
        return int(result.scalar_one())
