"""The narrowly granted, versioned PostgreSQL calls used by this worker."""

from __future__ import annotations

import json
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from .contract import (
    AssetStorageDeletion,
    DeletionCursor,
    ImageGarbageClaim,
    ImageGarbageClaimData,
    ImageGarbageCompleteData,
    ImageGarbageEnvelope,
    PendingAssetStorageDeletion,
)

PENDING_SQL = """
SELECT operation_id, asset_id, bucket, prefix, created_at
FROM aap_assets_v1.pending_storage_deletions_v2(
    $1::text, $2::timestamptz, $3::uuid, $4::uuid, $5::integer
)
"""
COMPLETE_SQL = """
SELECT aap_assets_v1.complete_storage_deletion($1::uuid, $2::uuid)
"""
CLAIM_IMAGE_GARBAGE_SQL = """
SELECT aap_assets_v1.claim_image_garbage($1::uuid, $2::integer, $3::integer)
"""
COMPLETE_IMAGE_GARBAGE_SQL = """
SELECT aap_assets_v1.complete_image_garbage($1::uuid, $2::uuid)
"""

_PREFLIGHT_SQL = """
WITH target_login AS (
    SELECT login_role.*
    FROM pg_catalog.pg_roles AS login_role
    WHERE login_role.rolname = current_user
),
allowed_functions AS (
    SELECT
        pg_catalog.to_regprocedure(
            'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'
        )::oid AS pending_oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.complete_storage_deletion(uuid,uuid)'
        )::oid AS complete_oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.claim_image_garbage(uuid,integer,integer)'
        )::oid AS claim_image_oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.complete_image_garbage(uuid,uuid)'
        )::oid AS complete_image_oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)'
        )::oid AS claim_inputs_oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)'
        )::oid AS complete_inputs_oid
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
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = login_role.oid
    )
    AND COALESCE(
        pg_catalog.has_schema_privilege(current_user, 'aap_assets_v1', 'USAGE'),
        false
    )
    AND NOT pg_catalog.has_schema_privilege(current_user, 'aap_assets_v1', 'CREATE')
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'aap_assets_v1'
          AND (
              pg_catalog.has_schema_privilege(current_user, ns.oid, 'USAGE')
              OR pg_catalog.has_schema_privilege(current_user, ns.oid, 'CREATE')
          )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(current_user, ns.oid, 'CREATE')
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc
          ON proc.pronamespace = ns.oid
        CROSS JOIN allowed_functions AS allowed
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND proc.oid <> COALESCE(allowed.pending_oid, 0::oid)
          AND proc.oid <> COALESCE(allowed.complete_oid, 0::oid)
          AND proc.oid <> COALESCE(allowed.claim_image_oid, 0::oid)
          AND proc.oid <> COALESCE(allowed.complete_image_oid, 0::oid)
          AND proc.oid <> COALESCE(allowed.claim_inputs_oid, 0::oid)
          AND proc.oid <> COALESCE(allowed.complete_inputs_oid, 0::oid)
          AND pg_catalog.has_function_privilege(
              current_user, proc.oid, 'EXECUTE'
          )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = ns.oid
        CROSS JOIN (
            VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
                ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_table_privilege(
              current_user, relation.oid, privilege.name
          )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = ns.oid
        CROSS JOIN (
            VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_any_column_privilege(
              current_user, relation.oid, privilege.name
          )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = ns.oid
        CROSS JOIN (
            VALUES ('USAGE'::text), ('SELECT'), ('UPDATE')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind = 'S'
          AND pg_catalog.has_sequence_privilege(
              current_user, relation.oid, privilege.name
          )
    )
    AND COALESCE(
        pg_catalog.has_function_privilege(
            current_user,
            allowed.pending_oid,
            'EXECUTE'
        ),
        false
    )
    AND COALESCE(
        pg_catalog.has_function_privilege(
            current_user,
            allowed.complete_oid,
            'EXECUTE'
        ),
        false
    )
    AND COALESCE(
        pg_catalog.has_function_privilege(
            current_user,
            allowed.claim_image_oid,
            'EXECUTE'
        ),
        false
    )
    AND COALESCE(
        pg_catalog.has_function_privilege(
            current_user,
            allowed.complete_image_oid,
            'EXECUTE'
        ),
        false
    )
    AND COALESCE(
        pg_catalog.has_function_privilege(
            current_user,
            allowed.claim_inputs_oid,
            'EXECUTE'
        ),
        false
    )
    AND COALESCE(
        pg_catalog.has_function_privilege(
            current_user,
            allowed.complete_inputs_oid,
            'EXECUTE'
        ),
        false
    ) AS is_scoped
FROM target_login AS login_role
CROSS JOIN allowed_functions AS allowed
"""


