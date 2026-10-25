"""Bind template query capabilities to the caller-owned API transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_workspace.template_query_contract import (
    TemplateQueryUnavailable,
    decode_template_query_policy,
)
from aap_workspace.template_query_postgres import template_query_step_arguments


class WorkspaceTemplateQueryData:
    def __init__(self, db):
        self.db = db
        self._policy_checked = False

    @asynccontextmanager
    async def transaction(self):
        self._policy_checked = False
        # The caller owns pending work, commits and rollbacks.
        yield self

    async def step(self, request, action, parameters=None, filters=None):
        if self.db.autoflush:
            await self.db.flush()
        if not self._policy_checked:
            try:
                policy = await self.db.scalar(
                    text(
                        "SELECT aap_workspace_template_query_v1.template_query_policy()"
                    )
                )
            except SQLAlchemyError:
                raise TemplateQueryUnavailable() from None
            decode_template_query_policy(policy)
            self._policy_checked = True
        values = template_query_step_arguments(request, action, parameters, filters)
        with self.db.no_autoflush:
            return await self.db.scalar(
                text(
                    "SELECT aap_workspace_template_query_v1.template_query_step("
                    "CAST(:request AS jsonb), CAST(:action AS text), "
                    "CAST(:parameters AS jsonb), CAST(:scope AS text), "
                    "CAST(:type_keys AS text[]), CAST(:search AS text))"
                ),
                dict(
                    zip(
                        (
                            "request",
                            "action",
                            "parameters",
                            "scope",
                            "type_keys",
                            "search",
                        ),
                        values,
                    )
                ),
            )
