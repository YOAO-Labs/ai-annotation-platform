"""Borrowed-session binding for the fixed Jobs list query functions."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select, text

from aap_jobs.async_job_list_query_contract import AsyncJobListQuery


class JobsListQuerySession(Protocol):
    async def execute(self, statement, params=None) -> Any: ...


class JobsListQueryPostgresData:
    """Call page then total while retaining ORM autoflush and identity behavior."""

    def __init__(self, session: JobsListQuerySession, async_job_model):
        self.session = session
        self.async_job_model = async_job_model

    @staticmethod
    def _params(query: AsyncJobListQuery, *, include_page: bool) -> dict[str, Any]:
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
        return params

    async def page(self, query: AsyncJobListQuery) -> list[Any]:
        statement = (
            select(self.async_job_model)
            .from_statement(
                text(
                    "SELECT * FROM aap_jobs_v1.async_job_list_page("
                    "CAST(:actor_id AS pg_catalog.uuid), "
                    "CAST(:platform_role AS pg_catalog.text), "
                    "CAST(:statuses AS pg_catalog.text[]), "
                    "CAST(:kinds AS pg_catalog.text[]), "
                    "CAST(:project_id AS pg_catalog.uuid), "
                    "CAST(:search_pattern AS pg_catalog.text), "
                    "CAST(:limit AS pg_catalog.int4), "
                    "CAST(:offset AS pg_catalog.int4))"
                )
            )
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(
            statement,
            self._params(query, include_page=True),
        )
        return list(result.scalars().all())

    async def total(self, query: AsyncJobListQuery) -> int:
        statement = select(
            func.count(self.async_job_model.id).label("async_job_list_total")
        ).from_statement(
            text(
                "SELECT aap_jobs_v1.async_job_list_total("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:platform_role AS pg_catalog.text), "
                "CAST(:statuses AS pg_catalog.text[]), "
                "CAST(:kinds AS pg_catalog.text[]), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:search_pattern AS pg_catalog.text))"
            )
        )
        result = await self.session.execute(
            statement,
            self._params(query, include_page=False),
        )
        return int(result.scalar_one())
