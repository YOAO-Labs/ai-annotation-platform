"""asyncpg binding for the scoped Assets catalog and spatial SQL capabilities."""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

import asyncpg
from pydantic import BaseModel, ValidationError

from . import spatial_contract as spatial
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
from .spatial_contract import (
    SPATIAL_NATIVE_SIGNATURES,
    SPATIAL_OPERATIONS,
    SPATIAL_SCHEMA,
    SpatialFailure,
    decode_spatial_reply,
    require_spatial_scope,
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
GUARD_SPATIAL_ACTOR_SQL = """
SELECT aap_assets_v1.guard_spatial_actor($1::uuid, $2::text)
"""
LIST_SPATIAL_SCENES_SQL = """
SELECT aap_assets_v1.list_spatial_scenes(
    $1::uuid, $2::uuid, $3::timestamptz, $4::uuid, $5::integer
)
"""
GET_SPATIAL_SCENE_SQL = """
SELECT aap_assets_v1.get_spatial_scene($1::uuid, $2::uuid)
"""
READ_SPATIAL_LAYOUT_SQL = """
SELECT aap_assets_v1.read_spatial_layout(
    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::integer
)
"""
READ_SPATIAL_ITEMS_SQL = """
SELECT aap_assets_v1.read_spatial_items(
    $1::uuid, $2::uuid[], $3::uuid, $4::integer
)
"""
READ_SPATIAL_POSES_SQL = """
SELECT aap_assets_v1.read_spatial_poses(
    $1::uuid, $2::uuid, $3::integer, $4::integer
)
"""
READ_SPATIAL_CALIBRATIONS_SQL = """
SELECT aap_assets_v1.read_spatial_calibrations(
    $1::uuid[], $2::boolean, $3::uuid, $4::integer, $5::integer
)
"""
LOCK_SPATIAL_CALIBRATION_FACTS_SQL = """
SELECT aap_assets_v1.lock_spatial_calibration_facts(
    $1::uuid, $2::uuid, $3::text, $4::uuid, $5::jsonb
)
"""
MUTATE_SPATIAL_SCENE_SQL = """
SELECT aap_assets_v1.mutate_spatial_scene(
    $1::uuid, $2::uuid, $3::uuid, $4::jsonb, $5::jsonb
)
"""
APPLY_SPATIAL_LAYOUT_SQL = """
SELECT aap_assets_v1.apply_spatial_layout(
    $1::uuid, $2::uuid, $3::uuid, $4::text, $5::boolean, $6::jsonb, $7::jsonb
)
"""
UPSERT_SPATIAL_POSES_SQL = """
SELECT aap_assets_v1.upsert_spatial_poses($1::uuid, $2::uuid, $3::jsonb)
"""
CREATE_SPATIAL_SCENE_SQL = """
SELECT aap_assets_v1.create_spatial_scene(
    $1::uuid, $2::uuid, $3::uuid, $4::text, $5::text, $6::jsonb, $7::uuid
)
"""
ASSIGN_SPATIAL_ITEMS_SQL = """
SELECT aap_assets_v1.assign_spatial_items($1::uuid, $2::uuid, $3::jsonb)
"""
SET_SPATIAL_SCENE_SOURCE_SQL = """
SELECT aap_assets_v1.set_spatial_scene_source(
    $1::uuid, $2::uuid, $3::text, $4::jsonb
)
"""
UPDATE_SPATIAL_CALIBRATION_SQL = """
SELECT aap_assets_v1.update_spatial_calibration(
    $1::uuid, $2::uuid, $3::uuid, $4::text, $5::integer, $6::text,
    $7::jsonb, $8::text, $9::text, $10::jsonb, $11::jsonb
)
"""
READ_POINTCLOUD_PREPARATION_SQL = """
SELECT aap_assets_v1.read_pointcloud_preparation(
    $1::uuid, $2::uuid, $3::integer
)
"""
APPLY_POINTCLOUD_PREPARATION_SQL = """
SELECT aap_assets_v1.apply_pointcloud_preparation(
    $1::uuid, $2::jsonb, $3::jsonb, $4::jsonb
)
"""
READ_SPATIAL_CAMERA_TARGET_SQL = """
SELECT aap_assets_v1.read_spatial_camera_target($1::uuid, $2::uuid, $3::text)
"""
RECORD_SPATIAL_CALIBRATION_PROVENANCE_SQL = """
SELECT aap_assets_v1.record_spatial_calibration_provenance(
    $1::uuid, $2::uuid, $3::jsonb
)
"""
SET_SPATIAL_DATASET_AXIS_SQL = """
SELECT aap_assets_v1.set_spatial_dataset_axis($1::uuid, $2::uuid, $3::text)
"""

_CATALOG_FUNCTIONS = frozenset(
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
_SPATIAL_FUNCTIONS = frozenset(
    f"{name}({signature})" for name, signature in SPATIAL_NATIVE_SIGNATURES.items()
)
EXPECTED_ASSET_FUNCTIONS = _CATALOG_FUNCTIONS | _SPATIAL_FUNCTIONS

_EXPECTED_FUNCTION_VALUES_SQL = ",\n".join(
    f"        ('{signature}'::text)" for signature in sorted(EXPECTED_ASSET_FUNCTIONS)
)

ROLE_PREFLIGHT_SQL = f"""
WITH expected(signature) AS (
    VALUES
{_EXPECTED_FUNCTION_VALUES_SQL}
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
    SELECT proc.oid AS function_oid
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
    AND NOT EXISTS (
        SELECT 1
        FROM expected_oids AS expected_row
        WHERE expected_row.function_oid IS NULL
           OR NOT EXISTS (
               SELECT 1
               FROM accessible_assets_functions AS accessible_row
               WHERE accessible_row.function_oid = expected_row.function_oid
           )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM accessible_assets_functions AS accessible_row
        WHERE NOT EXISTS (
            SELECT 1
            FROM expected_oids AS expected_row
            WHERE expected_row.function_oid = accessible_row.function_oid
        )
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

    async def guard_spatial_actor(
        self, request: spatial.GuardSpatialActorRequest
    ) -> spatial.SpatialActorResult:
        return await self._spatial_call(
            "guard_spatial_actor",
            GUARD_SPATIAL_ACTOR_SQL,
            request,
            request.actor_id,
            request.access,
        )

    async def list_spatial_scenes(
        self, request: spatial.ListScenesRequest
    ) -> spatial.ScenePage:
        after = request.after
        return await self._spatial_call(
            "list_spatial_scenes",
            LIST_SPATIAL_SCENES_SQL,
            request,
            request.actor_id,
            request.dataset_id,
            after.created_at if after else None,
            after.scene_id if after else None,
            request.page_size,
        )

    async def get_spatial_scene(
        self, request: spatial.GetSceneRequest
    ) -> spatial.SceneResult:
        return await self._spatial_call(
            "get_spatial_scene",
            GET_SPATIAL_SCENE_SQL,
            request,
            request.actor_id,
            request.scene_id,
        )

    async def read_spatial_layout(
        self, request: spatial.ReadLayoutRequest
    ) -> spatial.LayoutPage:
        return await self._spatial_call(
            "read_spatial_layout",
            READ_SPATIAL_LAYOUT_SQL,
            request,
            request.actor_id,
            request.dataset_id,
            request.creation_operation_id,
            request.after_item_id,
            request.page_size,
        )

    async def read_spatial_items(
        self, request: spatial.ReadItemsRequest
    ) -> spatial.ItemFactsPage:
        return await self._spatial_call(
            "read_spatial_items",
            READ_SPATIAL_ITEMS_SQL,
            request,
            request.dataset_id,
            request.item_ids,
            request.after_item_id,
            request.page_size,
        )

    async def read_spatial_poses(
        self, request: spatial.ReadPosesRequest
    ) -> spatial.PosePage:
        return await self._spatial_call(
            "read_spatial_poses",
            READ_SPATIAL_POSES_SQL,
            request,
            request.actor_id,
            request.scene_id,
            request.after_frame_index,
            request.page_size,
        )

    async def read_spatial_calibrations(
        self, request: spatial.ReadCalibrationsRequest
    ) -> spatial.CalibrationPage:
        after = request.after
        return await self._spatial_call(
            "read_spatial_calibrations",
            READ_SPATIAL_CALIBRATIONS_SQL,
            request,
            request.dataset_item_ids,
            request.include_history,
            after.dataset_item_id if after else None,
            after.revision if after else None,
            request.page_size,
        )

    async def lock_spatial_calibration_facts(
        self, request: spatial.LockCalibrationFactsRequest
    ) -> spatial.CalibrationRecord:
        def check_source_snapshot(result: BaseModel) -> None:
            if result.source_snapshot != request.expected_source_snapshot:
                raise ValueError("Calibration facts escaped the source snapshot")

        return await self._spatial_call(
            "lock_spatial_calibration_facts",
            LOCK_SPATIAL_CALIBRATION_FACTS_SQL,
            request,
            request.actor_id,
            request.task_id,
            request.camera_role,
            request.dataset_item_id,
            request.expected_source_snapshot,
            result_validator=check_source_snapshot,
        )

    async def mutate_spatial_scene(
        self, request: spatial.MutateSceneRequest
    ) -> spatial.SceneMutationResult:
        return await self._spatial_call(
            "mutate_spatial_scene",
            MUTATE_SPATIAL_SCENE_SQL,
            request,
            request.operation_id,
            request.actor_id,
            request.scene_id,
            request.patch.native_payload(),
            request.audit_context,
        )

    async def apply_spatial_layout(
        self, request: spatial.ApplyLayoutRequest
    ) -> spatial.LayoutMutationResult:
        return await self._spatial_call(
            "apply_spatial_layout",
            APPLY_SPATIAL_LAYOUT_SQL,
            request,
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.mode,
            request.dry_run,
            request.plan.model_dump(mode="json"),
            request.snapshot,
        )

    async def upsert_spatial_poses(
        self, request: spatial.UpsertPosesRequest
    ) -> spatial.PoseMutationResult:
        return await self._spatial_call(
            "upsert_spatial_poses",
            UPSERT_SPATIAL_POSES_SQL,
            request,
            request.operation_id,
            request.scene_id,
            [pose.model_dump(mode="json") for pose in request.poses],
        )

    async def create_spatial_scene(
        self, request: spatial.CreateSceneRequest
    ) -> spatial.SceneMutationResult:
        return await self._spatial_call(
            "create_spatial_scene",
            CREATE_SPATIAL_SCENE_SQL,
            request,
            request.operation_id,
            request.scene_id,
            request.dataset_id,
            request.name,
            request.source_format,
            request.source_metadata,
            request.created_by,
        )

    async def assign_spatial_items(
        self, request: spatial.AssignItemsRequest
    ) -> spatial.AssignmentMutationResult:
        return await self._spatial_call(
            "assign_spatial_items",
            ASSIGN_SPATIAL_ITEMS_SQL,
            request,
            request.operation_id,
            request.dataset_id,
            [assignment.model_dump(mode="json") for assignment in request.assignments],
        )

    async def set_spatial_scene_source(
        self, request: spatial.SetSceneSourceRequest
    ) -> spatial.SceneMutationResult:
        return await self._spatial_call(
            "set_spatial_scene_source",
            SET_SPATIAL_SCENE_SOURCE_SQL,
            request,
            request.operation_id,
            request.scene_id,
            request.source_format,
            request.source_metadata,
        )

    async def update_spatial_calibration(
        self, request: spatial.ApplyCalibrationRequest
    ) -> spatial.CalibrationMutationResult:
        def check_source_snapshot(result: BaseModel) -> None:
            if result.state.source_snapshot != request.source_snapshot:
                raise ValueError("Calibration mutation escaped the source snapshot")

        return await self._spatial_call(
            "update_spatial_calibration",
            UPDATE_SPATIAL_CALIBRATION_SQL,
            request,
            request.operation_id,
            request.actor_id,
            request.task_id,
            request.camera_role,
            request.expected_revision,
            request.expected_digest,
            request.calibration.model_dump(mode="json"),
            request.canonical_document,
            request.current_canonical_document,
            request.source_snapshot,
            request.audit_context,
            result_validator=check_source_snapshot,
        )

    async def read_pointcloud_preparation(
        self, request: spatial.ReadPreparationRequest
    ) -> spatial.PreparationPage:
        return await self._spatial_call(
            "read_pointcloud_preparation",
            READ_POINTCLOUD_PREPARATION_SQL,
            request,
            request.creation_operation_id,
            request.after_item_id,
            request.page_size,
        )

    async def apply_pointcloud_preparation(
        self, request: spatial.ApplyPreparationRequest
    ) -> spatial.PreparationMutationResult:
        return await self._spatial_call(
            "apply_pointcloud_preparation",
            APPLY_POINTCLOUD_PREPARATION_SQL,
            request,
            request.creation_operation_id,
            request.snapshot,
            [update.model_dump(mode="json") for update in request.calibration_updates],
            request.frame_plan.model_dump(mode="json"),
        )

    async def read_spatial_camera_target(
        self, request: spatial.ReadCameraTargetRequest
    ) -> spatial.CameraTargetResult:
        return await self._spatial_call(
            "read_spatial_camera_target",
            READ_SPATIAL_CAMERA_TARGET_SQL,
            request,
            request.actor_id,
            request.task_id,
            request.camera_role,
        )

    async def record_spatial_calibration_provenance(
        self, request: spatial.RecordCalibrationProvenanceRequest
    ) -> spatial.ProvenanceMutationResult:
        return await self._spatial_call(
            "record_spatial_calibration_provenance",
            RECORD_SPATIAL_CALIBRATION_PROVENANCE_SQL,
            request,
            request.operation_id,
            request.dataset_id,
            [entry.model_dump(mode="json") for entry in request.entries],
        )

    async def set_spatial_dataset_axis(
        self, request: spatial.SetDatasetAxisRequest
    ) -> spatial.DatasetAxisMutationResult:
        return await self._spatial_call(
            "set_spatial_dataset_axis",
            SET_SPATIAL_DATASET_AXIS_SQL,
            request,
            request.operation_id,
            request.dataset_id,
            request.axis_convention,
        )

    async def _spatial_call(
        self,
        operation: str,
        sql: str,
        request: BaseModel,
        *arguments: Any,
        result_validator: Callable[[BaseModel], None] | None = None,
    ) -> BaseModel:
        expected_request, result_type = SPATIAL_OPERATIONS[operation]
        if not isinstance(request, expected_request):
            raise TypeError(f"Invalid request model for {operation}")

        def validate(result: BaseModel) -> None:
            require_spatial_scope(request, result)
            if result_validator is not None:
                result_validator(result)

        return await self._call(
            sql,
            result_type,
            *arguments,
            decoder=decode_spatial_reply,
            result_validator=validate,
        )

    async def _call(
        self,
        sql: str,
        result_type: type[_ResultModel],
        *arguments: Any,
        decoder: _ReplyDecoder = decode_reply,
        result_validator: Callable[[_ResultModel], None] | None = None,
    ) -> _ResultModel:
        is_association_reply = decoder is decode_associations_reply
        is_spatial_reply = decoder is decode_spatial_reply
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
                            else "Assets spatial reply exceeds the response budget"
                            if is_spatial_reply
                            else "Assets catalog native reply exceeds the response budget"
                        )
                        raise DataContractError(message)
                    try:
                        raw_result = json.loads(raw_result)
                    except json.JSONDecodeError as exc:
                        message = (
                            "Assets associations returned invalid JSON"
                            if is_association_reply
                            else "Assets spatial returned invalid JSON"
                            if is_spatial_reply
                            else "Assets catalog returned invalid JSON"
                        )
                        raise DataContractError(message) from exc
                if self._encoded_size_exceeds_budget(raw_result):
                    message = (
                        "Assets associations reply exceeds the response budget"
                        if is_association_reply
                        else "Assets spatial reply exceeds the response budget"
                        if is_spatial_reply
                        else "Assets catalog native reply exceeds the response budget"
                    )
                    raise DataContractError(message)
                try:
                    decoded = decoder(raw_result, result_type)
                    if result_validator is not None:
                        result_validator(decoded)
                    if is_spatial_reply:
                        encoded = json.dumps(
                            {
                                "schema": SPATIAL_SCHEMA,
                                "ok": True,
                                "data": decoded.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        if len(encoded) > self._max_reply_bytes:
                            raise DataContractError(
                                "Assets spatial encoded result exceeds the response budget"
                            )
                except (CatalogFailure, AssociationFailure, SpatialFailure):
                    raise
                except DataContractError:
                    raise
                except (TypeError, ValueError, ValidationError) as exc:
                    message = (
                        "Assets associations returned a reply outside the frozen contract"
                        if is_association_reply
                        else "Assets spatial returned a reply outside the frozen contract"
                        if is_spatial_reply
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
