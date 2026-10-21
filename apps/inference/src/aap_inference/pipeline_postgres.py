"""Function-only asyncpg binding for the independent pipeline owner."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
import json
import logging
import math
from typing import Any

from .pipeline_contract import (
    PIPELINE_ACTIONS,
    PIPELINE_INSERT_COLUMNS,
    PIPELINE_UPDATE_COLUMNS,
    PipelineCommandRequest,
    PipelineInvalidRequest,
    PipelinePolicy,
    PipelineQueryRequest,
    PipelineRejected,
    PipelineStatusRequest,
    PipelineUnavailable,
    decode_pipeline_command_request,
    decode_pipeline_policy,
    decode_pipeline_query_request,
    decode_pipeline_status_request,
    pipeline_policy,
)

_SCHEMA = "aap_inference_pipelines_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.pipeline_policy()"
_STEP_SQL = (
    f"SELECT {_SCHEMA}.pipeline_step("
    "$1::jsonb,$2::text,$3::jsonb,$4::text[],$5::text[])"
)
_STEP_FUNCTIONS = {
    ("pipeline_policy", ""),
    ("pipeline_step", "jsonb, text, jsonb, text[], text[]"),
}
_COMMAND_ACTIONS = frozenset(
    {
        "begin",
        "organization_ids",
        "pipeline_get",
        "backend_get",
        "backend_pool",
        "backend_enabled",
        "project_query",
        "project_access",
        "default_clear",
        "pipeline_insert",
        "pipeline_update",
        "pipeline_delete",
        "source_usage_increment",
        "finish",
    }
)
_INSERT_COLUMNS = frozenset(PIPELINE_INSERT_COLUMNS)
_UPDATE_COLUMNS = frozenset(PIPELINE_UPDATE_COLUMNS)
_BUSY_HINTS = {
    "aap.workspace.project_access.account_busy": "account_busy",
    "aap.workspace.project_access.project_busy": "project_busy",
    "aap.workspace.project_access.membership_busy": "membership_busy",
}
_CONNECTION_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})
_logger = logging.getLogger(__name__)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PipelineInvalidRequest() from None


def _request_model(request: Any, action: str):
    if isinstance(request, PipelineCommandRequest):
        return decode_pipeline_command_request(request)
    if isinstance(request, PipelineQueryRequest):
        return decode_pipeline_query_request(request)
    if isinstance(request, PipelineStatusRequest):
        return decode_pipeline_status_request(request)
    if action == "status":
        return decode_pipeline_status_request(request)
    if action in {"pipeline_get", "project_query"}:
        try:
            return decode_pipeline_command_request(request)
        except PipelineInvalidRequest:
            return decode_pipeline_query_request(request)
    if action in _COMMAND_ACTIONS:
        return decode_pipeline_command_request(request)
    return decode_pipeline_query_request(request)


def _step_arguments(args: Mapping[str, Any] | None) -> dict[str, Any]:
    if args is None:
        return {}
    if not isinstance(args, Mapping) or any(type(key) is not str for key in args):
        raise PipelineInvalidRequest()
    return dict(args)


def _columns_and_values(action, columns, values) -> tuple[list[str], list[str | None]]:
    if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
        raise PipelineInvalidRequest()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise PipelineInvalidRequest()
    column_list = list(columns)
    value_list = list(values)
    if len(column_list) != len(value_list) or any(
        type(x) is not str for x in column_list
    ):
        raise PipelineInvalidRequest()
    if any(value is not None and type(value) is not str for value in value_list):
        raise PipelineInvalidRequest()
    if action == "pipeline_insert":
        allowed = _INSERT_COLUMNS
    elif action == "pipeline_update":
        allowed = _UPDATE_COLUMNS
    else:
        if column_list or value_list:
            raise PipelineInvalidRequest()
        return [], []
    if (action == "pipeline_insert" and not column_list) or len(
        set(column_list)
    ) != len(column_list):
        raise PipelineInvalidRequest()
    if not set(column_list) <= allowed:
        raise PipelineInvalidRequest()
    return column_list, value_list


def _busy_rejection(error: BaseException) -> PipelineRejected | None:
    sqlstate = getattr(error, "sqlstate", None)
    hint = getattr(error, "hint", None)
    reason = _BUSY_HINTS.get(hint)
    if sqlstate in {"55P03", "40001", "40P01"} and reason is not None:
        return PipelineRejected(409, {"reason": reason}, rollback=True)
    return None


def _commit_rejection(error: BaseException) -> PipelineRejected | None:
    busy = _busy_rejection(error)
    if busy is not None:
        return busy
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and (
        sqlstate.startswith("23") or (sqlstate.startswith("40") and sqlstate != "40003")
    ):
        return PipelineRejected(500, "Internal Server Error", rollback=True)
    return None


def _is_connection_state(error: BaseException) -> bool:
    sqlstate = getattr(error, "sqlstate", None)
    return (
        isinstance(sqlstate, str) and sqlstate.startswith("08")
    ) or sqlstate in _CONNECTION_SQLSTATES


class PipelinePostgresTransaction:
    """One fixed step RPC inside the manager-owned transaction."""

    autoflush = False
    owns_commit = True

    def __init__(self, connection, asyncpg_module) -> None:
        self._connection = connection
        self._asyncpg = asyncpg_module

    async def step(
        self,
        request,
        action: str,
        args: Mapping[str, Any] | None = None,
        columns: Sequence[str] = (),
        values: Sequence[str | None] = (),
    ):
        if type(action) is not str or action not in PIPELINE_ACTIONS:
            raise PipelineInvalidRequest()
        typed_request = _request_model(request, action)
        if action == "status" and not isinstance(typed_request, PipelineStatusRequest):
            raise PipelineInvalidRequest()
        if (
            action != "status"
            and action not in _COMMAND_ACTIONS
            and isinstance(typed_request, PipelineStatusRequest)
        ):
            raise PipelineInvalidRequest()
        request_json = _json_text(typed_request.model_dump(mode="json", by_alias=True))
        args_json = _json_text(_step_arguments(args))
        column_list, value_list = _columns_and_values(action, columns, values)

        try:
            raw = await self._connection.fetchval(
                _STEP_SQL,
                request_json,
                action,
                args_json,
                column_list,
                value_list,
            )
        except self._asyncpg.PostgresError as error:
            busy = _busy_rejection(error)
            if busy is not None:
                raise busy from None
            if _is_connection_state(error):
                raise PipelineUnavailable() from None
            raise PipelineRejected(
                500, "Internal Server Error", rollback=True
            ) from None
        except (self._asyncpg.InterfaceError, OSError, TimeoutError):
            raise PipelineUnavailable() from None

        if raw is None:
            raise PipelineUnavailable()
        # The portable manager decodes and binds the step envelope before it
        # consumes any fact; the port preserves the Native driver value.
        return raw


class PipelinePostgresData:
    """Lazy per-transaction asyncpg binding with exact function-only role checks."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: float = 5.0,
        command_timeout: float = 30.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(char.isspace() for char in dsn)
        ):
            raise PipelineUnavailable()
        if (
            type(connect_timeout) not in {int, float}
            or not math.isfinite(float(connect_timeout))
            or connect_timeout <= 0
            or type(command_timeout) not in {int, float}
            or not math.isfinite(float(command_timeout))
            or command_timeout <= 0
        ):
            raise PipelineInvalidRequest()
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = float(connect_timeout)
        self.command_timeout = float(command_timeout)

    async def _close_connection(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference pipeline connection cleanup failed")
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference pipeline connection cleanup failed")
                return
            raise PipelineUnavailable() from None

    async def _open_verified_connection(self):
        import asyncpg

        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        connection = None
        primary_error: BaseException | None = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-inference-pipelines"},
            )
            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_STEP_FUNCTIONS,
            )
            policy = decode_pipeline_policy(await connection.fetchval(_POLICY_SQL))
            if policy != pipeline_policy():
                raise PipelineUnavailable()
            return connection, asyncpg, policy
        except PipelineUnavailable as error:
            primary_error = error
            raise
        except (
            FunctionRoleUnavailable,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as error:
            primary_error = error
            raise PipelineUnavailable() from None
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if primary_error is not None and connection is not None:
                await self._close_connection(connection, primary_failure=True)

    async def close(self) -> None:
        """Keep the provider interface uniform; transactions close their own connection."""

    async def readiness(self) -> bool:
        try:
            connection, _asyncpg, policy = await self._open_verified_connection()
        except PipelineUnavailable:
            return False
        ready = policy == pipeline_policy()
        try:
            await self._close_connection(connection, primary_failure=False)
        except PipelineUnavailable:
            return False
        return ready

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False):
        if type(readonly) is not bool:
            raise PipelineInvalidRequest()
        connection = None
        asyncpg = None
        transaction = None
        transaction_started = False
        body_completed = False
        primary_error: BaseException | None = None
        try:
            connection, asyncpg, _policy = await self._open_verified_connection()
            transaction = connection.transaction(readonly=readonly)
            await transaction.start()
            transaction_started = True
            port = PipelinePostgresTransaction(connection, asyncpg)
            try:
                yield port
                body_completed = True
            except BaseException:
                try:
                    await asyncio.shield(transaction.rollback())
                except BaseException:
                    # The body failure remains authoritative; COMMIT was never attempted.
                    pass
                raise
            try:
                await transaction.commit()
            except asyncpg.PostgresError as error:
                rejected = _commit_rejection(error)
                if rejected is not None:
                    raise rejected from None
                raise PipelineUnavailable() from None
            except (asyncpg.InterfaceError, OSError, TimeoutError):
                # COMMIT may have reached PostgreSQL. The status RPC resolves this.
                raise PipelineUnavailable() from None
        except BaseException as error:
            primary_error = error
            if asyncpg is not None and isinstance(
                error,
                (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError),
            ):
                if body_completed:
                    raise PipelineUnavailable() from None
                if not transaction_started:
                    raise PipelineUnavailable() from None
                busy = _busy_rejection(error)
                if busy is not None:
                    raise busy from None
                if _is_connection_state(error):
                    raise PipelineUnavailable() from None
                raise PipelineRejected(
                    500, "Internal Server Error", rollback=True
                ) from None
            raise
        finally:
            if connection is not None:
                await self._close_connection(
                    connection,
                    primary_failure=primary_error is not None,
                )

    async def policy(self) -> PipelinePolicy:
        connection, _asyncpg, policy = await self._open_verified_connection()
        await self._close_connection(connection, primary_failure=False)
        return policy


__all__ = ["PipelinePostgresData", "PipelinePostgresTransaction"]
