"""asyncpg-only bindings for Dataset import and image admission capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from typing import Any, AsyncIterator
from uuid import UUID

import asyncpg
from pydantic import BaseModel, model_validator
from yoao_assets_images.policy import ImagePolicy

from yoao_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalOperation,
    decode_managed_original_reply,
    require_managed_original_ok,
)
from yoao_assets_source.import_contract import (
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportSourceObject,
    ImportState,
    decode_import_reply,
)
from yoao_assets_source_runtime.image_admission import (
    AsyncpgImageAdmissionData,
    ImageAdmission,
)

from .config import ImportConfig


EXPECTED_IMPORT_FUNCTIONS = frozenset(
    {
        "resume_import_original_verification(jsonb)",
        "reserve_import_original_read(jsonb)",
        "cancel_unstarted_import_original_read(jsonb)",
        "claim_dataset_import(uuid,integer,uuid)",
        "heartbeat_dataset_import(uuid,uuid,integer)",
        "save_observed_dataset_import_manifest(uuid,uuid,jsonb)",
        "next_dataset_import_entry(uuid,uuid,uuid)",
        "complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)",
        "finish_dataset_import(uuid,uuid,text)",
        "resolve_internal_image_source(text,uuid,text,jsonb)",
        "schedule_image_media(uuid,text,uuid,jsonb,boolean)",
        "begin_dataset_import_original(uuid,uuid,jsonb)",
        "open_dataset_import_original(uuid,uuid,bigint,uuid,uuid)",
        "record_import_original_prepared(jsonb,jsonb)",
        "mark_import_original_writing(jsonb)",
        "record_import_original_written(jsonb,jsonb)",
        "fail_import_original(jsonb,text,text,jsonb)",
    }
)

ROLE_PREFLIGHT_SQL = """
WITH expected(signature) AS (
    VALUES
        ('resume_import_original_verification(jsonb)'::text),
        ('reserve_import_original_read(jsonb)'::text),
        ('cancel_unstarted_import_original_read(jsonb)'::text),
        ('claim_dataset_import(uuid,integer,uuid)'::text),
        ('heartbeat_dataset_import(uuid,uuid,integer)'::text),
        ('save_observed_dataset_import_manifest(uuid,uuid,jsonb)'::text),
        ('next_dataset_import_entry(uuid,uuid,uuid)'::text),
        ('complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)'::text),
        ('finish_dataset_import(uuid,uuid,text)'::text),
        ('resolve_internal_image_source(text,uuid,text,jsonb)'::text),
        ('schedule_image_media(uuid,text,uuid,jsonb,boolean)'::text),
        ('begin_dataset_import_original(uuid,uuid,jsonb)'::text),
        ('open_dataset_import_original(uuid,uuid,bigint,uuid,uuid)'::text),
        ('record_import_original_prepared(jsonb,jsonb)'::text),
        ('mark_import_original_writing(jsonb)'::text),
        ('record_import_original_written(jsonb,jsonb)'::text),
        ('fail_import_original(jsonb,text,text,jsonb)'::text)
),
expected_oids AS (
    SELECT
        expected.signature,
        pg_catalog.to_regprocedure(
            'yoao_assets_v1.' || expected.signature
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
    WHERE namespace.nspname = 'yoao_assets_v1'
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
        current_user, 'yoao_assets_v1', 'USAGE'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'yoao_assets_v1'
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


_MAX_ORIGINAL_REPLY_BYTES = 16 * 1024 * 1024


def _parse_json_reply(raw: Any, max_bytes: int = _MAX_ORIGINAL_REPLY_BYTES) -> object:
    """Bound every native reply and reject non-finite JSON constants."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ImportFailure("invalid_response") from None
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > max_bytes:
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
    if encoded_size > max_bytes:
        raise ImportFailure("invalid_response")
    return raw


class DataContractError(RuntimeError):
    """The database role or native capability response violated the contract."""


class ImportOriginalMaterial(ManagedOriginalMaterial):
    """Allocation facts only; connector material is not a valid importer reply."""

    destination: None = None

    @model_validator(mode="after")
    def check_target(self):
        if (
            self.origin != "connector"
            or self.mode != "write"
            or self.staging is not None
            or self.publication_id is None
            or self.object_key != self.key_prefix + self.file_path
            or (self.connection_id is None) != (self.connection_revision is None)
        ):
            raise ValueError("Invalid importer original allocation")
        return self


class ImportData:
    """One transaction over a single LOGIN database connection."""

    def __init__(
        self,
        connection: asyncpg.Connection,
        max_reply_bytes: int,
        image_policy: ImagePolicy,
    ) -> None:
        self._connection = connection
        self._max_reply_bytes = max_reply_bytes
        self.image_admission = ImageAdmission(
            AsyncpgImageAdmissionData(connection, max_reply_bytes), image_policy
        )

    async def claim(
        self, token: UUID, lease_seconds: int, requested_job: UUID | None = None
    ) -> ImportState | None:
        result = await self._call(
            "SELECT yoao_assets_v1.claim_dataset_import("
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
            "SELECT yoao_assets_v1.heartbeat_dataset_import("
            "$1::uuid, $2::uuid, $3::integer)",
            (job_id, token, lease_seconds),
            ImportState,
        )
        assert result is not None
        self._require_lease(result, job_id, token)
        return result

    async def save_manifest(
        self, job_id: UUID, token: UUID, objects: list[ImportSourceObject]
    ) -> ImportState:
        payload = [item.model_dump(mode="json") for item in objects]
        result = await self._call(
            "SELECT yoao_assets_v1.save_observed_dataset_import_manifest("
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
            "SELECT yoao_assets_v1.next_dataset_import_entry("
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
            "SELECT yoao_assets_v1.complete_dataset_import_entry("
            "$1::uuid, $2::uuid, $3::bigint, $4::uuid, $5::jsonb)",
            (
                job_id,
                token,
                index,
                publication_id,
                completion.model_dump(mode="json", exclude_none=True),
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
            "SELECT yoao_assets_v1.finish_dataset_import($1::uuid, $2::uuid, $3::text)",
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
        except Exception as exc:
            raise ImportFailure("invalid_response") from exc

    def _parse_payload(self, raw: Any) -> object:
        return _parse_json_reply(raw, self._max_reply_bytes)


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
                yield ImportData(
                    connection,
                    max_reply_bytes=16 * 1024 * 1024,
                    image_policy=self._config.image_policy,
                )

    async def _original_ok(self, statement: str, arguments: tuple[Any, ...]) -> Any:
        raw = await self._original_reply(statement, arguments)
        require_managed_original_ok(raw)
        return raw

    async def allocate(
        self, entry: ImportEntry, request: dict[str, Any]
    ) -> ImportOriginalMaterial:
        """Allocate independently and durably before any physical I/O."""
        raw = await self._original_reply(
            "SELECT yoao_assets_v1.begin_dataset_import_original("
            "$1::uuid,$2::uuid,$3::jsonb)",
            (entry.job_id, entry.lease_token, request),
        )
        return decode_managed_original_reply(raw, ImportOriginalMaterial)

    async def open_operation(
        self, entry: ImportEntry, creation_id: UUID
    ) -> ManagedOriginalOperation:
        """Acquire this creation's one permit; verified reuse stays read-only."""
        raw = await self._original_reply(
            "SELECT yoao_assets_v1.open_dataset_import_original("
            "$1::uuid,$2::uuid,$3::bigint,$4::uuid,$5::uuid)",
            (
                entry.job_id,
                entry.lease_token,
                entry.index,
                entry.publication_id,
                creation_id,
            ),
        )
        return decode_managed_original_reply(raw, ManagedOriginalOperation)

    async def reserve_original_read(
        self, request: dict[str, Any]
    ) -> ManagedOriginalOperation:
        raw = await self._original_reply(
            "SELECT yoao_assets_v1.reserve_import_original_read($1::jsonb)",
            (request,),
        )
        return decode_managed_original_reply(raw, ManagedOriginalOperation)

    async def cancel_unstarted_original_read(self, request: dict[str, Any]) -> None:
        raw = await self._original_reply(
            "SELECT yoao_assets_v1.cancel_unstarted_import_original_read($1::jsonb)",
            (request,),
        )
        if not isinstance(raw, dict) or raw.get("state") not in {
            "cancelled",
            "pending",
            "drained",
            "completed",
            "failed",
            "fenced",
        }:
            raise ImportFailure("invalid_response")

    async def resume_original(
        self, request: dict[str, Any]
    ) -> ManagedOriginalOperation:
        raw = await self._original_reply(
            "SELECT yoao_assets_v1.resume_import_original_verification($1::jsonb)",
            (request,),
        )
        return decode_managed_original_reply(raw, ManagedOriginalOperation)

    async def original_prepared(
        self, scope: dict[str, Any], prepared: dict[str, Any]
    ) -> None:
        await self._original_ok(
            "SELECT yoao_assets_v1.record_import_original_prepared($1::jsonb,$2::jsonb)",
            (scope, prepared),
        )

    async def original_writing(self, scope: dict[str, Any]) -> None:
        await self._original_ok(
            "SELECT yoao_assets_v1.mark_import_original_writing($1::jsonb)",
            (scope,),
        )

    async def original_written(
        self, scope: dict[str, Any], receipt: dict[str, Any]
    ) -> None:
        await self._original_ok(
            "SELECT yoao_assets_v1.record_import_original_written($1::jsonb,$2::jsonb)",
            (scope, receipt),
        )

    async def fail_original(
        self,
        scope: dict[str, Any],
        outcome: str,
        error_code: str,
        evidence: dict[str, Any],
    ) -> None:
        await self._original_ok(
            "SELECT yoao_assets_v1.fail_import_original("
            "$1::jsonb,$2::text,$3::text,$4::jsonb)",
            (scope, outcome, error_code, evidence),
        )

    async def _original_reply(self, statement: str, arguments: tuple[Any, ...]) -> Any:
        async with self._pool.acquire(
            timeout=self._config.pool_timeout_seconds
        ) as connection:
            raw = await connection.fetchval(statement, *arguments)
        return _parse_json_reply(raw)

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
