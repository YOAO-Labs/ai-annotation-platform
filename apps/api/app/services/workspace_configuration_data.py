"""Bind configuration capabilities to the caller-owned API transaction."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from aap_workspace.configuration import ConfigurationCommandFailure
from aap_workspace.configuration_contract import (
    ConfigurationError,
    ConfigurationUnavailable,
    decode_configuration_policy,
)
from aap_workspace.configuration_postgres import configuration_busy_error


def _diagnostic(error):
    state, hint = None, ""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        state = (
            state or getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        )
        hint = hint or getattr(error, "hint", "") or ""
        error = getattr(error, "orig", None) or error.__cause__
    return state, hint


class WorkspaceConfigurationData:
    # This adapter borrows the API request transaction; the caller owns commit.
    independent = False

    def __init__(self, db):
        self.db = db

    @property
    def autoflush(self):
        return self.db.autoflush

    @asynccontextmanager
    async def transaction(self, *, write):
        if write is not True:
            raise ConfigurationUnavailable()
        # No commit, rollback or nested transaction: the request/caller owns it.
        yield self

    async def before_project_insert(self):
        await self.db.flush()

    async def _flush_account(self):
        try:
            await self.db.flush()
        except DBAPIError as error:
            if _diagnostic(error)[0] in {"55P03", "40001", "40P01"}:
                raise ConfigurationCommandFailure(
                    ConfigurationError(
                        status_code=409, detail={"reason": "account_busy"}
                    )
                ) from error
            raise

    async def step(self, request_json, action, args, columns, values):
        if action == "begin":
            # The legacy account-lock helper also maps a busy failure raised
            # by its preliminary flush. Plain account reads do not do so.
            if self.db.autoflush:
                if request_json["operation"] in {
                    "update_template",
                    "delete_template",
                    "duplicate_template",
                }:
                    await self._flush_account()
                else:
                    await self.db.flush()
            try:
                policy = await self.db.scalar(
                    text("SELECT aap_workspace_configuration_v1.configuration_policy()")
                )
            except SQLAlchemyError:
                raise ConfigurationUnavailable() from None
            decode_configuration_policy(policy)
        elif (
            action == "authority"
            and self.db.autoflush
            and request_json["selectors"]["source_project_id"] is None
            and request_json["operation"] in {"create_project", "create_template"}
        ):
            await self._flush_account()
        try:
            with self.db.no_autoflush:
                return await self.db.scalar(
                    text(
                        "SELECT aap_workspace_configuration_v1.configuration_step("
                        "CAST(:command AS jsonb), CAST(:action AS text), "
                        "CAST(:args AS jsonb), CAST(:columns AS text[]), "
                        "CAST(:values AS text[]))"
                    ),
                    {
                        "command": json.dumps(
                            request_json, ensure_ascii=True, allow_nan=False
                        ),
                        "action": action,
                        "args": json.dumps(args, ensure_ascii=True, allow_nan=False),
                        "columns": columns,
                        "values": values,
                    },
                )
        except DBAPIError as error:
            if action == "backend" and _diagnostic(error)[0] == "55000":
                raise ConfigurationUnavailable() from None
            mapped = configuration_busy_error(*_diagnostic(error))
            if mapped is not None:
                raise ConfigurationCommandFailure(mapped) from error
            raise
