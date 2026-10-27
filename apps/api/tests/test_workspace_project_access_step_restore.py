"""Restore inference's fixed Workspace Project access facts from pg_dump."""

from __future__ import annotations

import json
import os
import stat
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from aap_workspace_contract.member_values import decode_project_member_rows
from aap_workspace_contract.project_query_values import decode_project_row
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _postgres_tool
from tests.test_workspace_project_access_step_native import (
    _FUNCTION,
    _cleanup,
    _connect_inference,
    _owner_dsn,
    _seed,
    _step,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _database_head(engine) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )


async def _function_metadata(engine) -> dict:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                              function.proacl::text AS acl,
                              function.prosecdef AS security_definer,
                              function.proconfig::text AS configuration,
                              function.provolatile::text AS volatility,
                              pg_catalog.pg_get_function_result(function.oid) AS result,
                              pg_catalog.pg_get_functiondef(function.oid) AS definition,
                              pg_catalog.has_function_privilege(
                                  'inference_code', function.oid, 'EXECUTE'
                              ) AS inference_can_execute,
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
                       WHERE function.oid = pg_catalog.to_regprocedure(:signature)"""
                    ),
                    {"signature": _FUNCTION},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _assert_function_capability(engine) -> dict:
    metadata = await _function_metadata(engine)
    assert metadata["owner"] == "workspace_code"
    assert metadata["security_definer"] is True
    assert metadata["result"] == "jsonb"
    assert metadata["volatility"] == "v"
    assert "search_path=pg_catalog, pg_temp" in metadata["configuration"]
    assert "TimeZone=UTC" in metadata["configuration"]
    assert "DateStyle=ISO, YMD" in metadata["configuration"]
    assert "extra_float_digits=3" in metadata["configuration"]
    assert metadata["inference_can_execute"] is True
    assert metadata["public_can_execute"] is False
    return metadata


async def _assert_inference_scope(engine, world) -> None:
    owner_connection = await asyncpg.connect(_owner_dsn(engine))
    inference = None
    try:
        assert (
            await owner_connection.fetchval(
                "SELECT pg_catalog.pg_has_role("
                "'inference_code', 'workspace_code', 'MEMBER')"
            )
            is False
        )
        rows = await owner_connection.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                         pg_catalog.replace(
                             pg_catalog.oidvectortypes(function.proargtypes), ' ', ''
                         ) || ')' AS signature
                  FROM pg_catalog.pg_proc AS function
                  JOIN pg_catalog.pg_namespace AS namespace
                    ON namespace.oid = function.pronamespace
                 WHERE namespace.nspname = 'aap_workspace_v1'
                   AND pg_catalog.has_function_privilege(
                       'inference_code', function.oid, 'EXECUTE'
                   )"""
        )
        assert {row["signature"] for row in rows} == {
            _FUNCTION,
            "aap_workspace_v1.clear_primary_backend_bindings(uuid)",
        }

        inference = await _connect_inference(engine)
        raw_reads = (
            (
                "SELECT id FROM public.users WHERE id=$1::uuid LIMIT 1",
                (world["member_id"],),
            ),
            (
                "SELECT id FROM public.projects WHERE id=$1::uuid LIMIT 1",
                (world["project_id"],),
            ),
            (
                "SELECT id FROM public.project_members "
                "WHERE project_id=$1::uuid LIMIT 1",
                (world["project_id"],),
            ),
        )
        for statement, parameters in raw_reads:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await inference.fetch(statement, *parameters)

        private_calls = (
            "SELECT aap_workspace_configuration_v1._class_rename_project_row(NULL::uuid,'key_share')",
            "SELECT aap_workspace_configuration_v1._class_rename_member_rows(NULL::uuid,NULL::uuid)",
            "SELECT aap_workspace_v1._project_read_failure('private')",
            "SELECT aap_workspace_v1.lock_project_manager(NULL::uuid,NULL::uuid)",
            "SELECT aap_labeling_v1.workspace_project_query_counters(ARRAY[]::uuid[])",
        )
        for statement in private_calls:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await inference.fetchval(statement)
    finally:
        try:
            if inference is not None:
                await inference.close()
        finally:
            await owner_connection.close()


