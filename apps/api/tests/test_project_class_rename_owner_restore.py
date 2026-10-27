"""Restore Native class-rename capabilities and replay frozen public behavior."""

from __future__ import annotations

import os
import stat
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_owner_data.postgres import verify_function_only_role
from tests import test_project_class_rename_legacy as legacy
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    make_request_runtime,
)
from tests.project_class_rename_owner_helpers import (
    aliased_uuid,
    class_rename_binding,
    cleanup_world,
    samples_fixture,
    seed_legacy_world,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _postgres_tool
from tests.test_project_class_rename_owner import (
    _assert_response,
    _business_snapshot,
    _observe_api_commits,
    _replay_http_samples,
    _verify_borrowed_caller_rollback,
)
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_project_query_helpers import (
    _dsn,
    project_query_briefs_dsn,
    project_query_dsn,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_BINDINGS = ("borrowed", "dedicated", "http")
_FUNCTIONS = {
    "aap_workspace_configuration_v1._class_rename_project_row(uuid,text)": "workspace_code",
    "aap_workspace_configuration_v1._class_rename_member_rows(uuid,uuid)": "workspace_code",
    "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])": "workspace_code",
    "aap_labeling_v1.rename_project_annotation_class(uuid,text,text)": "labeling_code",
}
_CONFIGURATION_CLIENT_FUNCTIONS = {
    ("configuration_policy", ""),
    ("configuration_step", "jsonb, text, jsonb, text[], text[]"),
}


async def _database_head(engine) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )


async def _class_rename_function_metadata(engine) -> dict[str, dict]:
    signatures = list(_FUNCTIONS)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """SELECT namespace.nspname || '.' || function.proname || '(' ||
                              pg_catalog.replace(
                                  pg_catalog.oidvectortypes(function.proargtypes),
                                  ' ', ''
                              ) || ')' AS signature,
                              pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                              function.proacl::text AS acl,
                              function.prosecdef AS security_definer,
                              function.proconfig::text AS configuration,
                              function.provolatile AS volatility,
                              pg_catalog.pg_get_function_result(function.oid) AS result,
                              pg_catalog.pg_get_functiondef(function.oid) AS definition
                       FROM pg_catalog.pg_proc AS function
                       JOIN pg_catalog.pg_namespace AS namespace
                         ON namespace.oid = function.pronamespace
                       WHERE namespace.nspname || '.' || function.proname || '(' ||
                             pg_catalog.replace(
                                 pg_catalog.oidvectortypes(function.proargtypes),
                                 ' ', ''
                             ) || ')' = ANY(CAST(:signatures AS text[]))
                       ORDER BY signature"""
                ),
                {"signatures": signatures},
            )
        ).mappings()
        metadata = {
            row["signature"]: {
                key: row[key]
                for key in (
                    "owner",
                    "acl",
                    "security_definer",
                    "configuration",
                    "volatility",
                    "result",
                    "definition",
                )
            }
            for row in rows
        }
        access = (
            await connection.execute(
                text(
                    """SELECT namespace.nspname || '.' || function.proname || '(' ||
                              pg_catalog.replace(
                                  pg_catalog.oidvectortypes(function.proargtypes),
                                  ' ', ''
                              ) || ')' AS signature,
                              pg_catalog.has_function_privilege(
                                  'workspace_code', function.oid, 'EXECUTE'
                              ) AS workspace_can_execute,
                              pg_catalog.has_function_privilege(
                                  'workspace_configuration_client', function.oid,
                                  'EXECUTE'
                              ) AS client_can_execute,
                              EXISTS (
                                  SELECT 1
                                  FROM pg_catalog.aclexplode(COALESCE(
                                      function.proacl,
                                      pg_catalog.acldefault('f', function.proowner)
                                  )) AS acl
                                  WHERE acl.grantee = 0
                                    AND acl.privilege_type = 'EXECUTE'
                              ) AS public_can_execute
                       FROM pg_catalog.pg_proc AS function
                       JOIN pg_catalog.pg_namespace AS namespace
                         ON namespace.oid = function.pronamespace
                       WHERE namespace.nspname || '.' || function.proname || '(' ||
                             pg_catalog.replace(
                                 pg_catalog.oidvectortypes(function.proargtypes),
                                 ' ', ''
                             ) || ')' = ANY(CAST(:signatures AS text[]))
                       ORDER BY signature"""
                ),
                {"signatures": signatures},
            )
        ).mappings()
        access_facts = {row["signature"]: dict(row) for row in access}

    assert set(metadata) == set(_FUNCTIONS)
    for signature, owner in _FUNCTIONS.items():
        facts = metadata[signature]
        assert facts["owner"] == owner
        assert facts["security_definer"] is True
        assert "search_path=pg_catalog, pg_temp" in facts["configuration"]
        assert access_facts[signature]["public_can_execute"] is False
    assert (
        access_facts[
            "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])"
        ]["client_can_execute"]
        is True
    )
    assert (
        access_facts[
            "aap_workspace_configuration_v1._class_rename_project_row(uuid,text)"
        ]["client_can_execute"]
        is False
    )
    assert (
        access_facts[
            "aap_workspace_configuration_v1._class_rename_member_rows(uuid,uuid)"
        ]["client_can_execute"]
        is False
    )
    labeling = access_facts[
        "aap_labeling_v1.rename_project_annotation_class(uuid,text,text)"
    ]
    assert labeling["workspace_can_execute"] is True
    assert labeling["client_can_execute"] is False
    return metadata


