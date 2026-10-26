"""Verify nonempty Inference pipeline state and receipts survive owned restore."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import stat
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_inference.pipeline_contract import (
    PIPELINE_PROTOCOL,
    PipelineQueryFailure,
    PipelineStatusSuccess,
    PipelineSuccess,
    pipeline_command_request,
    pipeline_query_request,
    pipeline_status_request,
    pipeline_policy,
)
from aap_inference.pipeline_manager import PipelineManager
from aap_inference.pipeline_postgres import PipelinePostgresData
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from app.services import inference_pipelines as gateway
from tests.factory import create_user
from tests.inference_pipeline_owner_helpers import (
    _BINDINGS,
    api_pipeline_owner_binding,
    pipeline_dsn,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _postgres_tool

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable Inference pipeline verifier",
    ),
]

_SCHEMA = "aap_inference_pipelines_v1"
_CLIENT_FUNCTIONS = {
    ("pipeline_policy", ""),
    ("pipeline_step", "jsonb, text, jsonb, text[], text[]"),
}
_RESTORE_PREFIX = "aap_inference_pipelines_restore_"


def _json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone(UTC).isoformat()


async def _database_head(engine) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )


async def _database_acl(engine) -> list[tuple[str, str, bool]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT COALESCE(role.rolname, 'PUBLIC') AS grantee, "
                "entry.privilege_type, entry.is_grantable "
                "FROM pg_catalog.pg_database AS database "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "database.datacl, pg_catalog.acldefault('d', database.datdba)"
                ")) AS entry "
                "LEFT JOIN pg_catalog.pg_roles AS role ON role.oid=entry.grantee "
                "WHERE database.datname=pg_catalog.current_database() "
                "ORDER BY grantee, entry.privilege_type, entry.is_grantable"
            )
        )
    return [(row.grantee, row.privilege_type, row.is_grantable) for row in rows]


async def _function_metadata(engine) -> dict[str, dict]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT namespace.nspname || '.' || function.proname || '(' || "
                "pg_catalog.replace(pg_catalog.oidvectortypes(function.proargtypes), "
                "' ', '') || ')' AS signature, "
                "pg_catalog.pg_get_userbyid(function.proowner) AS owner, "
                "function.proacl::text AS acl, function.prosecdef AS security_definer, "
                "function.proconfig::text AS configuration, "
                "function.provolatile::text AS volatility, "
                "pg_catalog.pg_get_function_result(function.oid) AS result, "
                "pg_catalog.pg_get_functiondef(function.oid) AS definition, "
                "pg_catalog.has_function_privilege("
                "'inference_pipelines_client', function.oid, 'EXECUTE'"
                ") AS client_can_execute, "
                "EXISTS (SELECT 1 FROM pg_catalog.aclexplode(COALESCE("
                "function.proacl, pg_catalog.acldefault('f', function.proowner)"
                ")) AS acl WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') "
                "AS public_can_execute "
                "FROM pg_catalog.pg_proc AS function "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid=function.pronamespace "
                "WHERE namespace.nspname=:schema ORDER BY signature"
            ),
            {"schema": _SCHEMA},
        )
        metadata = {
            row.signature: {
                key: getattr(row, key)
                for key in (
                    "owner",
                    "acl",
                    "security_definer",
                    "configuration",
                    "volatility",
                    "result",
                    "definition",
                    "client_can_execute",
                    "public_can_execute",
                )
            }
            for row in rows
        }

    assert metadata
    assert {
        signature
        for signature, values in metadata.items()
        if values["client_can_execute"]
    } == {
        f"{_SCHEMA}.pipeline_policy()",
        f"{_SCHEMA}.pipeline_step(jsonb,text,jsonb,text[],text[])",
    }
    for signature, values in metadata.items():
        assert values["owner"] == "inference_code", signature
        assert values["security_definer"] is True, signature
        assert values["public_can_execute"] is False, signature
        configuration = values["configuration"] or ""
        assert "search_path=pg_catalog, pg_temp" in configuration, signature
        if values["client_can_execute"]:
            assert "TimeZone=UTC" in configuration, signature
            assert "DateStyle=ISO, YMD" in configuration, signature
            assert "extra_float_digits=3" in configuration, signature
    return metadata


async def _pipeline_policy(test_engine, database: str | None = None) -> dict:
    connection = await asyncpg.connect(
        pipeline_dsn(test_engine, database), timeout=5, command_timeout=10
    )
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "inference_pipelines_client"
        )
        await verify_function_only_role(
            connection,
            schema=_SCHEMA,
            functions=_CLIENT_FUNCTIONS,
        )
        return _json_value(
            await connection.fetchval(f"SELECT {_SCHEMA}.pipeline_policy()")
        )
    finally:
        await connection.close()


async def _assert_function_only_client(
    test_engine, database: str | None = None
) -> None:
    connection = await asyncpg.connect(
        pipeline_dsn(test_engine, database), timeout=5, command_timeout=10
    )
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "inference_pipelines_client"
        )
        for statement in (
            "SELECT id FROM public.project_pipelines LIMIT 1",
            f"SELECT * FROM {_SCHEMA}.operation_receipts LIMIT 1",
            f"SELECT {_SCHEMA}._pipeline_row_value(NULL::public.project_pipelines)",
            "SELECT aap_workspace_v1.lock_project_manager(NULL::uuid,NULL::uuid)",
            "SET ROLE inference_code",
            "CREATE TEMP TABLE forbidden_pipeline_restore(id integer)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


async def _owner_snapshot(engine) -> dict:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        pipeline_models = (
            await db.execute(select(ProjectPipeline).order_by(ProjectPipeline.id))
        ).scalars()
        pipelines = [
            {
                "id": str(row.id),
                "scope": row.scope,
                "project_id": str(row.project_id) if row.project_id else None,
                "organization_id": (
                    str(row.organization_id) if row.organization_id else None
                ),
                "name": row.name,
                "stages": row.stages,
                "is_default": row.is_default,
                "created_by": str(row.created_by),
                "usage_count": row.usage_count,
                "created_at": _utc(row.created_at),
                "updated_at": _utc(row.updated_at),
            }
            for row in pipeline_models
        ]
        receipts = (
            await db.execute(
                text(
                    f"SELECT operation_id, transaction_id, actor_id, operation, "
                    "target_id, request_hash, request, resource_id, created_at "
                    f"FROM {_SCHEMA}.operation_receipts "
                    "ORDER BY operation_id"
                )
            )
        ).mappings()
        normalized_receipts = [
            {
                "operation_id": str(row["operation_id"]),
                "transaction_id": row["transaction_id"],
                "actor_id": str(row["actor_id"]),
                "operation": row["operation"],
                "target_id": str(row["target_id"]) if row["target_id"] else None,
                "request_hash": row["request_hash"],
                "request": row["request"],
                "resource_id": str(row["resource_id"]),
                "created_at": _utc(row["created_at"]),
            }
            for row in receipts
        ]
        state_rows = (
            await db.execute(
                text(
                    f"SELECT operation_id, transaction_id, request_hash "
                    f"FROM {_SCHEMA}.operation_states "
                    "ORDER BY operation_id"
                )
            )
        ).all()
        attempt_rows = (
            await db.execute(
                text(
                    f"SELECT attempt.operation_id, attempt.transaction_id, "
                    "attempt.request_hash "
                    f"FROM {_SCHEMA}.operation_attempts AS attempt "
                    "ORDER BY attempt.operation_id"
                )
            )
        ).all()

    return {
        "pipelines": pipelines,
        "receipts": normalized_receipts,
        "states": [tuple(row) for row in state_rows],
        "attempts": [tuple(row) for row in attempt_rows],
    }


async def _seed_receipt(test_engine, actor_id: uuid.UUID):
    operation_id = uuid.uuid4()
    command = pipeline_command_request(
        "create_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        payload={
            "name": "Inference restore seed",
            "scope": "public",
            "project_id": None,
            "organization_id": None,
            "stages": [{"stage": 0, "ml_backend_id": str(uuid.uuid4())}],
            "is_default": False,
        },
        operation_id=operation_id,
    )
    data = PipelinePostgresData(pipeline_dsn(test_engine))
    reply = await PipelineManager(data).execute(command)
    assert isinstance(reply, PipelineSuccess), reply
    assert reply.commit_state == "committed"
    return command, reply.result.resource_id


async def _run_binding_call(owner, maker, operation: str, request):
    if owner.mode == "borrowed":
        async with maker.begin() as db:
            return await gateway._invoke(owner, db, operation, request)
    return await gateway._invoke(owner, None, operation, request)


async def _assert_query_rows(owner, maker, actor_id):
    request = pipeline_query_request(
        "list_pipelines",
        actor_id=actor_id,
        actor_role="super_admin",
        filters={"scope": "public", "project_id": None, "organization_id": None},
    )
    reply = await _run_binding_call(owner, maker, "query", request)
    assert reply.ok is True, reply
    return reply.rows


async def _exercise_bound_binding(
    mode: str,
    owner,
    maker,
    actor_id: uuid.UUID,
    seed_command,
    seed_pipeline_id: uuid.UUID,
) -> None:
    seed_status_request = pipeline_status_request(seed_command)
    seed_status = await _run_binding_call(owner, maker, "status", seed_status_request)
    assert isinstance(seed_status, PipelineStatusSuccess)
    assert seed_status.state == "committed"
    assert seed_status.resource_id == seed_pipeline_id

    seed_replay = await _run_binding_call(owner, maker, "execute", seed_command)
    assert isinstance(seed_replay, PipelineSuccess), seed_replay
    assert seed_replay.result.resource_id == seed_pipeline_id
    assert seed_replay.commit_state == (
        "caller_pending" if mode == "borrowed" else "committed"
    )

    rows = await _assert_query_rows(owner, maker, actor_id)
    seed = next(row for row in rows if row.id == seed_pipeline_id)
    seed_stages = json.loads(seed_command.payload_ascii)["stages"]
    assert seed.stages
    assert seed.stages[0]["ml_backend_id"] == seed_stages[0]["ml_backend_id"]
    assert sum(row.id == seed_pipeline_id for row in rows) == 1

    created_operation_id = uuid.uuid4()
    create_stages = [{"stage": 0, "ml_backend_id": str(uuid.uuid4())}]
    create_request = pipeline_command_request(
        "create_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        payload={
            "name": f"Restore CRUD {mode}",
            "scope": "public",
            "project_id": None,
            "organization_id": None,
            "stages": create_stages,
            "is_default": False,
        },
        operation_id=created_operation_id,
    )
    created = await _run_binding_call(owner, maker, "execute", create_request)
    assert isinstance(created, PipelineSuccess), created
    pipeline_id = created.result.resource_id
    assert created.commit_state == (
        "caller_pending" if mode == "borrowed" else "committed"
    )
    created_status = await _run_binding_call(
        owner, maker, "status", pipeline_status_request(create_request)
    )
    assert created_status.state == "committed"
    assert created_status.resource_id == pipeline_id

    create_replay = await _run_binding_call(owner, maker, "execute", create_request)
    assert isinstance(create_replay, PipelineSuccess), create_replay
    assert create_replay.result.resource_id == pipeline_id
    assert create_replay.commit_state == (
        "caller_pending" if mode == "borrowed" else "committed"
    )
    created_rows = await _assert_query_rows(owner, maker, actor_id)
    assert sum(row.id == pipeline_id for row in created_rows) == 1

    update_operation_id = uuid.uuid4()
    update_request = pipeline_command_request(
        "update_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        target_id=pipeline_id,
        payload={"name": f"Restore CRUD updated {mode}"},
        operation_id=update_operation_id,
    )
    updated = await _run_binding_call(owner, maker, "execute", update_request)
    assert isinstance(updated, PipelineSuccess), updated
    assert updated.result.resource_id == pipeline_id
    update_status = await _run_binding_call(
        owner, maker, "status", pipeline_status_request(update_request)
    )
    assert update_status.state == "committed"
    assert update_status.resource_id == pipeline_id
    update_replay = await _run_binding_call(owner, maker, "execute", update_request)
    assert isinstance(update_replay, PipelineSuccess), update_replay
    assert update_replay.result.resource_id == pipeline_id
    assert update_replay.commit_state == (
        "caller_pending" if mode == "borrowed" else "committed"
    )

    row_request = pipeline_query_request(
        "get_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        target_id=pipeline_id,
    )
    readback = await _run_binding_call(owner, maker, "query", row_request)
    assert readback.ok is True, readback
    assert readback.rows[0].name == f"Restore CRUD updated {mode}"
    assert readback.rows[0].stages
    assert (
        readback.rows[0].stages[0]["ml_backend_id"] == create_stages[0]["ml_backend_id"]
    )

    delete_operation_id = uuid.uuid4()
    delete_request = pipeline_command_request(
        "delete_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        target_id=pipeline_id,
        payload={},
        operation_id=delete_operation_id,
    )
    deleted = await _run_binding_call(owner, maker, "execute", delete_request)
    assert isinstance(deleted, PipelineSuccess), deleted
    assert deleted.result.resource_id == pipeline_id
    delete_status = await _run_binding_call(
        owner, maker, "status", pipeline_status_request(delete_request)
    )
    assert delete_status.state == "committed"
    assert delete_status.resource_id == pipeline_id

    delete_replay = await _run_binding_call(owner, maker, "execute", delete_request)
    assert isinstance(delete_replay, PipelineSuccess), delete_replay
    assert delete_replay.result.resource_id == pipeline_id
    assert delete_replay.commit_state == (
        "caller_pending" if mode == "borrowed" else "committed"
    )
    listed = await _assert_query_rows(owner, maker, actor_id)
    assert pipeline_id not in [row.id for row in listed]

    missing = await _run_binding_call(owner, maker, "query", row_request)
    assert isinstance(missing, PipelineQueryFailure)
    assert missing.error.status_code == 404


async def _exercise_binding(
    test_engine,
    mode: str,
    database: str | None,
    monkeypatch,
    tmp_path,
    actor_id: uuid.UUID,
    seed_command,
    seed_pipeline_id: uuid.UUID,
) -> None:
    async with api_pipeline_owner_binding(
        test_engine,
        monkeypatch,
        mode,
        tmp_path,
        database=database,
    ):
        owner = gateway._select_owner()
        assert owner.mode == mode
        borrowed_engine = None
        if mode == "borrowed":
            if database is None:
                borrowed_engine = test_engine
            else:
                borrowed_engine = create_async_engine(
                    test_engine.url.set(database=database), hide_parameters=True
                )
            maker = async_sessionmaker(borrowed_engine, expire_on_commit=False)
        else:
            maker = None
        try:
            await _exercise_bound_binding(
                mode,
                owner,
                maker,
                actor_id,
                seed_command,
                seed_pipeline_id,
            )
        finally:
            if database is not None and borrowed_engine is not None:
                await borrowed_engine.dispose()


async def _cleanup_source(test_engine, actor_id: uuid.UUID | None) -> None:
    if actor_id is None:
        return
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        await db.execute(
            text(
                f"DELETE FROM {_SCHEMA}.operation_attempts AS attempt "
                "WHERE attempt.operation_id IN ("
                f"SELECT operation_id FROM {_SCHEMA}.operation_states "
                "WHERE actor_id=CAST(:actor_id AS uuid) UNION "
                f"SELECT operation_id FROM {_SCHEMA}.operation_receipts "
                "WHERE actor_id=CAST(:actor_id AS uuid))"
            ),
            {"actor_id": str(actor_id)},
        )
        await db.execute(
            text(
                f"DELETE FROM {_SCHEMA}.operation_states "
                "WHERE actor_id=CAST(:actor_id AS uuid)"
            ),
            {"actor_id": str(actor_id)},
        )
        await db.execute(
            text(
                f"DELETE FROM {_SCHEMA}.operation_receipts "
                "WHERE actor_id=CAST(:actor_id AS uuid)"
            ),
            {"actor_id": str(actor_id)},
        )
        await db.execute(
            delete(ProjectPipeline).where(ProjectPipeline.created_by == actor_id)
        )
        await db.execute(delete(User).where(User.id == actor_id))


async def test_inference_pipeline_nonempty_state_and_receipts_survive_owned_restore(
    test_engine, monkeypatch, tmp_path
):
    assert test_engine.url.username == "owner"
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    _verify_postgres(test_engine.url)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    clone = f"{_RESTORE_PREFIX}{uuid.uuid4().hex[:10]}_test"
    assert len(clone.encode("utf-8")) <= 63
    ownership = f"inference-pipelines-restore:{uuid.uuid4()}"
    quoted_clone = test_engine.dialect.identifier_preparer.quote_identifier(clone)
    snapshot = tmp_path / f"{clone}.dump"
    database_created = False
    restored_engine = None
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )

    try:
        source_head = await _database_head(test_engine)
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"inference-pipeline-restore-{uuid.uuid4().hex}@example.test",
                "Inference Pipeline Restore",
            )
            actor_id = actor.id

        seed_command, seed_pipeline_id = await _seed_receipt(test_engine, actor_id)
        assert seed_command.protocol == PIPELINE_PROTOCOL
        source_policy = await _pipeline_policy(test_engine)
        expected_policy = pipeline_policy().model_dump(mode="json", by_alias=True)
        assert source_policy == expected_policy
        source_functions = await _function_metadata(test_engine)
        source_state = await _owner_snapshot(test_engine)
        source_acl = await _database_acl(test_engine)
        seeded_pipelines = [
            row
            for row in source_state["pipelines"]
            if row["created_by"] == str(actor_id)
        ]
        assert len(seeded_pipelines) == 1
        assert seeded_pipelines[0]["id"] == str(seed_pipeline_id)
        assert seeded_pipelines[0]["stages"]
        seeded_receipts = [
            receipt
            for receipt in source_state["receipts"]
            if receipt["actor_id"] == str(actor_id)
        ]
        assert len(seeded_receipts) == 1
        seed_receipt = seeded_receipts[0]
        assert seed_receipt["operation_id"] == str(seed_command.operation_id)
        assert seed_receipt["resource_id"] == str(seed_pipeline_id)
        assert seed_receipt["request"] == seed_command.model_dump(
            mode="json", by_alias=True
        )
        assert len(seed_receipt["request_hash"]) == 64
        assert source_state["states"] == []
        assert source_state["attempts"] == []
        await _assert_function_only_client(test_engine)

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600

        _verify_postgres(test_engine.url)
        async with admin.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE {quoted_clone} TEMPLATE template0")
            )
            database_created = True
            await connection.execute(
                text(f"COMMENT ON DATABASE {quoted_clone} IS '{ownership}'")
            )
            await connection.execute(
                text(f"REVOKE ALL ON DATABASE {quoted_clone} FROM PUBLIC")
            )
            quote = connection.dialect.identifier_preparer.quote
            for grantee, privilege, grantable in source_acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                grant_option = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(
                        f"GRANT {privilege} ON DATABASE {quoted_clone} "
                        f"TO {role}{grant_option}"
                    )
                )

        _verify_postgres(test_engine.url)
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )
        restored_engine = create_async_engine(
            test_engine.url.set(database=clone), hide_parameters=True
        )
        async with restored_engine.connect() as connection:
            current_user, current_database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert current_database == clone and current_database.endswith("_test")
        assert current_user == test_engine.url.username
        async with admin.connect() as connection:
            clone_marker = await connection.scalar(
                text(
                    "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
                    "FROM pg_catalog.pg_database WHERE datname=:name"
                ),
                {"name": clone},
            )
        assert clone_marker == ownership

        assert await _database_head(restored_engine) == source_head
        clone_policy = await _pipeline_policy(test_engine, clone)
        assert clone_policy == source_policy == expected_policy
        clone_functions = await _function_metadata(restored_engine)
        assert clone_functions == source_functions
        clone_state = await _owner_snapshot(restored_engine)
        assert clone_state == source_state
        assert await _database_acl(restored_engine) == source_acl
        await _assert_function_only_client(test_engine, clone)

        for database in (None, clone):
            for mode in _BINDINGS:
                await _exercise_binding(
                    test_engine,
                    mode,
                    database,
                    monkeypatch,
                    tmp_path,
                    actor_id,
                    seed_command,
                    seed_pipeline_id,
                )

        source_after_operations = await _owner_snapshot(test_engine)
        clone_before_isolation_probe = await _owner_snapshot(restored_engine)
        isolation_operation_id = uuid.uuid4()
        clone_only_update = pipeline_command_request(
            "update_pipeline",
            actor_id=actor_id,
            actor_role="super_admin",
            target_id=seed_pipeline_id,
            payload={"name": "Clone-only pipeline mutation"},
            operation_id=isolation_operation_id,
        )
        async with api_pipeline_owner_binding(
            test_engine,
            monkeypatch,
            "http",
            tmp_path,
            database=clone,
        ):
            clone_owner = gateway._select_owner()
            assert clone_owner.mode == "http"
            reply = await gateway._invoke(
                clone_owner, None, "execute", clone_only_update
            )
        assert isinstance(reply, PipelineSuccess), reply
        assert reply.commit_state == "committed"
        source_after_clone_mutation = await _owner_snapshot(test_engine)
        clone_after_isolation_probe = await _owner_snapshot(restored_engine)
        assert source_after_clone_mutation == source_after_operations
        assert clone_after_isolation_probe != clone_before_isolation_probe
        assert any(
            pipeline["id"] == str(seed_pipeline_id)
            and pipeline["name"] == "Clone-only pipeline mutation"
            for pipeline in clone_after_isolation_probe["pipelines"]
        )
    finally:
        if restored_engine is not None:
            await restored_engine.dispose()
        try:
            if database_created:
                _verify_postgres(test_engine.url)
                async with admin.connect() as connection:
                    marker = await connection.scalar(
                        text(
                            "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
                            "FROM pg_catalog.pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    assert marker == ownership
                    assert not await connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=:name "
                            "AND pid <> pg_catalog.pg_backend_pid() "
                            "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                        ),
                        {"name": clone},
                    )
                    await connection.execute(text(f"DROP DATABASE {quoted_clone}"))
        finally:
            await admin.dispose()
            if actor_id is not None:
                await _cleanup_source(test_engine, actor_id)
            snapshot.unlink(missing_ok=True)