async def _decode_all_phases(engine, world, *, active: bool, membership_count: int):
    connection = await _connect_inference(engine)
    try:
        async with connection.transaction():
            actor = await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_actor_share",
            )
            assert set(actor) == {"actor"}
            assert actor["actor"] == {
                "actor_id": str(world["member_id"]),
                "platform_role": "employee",
                "is_active": active,
            }

            project = await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_project_key_share",
            )
            assert set(project) == {"row_json"}
            assert isinstance(project["row_json"], str)
            decoded_project = decode_project_row(
                project["row_json"], target_id=world["project_id"]
            )
            assert decoded_project["id"] == str(world["project_id"])
            assert len(json.loads(project["row_json"])) == 44

            member = await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_member_share",
            )
            assert set(member) == {"rows_json"}
            decoded_members = decode_project_member_rows(
                member["rows_json"],
                target_id=world["project_id"],
                user_id=world["member_id"],
                max_rows=1,
            )
            assert len(decoded_members) == membership_count
            if membership_count:
                assert decoded_members[0].id == world["membership_id"]
                assert decoded_members[0].project_id == world["project_id"]
                assert decoded_members[0].user_id == world["member_id"]
                assert decoded_members[0].role == "annotator"
                assert decoded_members[0].assigned_by == world["owner_id"]
            else:
                assert json.loads(member["rows_json"]) == []
            return {
                "actor": actor,
                "project_row_json": project["row_json"],
                "member_rows_json": member["rows_json"],
            }
    finally:
        await connection.close()


async def _assert_clone_owner(engine, clone: str, ownership: str) -> None:
    _verify_postgres(engine.url)
    async with engine.connect() as connection:
        current_user, database, can_create = (
            await connection.execute(
                text(
                    "SELECT current_user, current_database(), "
                    "pg_catalog.has_schema_privilege(current_user, "
                    "'aap_labeling_class_query_v1', 'CREATE')"
                )
            )
        ).one()
    assert database == clone and database.endswith("_test")
    assert current_user not in {
        "legacy_runtime",
        "inference_code",
        "workspace_project_query_client",
    }
    assert can_create
    async with engine.connect() as connection:
        marker = await connection.scalar(
            text(
                "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                "FROM pg_catalog.pg_database WHERE datname=:name"
            ),
            {"name": clone},
        )
    assert marker == ownership


async def test_workspace_project_access_step_survives_owned_database_restore(
    test_engine, tmp_path
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    maker, world = await _seed(test_engine)
    clone = f"aap_workspace_access_step_restore_{uuid.uuid4().hex[:10]}_test"
    assert len(clone.encode("utf-8")) <= 63 and clone.endswith("_test")
    ownership = f"workspace-project-access-step-restore:{uuid.uuid4()}"
    quoted_clone = test_engine.dialect.identifier_preparer.quote_identifier(clone)
    snapshot = tmp_path / "workspace-project-access-step.dump"
    database_created = False
    restored_engine = None
    admin = None

    try:
        admin = create_async_engine(
            test_engine.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )

        source_function = await _assert_function_capability(test_engine)
        await _assert_inference_scope(test_engine, world)
        source_facts = await _decode_all_phases(
            test_engine, world, active=True, membership_count=1
        )

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
        await _assert_clone_owner(restored_engine, clone, ownership)

        clone_function = await _assert_function_capability(restored_engine)
        assert clone_function == source_function
        await _assert_inference_scope(restored_engine, world)
        clone_facts = await _decode_all_phases(
            restored_engine, world, active=True, membership_count=1
        )
        assert clone_facts == source_facts

        async with restored_engine.begin() as connection:
            result = await connection.execute(
                update(User)
                .where(User.id == world["member_id"])
                .values(is_active=False)
            )
            assert result.rowcount == 1
            result = await connection.execute(
                delete(ProjectMember).where(ProjectMember.id == world["membership_id"])
            )
            assert result.rowcount == 1
        inactive_clone_facts = await _decode_all_phases(
            restored_engine, world, active=False, membership_count=0
        )
        assert (
            inactive_clone_facts["project_row_json"] == source_facts["project_row_json"]
        )
        assert inactive_clone_facts["member_rows_json"] == "[]"

        async with test_engine.connect() as source:
            active, role, membership_id = (
                await source.execute(
                    text(
                        "SELECT actor.is_active, member.role, member.id "
                        "FROM public.users AS actor "
                        "JOIN public.project_members AS member "
                        "ON member.user_id=actor.id "
                        "WHERE actor.id=:actor_id AND member.project_id=:project_id"
                    ),
                    {
                        "actor_id": world["member_id"],
                        "project_id": world["project_id"],
                    },
                )
            ).one()
        assert active is True
        assert role == "annotator"
        assert membership_id == world["membership_id"]
        assert (
            await _decode_all_phases(
                test_engine, world, active=True, membership_count=1
            )
            == source_facts
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
                    active = await connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=:name "
                            "AND pid <> pg_catalog.pg_backend_pid() "
                            "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                        ),
                        {"name": clone},
                    )
                    assert not active
                    await connection.execute(text(f"DROP DATABASE {quoted_clone}"))
        finally:
            if admin is not None:
                await admin.dispose()
            await _cleanup(maker, world)
            snapshot.unlink(missing_ok=True)
