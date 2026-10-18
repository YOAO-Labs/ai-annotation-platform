"""asyncpg binding for the seven public Assets catalog SQL capabilities."""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

import asyncpg
from pydantic import BaseModel, ValidationError

from .config import CatalogConfig
from .contract import (
    CatalogFailure,
    CreateDatasetRequest,
    DatasetMutationResult,
    DatasetPage,
    DatasetResult,
    GetDatasetRequest,
    ItemPage,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
    decode_reply,
)
from .associations_contract import (
    AssociationFailure,
    DatasetProjectsResult,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
    ProjectDatasetsResult,
    decode_associations_reply,
)

LIST_DATASETS_SQL = """
SELECT aap_assets_v1.list_catalog_datasets(
    $1::uuid, $2::text, $3::text, $4::boolean, $5::integer, $6::bigint
)
"""
GET_DATASET_SQL = """
SELECT aap_assets_v1.get_catalog_dataset($1::uuid, $2::uuid)
"""
LIST_ITEMS_SQL = """
SELECT aap_assets_v1.list_catalog_items(
    $1::uuid, $2::uuid, $3::integer, $4::bigint
)
"""
CREATE_DATASET_SQL = """
SELECT aap_assets_v1.create_catalog_dataset($1::uuid, $2::uuid, $3::jsonb)
"""
UPDATE_DATASET_SQL = """
SELECT aap_assets_v1.update_catalog_dataset(
    $1::uuid, $2::uuid, $3::uuid, $4::jsonb
)
"""
LIST_DATASET_PROJECTS_SQL = """
SELECT aap_assets_v1.list_dataset_projects($1::uuid, $2::uuid)
"""
LIST_PROJECT_DATASETS_SQL = """
SELECT aap_assets_v1.list_project_datasets($1::uuid, $2::uuid)
"""

EXPECTED_ASSET_FUNCTIONS = frozenset(
    {
        "list_catalog_datasets(uuid,text,text,boolean,integer,bigint)",
        "get_catalog_dataset(uuid,uuid)",
        "list_catalog_items(uuid,uuid,integer,bigint)",
        "create_catalog_dataset(uuid,uuid,jsonb)",
        "update_catalog_dataset(uuid,uuid,uuid,jsonb)",
        "list_dataset_projects(uuid,uuid)",
        "list_project_datasets(uuid,uuid)",
    }
)