async def _assert_configuration_client(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "workspace_configuration_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_workspace_configuration_v1",
            functions=_CONFIGURATION_CLIENT_FUNCTIONS,
        )
        for statement in (
            "SELECT id FROM public.users LIMIT 1",
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.annotations LIMIT 1",
            "SELECT aap_workspace_configuration_v1._class_rename_project_row(NULL::uuid, NULL)",
            "SELECT aap_workspace_configuration_v1._class_rename_member_rows(NULL::uuid, NULL::uuid)",
            "SELECT aap_labeling_v1.rename_project_annotation_class(NULL::uuid, NULL, NULL)",
            "CREATE TEMP TABLE forbidden_project_class_restore(id integer)",
            "SET ROLE workspace_code",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


async def _replay_bindings(
    database_engine, app_module, monkeypatch, tmp_path, worlds, fixture
) -> dict[str, int]:
    maker = async_sessionmaker(database_engine, expire_on_commit=False)
    replayed = {}
    for mode in _BINDINGS:
        runtime_engine = None
        async with class_rename_binding(
            monkeypatch, database_engine, mode=mode, tmp_path=tmp_path
        ):
            runtime_engine, runtime_maker = await make_request_runtime(
                database_engine,
                application_name=f"class-rename-restore-{mode}",
            )
            try:
                async with api_db_binding(app_module, runtime_maker):
                    transport = httpx.ASGITransport(
                        app=app_module, raise_app_exceptions=False
                    )
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://class-rename-restore-replay",
                        timeout=30,
                    ) as client:
                        with _observe_api_commits(runtime_engine) as commit_state:
                            await _replay_http_samples(
                                client, maker, worlds[mode], fixture, commit_state
                            )
                            if mode == "borrowed":
                                await _verify_borrowed_caller_rollback(
                                    maker, runtime_maker, worlds[mode]
                                )
            finally:
                await runtime_engine.dispose()
        replayed[mode] = len(fixture["samples"])
    return replayed


async def _clone_only_revocation_probe(
    engine, app_module, monkeypatch, tmp_path, world, inactive_response
) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = world["actors"]["owner"].id
    project_id = aliased_uuid(world, "project_scoped")
    before = await legacy._snapshot(maker, project_id)
    async with engine.begin() as connection:
        result = await connection.execute(
            text("UPDATE public.users SET is_active=false WHERE id=:id"),
            {"id": actor_id},
        )
        assert result.rowcount == 1

    runtime_engine = None
    try:
        async with class_rename_binding(
            monkeypatch, engine, mode="borrowed", tmp_path=tmp_path
        ):
            runtime_engine, runtime_maker = await make_request_runtime(
                engine, application_name="class-rename-restore-clone-revoked"
            )
            async with api_db_binding(app_module, runtime_maker):
                transport = httpx.ASGITransport(
                    app=app_module, raise_app_exceptions=False
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://class-rename-restore-clone-revoked",
                    timeout=20,
                ) as client:
                    with _observe_api_commits(runtime_engine) as commit_state:
                        response, commits = await _request_with_commit_count(
                            commit_state,
                            client.post(
                                f"{legacy._ROUTE}/{project_id}/classes/rename",
                                headers={
                                    "Authorization": "Bearer "
                                    + world["tokens"]["owner"]
                                },
                                json={
                                    "old_name": "Truck",
                                    "new_name": "Truck",
                                    "tool_unit_id": "bbox",
                                },
                            ),
                        )
                    _assert_response(inactive_response, response, world["aliases"])
                    assert commits == 0
        assert _business_snapshot(
            await legacy._snapshot(maker, project_id), world["aliases"]
        ) == _business_snapshot(before, world["aliases"])
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()


async def _request_with_commit_count(commit_state, awaitable):
    commit_state["count"] = 0
    commit_state["active"] = True
    try:
        response = await awaitable
    finally:
        commit_state["active"] = False
    return response, commit_state["count"]


