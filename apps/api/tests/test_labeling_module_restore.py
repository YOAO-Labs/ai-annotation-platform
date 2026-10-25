"""Current-head restore proof for Labeling public metadata and one owned write."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import asyncpg
import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aap_labeling.annotation_write_contract import (
    ANNOTATION_WRITE_PROTOCOL,
    ReadCreateReceipt,
    WriteCreateReceipt,
)
from aap_labeling.annotation_write_data import AnnotationWriteDataClient
from aap_labeling.core_read_contract import CORE_READ_RELATIONS
from aap_owner_data.postgres import verify_function_only_role
from tests.factory import create_task
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_identity_recovery import _alembic, _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _create_owned_clone,
    _database_acl,
    _database_head,
    _drop_owned_clone,
)
from tests.test_labeling_annotation_writes_native import _create
from tests.test_workspace_project_access_step_native import _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_LABELING_SCHEMA = "aap_labeling_v1"
_ANNOTATION_WRITE_ROLE = "labeling_annotation_write_client"
_CORE_READ_ROLE = "labeling_core_read_client"
_ANNOTATION_WRITE_FUNCTIONS = {
    ("annotation_statistics_command", "jsonb"),
    ("scene_track_domain_command", "jsonb"),
    ("annotation_write_command", "jsonb"),
}


async def _assert_owned_database(engine, database: str, marker: str) -> None:
    await assert_owned_test_database(engine)
    assert engine.url.database == database
    async with engine.connect() as connection:
        actual = await connection.scalar(
            text(
                "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
                "FROM pg_catalog.pg_database WHERE datname=:name"
            ),
            {"name": database},
        )
    assert actual == marker


async def _labeling_catalog_snapshot(engine) -> dict[str, tuple]:
    async with engine.connect() as connection:
        schema = (
            await connection.execute(
                text(
                    "SELECT namespace.nspname, owner.rolname, "
                    "(SELECT COALESCE(pg_catalog.jsonb_agg("
                    "pg_catalog.jsonb_build_object("
                    "'grantor', grantor.rolname, "
                    "'grantee', CASE WHEN entry.grantee=0 THEN 'PUBLIC' "
                    "ELSE grantee.rolname END, "
                    "'privilege', entry.privilege_type, "
                    "'grantable', entry.is_grantable) "
                    "ORDER BY entry.grantor, entry.grantee, "
                    "entry.privilege_type, entry.is_grantable), '[]'::jsonb)::text "
                    "FROM pg_catalog.aclexplode(COALESCE(namespace.nspacl, "
                    "pg_catalog.acldefault('n', namespace.nspowner))) AS entry "
                    "JOIN pg_catalog.pg_roles AS grantor "
                    "ON grantor.oid=entry.grantor "
                    "LEFT JOIN pg_catalog.pg_roles AS grantee "
                    "ON grantee.oid=entry.grantee) "
                    "FROM pg_catalog.pg_namespace AS namespace "
                    "JOIN pg_catalog.pg_roles AS owner "
                    "ON owner.oid=namespace.nspowner "
                    "WHERE namespace.nspname=:schema"
                ),
                {"schema": _LABELING_SCHEMA},
            )
        ).one_or_none()
        functions = (
            await connection.execute(
                text(
                    "SELECT function.proname, "
                    "pg_catalog.pg_get_function_identity_arguments(function.oid), "
                    "pg_catalog.pg_get_function_result(function.oid), "
                    "pg_catalog.pg_get_functiondef(function.oid), owner.rolname, "
                    "(SELECT COALESCE(pg_catalog.jsonb_agg("
                    "pg_catalog.jsonb_build_object("
                    "'grantor', grantor.rolname, "
                    "'grantee', CASE WHEN entry.grantee=0 THEN 'PUBLIC' "
                    "ELSE grantee.rolname END, "
                    "'privilege', entry.privilege_type, "
                    "'grantable', entry.is_grantable) "
                    "ORDER BY entry.grantor, entry.grantee, "
                    "entry.privilege_type, entry.is_grantable), '[]'::jsonb)::text "
                    "FROM pg_catalog.aclexplode(COALESCE(function.proacl, "
                    "pg_catalog.acldefault('f', function.proowner))) AS entry "
                    "JOIN pg_catalog.pg_roles AS grantor "
                    "ON grantor.oid=entry.grantor "
                    "LEFT JOIN pg_catalog.pg_roles AS grantee "
                    "ON grantee.oid=entry.grantee), "
                    "function.proconfig::text, "
                    "function.provolatile, function.prosecdef, function.proretset, "
                    "function.prokind "
                    "FROM pg_catalog.pg_proc AS function "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid=function.pronamespace "
                    "JOIN pg_catalog.pg_roles AS owner "
                    "ON owner.oid=function.proowner "
                    "WHERE namespace.nspname=:schema "
                    "ORDER BY function.proname, "
                    "pg_catalog.pg_get_function_identity_arguments(function.oid)"
                ),
                {"schema": _LABELING_SCHEMA},
            )
        ).all()
        relations = (
            await connection.execute(
                text(
                    "SELECT relation.relname, relation.relkind, owner.rolname, "
                    "(SELECT COALESCE(pg_catalog.jsonb_agg("
                    "pg_catalog.jsonb_build_object("
                    "'grantor', grantor.rolname, "
                    "'grantee', CASE WHEN entry.grantee=0 THEN 'PUBLIC' "
                    "ELSE grantee.rolname END, "
                    "'privilege', entry.privilege_type, "
                    "'grantable', entry.is_grantable) "
                    "ORDER BY entry.grantor, entry.grantee, "
                    "entry.privilege_type, entry.is_grantable), '[]'::jsonb)::text "
                    "FROM pg_catalog.aclexplode(COALESCE(relation.relacl, "
                    "pg_catalog.acldefault('r', relation.relowner))) AS entry "
                    "JOIN pg_catalog.pg_roles AS grantor "
                    "ON grantor.oid=entry.grantor "
                    "LEFT JOIN pg_catalog.pg_roles AS grantee "
                    "ON grantee.oid=entry.grantee), "
                    "relation.reloptions::text, "
                    "pg_catalog.pg_get_viewdef(relation.oid, true), "
                    "(SELECT COALESCE(pg_catalog.jsonb_agg("
                    "pg_catalog.jsonb_build_object("
                    "'name', attribute.attname, "
                    "'type', pg_catalog.format_type(attribute.atttypid, "
                    "attribute.atttypmod), "
                    "'not_null', attribute.attnotnull, "
                    "'collation', CASE WHEN attribute.attcollation=0 THEN NULL "
                    "ELSE collation_schema.nspname || '.' || collation.collname END, "
                    "'identity', attribute.attidentity, "
                    "'generated', attribute.attgenerated) "
                    "ORDER BY attribute.attnum), '[]'::jsonb)::text "
                    "FROM pg_catalog.pg_attribute AS attribute "
                    "LEFT JOIN pg_catalog.pg_collation AS collation "
                    "ON collation.oid=attribute.attcollation "
                    "LEFT JOIN pg_catalog.pg_namespace AS collation_schema "
                    "ON collation_schema.oid=collation.collnamespace "
                    "WHERE attribute.attrelid=relation.oid "
                    "AND attribute.attnum > 0 AND NOT attribute.attisdropped) "
                    "FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid=relation.relnamespace "
                    "JOIN pg_catalog.pg_roles AS owner "
                    "ON owner.oid=relation.relowner "
                    "WHERE namespace.nspname=:schema "
                    "AND relation.relkind IN ('v','m') "
                    "ORDER BY relation.relname"
                ),
                {"schema": _LABELING_SCHEMA},
            )
        ).all()
    assert schema is not None
    assert functions and relations
    assert set(CORE_READ_RELATIONS).issubset({row[0] for row in relations})
    assert schema[1] == "labeling_code"
    assert all(row[4] == "labeling_code" for row in functions)
    assert all(row[2] == "labeling_code" for row in relations)
    return {
        "schema": tuple(schema),
        "functions": tuple(tuple(row) for row in functions),
        "relations": tuple(tuple(row) for row in relations),
    }


async def _labeling_business_snapshot(
    engine,
    *,
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    actor_id: uuid.UUID,
    idempotency_key: str,
) -> dict:
    async with engine.connect() as connection:
        task_json = await connection.scalar(
            text(
                "SELECT pg_catalog.to_jsonb(task)::text "
                "FROM aap_labeling_v1.task_rows AS task WHERE task.id=:id"
            ),
            {"id": task_id},
        )
        annotation_json = await connection.scalar(
            text(
                "SELECT pg_catalog.to_jsonb(annotation)::text "
                "FROM aap_labeling_v1.annotation_rows AS annotation "
                "WHERE annotation.id=:id"
            ),
            {"id": annotation_id},
        )
        operation_json = await connection.scalar(
            text(
                "SELECT pg_catalog.to_jsonb(operation)::text "
                "FROM public.annotation_operations AS operation "
                "WHERE operation.task_id=:task_id "
                "AND operation.actor_id=:actor_id "
                "AND operation.idempotency_key=:key"
            ),
            {
                "task_id": task_id,
                "actor_id": actor_id,
                "key": idempotency_key,
            },
        )
    assert task_json is not None and annotation_json is not None
    assert operation_json is not None
    return {
        "task": json.loads(task_json),
        "annotation": json.loads(annotation_json),
        "operation": json.loads(operation_json),
    }


def _annotation_write_client(connection) -> AnnotationWriteDataClient:
    async def execute(request_json: str):
        return await connection.fetchval(
            "SELECT aap_labeling_v1.annotation_write_command($1::jsonb)",
            request_json,
        )

    return AnnotationWriteDataClient(execute)


async def _write_annotation_and_receipt(
    connection,
    *,
    annotation_id: uuid.UUID,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    operation_id: uuid.UUID,
    idempotency_key: str,
    request_digest: str,
) -> dict:
    data = _annotation_write_client(connection)
    mutation = await data.command(
        _create(
            annotation_id=annotation_id,
            task_id=task_id,
            project_id=project_id,
            actor_id=actor_id,
        )
    )
    assert mutation.mutation is not None
    assert mutation.mutation.annotation_id == annotation_id
    response_json = {"annotation_id": str(annotation_id), "status": "created"}
    await data.command(
        WriteCreateReceipt(
            schema=ANNOTATION_WRITE_PROTOCOL,
            operation="write_create_receipt",
            operation_id=operation_id,
            task_id=task_id,
            project_id=project_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            response_json=response_json,
        )
    )
    receipt = await data.command(
        ReadCreateReceipt(
            schema=ANNOTATION_WRITE_PROTOCOL,
            operation="read_create_receipt",
            task_id=task_id,
            project_id=project_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
    )
    assert receipt.receipt is not None
    assert receipt.receipt.found is True
    assert receipt.receipt.response_json == response_json
    return response_json


async def test_current_labeling_module_state_survives_owned_restore(
    test_engine, tmp_path
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    current_head = ScriptDirectory(
        str(Path(__file__).resolve().parents[1] / "alembic")
    ).get_current_head()
    assert current_head
    await assert_owned_test_database(test_engine)
    assert await _database_head(test_engine) == current_head

    source_database = f"aap_labeling_source_{uuid.uuid4().hex[:8]}_test"
    restored_database = f"aap_labeling_restore_{uuid.uuid4().hex[:8]}_test"
    source_marker = f"labeling-module-source:{uuid.uuid4()}"
    restored_marker = f"labeling-module-restore:{uuid.uuid4()}"
    created: set[str] = set()
    source_engine = restored_engine = None
    source_seed = test_engine.url.database
    baseline_dump = tmp_path / "labeling-module-baseline.dump"
    source_dump = tmp_path / "labeling-module-source.dump"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    role_connections: list[asyncpg.Connection] = []

    try:
        database_acl = await _database_acl(test_engine)
        baseline_dump.touch(mode=0o600, exist_ok=False)
        baseline_dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_seed],
            )
        )
        assert baseline_dump.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(baseline_dump.stat().st_mode) == 0o600

        await _create_owned_clone(
            admin,
            test_engine,
            source_database,
            source_marker,
            database_acl,
            created,
        )
        _postgres_tool(
            test_engine,
            [
                "pg_restore",
                "-U",
                "owner",
                "--exit-on-error",
                "--dbname",
                source_database,
            ],
            body=baseline_dump.read_bytes(),
        )
        source_engine = create_async_engine(
            test_engine.url.set(database=source_database), hide_parameters=True
        )
        await _assert_owned_database(source_engine, source_database, source_marker)
        migration = _alembic(source_engine.url, "upgrade", "head")
        assert migration.returncode == 0
        assert await _database_head(source_engine) == current_head

        maker, world = await _seed(source_engine)
        async with maker.begin() as db:
            task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
        task_id = task.id
        annotation_id = uuid.uuid4()
        operation_id = uuid.uuid4()
        idempotency_key = f"module-restore-{uuid.uuid4().hex}"
        request_digest = "a" * 64
        source_write_dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_ANNOTATION_WRITE_URL",
            _ANNOTATION_WRITE_ROLE,
            database=source_database,
        )
        source_writer = await asyncpg.connect(
            source_write_dsn, timeout=5, command_timeout=10
        )
        role_connections.append(source_writer)
        assert await source_writer.fetchval("SELECT current_user") == (
            _ANNOTATION_WRITE_ROLE
        )
        async with source_writer.transaction():
            await _write_annotation_and_receipt(
                source_writer,
                annotation_id=annotation_id,
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        await source_writer.close()
        role_connections.remove(source_writer)

        source_catalog = await _labeling_catalog_snapshot(source_engine)
        source_business = await _labeling_business_snapshot(
            source_engine,
            task_id=task_id,
            annotation_id=annotation_id,
            actor_id=world["member_id"],
            idempotency_key=idempotency_key,
        )

        source_dump.touch(mode=0o600, exist_ok=False)
        source_dump.write_bytes(
            _postgres_tool(
                source_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_database],
            )
        )
        assert source_dump.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(source_dump.stat().st_mode) == 0o600

        await _create_owned_clone(
            admin,
            test_engine,
            restored_database,
            restored_marker,
            await _database_acl(source_engine),
            created,
        )
        _postgres_tool(
            test_engine,
            [
                "pg_restore",
                "-U",
                "owner",
                "--exit-on-error",
                "--dbname",
                restored_database,
            ],
            body=source_dump.read_bytes(),
        )
        restored_engine = create_async_engine(
            test_engine.url.set(database=restored_database), hide_parameters=True
        )
        await _assert_owned_database(
            restored_engine, restored_database, restored_marker
        )
        assert await _database_head(restored_engine) == current_head

        # Compare native metadata and the seeded Task, Annotation, and receipt
        # before any grant script or owner operation can alter the restored clone.
        assert sorted(await _database_acl(source_engine)) == sorted(
            await _database_acl(restored_engine)
        )
        assert await _labeling_catalog_snapshot(restored_engine) == source_catalog
        assert (
            await _labeling_business_snapshot(
                restored_engine,
                task_id=task_id,
                annotation_id=annotation_id,
                actor_id=world["member_id"],
                idempotency_key=idempotency_key,
            )
            == source_business
        )

        core_read_dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_CORE_READ_URL",
            _CORE_READ_ROLE,
            database=restored_database,
        )
        core_reader = await asyncpg.connect(
            core_read_dsn, timeout=5, command_timeout=10
        )
        role_connections.append(core_reader)
        assert await core_reader.fetchval("SELECT current_user") == _CORE_READ_ROLE
        task_row = await core_reader.fetchrow(
            "SELECT project_id, status FROM aap_labeling_v1.task_rows WHERE id=$1",
            task_id,
        )
        annotation_row = await core_reader.fetchrow(
            "SELECT task_id, class_name, geometry FROM "
            "aap_labeling_v1.annotation_rows WHERE id=$1",
            annotation_id,
        )
        assert (task_row["project_id"], task_row["status"]) == (
            world["project_id"],
            "in_progress",
        )
        assert (annotation_row["task_id"], annotation_row["class_name"]) == (
            task_id,
            "person",
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await core_reader.fetch("SELECT id FROM public.annotations LIMIT 1")

        restored_write_dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_ANNOTATION_WRITE_URL",
            _ANNOTATION_WRITE_ROLE,
            database=restored_database,
        )
        restored_writer = await asyncpg.connect(
            restored_write_dsn, timeout=5, command_timeout=10
        )
        role_connections.append(restored_writer)
        assert await restored_writer.fetchval("SELECT current_user") == (
            _ANNOTATION_WRITE_ROLE
        )
        await verify_function_only_role(
            restored_writer,
            schema=_LABELING_SCHEMA,
            functions=_ANNOTATION_WRITE_FUNCTIONS,
            role=_ANNOTATION_WRITE_ROLE,
        )
        rollback_annotation_id = uuid.uuid4()
        rollback_key = f"module-restore-rollback-{uuid.uuid4().hex}"
        transaction = restored_writer.transaction()
        await transaction.start()
        await _write_annotation_and_receipt(
            restored_writer,
            annotation_id=rollback_annotation_id,
            task_id=task_id,
            project_id=world["project_id"],
            actor_id=world["member_id"],
            operation_id=uuid.uuid4(),
            idempotency_key=rollback_key,
            request_digest="b" * 64,
        )
        await transaction.rollback()
        rollback_receipt = await _annotation_write_client(restored_writer).command(
            ReadCreateReceipt(
                schema=ANNOTATION_WRITE_PROTOCOL,
                operation="read_create_receipt",
                task_id=task_id,
                project_id=world["project_id"],
                actor_id=world["member_id"],
                idempotency_key=rollback_key,
                request_digest="b" * 64,
            )
        )
        assert rollback_receipt.receipt is not None
        assert rollback_receipt.receipt.found is False
        assert (
            await _labeling_business_snapshot(
                restored_engine,
                task_id=task_id,
                annotation_id=annotation_id,
                actor_id=world["member_id"],
                idempotency_key=idempotency_key,
            )
            == source_business
        )
    finally:
        for connection in role_connections:
            await connection.close()
        if restored_engine is not None:
            await restored_engine.dispose()
        if source_engine is not None:
            await source_engine.dispose()
        for database, marker in (
            (restored_database, restored_marker),
            (source_database, source_marker),
        ):
            if database in created:
                await _drop_owned_clone(admin, test_engine, database, marker)
        await admin.dispose()
