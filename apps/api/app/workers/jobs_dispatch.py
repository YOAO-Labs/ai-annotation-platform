"""Recover committed Jobs dispatch intents on the dedicated Celery queue."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import unquote, urlsplit

import asyncpg
from yoao_jobs.dispatch_postgres import RECOVERY_FUNCTIONS, JobsDispatchPostgres
from yoao_jobs.dispatch_relay import JobsDispatchRelay
from yoao_jobs.publication_postgres import (
    PUBLICATION_FUNCTIONS,
    publication_database_url,
)
from yoao_owner_data.postgres import FunctionRoleUnavailable, verify_function_only_role

from app.services.jobs_queue import publish_queue_envelope
from app.workers.celery_app import celery_app


def _credential(name: str) -> str:
    try:
        url = publication_database_url(
            os.environ.get(name, ""), os.environ.get(name + "_FILE", "")
        )
        parsed = urlsplit(url)
        if not parsed.username or not parsed.path.removeprefix("/"):
            raise ValueError
    except ValueError:
        raise ValueError(f"Supply one valid {name} or {name}_FILE") from None
    return url


async def _verify_login(url: str, functions: set[tuple[str, str]]) -> tuple:
    login = unquote(urlsplit(url).username)
    connection = await asyncpg.connect(url, timeout=5, command_timeout=5)
    try:
        await verify_function_only_role(
            connection, schema="yoao_jobs_v1", functions=functions, role=login
        )
        if (
            await connection.fetchval(
                "SELECT rolinherit FROM pg_catalog.pg_roles WHERE rolname=current_user"
            )
            is not False
        ):
            raise FunctionRoleUnavailable()
        row = await connection.fetchrow(
            "SELECT current_database(), pg_catalog.inet_server_addr(), pg_catalog.inet_server_port()"
        )
        return tuple(row)
    finally:
        await connection.close(timeout=5)


async def _check_login(selector: str, functions: set[tuple[str, str]]) -> tuple:
    url = _credential(selector)
    try:
        return await _verify_login(url, functions)
    except (asyncpg.PostgresError, OSError, ValueError, FunctionRoleUnavailable):
        raise ValueError(
            f"{selector} did not authenticate as its function-only Jobs LOGIN"
        ) from None


async def _check_credentials() -> None:
    dispatch_target = await _check_login(
        "JOBS_DISPATCH_DATABASE_URL", RECOVERY_FUNCTIONS
    )
    publication_target = await _check_login(
        "JOBS_PUBLICATION_DATABASE_URL", PUBLICATION_FUNCTIONS
    )
    if dispatch_target != publication_target:
        raise ValueError(
            "Jobs recovery and publication LOGINs must target the same database"
        )


class _BrokerPublisher:
    publish_envelope = staticmethod(publish_queue_envelope)


@celery_app.task(
    name="app.workers.jobs_dispatch.recover_job_dispatches",
    queue="jobs.recovery",
    ignore_result=True,
)
def recover_job_dispatches() -> int:
    return asyncio.run(_recover())


async def _recover() -> int:
    dispatch_url = _credential("JOBS_DISPATCH_DATABASE_URL")
    publication_url = _credential("JOBS_PUBLICATION_DATABASE_URL")
    async with await JobsDispatchPostgres.connect(dispatch_url) as provider:
        relay = JobsDispatchRelay(provider.client, _BrokerPublisher(), publication_url)
        return await relay.run_once()


if __name__ == "__main__":
    try:
        asyncio.run(_check_credentials())
    except ValueError as error:
        raise SystemExit(str(error)) from None