async def _assert_source_survived_clone_revocation(
    test_engine, app_module, monkeypatch, tmp_path, world
) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = world["actors"]["owner"].id
    project_id = aliased_uuid(world, "project_scoped")
    async with test_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT is_active FROM public.users WHERE id=:id"),
                {"id": actor_id},
            )
            is True
        )
    before = await legacy._snapshot(maker, project_id)
    runtime_engine = None
    try:
        async with class_rename_binding(
            monkeypatch, test_engine, mode="borrowed", tmp_path=tmp_path
        ):
            runtime_engine, runtime_maker = await make_request_runtime(
                test_engine, application_name="class-rename-restore-source-active"
            )
            async with api_db_binding(app_module, runtime_maker):
                transport = httpx.ASGITransport(
                    app=app_module, raise_app_exceptions=False
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://class-rename-restore-source-active",
                    timeout=20,
                ) as client:
                    with _observe_api_commits(runtime_engine) as commit_state:
                        response, commits = await _request_with_commit_count(
                            commit_state,
                            client.post(
                                f"{legacy._ROUTE}/{project_id}/classes/rename",
                                headers={
                                    "Authorization": "Bearer "
                                    + world["tokens"]["owner"]
                                },
                                json={
                                    "old_name": "Truck",
                                    "new_name": "Truck",
                                    "tool_unit_id": "bbox",
                                },
                            ),
                        )
                    assert response.status_code == 200, response.text
                    assert response.json()["id"] == str(project_id)
                    assert (
                        response.json()["tool_bindings"]["bbox"]["classes"][0]["name"]
                        == "Truck"
                    )
                    assert commits == 0
        assert _business_snapshot(
            await legacy._snapshot(maker, project_id), world["aliases"]
        ) == _business_snapshot(before, world["aliases"])
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()


def _clone_urls(test_engine, clone: str) -> dict[str, str]:
    values = {
        "TEST_DATABASE_URL": test_engine.url.set(database=clone).render_as_string(
            hide_password=False
        ),
        "AAP_MODULE_DATA_LEGACY_RUNTIME_URL": _dsn(
            test_engine, "AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime", clone
        ),
        "AAP_MODULE_DATA_WORKSPACE_CONFIGURATION_URL": configuration_dsn(
            test_engine, clone
        ),
        "AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL": project_query_dsn(
            test_engine, clone
        ),
        "AAP_MODULE_DATA_IDENTITY_BRIEFS_URL": project_query_briefs_dsn(
            test_engine, clone
        ),
    }
    return {
        name: make_url(value)
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
        for name, value in values.items()
    }


async def test_project_class_rename_owner_survives_owned_database_restore(
    test_engine, app_module, monkeypatch, tmp_path
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    await assert_owned_test_database(test_engine)
    fixture = samples_fixture()
    worlds = {}
    clone = f"aap_workspace_configuration_restore_{uuid.uuid4().hex[:10]}_test"
    assert len(clone.encode("utf-8")) <= 63 and clone.endswith("_test")
    ownership = f"project-class-rename-restore:{uuid.uuid4()}"
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
        for mode in _BINDINGS:
            worlds[mode] = await seed_legacy_world(
                test_engine, app_module, monkeypatch, tmp_path
            )

        source_functions = await _class_rename_function_metadata(test_engine)
        source_configuration_dsn = configuration_dsn(test_engine)
        await _assert_configuration_client(source_configuration_dsn)

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600

        source_replayed = await _replay_bindings(
            test_engine, app_module, monkeypatch, tmp_path, worlds, fixture
        )
        assert source_replayed == {mode: 21 for mode in _BINDINGS}
        assert await _class_rename_function_metadata(test_engine) == source_functions

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
                source_acl = (
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
        clone_urls = _clone_urls(test_engine, clone)
        for value in clone_urls.values():
            _verify_postgres(make_url(value))

        with monkeypatch.context() as clone_environment:
            for name, value in clone_urls.items():
                clone_environment.setenv(name, value)
            await assert_owned_test_database(restored_engine)

            clone_functions = await _class_rename_function_metadata(restored_engine)
            assert clone_functions == source_functions
            clone_configuration_dsn = configuration_dsn(restored_engine)
            await _assert_configuration_client(clone_configuration_dsn)

            clone_replayed = await _replay_bindings(
                restored_engine,
                app_module,
                monkeypatch,
                tmp_path,
                worlds,
                fixture,
            )
            assert clone_replayed == source_replayed == {mode: 21 for mode in _BINDINGS}
            assert (
                await _class_rename_function_metadata(restored_engine)
                == clone_functions
            )

            inactive_response = next(
                sample["response"]
                for sample in fixture["samples"]
                if sample["label"] == "inactive_owner"
            )
            await _clone_only_revocation_probe(
                restored_engine,
                app_module,
                monkeypatch,
                tmp_path,
                worlds["borrowed"],
                inactive_response,
            )
            async with test_engine.connect() as source:
                assert (
                    await source.scalar(
                        text("SELECT is_active FROM public.users WHERE id=:id"),
                        {"id": worlds["borrowed"]["actors"]["owner"].id},
                    )
                    is True
                )

        await _assert_source_survived_clone_revocation(
            test_engine,
            app_module,
            monkeypatch,
            tmp_path,
            worlds["borrowed"],
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
        finally:
            await admin.dispose()
            for world in worlds.values():
                await cleanup_world(test_engine, world)
            snapshot.unlink(missing_ok=True)