class DataContractError(RuntimeError):
    """The database role or a native function response violated the contract."""


class StorageDeletionData:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def verify_role_scope(self) -> None:
        async with self._pool.acquire() as connection:
            allowed = await connection.fetchval(_PREFLIGHT_SQL)
        if allowed is not True:
            raise DataContractError(
                "Database login is not limited to the Assets cleanup contract"
            )

    async def pending(
        self,
        *,
        bucket: str,
        cursor: DeletionCursor | None,
        limit: int,
    ) -> tuple[PendingAssetStorageDeletion, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Storage cleanup page limit must be 1-1000")
        cursor_values = (
            (None, None, None)
            if cursor is None
            else (cursor.created_at, cursor.operation_id, cursor.asset_id)
        )
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    PENDING_SQL,
                    bucket,
                    *cursor_values,
                    limit,
                )
        try:
            return tuple(
                PendingAssetStorageDeletion.model_validate(dict(row)) for row in rows
            )
        except ValidationError as exc:
            raise DataContractError("Assets returned an invalid cleanup row") from exc

    async def complete(self, reference: AssetStorageDeletion) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                completed = await connection.fetchval(
                    COMPLETE_SQL,
                    reference.operation_id,
                    reference.asset_id,
                )
                if completed is not True:
                    raise DataContractError(
                        "Assets did not acknowledge the matching cleanup row"
                    )

    async def claim_image_garbage(
        self,
        *,
        worker_id: UUID,
        lease_seconds: int,
        batch_size: int,
    ) -> tuple[ImageGarbageClaim, ...]:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("Image cleanup lease must be 1-3600 seconds")
        if not 1 <= batch_size <= 500:
            raise ValueError("Image cleanup batch size must be 1-500")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                payload = await connection.fetchval(
                    CLAIM_IMAGE_GARBAGE_SQL,
                    worker_id,
                    lease_seconds,
                    batch_size,
                )
        data = _decode_image_reply(payload, ImageGarbageClaimData)
        if len(data.items) > batch_size:
            raise DataContractError("Assets exceeded the requested image cleanup batch")
        return tuple(data.items)

    async def complete_image_garbage(self, claim: ImageGarbageClaim) -> None:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                payload = await connection.fetchval(
                    COMPLETE_IMAGE_GARBAGE_SQL,
                    claim.garbage_id,
                    claim.lease_token,
                )
        data = _decode_image_reply(payload, ImageGarbageCompleteData)
        if data.garbage_id != claim.garbage_id or data.completed is not True:
            raise DataContractError(
                "Assets did not acknowledge the matching image cleanup lease"
            )


def _decode_image_reply(payload, result_type):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DataContractError(
                "Assets returned invalid image cleanup JSON"
            ) from exc
    try:
        reply = ImageGarbageEnvelope.model_validate(payload)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DataContractError(
            "Assets returned an invalid image cleanup reply"
        ) from exc
    if not reply.ok:
        if reply.error is None:
            raise DataContractError("Assets returned an invalid image cleanup error")
        code = reply.error.code
        raise DataContractError(f"Assets rejected image cleanup: {code[:64]}")
    try:
        return result_type.model_validate(reply.data)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DataContractError("Assets returned invalid image cleanup data") from exc
