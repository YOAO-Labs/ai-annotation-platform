"""asyncpg bindings for the nine public Assets source capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from typing import Any, AsyncIterator, Callable, TypeVar
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ValidationError

from .config import SourceConfig
from aap_assets_source.contract import SourceFailure, UploadInitRequest
from aap_assets_source.deletion_contract import (
    DatasetSourceDeletionCommand,
    DatasetSourceDeletionFailure,
    DatasetSourceDeletionReceipt,
    decode_source_deletion_reply,
)
from aap_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
    LegacyUploadState,
    decode_legacy_upload_reply,
)
from aap_assets_source.upload_contract import (
    DatasetUploadAcceptance,
    DatasetUploadCompletion,
    DatasetUploadDiscard,
    DatasetUploadDuplicate,
    DatasetUploadFailure,
    DatasetUploadIntent,
    decode_dataset_upload_reply,
)

EXPECTED_ASSET_SOURCE_FUNCTIONS = frozenset(
    {
        "reserve_dataset_upload(uuid,uuid,uuid,text,text)",
        "lock_dataset_upload(uuid,uuid,uuid)",
        "find_dataset_upload_duplicate(uuid,uuid,uuid,text)",
        "complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)",
        "discard_dataset_upload(uuid,uuid,uuid,text)",
        "delete_dataset(uuid,uuid,uuid,text,jsonb)",
        "delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)",
        "lock_legacy_dataset_upload(uuid,uuid,uuid,uuid)",
        "complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer)",
    }
)

ROLE_PREFLIGHT_SQL = """
WITH expected(signature) AS (
    VALUES
        ('reserve_dataset_upload(uuid,uuid,uuid,text,text)'::text),
        ('lock_dataset_upload(uuid,uuid,uuid)'::text),
        ('find_dataset_upload_duplicate(uuid,uuid,uuid,text)'::text),
        ('complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)'::text),
        ('discard_dataset_upload(uuid,uuid,uuid,text)'::text),
        ('delete_dataset(uuid,uuid,uuid,text,jsonb)'::text),
        ('delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)'::text),
        ('lock_legacy_dataset_upload(uuid,uuid,uuid,uuid)'::text),
        ('complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer)'::text)
),
target_login AS (
    SELECT role_row.*
    FROM pg_catalog.pg_roles AS role_row
    WHERE role_row.rolname = current_user
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
                VALUES
                    ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
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
FROM target_login AS login_role
CROSS JOIN runtime_privileges
"""

_Result = TypeVar("_Result", bound=BaseModel)
_NativeDecoder = Callable[[object], _Result]


class DataContractError(RuntimeError):
    """The Assets source database role or reply violated the runtime contract."""


@dataclass(frozen=True, slots=True)
class SourceDataPorts:
    """Bindings sharing one connection and transaction for a whole operation."""

    uploads: NativeUploadData
    legacy_uploads: NativeLegacyUploadData
    deletions: NativeDeletionData


class _NativeData:
    def __init__(self, connection: asyncpg.Connection, max_reply_bytes: int) -> None:
        self._connection = connection
        self._max_reply_bytes = max_reply_bytes

    async def _call(
        self,
        statement: str,
        arguments: tuple[Any, ...],
        decoder: _NativeDecoder[_Result],
        expected_failures: tuple[type[Exception], ...],
    ) -> _Result:
        raw = await self._connection.fetchval(statement, *arguments)
        payload = self._parse_payload(raw)
        try:
            return decoder(payload)
        except expected_failures:
            raise
        except (TypeError, ValueError, ValidationError):
            raise SourceFailure("invalid_response") from None

    def _parse_payload(self, raw: Any) -> object:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise SourceFailure("invalid_response") from None
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > self._max_reply_bytes:
                raise SourceFailure("invalid_response")
            try:
                payload = json.loads(raw, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError):
                raise SourceFailure("invalid_response") from None
        else:
            payload = raw
        try:
            encoded_size = 0
            encoder = json.JSONEncoder(
                ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            for part in encoder.iterencode(payload):
                encoded_size += len(part.encode("utf-8"))
                if encoded_size > self._max_reply_bytes:
                    raise SourceFailure("invalid_response")
        except (TypeError, ValueError):
            raise SourceFailure("invalid_response") from None
        return payload


class NativeUploadData(_NativeData):
    async def reserve(self, request: UploadInitRequest) -> DatasetUploadIntent:
        result = await self._call(
            "SELECT aap_assets_v1.reserve_dataset_upload("
            "$1::uuid, $2::uuid, $3::uuid, $4::text, $5::text)",
            (
                request.item_id,
                request.actor_id,
                request.dataset_id,
                request.file_name,
                request.content_type,
            ),
            lambda payload: decode_dataset_upload_reply(payload, DatasetUploadIntent),
            (DatasetUploadFailure,),
        )
        self._require_scope(
            result,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        if (result.file_name, result.content_type) != (
            request.file_name,
            request.content_type,
        ):
            raise SourceFailure("invalid_response")
        return result

    async def lock(
        self, *, actor_id: UUID, dataset_id: UUID, item_id: UUID
    ) -> DatasetUploadIntent:
        result = await self._call(
            "SELECT aap_assets_v1.lock_dataset_upload($1::uuid, $2::uuid, $3::uuid)",
            (actor_id, dataset_id, item_id),
            lambda payload: decode_dataset_upload_reply(payload, DatasetUploadIntent),
            (DatasetUploadFailure,),
        )
        self._require_scope(
            result, actor_id=actor_id, dataset_id=dataset_id, item_id=item_id
        )
        return result

    async def find_duplicate(
        self,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        item_id: UUID,
        content_hash: str | None,
    ) -> DatasetUploadDuplicate:
        result = await self._call(
            "SELECT aap_assets_v1.find_dataset_upload_duplicate("
            "$1::uuid, $2::uuid, $3::uuid, $4::text)",
            (actor_id, dataset_id, item_id, content_hash),
            lambda payload: decode_dataset_upload_reply(
                payload, DatasetUploadDuplicate
            ),
            (DatasetUploadFailure,),
        )
        if result.duplicate_of == item_id:
            raise SourceFailure("invalid_response")
        return result

    async def complete(
        self, request: DatasetUploadCompletion
    ) -> DatasetUploadAcceptance:
        result = await self._call(
            "SELECT aap_assets_v1.complete_dataset_upload("
            "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::bigint, $6::text, "
            "$7::integer, $8::integer)",
            (
                request.actor_id,
                request.dataset_id,
                request.item_id,
                request.publication_id,
                request.file_size,
                request.content_hash,
                request.width,
                request.height,
            ),
            lambda payload: decode_dataset_upload_reply(
                payload, DatasetUploadAcceptance
            ),
            (DatasetUploadFailure,),
        )
        self._require_scope(
            result,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        if result.publication_id != request.publication_id:
            raise SourceFailure("invalid_response")
        return result

    async def discard(self, request: DatasetUploadDiscard) -> DatasetUploadIntent:
        result = await self._call(
            "SELECT aap_assets_v1.discard_dataset_upload("
            "$1::uuid, $2::uuid, $3::uuid, $4::text)",
            (
                request.actor_id,
                request.dataset_id,
                request.item_id,
                request.reason,
            ),
            lambda payload: decode_dataset_upload_reply(payload, DatasetUploadIntent),
            (DatasetUploadFailure,),
        )
        self._require_scope(
            result,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        return result

    @staticmethod
    def _require_scope(
        result: DatasetUploadIntent | DatasetUploadAcceptance,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        item_id: UUID,
    ) -> None:
        if (result.actor_id, result.dataset_id, result.item_id) != (
            actor_id,
            dataset_id,
            item_id,
        ):
            raise SourceFailure("invalid_response")


class NativeLegacyUploadData(_NativeData):
    async def lock(self, request: LegacyUploadLockRequest) -> LegacyUploadState:
        result = await self._call(
            "SELECT aap_assets_v1.lock_legacy_dataset_upload("
            "$1::uuid, $2::uuid, $3::uuid, $4::uuid)",
            (
                request.operation_id,
                request.actor_id,
                request.dataset_id,
                request.item_id,
            ),
            decode_legacy_upload_reply,
            (DatasetUploadFailure,),
        )
        self._require_scope(result, request)
        return result

    async def complete(self, request: LegacyUploadCompletion) -> LegacyUploadState:
        result = await self._call(
            "SELECT aap_assets_v1.complete_legacy_dataset_upload("
            "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::text, $6::bigint, "
            "$7::text, $8::integer, $9::integer)",
            (
                request.operation_id,
                request.actor_id,
                request.dataset_id,
                request.item_id,
                request.expected_file_path,
                request.file_size,
                request.content_hash,
                request.width,
                request.height,
            ),
            decode_legacy_upload_reply,
            (DatasetUploadFailure,),
        )
        self._require_scope(result, request)
        if result.file_path != request.expected_file_path:
            raise SourceFailure("invalid_response")
        return result

    @staticmethod
    def _require_scope(
        result: LegacyUploadState,
        request: LegacyUploadLockRequest | LegacyUploadCompletion,
    ) -> None:
        if (
            result.operation_id,
            result.actor_id,
            result.dataset_id,
            result.item_id,
        ) != (
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.item_id,
        ):
            raise SourceFailure("invalid_response")


class NativeDeletionData(_NativeData):
    async def delete(
        self, command: DatasetSourceDeletionCommand
    ) -> DatasetSourceDeletionReceipt:
        audit = (
            command.audit_context.model_dump(mode="json")
            if command.audit_context is not None
            else None
        )
        if command.item_id is None:
            statement = (
                "SELECT aap_assets_v1.delete_dataset("
                "$1::uuid, $2::uuid, $3::uuid, $4::text, $5::jsonb)"
            )
            arguments = (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                command.cleanup_bucket,
                audit,
            )
        else:
            statement = (
                "SELECT aap_assets_v1.delete_dataset_item("
                "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::text, $6::jsonb)"
            )
            arguments = (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                command.item_id,
                command.cleanup_bucket,
                audit,
            )
        receipt = await self._call(
            statement,
            arguments,
            decode_source_deletion_reply,
            (DatasetSourceDeletionFailure,),
        )
        if (
            receipt.operation_id,
            receipt.actor_id,
            receipt.dataset_id,
            receipt.item_id,
            receipt.cleanup_bucket,
        ) != (
            command.operation_id,
            command.actor_id,
            command.dataset_id,
            command.item_id,
            command.cleanup_bucket,
        ):
            raise SourceFailure("invalid_response")
        return receipt


class SourceDatabase:
    """Bound pool with startup role preflight and one-transaction operations."""

    def __init__(self, pool: asyncpg.Pool, config: SourceConfig) -> None:
        self._pool = pool
        self._config = config

    @classmethod
    async def connect(cls, config: SourceConfig) -> SourceDatabase:
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
    async def transaction(self) -> AsyncIterator[SourceDataPorts]:
        async with self._pool.acquire(
            timeout=self._config.pool_timeout_seconds
        ) as connection:
            async with connection.transaction():
                yield SourceDataPorts(
                    uploads=NativeUploadData(
                        connection, self._config.max_response_bytes
                    ),
                    legacy_uploads=NativeLegacyUploadData(
                        connection, self._config.max_response_bytes
                    ),
                    deletions=NativeDeletionData(
                        connection, self._config.max_response_bytes
                    ),
                )

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
                "Assets source login does not have the required scoped capabilities"
            )


async def _init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "json", schema="pg_catalog", encoder=json.dumps, decoder=json.loads
    )
    await connection.set_type_codec(
        "jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")
