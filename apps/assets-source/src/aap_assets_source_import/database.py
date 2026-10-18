"""asyncpg-only bindings for the seven Assets Dataset import capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from typing import Any, AsyncIterator
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from aap_assets_source.import_contract import (
    ImportConnection,
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportSourceObject,
    ImportState,
    decode_import_reply,
)

from .config import ImportConfig


EXPECTED_IMPORT_FUNCTIONS = frozenset(
    {
        "claim_dataset_import(uuid,integer,uuid)",
        "heartbeat_dataset_import(uuid,uuid,integer)",
        "read_dataset_import_connection(uuid,uuid,jsonb)",
        "save_dataset_import_manifest(uuid,uuid,jsonb)",
        "next_dataset_import_entry(uuid,uuid,uuid)",
        "complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)",
        "finish_dataset_import(uuid,uuid,text)",
    }
)

ROLE_PREFLIGHT_SQL = """
WITH expected(signature) AS (
    VALUES
        ('claim_dataset_import(uuid,integer,uuid)'::text),
        ('heartbeat_dataset_import(uuid,uuid,integer)'::text),
        ('read_dataset_import_connection(uuid,uuid,jsonb)'::text),
        ('save_dataset_import_manifest(uuid,uuid,jsonb)'::text),
        ('next_dataset_import_entry(uuid,uuid,uuid)'::text),
        ('complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)'::text),
        ('finish_dataset_import(uuid,uuid,text)'::text)
),
expected_oids AS (
    SELECT
        expected.signature,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.' || expected.signature
        )::oid AS function_oid
    FROM expected
),
accessible_assets_functions AS (
    SELECT
        proc.proname || '('
            || pg_catalog.replace(
                pg_catalog.oidvectortypes(proc.proargtypes), ' ', ''
            ) || ')' AS signature
    FROM pg_catalog.pg_proc AS proc
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname = 'aap_assets_v1'
      AND proc.prokind IN ('f', 'p')
      AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')
),
runtime_privileges AS (
    SELECT
        NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation
              ON relation.relnamespace = namespace.oid
            CROSS JOIN (
                VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
                       ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(
                  current_user, relation.oid, privilege.name
              )
        ) AS no_table_privileges,
        NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation
              ON relation.relnamespace = namespace.oid
            CROSS JOIN (
                VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_any_column_privilege(
                  current_user, relation.oid, privilege.name
              )
        ) AS no_column_privileges,
        NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation
              ON relation.relnamespace = namespace.oid
            CROSS JOIN (
                VALUES ('USAGE'::text), ('SELECT'), ('UPDATE')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind = 'S'
              AND pg_catalog.has_sequence_privilege(
                  current_user, relation.oid, privilege.name
              )
        ) AS no_sequence_privileges
)
SELECT
    current_user = session_user
    AND login_role.rolcanlogin
    AND NOT login_role.rolsuper
    AND NOT login_role.rolcreaterole
    AND NOT login_role.rolcreatedb
    AND NOT login_role.rolreplication
    AND NOT login_role.rolbypassrls
    AND pg_catalog.has_database_privilege(
        current_user, current_database(), 'CONNECT'
    )
    AND NOT pg_catalog.has_database_privilege(
        current_user, current_database(), 'CREATE'
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = login_role.oid
           OR membership.roleid = login_role.oid
    )
    AND pg_catalog.has_schema_privilege(
        current_user, 'aap_assets_v1', 'USAGE'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_schema_privilege(
              current_user, namespace.oid, 'USAGE'
          )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(
              current_user, namespace.oid, 'CREATE'
          )
    )
    AND NOT EXISTS (
        SELECT 1 FROM expected_oids
        WHERE function_oid IS NULL
           OR NOT pg_catalog.has_function_privilege(
               current_user, function_oid, 'EXECUTE'
           )
    )
    AND (
        SELECT pg_catalog.array_agg(signature ORDER BY signature)
        FROM accessible_assets_functions
    ) = (
        SELECT pg_catalog.array_agg(signature ORDER BY signature)
        FROM expected
    )
    AND runtime_privileges.no_table_privileges
    AND runtime_privileges.no_column_privileges
    AND runtime_privileges.no_sequence_privileges AS is_scoped
