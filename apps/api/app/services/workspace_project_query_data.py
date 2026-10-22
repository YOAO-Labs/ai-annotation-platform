"""Borrow caller transactions and expose fixed Workspace Search relations."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.workspace_read_relations import (
    actor_project_memberships as actor_project_memberships,
    project_search_scope as project_search_scope,
    project_search_member_ids as project_search_member_ids,
)

from aap_workspace_contract.project_query_contract import (
    ProjectQueryUnavailable,
    decode_project_query_policy,
)
from aap_workspace.project_query_postgres import (
    PROJECT_QUERY_SCHEMA,
    project_query_step_arguments,
)

_ARGUMENT_NAMES = (
    "request",
    "action",
    "parameters",
    "status",
    "search",
    "type_keys",
    "data_types",
    "member_id",
    "project_role",
    "created_from",
    "created_to",
    "offset",
    "limit",
)
_STEP = text(
    f"SELECT {PROJECT_QUERY_SCHEMA}.project_query_step("
    "CAST(:request AS jsonb),CAST(:action AS text),CAST(:parameters AS jsonb),"
    "CAST(:status AS text),CAST(:search AS text),CAST(:type_keys AS text[]),"
    "CAST(:data_types AS text[]),CAST(:member_id AS uuid),CAST(:project_role AS text),"
    "CAST(:created_from AS timestamptz),CAST(:created_to AS timestamptz),"
    "CAST(:offset AS bigint),CAST(:limit AS integer))"
)


class WorkspaceProjectQueryData:
    def __init__(self, db):
        self.db = db
        self._policy_checked = False

    @asynccontextmanager
    async def transaction(self):
        self._policy_checked = False
        yield self

    async def step(self, request, action, parameters=None, filters=None):
        if self.db.autoflush:
            await self.db.flush()
        if not self._policy_checked:
            try:
                policy = await self.db.scalar(
                    text(f"SELECT {PROJECT_QUERY_SCHEMA}.project_query_policy()")
                )
            except SQLAlchemyError:
                raise ProjectQueryUnavailable() from None
            decode_project_query_policy(policy)
            self._policy_checked = True
        arguments = project_query_step_arguments(request, action, parameters, filters)
        with self.db.no_autoflush:
            return await self.db.scalar(_STEP, dict(zip(_ARGUMENT_NAMES, arguments)))
