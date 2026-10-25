"""Bind directory facts to the caller's existing transaction."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_workspace.directory_query_contract import (
    DirectoryQueryUnavailable,
    decode_directory_query_policy,
)
from aap_workspace.directory_query_postgres import (
    DIRECTORY_QUERY_SCHEMA,
    directory_query_step_arguments,
)

_STEP = text(
    f"SELECT {DIRECTORY_QUERY_SCHEMA}.directory_query_step("
    "CAST(:request AS jsonb),CAST(:action AS text),CAST(:parameters AS jsonb))"
)


class WorkspaceDirectoryQueryData:
    def __init__(self, db):
        self.db = db
        self._policy_checked = False

    @asynccontextmanager
    async def transaction(self):
        self._policy_checked = False
        yield self

    async def step(self, request, action, parameters=None):
        if self.db.autoflush:
            await self.db.flush()
        if not self._policy_checked:
            try:
                policy = await self.db.scalar(
                    text(f"SELECT {DIRECTORY_QUERY_SCHEMA}.directory_query_policy()")
                )
            except SQLAlchemyError:
                raise DirectoryQueryUnavailable() from None
            decode_directory_query_policy(policy)
            self._policy_checked = True
        arguments = directory_query_step_arguments(request, action, parameters)
        with self.db.no_autoflush:
            return await self.db.scalar(
                _STEP, dict(zip(("request", "action", "parameters"), arguments))
            )