FROM pg_catalog.pg_roles AS login_role
CROSS JOIN runtime_privileges
WHERE login_role.rolname = current_user
"""


class DataContractError(RuntimeError):
    """The database role or native capability response violated the contract."""


class ImportData:
    """One transaction over a single LOGIN database connection."""

    def __init__(self, connection: asyncpg.Connection, max_reply_bytes: int) -> None:
        self._connection = connection
        self._max_reply_bytes = max_reply_bytes

    async def claim(
        self, token: UUID, lease_seconds: int, requested_job: UUID | None = None
    ) -> ImportState | None:
        result = await self._call(
            "SELECT aap_assets_v1.claim_dataset_import("
            "$1::uuid, $2::integer, $3::uuid)",
            (token, lease_seconds, requested_job),
            ImportState,
        )
        if result is not None and result.lease_token != token:
            raise ImportFailure("invalid_response")
        if result is not None and result.stage not in {"enumerating", "importing"}:
            raise ImportFailure("invalid_response")
        if requested_job is not None and result is not None:
            if result.job_id != requested_job:
                raise ImportFailure("invalid_response")
        return result

    async def heartbeat(
        self, job_id: UUID, token: UUID, lease_seconds: int
    ) -> ImportState:
        result = await self._call(
            "SELECT aap_assets_v1.heartbeat_dataset_import("
            "$1::uuid, $2::uuid, $3::integer)",
            (job_id, token, lease_seconds),
            ImportState,
        )
        assert result is not None
        self._require_lease(result, job_id, token)
        return result

    async def read_connection(
        self, job_id: UUID, token: UUID, fallback_allowlist: list[str]
    ) -> ImportConnection:
        result = await self._call(
            "SELECT aap_assets_v1.read_dataset_import_connection("
            "$1::uuid, $2::uuid, $3::jsonb)",
            (job_id, token, fallback_allowlist),
            ImportConnection,
        )
        assert result is not None
        if (result.job_id, result.lease_token) != (job_id, token):
            raise ImportFailure("invalid_response")
        return result

    async def save_manifest(
        self, job_id: UUID, token: UUID, objects: list[ImportSourceObject]
    ) -> ImportState:
        payload = [item.model_dump(mode="json") for item in objects]
        result = await self._call(
            "SELECT aap_assets_v1.save_dataset_import_manifest("
            "$1::uuid, $2::uuid, $3::jsonb)",
            (job_id, token, payload),
            ImportState,
        )
        assert result is not None
        self._require_lease(result, job_id, token)
        if (
            result.stage != "importing"
            or result.cursor != 0
            or result.total != len(objects)
            or result.total_bytes != sum(item.size for item in objects)
        ):
            raise ImportFailure("invalid_response")
        return result

    async def next_entry(
        self, job_id: UUID, token: UUID, publication_id: UUID
    ) -> ImportEntry | None:
        result = await self._call(
            "SELECT aap_assets_v1.next_dataset_import_entry("
            "$1::uuid, $2::uuid, $3::uuid)",
            (job_id, token, publication_id),
            ImportEntry,
        )
        if result is not None and (
            (result.job_id, result.lease_token) != (job_id, token)
            or result.publication_id != publication_id
        ):
            raise ImportFailure("invalid_response")
        return result

    async def complete_entry(
        self,
        job_id: UUID,
        token: UUID,
        index: int,
        publication_id: UUID,
        completion: ImportEntryCompletion,
    ) -> ImportState:
        result = await self._call(
            "SELECT aap_assets_v1.complete_dataset_import_entry("
            "$1::uuid, $2::uuid, $3::bigint, $4::uuid, $5::jsonb)",
            (
                job_id,
                token,
                index,
                publication_id,
                completion.model_dump(mode="json"),
            ),
            ImportState,
        )
        assert result is not None
        self._require_lease(result, job_id, token)
        if result.cursor != index + 1:
            raise ImportFailure("invalid_response")
        return result

    async def finish(
        self, job_id: UUID, token: UUID, fatal_error: str | None
    ) -> ImportState:
        result = await self._call(
            "SELECT aap_assets_v1.finish_dataset_import($1::uuid, $2::uuid, $3::text)",
            (job_id, token, fatal_error),
            ImportState,
        )
        assert result is not None
        if result.job_id != job_id:
            raise ImportFailure("invalid_response")
        if result.stage not in {"completed", "failed", "cancelled"}:
            raise ImportFailure("invalid_response")
        if fatal_error is None and result.stage != "completed":
            raise ImportFailure("invalid_response")
        if fatal_error is not None and result.stage not in {"failed", "cancelled"}:
            raise ImportFailure("invalid_response")
        return result

    @staticmethod
    def _require_lease(state: ImportState, job_id: UUID, token: UUID) -> None:
        if state.job_id != job_id or state.lease_token != token:
            raise ImportFailure("lease_lost")

    async def _call(
        self,
        statement: str,
        arguments: tuple[Any, ...],
        result_model: type[BaseModel],
    ) -> Any:
        raw = await self._connection.fetchval(statement, *arguments)
        payload = self._parse_payload(raw)
        try:
            return decode_import_reply(payload, result_model)
        except ImportFailure:
            raise
        except (
            Exception
        ) as exc:  # defensive: Pydantic implementation errors fail closed
            raise ImportFailure("invalid_response") from exc

    def _parse_payload(self, raw: Any) -> object:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ImportFailure("invalid_response") from None
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > self._max_reply_bytes:
                raise ImportFailure("invalid_response")
            try:
                return json.loads(raw, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError):
                raise ImportFailure("invalid_response") from None
        try:
            encoded_size = len(
                json.dumps(
                    raw,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            raise ImportFailure("invalid_response") from None
        if encoded_size > self._max_reply_bytes:
            raise ImportFailure("invalid_response")
        return raw


class ImportDatabase:
    """Dedicated connection pool with exact capability/role startup preflight."""

    def __init__(self, pool: asyncpg.Pool, config: ImportConfig) -> None:
        self._pool = pool
        self._config = config

    @classmethod
    async def connect(cls, config: ImportConfig) -> ImportDatabase:
        pool = await asyncpg.create_pool(
            dsn=config.driver_database_url,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            timeout=config.pool_timeout_seconds,
            command_timeout=config.sql_timeout_seconds,
            init=_init_connection,
            server_settings={"search_path": "pg_catalog,pg_temp"},
        )
        database = cls(pool, config)
        try:
            await database._verify_role_scope()
        except Exception:
            await pool.close()
            raise
        return database

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ImportData]:
        async with self._pool.acquire(
            timeout=self._config.pool_timeout_seconds
        ) as connection:
            async with connection.transaction():
                yield ImportData(connection, max_reply_bytes=16 * 1024 * 1024)

    async def ready(self) -> bool:
        async with self._pool.acquire(
            timeout=self._config.pool_timeout_seconds
        ) as connection:
            return await connection.fetchval("SELECT 1") == 1

    async def close(self) -> None:
        await self._pool.close()

    async def _verify_role_scope(self) -> None:
        async with self._pool.acquire(
            timeout=self._config.pool_timeout_seconds
        ) as connection:
            is_scoped = await connection.fetchval(ROLE_PREFLIGHT_SQL)
        if is_scoped is not True:
            raise DataContractError(
                "Dataset import LOGIN role does not have the exact scoped capabilities"
            )


async def _init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "json", schema="pg_catalog", encoder=_encode_json, decoder=json.loads
    )
    await connection.set_type_codec(
        "jsonb", schema="pg_catalog", encoder=_encode_json, decoder=json.loads
    )


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")
