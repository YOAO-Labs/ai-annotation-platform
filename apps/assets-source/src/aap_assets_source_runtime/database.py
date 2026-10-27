"""asyncpg bindings for the public Assets source capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from typing import Any, AsyncIterator, Callable, TypeVar
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ValidationError

from .config import SourceConfig
from aap_assets_source.connections_contract import (
    ConnectionProbeFinish,
    ConnectionProbeLease,
    CONNECTION_NATIVE_ARGUMENTS,
    CONNECTION_NATIVE_OPERATIONS,
    CONNECTION_NATIVE_SIGNATURES,
    RUNTIME_NATIVE_ARGUMENTS,
    RUNTIME_NATIVE_OPERATIONS,
    RUNTIME_NATIVE_SIGNATURES,
    CaptureConnectionRequest,
    ConnectionCapture,
    ConnectionFailure,
    ConnectionPolicy,
    ConnectionPolicyRequest,
    ConnectionProbeReceipt,
    ConnectionReceiptLookup,
    ConnectionReceiptRequest,
    CreateConnectionNativeRequest,
    DeleteConnectionRequest,
    ListConnectionsRequest,
    NativeConnectionMutation,
    NativeConnectionRead,
    NativeConnectionsPage,
    ReadConnectionRequest,
    RecordConnectionTestRequest,
    decode_connection_probe_reply,
    decode_connection_reply,
    require_connection_scope,
)
from aap_assets_source.contract import SourceFailure, UploadInitRequest
from aap_assets_source.ingest_contract import (
    IngestBatchPlan,
    IngestBatchResult,
    IngestCommand,
    IngestContext,
    IngestDescriptor,
    IngestEntryCompletion,
    IngestFailure,
    IngestFinish,
    IngestLayoutPage,
    IngestReceipt,
    IngestSourceEntry,
    decode_ingest_reply,
    require_ingest_scope,
)
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
from aap_assets_source.managed_original_contract import (
    ManagedOriginalFailure,
    ManagedOriginalMaterial,
    ManagedOriginalOperation,
    decode_managed_original_reply,
    require_managed_original_ok,
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
from .image_admission import AsyncpgImageAdmissionData

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
        "begin_dataset_ingest(uuid,uuid,uuid,jsonb)",
        "prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
        "complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
        "read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer)",
        "finish_dataset_ingest(uuid,uuid,uuid,jsonb)",
        "resolve_internal_image_source(text,uuid,text,jsonb)",
        "schedule_image_media(uuid,text,uuid,jsonb,boolean)",
        "read_legacy_dataset_upload(uuid,uuid,uuid,uuid)",
        "begin_operator_auxiliary(jsonb)",
        "open_operator_storage_operation(uuid)",
        "record_operator_original_prepared(uuid,jsonb)",
        "mark_operator_original_writing(uuid)",
        "record_operator_original_written(uuid,jsonb)",
        "record_operator_original_verified(uuid,jsonb)",
        "fail_operator_original(uuid,text,text,jsonb)",
        "close_operator_storage_operation(uuid,uuid,boolean)",
    }
    | {
        f"{name}({signature})"
        for name, signature in {
            **CONNECTION_NATIVE_SIGNATURES,
            **RUNTIME_NATIVE_SIGNATURES,
        }.items()
    }
)

_EXPECTED_FUNCTION_VALUES = ",\n        ".join(
    f"('{signature}'::text)" for signature in sorted(EXPECTED_ASSET_SOURCE_FUNCTIONS)
)

ROLE_PREFLIGHT_SQL = """
WITH expected(signature) AS (
    VALUES
        {expected_functions}
),
target_login AS (
    SELECT role_row.*
    FROM pg_catalog.pg_roles AS role_row
    WHERE role_row.rolname = current_user
),
expected_oids AS (
    SELECT pg_catalog.to_regprocedure(
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
    AND (
        SELECT pg_catalog.array_agg(function_oid ORDER BY function_oid)
        FROM accessible_assets_functions
    ) = (
        SELECT pg_catalog.array_agg(function_oid ORDER BY function_oid)
        FROM expected_oids
    )
    AND runtime_privileges.no_table_privileges
    AND runtime_privileges.no_column_privileges
    AND runtime_privileges.no_sequence_privileges AS is_scoped
FROM target_login AS login_role
CROSS JOIN runtime_privileges
""".replace("{expected_functions}", _EXPECTED_FUNCTION_VALUES)

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
    ingest: NativeIngestData
    connections: NativeConnectionsData
    connection_probes: NativeConnectionProbeData
    images: AsyncpgImageAdmissionData


class _NativeData:
    def __init__(
        self,
        connection: asyncpg.Connection,
        max_reply_bytes: int,
        *,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._connection = connection
        self._max_reply_bytes = max_reply_bytes
        self._original_pool = pool

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

    async def _original_reply(self, statement: str, arguments: tuple[Any, ...]) -> Any:
        if self._original_pool is None:
            raw = await self._connection.fetchval(statement, *arguments)
        else:
            async with self._original_pool.acquire() as connection:
                raw = await connection.fetchval(statement, *arguments)
        return self._parse_payload(raw)

    async def _original_ok(self, statement: str, arguments: tuple[Any, ...]) -> Any:
        raw = await self._original_reply(statement, arguments)
        require_managed_original_ok(raw)
        return raw

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

    async def allocate(self, request: dict[str, Any]) -> ManagedOriginalMaterial:
        """Allocate independently and durably before any physical I/O."""
        payload = dict(request)
        payload.setdefault("kind", "role_media")
        raw = await self._original_reply(
            "SELECT aap_assets_v1.begin_operator_auxiliary($1::jsonb)",
            (payload,),
        )
        try:
            return decode_managed_original_reply(raw, ManagedOriginalMaterial)
        except ManagedOriginalFailure:
            raise
        except (TypeError, ValueError, ValidationError):
            raise SourceFailure("invalid_response") from None

    async def open_operation(self, creation_id: UUID) -> ManagedOriginalOperation:
        """Acquire this creation's one permit; verified reuse stays read-only."""
        raw = await self._original_reply(
            "SELECT aap_assets_v1.open_operator_storage_operation($1::uuid)",
            (creation_id,),
        )
        try:
            return decode_managed_original_reply(raw, ManagedOriginalOperation)
        except ManagedOriginalFailure:
            raise
        except (TypeError, ValueError, ValidationError):
            raise SourceFailure("invalid_response") from None

    async def original_prepared(
        self, creation_id: UUID, prepared: dict[str, Any]
    ) -> None:
        await self._original_ok(
            "SELECT aap_assets_v1.record_operator_original_prepared($1::uuid,$2::jsonb)",
            (creation_id, prepared),
        )

    async def original_writing(self, creation_id: UUID) -> None:
        await self._original_ok(
            "SELECT aap_assets_v1.mark_operator_original_writing($1::uuid)",
            (creation_id,),
        )

    async def original_written(
        self, creation_id: UUID, receipt: dict[str, Any]
    ) -> None:
        await self._original_ok(
            "SELECT aap_assets_v1.record_operator_original_written($1::uuid,$2::jsonb)",
            (creation_id, receipt),
        )

    async def original_verified(
        self, creation_id: UUID, verification: dict[str, Any]
    ) -> None:
        await self._original_ok(
            "SELECT aap_assets_v1.record_operator_original_verified($1::uuid,$2::jsonb)",
            (creation_id, verification),
        )

    async def fail_original(
        self,
        creation_id: UUID,
        outcome: str,
        error_code: str,
        evidence: dict[str, Any],
    ) -> None:
        await self._original_ok(
            "SELECT aap_assets_v1.fail_operator_original("
            "$1::uuid,$2::text,$3::text,$4::jsonb)",
            (creation_id, outcome, error_code, evidence),
        )

    async def close_operation(
        self, creation_id: UUID, operation_id: UUID, physical_settled: bool
    ) -> None:
        """Settle only this creation's own read permit after a proven drain."""
        await self._original_ok(
            "SELECT aap_assets_v1.close_operator_storage_operation("
            "$1::uuid,$2::uuid,$3::boolean)",
            (creation_id, operation_id, physical_settled),
        )


class NativeConnectionProbeData(_NativeData):
    """Probe lease and finish bindings with an independently durable ledger."""

    _pool = None

    def __init__(self, connection, max_reply_bytes, *, pool=None):
        super().__init__(connection, max_reply_bytes)
        self._pool = pool

    async def begin_connection_probe(self, request):
        return await self._runtime(
            "begin_connection_probe", request, ConnectionProbeLease
        )

    async def finish_connection_probe(self, request):
        return await self._runtime(
            "finish_connection_probe", request, ConnectionProbeFinish
        )

    async def _runtime(self, name, request, result_type):
        try:
            request_type, expected_result_type = RUNTIME_NATIVE_OPERATIONS[name]
            arguments = RUNTIME_NATIVE_ARGUMENTS[name]
            kinds = RUNTIME_NATIVE_SIGNATURES[name].split(",")
            if type(request) is not request_type:
                raise ConnectionFailure("invalid_request")
            if result_type is not expected_result_type or len(arguments) != len(kinds):
                raise ConnectionFailure("invalid_response")
        except KeyError:
            raise ConnectionFailure("invalid_request") from None
        placeholders = ", ".join(
            f"CAST(${index} AS {kind})" for index, kind in enumerate(kinds, 1)
        )
        values = []
        for argument, kind in zip(arguments, kinds, strict=True):
            value = getattr(request, argument)
            if kind == "jsonb":
                # The pool registers json/jsonb codecs, so bind objects not text.
                values.append(
                    value.model_dump(mode="json")
                    if hasattr(value, "model_dump")
                    else value
                )
            elif kind == "text[]":
                values.append(list(value))
            else:
                values.append(value)
        statement = f"SELECT aap_assets_v1.{name}({placeholders})"
        if self._pool is None:
            raw = await self._connection.fetchval(statement, *values)
        else:
            # Independently durable ledger/fence: a fresh pool transaction, not
            # the caller's operation transaction.
            async with self._pool.acquire() as connection:
                raw = await connection.fetchval(statement, *values)
        try:
            return decode_connection_probe_reply(self._parse_payload(raw), result_type)
        except ConnectionFailure:
            raise
        except SourceFailure:
            raise ConnectionFailure("invalid_response") from None
        except (TypeError, ValueError, ValidationError):
            raise ConnectionFailure("invalid_response") from None


class NativeConnectionsData(NativeConnectionProbeData):
    """Bindings for the nine Assets-owned connection capabilities."""

    async def list_storage_connections(
        self, request: ListConnectionsRequest
    ) -> NativeConnectionsPage:
        after = request.after
        return await self._call_connection(
            "list_storage_connections",
            request,
            (
                request.actor_id,
                after.created_at if after is not None else None,
                after.id if after is not None else None,
                request.page_size,
            ),
            NativeConnectionsPage,
        )

    async def read_storage_connection(
        self, request: ReadConnectionRequest
    ) -> NativeConnectionRead:
        return await self._call_connection(
            "read_storage_connection",
            request,
            (request.actor_id, request.connection_id),
            NativeConnectionRead,
        )

    async def read_storage_connection_policy(
        self, request: ConnectionPolicyRequest
    ) -> ConnectionPolicy:
        return await self._call_connection(
            "read_storage_connection_policy",
            request,
            (
                request.actor_id,
                request.fallback_allowlist,
            ),
            ConnectionPolicy,
        )

    async def capture_storage_connection(
        self, request: CaptureConnectionRequest
    ) -> ConnectionCapture:
        return await self._call_connection(
            "capture_storage_connection",
            request,
            (
                request.actor_id,
                request.connection_id,
                request.access,
            ),
            ConnectionCapture,
        )

    async def read_storage_connection_receipt(
        self, request: ConnectionReceiptRequest
    ) -> ConnectionReceiptLookup:
        return await self._call_connection(
            "read_storage_connection_receipt",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.action,
                request.connection_id,
                request.intent.model_dump(mode="json"),
            ),
            ConnectionReceiptLookup,
        )

    async def create_storage_connection(
        self, request: CreateConnectionNativeRequest
    ) -> NativeConnectionMutation:
        return await self._call_connection(
            "create_storage_connection",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.intent.model_dump(mode="json"),
                request.config,
                request.secret_enc,
                request.policy_digest,
                request.fallback_allowlist,
                request.audit_context.model_dump(mode="json"),
            ),
            NativeConnectionMutation,
        )

    async def update_storage_connection_authorized(
        self, request
    ) -> NativeConnectionMutation:
        return await self._call_connection(
            "update_storage_connection_authorized",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.connection_id,
                request.intent.model_dump(mode="json"),
                request.config,
                request.secret_enc,
                request.source_snapshot,
                request.policy_digest,
                request.fallback_allowlist,
                request.audit_context.model_dump(mode="json"),
                request.api_key_id,
                request.required_scopes,
                request.expected_proof.model_dump(mode="json")
                if request.expected_proof is not None
                else None,
            ),
            NativeConnectionMutation,
        )

    async def delete_storage_connection(
        self, request: DeleteConnectionRequest
    ) -> NativeConnectionMutation:
        return await self._call_connection(
            "delete_storage_connection",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.connection_id,
                request.audit_context.model_dump(mode="json"),
            ),
            NativeConnectionMutation,
        )

    async def record_storage_connection_test(
        self, request: RecordConnectionTestRequest
    ) -> ConnectionProbeReceipt:
        return await self._call_connection(
            "record_storage_connection_test",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.connection_id,
                request.source_snapshot,
                request.policy_digest,
                request.fallback_allowlist,
                request.result.model_dump(mode="json"),
                request.audit_context.model_dump(mode="json"),
            ),
            ConnectionProbeReceipt,
        )

    async def _call_connection(
        self,
        operation: str,
        request: BaseModel,
        arguments: tuple[Any, ...],
        result_type: type[_Result],
    ) -> _Result:
        try:
            request_type, expected_result_type = CONNECTION_NATIVE_OPERATIONS[operation]
            if type(request) is not request_type:
                raise ConnectionFailure("invalid_request")
            if result_type is not expected_result_type or len(arguments) != len(
                CONNECTION_NATIVE_ARGUMENTS[operation]
            ):
                raise ConnectionFailure("invalid_response")
            signature = CONNECTION_NATIVE_SIGNATURES[operation]
        except (KeyError, TypeError):
            raise ConnectionFailure("invalid_request") from None

        placeholders = ", ".join(
            f"${index}::{type_name}"
            for index, type_name in enumerate(signature.split(","), start=1)
        )
        statement = f"SELECT aap_assets_v1.{operation}({placeholders})"
        raw = await self._connection.fetchval(statement, *arguments)
        try:
            payload = self._parse_payload(raw)
            result = decode_connection_reply(payload, result_type)
            require_connection_scope(request, result)
        except ConnectionFailure:
            raise
        except SourceFailure:
            raise ConnectionFailure("invalid_response") from None
        except (TypeError, ValueError, ValidationError):
            raise ConnectionFailure("invalid_response") from None
        return result


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
    async def read(self, request: LegacyUploadLockRequest) -> LegacyUploadState:
        result = await self._call(
            "SELECT aap_assets_v1.read_legacy_dataset_upload("
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


class NativeIngestData(_NativeData):
    """Bindings for the five transaction-bound source ingest capabilities."""

    async def begin(
        self, command: IngestCommand, descriptor: IngestDescriptor
    ) -> IngestContext:
        result = await self._call_ingest(
            "SELECT aap_assets_v1.begin_dataset_ingest("
            "$1::uuid, $2::uuid, $3::uuid, $4::jsonb)",
            (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                descriptor.model_dump(mode="json"),
            ),
            IngestContext,
        )
        require_ingest_scope(command, result)
        return result

    async def prepare_batch(
        self,
        command: IngestCommand,
        batch_id: UUID,
        entries: list[IngestSourceEntry],
    ) -> IngestBatchPlan:
        result = await self._call_ingest(
            "SELECT aap_assets_v1.prepare_dataset_ingest_batch("
            "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::jsonb)",
            (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                batch_id,
                [entry.model_dump(mode="json") for entry in entries],
            ),
            IngestBatchPlan,
        )
        require_ingest_scope(command, result)
        if result.batch_id != batch_id:
            raise IngestFailure("invalid_response")
        return result

    async def complete_batch(
        self,
        command: IngestCommand,
        batch_id: UUID,
        entries: list[IngestEntryCompletion],
    ) -> IngestBatchResult:
        result = await self._call_ingest(
            "SELECT aap_assets_v1.complete_dataset_ingest_batch("
            "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::jsonb)",
            (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                batch_id,
                [entry.model_dump(mode="json") for entry in entries],
            ),
            IngestBatchResult,
        )
        require_ingest_scope(command, result)
        if result.batch_id != batch_id:
            raise IngestFailure("invalid_response")
        return result

    async def read_layout(
        self,
        command: IngestCommand,
        *,
        after_item_id: UUID | None,
        limit: int = 500,
    ) -> IngestLayoutPage:
        result = await self._call_ingest(
            "SELECT aap_assets_v1.read_dataset_ingest_layout("
            "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::integer)",
            (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                after_item_id,
                limit,
            ),
            IngestLayoutPage,
        )
        require_ingest_scope(command, result)
        return result

    async def finish(
        self, command: IngestCommand, finish: IngestFinish
    ) -> IngestReceipt:
        result = await self._call_ingest(
            "SELECT aap_assets_v1.finish_dataset_ingest("
            "$1::uuid, $2::uuid, $3::uuid, $4::jsonb)",
            (
                command.operation_id,
                command.actor_id,
                command.dataset_id,
                finish.model_dump(mode="json"),
            ),
            IngestReceipt,
        )
        require_ingest_scope(command, result)
        return result

    async def _call_ingest(
        self, statement: str, arguments: tuple[Any, ...], result_type: type[_Result]
    ) -> _Result:
        raw = await self._connection.fetchval(statement, *arguments)
        try:
            payload = self._parse_payload(raw)
        except SourceFailure:
            raise IngestFailure("invalid_response") from None
        try:
            result = decode_ingest_reply(payload, result_type)
        except IngestFailure:
            raise
        except (TypeError, ValueError, ValidationError):
            raise IngestFailure("invalid_response") from None
        if result is None:
            raise IngestFailure("invalid_response")
        return result


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
                        connection, self._config.max_response_bytes, pool=self._pool
                    ),
                    legacy_uploads=NativeLegacyUploadData(
                        connection, self._config.max_response_bytes
                    ),
                    deletions=NativeDeletionData(
                        connection, self._config.max_response_bytes
                    ),
                    ingest=NativeIngestData(
                        connection, self._config.max_response_bytes, pool=self._pool
                    ),
                    connections=NativeConnectionsData(
                        connection, self._config.max_response_bytes, pool=self._pool
                    ),
                    connection_probes=NativeConnectionProbeData(
                        connection, self._config.max_response_bytes
                    ),
                    images=AsyncpgImageAdmissionData(
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
