"""Owned pg_dump/pg_restore verification for the Labeling class-query owner."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import stat
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_labeling.class_query_postgres import (
    CLASS_QUERY_FUNCTIONS,
    CLASS_QUERY_SCHEMA,
    LabelingClassQueryPostgresData,
)
from aap_owner_data.postgres import verify_function_only_role
from aap_workspace.project_queries import ProjectQueryManager
from aap_workspace_contract.project_query_contract import project_query_request
from aap_workspace.project_query_postgres import ProjectQueryPostgresData
from aap_workspace_contract.project_query_values import materialize_project_query_data
from tests.labeling_class_query_helpers import (
    add_frozen_interleave_row,
    aliased_uuid,
    assert_owned_test_database,
    bound_api_client,
    cleanup_world,
    fixture_data,
    gated_class_counts,
    labeling_class_query_dsn,
    request_sample,
    run_read_committed_phase_probe,
    seed_legacy_world,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _postgres_tool
from tests.workspace_project_query_helpers import _dsn, project_query_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_PHASE_SAMPLE = "class_then_attributes_second_session_commit"
_OWNER_BINDINGS = ("borrowed", "dedicated", "http")
_CLASS_QUERY_NAMES = ("attribute_rows", "class_counts", "class_query_policy")


def _legacy_public_sample(sample: dict) -> dict:
    request = sample["request"]
    response = sample["response"]
    return {
        "label": sample["label"],
        "request": {
            "method": request["method"],
            "path": request["path"],
            "actor_alias": request["actor"]["alias"],
        },
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": response["body"],
        },
    }


def _public_response_sample(sample: dict) -> dict:
    return {
        key: value
        for key, value in sample.items()
        if key != "borrowed_labeling_rpc_phases"
    }


def _expected_labeling_rpc_phases(sample: dict) -> list[str]:
    if sample["response"]["status"] in (200, 500):
        return ["class_query_policy", "class_counts", "attribute_rows"]
    return []


async def _labeling_function_facts(engine) -> list[tuple]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT function.proname, "
                    "pg_catalog.pg_get_function_identity_arguments(function.oid) "
                    "AS identity_arguments, "
                    "pg_catalog.pg_get_function_result(function.oid) AS result, "
                    "owner.rolname AS owner_name, function.proacl::text AS acl, "
                    "function.proconfig::text AS configuration, "
                    "function.provolatile, function.prosecdef, function.proretset "
                    "FROM pg_catalog.pg_proc AS function "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = function.pronamespace "
                    "JOIN pg_catalog.pg_roles AS owner "
                    "ON owner.oid = function.proowner "
                    "WHERE namespace.nspname = 'aap_labeling_class_query_v1' "
                    "AND function.proname IN "
                    "('class_query_policy', 'class_counts', 'attribute_rows') "
                    "ORDER BY function.proname"
                )
            )
        ).mappings()
        facts = [
            (
                row["proname"],
                row["identity_arguments"],
                row["result"],
                row["owner_name"],
                row["acl"],
                row["configuration"],
                row["provolatile"],
                row["prosecdef"],
                row["proretset"],
            )
            for row in rows
        ]
    assert [row[0] for row in facts] == list(_CLASS_QUERY_NAMES)
    assert all(row[3] == "labeling_code" for row in facts)
    return facts


async def _assert_labeling_client(dsn: str, project_id: uuid.UUID) -> None:
    data = LabelingClassQueryPostgresData(dsn)
    await data.class_query_policy()
    async with data.transaction() as transaction:
        assert (
            await transaction.connection.fetchval("SHOW transaction_read_only") == "on"
        )
        assert (
            await transaction.connection.fetchval("SHOW transaction_isolation")
            == "read committed"
        )
        assert await transaction.class_rows(project_id)
        assert await transaction.attribute_rows(project_id)

    connection = await asyncpg.connect(dsn)
    try:
        await verify_function_only_role(
            connection,
            schema=CLASS_QUERY_SCHEMA,
            functions=CLASS_QUERY_FUNCTIONS,
        )
        for statement in (
            "SELECT attributes FROM public.annotations LIMIT 1",
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.users LIMIT 1",
            "CREATE TEMP TABLE forbidden_class_query_restore(id integer)",
            "SELECT aap_workspace_project_query_v1.project_query_policy()",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


class _ReadOnlyProjectQueryData(ProjectQueryPostgresData):
    @asynccontextmanager
    async def transaction(self):
        async with super().transaction() as transaction:
            assert (
                await transaction.connection.fetchval("SHOW transaction_read_only")
                == "on"
            )
            assert (
                await transaction.connection.fetchval("SHOW transaction_isolation")
                == "read committed"
            )
            yield transaction


async def _manager_reply(dsn: str, *, actor_id, actor_role, project_id):
    data = _ReadOnlyProjectQueryData(dsn)
    await data.project_query_policy()
    manager = ProjectQueryManager(data, profiles=None)
    request = project_query_request(
        "require_project_manager",
        actor_id,
        getattr(actor_role, "value", actor_role),
        target_id=project_id,
    )
    return await manager.execute(request)


async def _capture_bound_samples(
    engine,
    app_module,
    monkeypatch,
    world: dict,
    reference: dict,
    *,
    binding: str,
) -> list[dict]:
    frozen_samples = reference["samples"]
    workspace_token = f"workspace-class-restore-{uuid.uuid4().hex}"
    labeling_token = f"labeling-class-restore-{uuid.uuid4().hex}"
    observed = []
    interleave_id = None
    async with bound_api_client(
        engine,
        app_module,
        monkeypatch,
        binding=binding,
        workspace_token=workspace_token,
        labeling_token=labeling_token,
        application_name=f"labeling-class-restore-{binding}",
    ) as (client, _runtime_engine, native_rpc):
        try:
            for frozen in frozen_samples:
                if frozen["label"] == _PHASE_SAMPLE:
                    # Replay the frozen sequence's committed row; the separate
                    # gated probe below supplies actual concurrency evidence.
                    interleave_id = await add_frozen_interleave_row(engine, world)
                sample = await request_sample(
                    client, world, frozen, native_rpc=native_rpc
                )
                assert _public_response_sample(sample) == _legacy_public_sample(frozen)
                if native_rpc is not None:
                    assert sample["borrowed_labeling_rpc_phases"] == (
                        _expected_labeling_rpc_phases(frozen)
                    )
                observed.append(sample)
        finally:
            if interleave_id is not None:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("DELETE FROM public.annotations WHERE id=:id"),
                        {"id": interleave_id},
                    )
    return observed


async def _assert_phase_probe(engine, app_module, monkeypatch, world, reference):
    frozen = next(
        sample
        for sample in reference["samples"]
        if sample["label"] == "target_project_owner_aggregation"
    )
    workspace_token = f"workspace-class-phase-{uuid.uuid4().hex}"
    labeling_token = f"labeling-class-phase-{uuid.uuid4().hex}"
    async with bound_api_client(
        engine,
        app_module,
        monkeypatch,
        binding="borrowed",
        workspace_token=workspace_token,
        labeling_token=labeling_token,
        application_name="labeling-class-restore-phase",
    ) as (client, _runtime_engine, native_rpc):
        async with gated_class_counts(engine):
            native_rpc["current"] = []
            result = await run_read_committed_phase_probe(engine, client, world)
            phases = list(native_rpc["current"])
            native_rpc["current"] = None

    expected_attributes = dict(frozen["response"]["body"]["attributes"])
    expected_attributes["phase_committed_key"] = 1
    expected_body = {
        "classes": frozen["response"]["body"]["classes"],
        "attributes": expected_attributes,
    }
    assert result["status"] == frozen["response"]["status"] == 200
    assert result["headers"] == frozen["response"]["headers"]
    assert result["body"] == expected_body
    assert "phase-new-class" not in result["body"]["classes"]
    assert phases == ["class_query_policy", "class_counts", "attribute_rows"]
    assert result["blocked_rpc"]["wait_event_type"] == "Lock"
    return {key: result[key] for key in ("status", "headers", "body")}


async def _database_head(engine) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )


async def test_labeling_class_query_owner_survives_owned_database_restore(
    test_engine, app_module, monkeypatch, tmp_path
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    await assert_owned_test_database(test_engine)
    reference = fixture_data()
    target_sample = next(
        sample
        for sample in reference["samples"]
        if sample["label"] == "target_project_owner_aggregation"
    )
    target_project_id = None
    world = None
    clone = f"aap_labeling_class_query_restore_{uuid.uuid4().hex[:10]}_test"
    ownership = f"labeling-class-query-restore:{uuid.uuid4()}"
    quoted_clone = test_engine.dialect.identifier_preparer.quote_identifier(clone)
    snapshot = tmp_path / f"{clone}.dump"
    database_created = False
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    restored_engine = None

    try:
        world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
        target_project_id = aliased_uuid(world, "project_target")
        assert await _database_head(test_engine) == "0276"

        source_functions = await _labeling_function_facts(test_engine)
        source_labeling_dsn = labeling_class_query_dsn(test_engine)
        source_workspace_dsn = project_query_dsn(test_engine)
        await _assert_labeling_client(source_labeling_dsn, target_project_id)
        source_manager = await _manager_reply(
            source_workspace_dsn,
            actor_id=world["actors"]["owner"].id,
            actor_role=world["actors"]["owner"].role,
            project_id=target_project_id,
        )
        assert source_manager.ok is True
        assert materialize_project_query_data(source_manager) == {
            "project_id": target_project_id
        }

        source_responses = {}
        for binding in _OWNER_BINDINGS:
            source_responses[binding] = await _capture_bound_samples(
                test_engine,
                app_module,
                monkeypatch,
                world,
                reference,
                binding=binding,
            )
        assert target_sample["response"]["body"]["classes"]
        assert target_sample["response"]["body"]["attributes"]

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
            async with test_engine.connect() as source:
                database_acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), "
                            "entry.privilege_type, entry.is_grantable "
                            "FROM pg_catalog.pg_database AS database "
                            "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                            "database.datacl, pg_catalog.acldefault('d',database.datdba)"
                            ")) AS entry "
                            "LEFT JOIN pg_catalog.pg_roles AS role "
                            "ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = connection.dialect.identifier_preparer.quote_identifier
            for grantee, privilege, grantable in database_acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix_sql = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(
                        f"GRANT {privilege} ON DATABASE {quoted_clone} "
                        f"TO {role}{suffix_sql}"
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
        cloned_legacy_dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LEGACY_RUNTIME_URL",
            "legacy_runtime",
            clone,
        )
        cloned_labeling_dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_CLASS_QUERY_URL",
            "labeling_class_query_client",
            clone,
        )
        cloned_workspace_dsn = project_query_dsn(test_engine, clone)
        _verify_postgres(make_url(source_labeling_dsn))
        _verify_postgres(make_url(source_workspace_dsn))
        _verify_postgres(make_url(cloned_legacy_dsn))
        _verify_postgres(make_url(cloned_labeling_dsn))
        _verify_postgres(make_url(cloned_workspace_dsn))

        cloned_urls = {
            "TEST_DATABASE_URL": restored_engine.url.render_as_string(
                hide_password=False
            ),
            "AAP_MODULE_DATA_LEGACY_RUNTIME_URL": cloned_legacy_dsn,
            "AAP_MODULE_DATA_LABELING_CLASS_QUERY_URL": cloned_labeling_dsn,
            "AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL": cloned_workspace_dsn,
        }
        with monkeypatch.context() as clone_environment:
            for name, value in cloned_urls.items():
                clone_environment.setenv(
                    name,
                    make_url(value)
                    .set(drivername="postgresql+asyncpg")
                    .render_as_string(hide_password=False),
                )
            await assert_owned_test_database(restored_engine)
            assert await _database_head(restored_engine) == "0276"
            assert await _labeling_function_facts(restored_engine) == source_functions

            await _assert_labeling_client(cloned_labeling_dsn, target_project_id)
            cloned_manager = await _manager_reply(
                cloned_workspace_dsn,
                actor_id=world["actors"]["owner"].id,
                actor_role=world["actors"]["owner"].role,
                project_id=target_project_id,
            )
            assert cloned_manager.ok is True
            assert materialize_project_query_data(cloned_manager) == {
                "project_id": target_project_id
            }
            restored_responses = {}
            for binding in _OWNER_BINDINGS:
                restored_responses[binding] = await _capture_bound_samples(
                    restored_engine,
                    app_module,
                    monkeypatch,
                    world,
                    reference,
                    binding=binding,
                )
            assert restored_responses == source_responses

            restored_phase = await _assert_phase_probe(
                restored_engine, app_module, monkeypatch, world, reference
            )

            inactive_actor = world["actors"]["owner"]
            async with restored_engine.begin() as connection:
                result = await connection.execute(
                    text(
                        "UPDATE public.users SET is_active=false "
                        "WHERE id=CAST(:actor_id AS uuid)"
                    ),
                    {"actor_id": str(inactive_actor.id)},
                )
                assert result.rowcount == 1

            disabled = await _manager_reply(
                cloned_workspace_dsn,
                actor_id=inactive_actor.id,
                actor_role=inactive_actor.role,
                project_id=target_project_id,
            )
            assert disabled.ok is False
            assert disabled.error.status_code == 401
            assert disabled.error.headers == {"WWW-Authenticate": "Bearer"}
            # Labeling's private projection trusts the API coordinator; this
            # disabled-actor check belongs to Workspace's current authority read.
            source_still_active = await _manager_reply(
                source_workspace_dsn,
                actor_id=inactive_actor.id,
                actor_role=inactive_actor.role,
                project_id=target_project_id,
            )
            assert source_still_active.ok is True

        source_phase = await _assert_phase_probe(
            test_engine, app_module, monkeypatch, world, reference
        )
        assert restored_phase == source_phase

    finally:
        if restored_engine is not None:
            await restored_engine.dispose()
        if database_created:
            _verify_postgres(test_engine.url)
            async with admin.connect() as connection:
                marker = await connection.scalar(
                    text(
                        "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                        "FROM pg_catalog.pg_database WHERE datname=:name"
                    ),
                    {"name": clone},
                )
                assert marker == ownership
                assert not await connection.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=:name AND pid <> pg_catalog.pg_backend_pid() "
                        "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                    ),
                    {"name": clone},
                )
                await connection.execute(text(f"DROP DATABASE {quoted_clone}"))
        await admin.dispose()
        if world is not None:
            await cleanup_world(test_engine, world)
        snapshot.unlink(missing_ok=True)