ROLE_PREFLIGHT_SQL = """
WITH expected(signature) AS (
    VALUES
        ('list_catalog_datasets(uuid,text,text,boolean,integer,bigint)'::text),
        ('get_catalog_dataset(uuid,uuid)'::text),
        ('list_catalog_items(uuid,uuid,integer,bigint)'::text),
        ('create_catalog_dataset(uuid,uuid,jsonb)'::text),
        ('update_catalog_dataset(uuid,uuid,uuid,jsonb)'::text),
        ('list_dataset_projects(uuid,uuid)'::text),
        ('list_project_datasets(uuid,uuid)'::text)
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

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)
_ReplyDecoder = Callable[[Any, type[_ResultModel]], _ResultModel]


class DataContractError(RuntimeError):
    """The role or a native Assets catalog reply violated its contract."""


async def _init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "json", schema="pg_catalog", encoder=json.dumps, decoder=json.loads
    )
    await connection.set_type_codec(
        "jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads
    )


class CatalogDatabase:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        acquire_timeout_seconds: float = 2.0,
        max_reply_bytes: int = 16_777_216,
    ) -> None:
        self._pool = pool
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._max_reply_bytes = max_reply_bytes

    @classmethod
    async def connect(cls, config: CatalogConfig) -> CatalogDatabase:
        pool = await asyncpg.create_pool(
            dsn=config.driver_database_url,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            timeout=config.pool_timeout_seconds,
            command_timeout=config.sql_timeout_seconds,
            init=_init_connection,
            server_settings={"application_name": "aap-assets-catalog"},
        )
        database = cls(
            pool,
            acquire_timeout_seconds=config.pool_timeout_seconds,
            max_reply_bytes=config.max_response_bytes,
        )
        try:
            await database.verify_role_scope()
        except Exception:
            await pool.close()
            raise
        return database

    async def close(self) -> None:
        await self._pool.close()

    async def verify_role_scope(self) -> None:
        async with self._pool.acquire(
            timeout=self._acquire_timeout_seconds
        ) as connection:
            allowed = await connection.fetchval(ROLE_PREFLIGHT_SQL)
        if allowed is not True:
            raise DataContractError(
                "Database login is not limited to the Assets catalog contract"
            )

    async def ping(self) -> None:
        async with self._pool.acquire(
            timeout=self._acquire_timeout_seconds
        ) as connection:
            value = await connection.fetchval("SELECT 1")
        if value != 1:
            raise DataContractError("Assets catalog database health check failed")

    async def list_datasets(self, request: ListDatasetsRequest) -> DatasetPage:
        return await self._call(
            LIST_DATASETS_SQL,
            DatasetPage,
            request.actor_id,
            request.search,
            request.data_type,
            request.has_scenes,
            request.limit,
            request.offset,
        )

    async def get_dataset(self, request: GetDatasetRequest) -> DatasetResult:
        return await self._call(
            GET_DATASET_SQL,
            DatasetResult,
            request.actor_id,
            request.dataset_id,
        )

    async def list_items(self, request: ListItemsRequest) -> ItemPage:
        return await self._call(
            LIST_ITEMS_SQL,
            ItemPage,
            request.actor_id,
            request.dataset_id,
            request.limit,
            request.offset,
        )

    async def create_dataset(
        self, request: CreateDatasetRequest
    ) -> DatasetMutationResult:
        return await self._call(
            CREATE_DATASET_SQL,
            DatasetMutationResult,
            request.operation_id,
            request.actor_id,
            request.native_payload(),
        )

    async def update_dataset(
        self, request: UpdateDatasetRequest
    ) -> DatasetMutationResult:
        return await self._call(
            UPDATE_DATASET_SQL,
            DatasetMutationResult,
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.native_payload(),
        )

    async def list_dataset_projects(
        self, request: ListDatasetProjectsRequest
    ) -> DatasetProjectsResult:
        result = await self._call(
            LIST_DATASET_PROJECTS_SQL,
            DatasetProjectsResult,
            request.actor_id,
            request.dataset_id,
            decoder=decode_associations_reply,
        )
        if result.dataset_id != request.dataset_id:
            raise DataContractError(
                "Assets associations response escaped the requested dataset scope"
            )
        return result

    async def list_project_datasets(
        self, request: ListProjectDatasetsRequest
    ) -> ProjectDatasetsResult:
        result = await self._call(
            LIST_PROJECT_DATASETS_SQL,
            ProjectDatasetsResult,
            request.actor_id,
            request.project_id,
            decoder=decode_associations_reply,
        )
        if result.project_id != request.project_id:
            raise DataContractError(
                "Assets associations response escaped the requested project scope"
            )
        return result

    async def _call(
        self,
        sql: str,
        result_type: type[_ResultModel],
        *arguments: Any,
        decoder: _ReplyDecoder = decode_reply,
    ) -> _ResultModel:
        is_association_reply = decoder is decode_associations_reply
        async with self._pool.acquire(
            timeout=self._acquire_timeout_seconds
        ) as connection:
            async with connection.transaction():
                raw_result = await connection.fetchval(sql, *arguments)
                if isinstance(raw_result, str):
                    if len(raw_result.encode("utf-8")) > self._max_reply_bytes:
                        message = (
                            "Assets associations reply exceeds the response budget"
                            if is_association_reply
                            else "Assets catalog native reply exceeds the response budget"
                        )
                        raise DataContractError(message)
                    try:
                        raw_result = json.loads(raw_result)
                    except json.JSONDecodeError as exc:
                        message = (
                            "Assets associations returned invalid JSON"
                            if is_association_reply
                            else "Assets catalog returned invalid JSON"
                        )
                        raise DataContractError(message) from exc
                if self._encoded_size_exceeds_budget(raw_result):
                    message = (
                        "Assets associations reply exceeds the response budget"
                        if is_association_reply
                        else "Assets catalog native reply exceeds the response budget"
                    )
                    raise DataContractError(message)
                try:
                    decoded = decoder(raw_result, result_type)
                except (CatalogFailure, AssociationFailure):
                    raise
                except (TypeError, ValueError, ValidationError) as exc:
                    message = (
                        "Assets associations returned a reply outside the frozen contract"
                        if is_association_reply
                        else "Assets catalog returned a reply outside the frozen contract"
                    )
                    raise DataContractError(message) from exc
        return decoded

    def _encoded_size_exceeds_budget(self, payload: Any) -> bool:
        size = 0
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
        for part in encoder.iterencode(payload):
            size += len(part.encode("utf-8"))
            if size > self._max_reply_bytes:
                return True
        return False
