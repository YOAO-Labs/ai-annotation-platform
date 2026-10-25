"""Owned-role acceptance for the stateless Workspace guide-asset actions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from aap_workspace_contract.project_query_values import decode_project_row
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_CONFIG_SCHEMA = "aap_workspace_configuration_v1"
_CONFIG_STEP = (
    "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])"
)
_PROJECT_SCHEMA = "aap_workspace_project_query_v1"
_PROJECT_STEP_SIGNATURE = "aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)"
_PROJECT_MEMBER_COLUMNS = {
    "id",
    "project_id",
    "user_id",
    "role",
    "assigned_by",
    "assigned_at",
    "version",
    "updated_at",
    "weekly_target",
}
_ROOT = Path(__file__).resolve().parents[3]


def _configuration_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Workspace Configuration LOGIN requires owned verifier resources")
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_CONFIGURATION_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_configuration_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _project_query_dsn(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_project_query_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _request(operation, actor_id, project_id, payload=None):
    selectors = {
        "source_project_id": None,
        "template_id": None,
        "ml_backend_source_id": None,
        "ml_backend_id": None,
        "ml_backend_present": False,
        "scope": None,
        "scope_present": False,
        "organization_id": None,
        "organization_present": False,
    }
    payload_ascii = json.dumps(
        {} if payload is None else payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "schema": "aap-workspace-configuration/v1",
        "operation_id": str(uuid.uuid4()),
        "operation": operation,
        "actor_id": str(actor_id),
        "target_id": str(project_id),
        "selectors": selectors,
        "payload_ascii": payload_ascii,
    }


def _json_object(value):
    return json.loads(value) if isinstance(value, str) else value


def _guide_assets_ascii(assets) -> str:
    return json.dumps(
        assets,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def _step(
    connection,
    request,
    action,
    args=None,
    *,
    column_names=None,
    column_values=None,
):
    raw = await connection.fetchval(
        f"SELECT {_CONFIG_SCHEMA}.configuration_step("
        "$1::jsonb,$2::text,$3::jsonb,$4::text[],$5::text[])",
        json.dumps(request, ensure_ascii=True, separators=(",", ":")),
        action,
        json.dumps({} if args is None else args, ensure_ascii=True),
        [] if column_names is None else column_names,
        [] if column_values is None else column_values,
    )
    reply = _json_object(raw)
    assert reply["schema"] == "aap-workspace-configuration/v1"
    assert reply["operation_id"] == request["operation_id"]
    assert reply["operation"] == request["operation"]
    assert reply["action"] == action
    return reply


async def _project_step(connection, request, action):
    raw = await connection.fetchval(
        f"SELECT {_PROJECT_SCHEMA}.project_query_step("
        "$1::jsonb,$2::text,$3::jsonb,$4::text,$5::text,$6::text[],"
        "$7::text[],$8::uuid,$9::text,$10::timestamptz,$11::timestamptz,"
        "$12::bigint,$13::integer)",
        json.dumps(request, ensure_ascii=True, separators=(",", ":")),
        action,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    return _json_object(raw)


async def _seed(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = {"actor_ids": [], "project_ids": []}
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"guide-native-owner-{uuid.uuid4().hex}@example.test",
                "Guide Native Owner",
            )
            member = await create_user(
                db,
                "employee",
                f"guide-native-member-{uuid.uuid4().hex}@example.test",
                "Guide Native Member",
            )
            world["actor_ids"].extend((owner.id, member.id))
            project = await create_project(db, owner_id=owner.id)
            world["project_ids"].append(project.id)
            key = f"projects/{project.id}/guide/{uuid.uuid4()}-existing.png"
            project.guide_assets = [
                {
                    "key": key,
                    "original_name": "existing.png",
                    "content_type": "image/png",
                    "size": 32,
                    "uploaded_at": "2026-10-21T00:00:00+00:00",
                }
            ]
            await create_membership(
                db,
                project_id=project.id,
                user_id=member.id,
                role="annotator",
                assigned_by=owner.id,
            )
            world.update(
                owner_id=owner.id,
                member_id=member.id,
                project_id=project.id,
                guide_key=key,
            )
        return maker, world
    except BaseException:
        await _cleanup(maker, world)
        raise


async def _cleanup(maker, world):
    async with maker.begin() as db:
        if world["project_ids"]:
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_(world["project_ids"])
                )
            )
            await db.execute(
                delete(Project).where(Project.id.in_(world["project_ids"]))
            )
        if world["actor_ids"]:
            await db.execute(delete(User).where(User.id.in_(world["actor_ids"])))


async def _project_state(maker, project_id):
    async with maker() as db:
        project = await db.get(Project, project_id, populate_existing=True)
        assert project is not None
        return {
            "name": project.name,
            "owner_id": project.owner_id,
            "tool_bindings": project.tool_bindings,
            "guide_assets": project.guide_assets,
            "updated_at": project.updated_at,
        }


async def _operation_counts(maker, operation_ids):
    result = {}
    async with maker() as db:
        for suffix in ("states", "attempts", "receipts"):
            result[suffix] = await db.scalar(
                text(
                    f"SELECT count(*) FROM public.workspace_configuration_operation_{suffix} "
                    "WHERE operation_id=ANY(:ids)"
                ),
                {"ids": list(operation_ids)},
            )
    return result


async def test_guide_native_returns_full_phase_facts_and_keeps_shared_query_visibility(
    test_engine,
):
    maker, world = await _seed(test_engine)
    connection = None
    query_connection = None
    try:
        connection = await asyncpg.connect(_configuration_dsn(test_engine))
        complete_request = _request(
            "guide_asset_upload_complete",
            world["owner_id"],
            world["project_id"],
            {"key": world["guide_key"], "content_type": "image/png"},
        )
        member_complete_request = _request(
            "guide_asset_upload_complete",
            world["member_id"],
            world["project_id"],
            {"key": world["guide_key"], "content_type": "image/png"},
        )
        sign_request = _request(
            "guide_asset_sign",
            world["member_id"],
            world["project_id"],
            {"key": world["guide_key"]},
        )

        async with connection.transaction():
            exists = await _step(connection, complete_request, "guide_account_exists")
            assert exists["ok"] is True
            assert exists["facts"] == {"exists": True}

            unknown_actor = uuid.uuid4()
            unknown_request = _request(
                "guide_asset_upload_complete",
                unknown_actor,
                world["project_id"],
                {"key": world["guide_key"], "content_type": "image/png"},
            )
            unknown_exists = await _step(
                connection, unknown_request, "guide_account_exists"
            )
            assert unknown_exists["facts"] == {"exists": False}
            unknown_status = await _step(
                connection, unknown_request, "guide_actor_status"
            )
            assert unknown_status["facts"] == {"actor": None}
            # The post-commit refresh deliberately does not reauthorize its actor.
            post_commit = await _step(connection, unknown_request, "guide_refresh")
            decode_project_row(
                post_commit["facts"]["row_json"],
                target_id=world["project_id"],
            )

            first_project = await _step(
                connection, complete_request, "guide_read_project"
            )
            project = decode_project_row(
                first_project["facts"]["row_json"],
                target_id=world["project_id"],
            )
            assert set(json.loads(first_project["facts"]["row_json"])) == set(
                PROJECT_ROW_COLUMNS
            )
            assert json.loads(project["guide_assets"])[0]["key"] == world["guide_key"]

            status = await _step(connection, complete_request, "guide_actor_status")
            assert status["facts"]["actor"] == {
                "actor_id": str(world["owner_id"]),
                "platform_role": "project_admin",
                "is_active": True,
            }

            member_read = await _step(
                connection, member_complete_request, "guide_read_member"
            )
            member_rows = json.loads(member_read["facts"]["rows_json"])
            assert len(member_rows) == 1
            assert set(member_rows[0]) == _PROJECT_MEMBER_COLUMNS
            assert member_rows[0]["user_id"] == str(world["member_id"])

            shared = await _step(connection, sign_request, "guide_shared")
            assert set(shared["facts"]) == {"actor", "row_json"}
            assert shared["facts"]["actor"] == {
                "actor_id": str(world["member_id"]),
                "platform_role": "employee",
                "is_active": True,
            }
            decode_project_row(
                shared["facts"]["row_json"],
                target_id=world["project_id"],
            )
            locked_member = await _step(connection, sign_request, "guide_member")
            locked_rows = json.loads(locked_member["facts"]["rows_json"])
            assert len(locked_rows) == 1
            assert set(locked_rows[0]) == _PROJECT_MEMBER_COLUMNS
            assert locked_rows[0]["user_id"] == str(world["member_id"])

            locked_project = await _step(
                connection,
                complete_request,
                "guide_lock",
            )
            decode_project_row(
                locked_project["facts"]["row_json"],
                target_id=world["project_id"],
            )
            refreshed = await _step(connection, complete_request, "guide_refresh")
            decode_project_row(
                refreshed["facts"]["row_json"],
                target_id=world["project_id"],
            )

        visible_request = {
            "schema": "aap-workspace-project-query/v1",
            "request_id": str(uuid.uuid4()),
            "operation": "require_project_visible",
            "admitted_actor": {
                "id": str(world["member_id"]),
                "role": "employee",
            },
            "target_id": str(world["project_id"]),
            "filters_ascii": None,
            "pagination": None,
            "projection_evidence": None,
        }
        query_connection = await asyncpg.connect(_project_query_dsn(test_engine))
        row_reply = await _project_step(query_connection, visible_request, "get_row")
        assert row_reply["schema"] == "aap-workspace-project-query/v1"
        assert row_reply["request"] == visible_request
        assert row_reply["action"] == "get_row"
        decode_project_row(
            row_reply["facts"]["row_json"],
            target_id=world["project_id"],
        )
        status_reply = await _project_step(
            query_connection, visible_request, "actor_status"
        )
        assert status_reply["facts"]["actor"] == {
            "actor_id": str(world["member_id"]),
            "platform_role": "employee",
            "is_active": True,
        }
        access_rows = await _project_step(
            query_connection, visible_request, "access_member_rows"
        )
        visible_members = json.loads(access_rows["facts"]["rows_json"])
        assert len(visible_members) == 1
        assert set(visible_members[0]) == _PROJECT_MEMBER_COLUMNS
    finally:
        if connection is not None:
            await connection.close()
        if query_connection is not None:
            await query_connection.close()
        await _cleanup(maker, world)


async def test_guide_apply_rechecks_manager_authority_and_only_changes_guide_assets(
    test_engine,
):
    maker, world = await _seed(test_engine)
    connection = None
    operation_ids = []
    try:
        before = await _project_state(maker, world["project_id"])
        connection = await asyncpg.connect(_configuration_dsn(test_engine))
        denied_request = _request(
            "guide_asset_delete", world["member_id"], world["project_id"], {}
        )
        operation_ids.append(uuid.UUID(denied_request["operation_id"]))
        async with connection.transaction():
            denied = await _step(
                connection,
                denied_request,
                "guide_apply",
                {"guide_assets_ascii": _guide_assets_ascii([])},
            )
            assert denied["ok"] is False
            assert denied["error"]["status_code"] == 403
        assert await _project_state(maker, world["project_id"]) == before

        rollback_request = _request(
            "guide_asset_upload_complete", world["owner_id"], world["project_id"], {}
        )
        operation_ids.append(uuid.UUID(rollback_request["operation_id"]))
        transaction = connection.transaction()
        await transaction.start()
        try:
            rolled_back = await _step(
                connection,
                rollback_request,
                "guide_apply",
                {"guide_assets_ascii": _guide_assets_ascii([])},
            )
            assert rolled_back["ok"] is True
        finally:
            await transaction.rollback()
        assert await _project_state(maker, world["project_id"]) == before

        owner_request = _request(
            "guide_asset_delete", world["owner_id"], world["project_id"], {}
        )
        operation_ids.append(uuid.UUID(owner_request["operation_id"]))
        async with connection.transaction():
            applied = await _step(
                connection,
                owner_request,
                "guide_apply",
                {"guide_assets_ascii": _guide_assets_ascii([])},
            )
            assert applied["ok"] is True and applied["facts"] == {}
            # The Native transaction has not committed yet.
            assert await _operation_counts(maker, operation_ids) == {
                "states": 0,
                "attempts": 0,
                "receipts": 0,
            }
        after = await _project_state(maker, world["project_id"])
        assert after["guide_assets"] == []
        assert after["name"] == before["name"]
        assert after["owner_id"] == before["owner_id"]
        assert after["tool_bindings"] == before["tool_bindings"]
        assert after["updated_at"] >= before["updated_at"]
        assert await _operation_counts(maker, operation_ids) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup(maker, world)


async def test_guide_native_closes_action_arguments_and_rejects_wrong_phase_values(
    test_engine,
):
    maker, world = await _seed(test_engine)
    connection = None
    try:
        connection = await asyncpg.connect(_configuration_dsn(test_engine))
        request = _request(
            "guide_asset_upload_complete", world["owner_id"], world["project_id"], {}
        )
        with pytest.raises(asyncpg.PostgresError) as error:
            await _step(
                connection,
                request,
                "guide_apply",
                {
                    "guide_assets_ascii": _guide_assets_ascii([]),
                    "extra": True,
                },
            )
        assert error.value.sqlstate == "22023"

        sign_request = _request(
            "guide_asset_sign", world["owner_id"], world["project_id"], {}
        )
        with pytest.raises(asyncpg.PostgresError) as error:
            await _step(connection, sign_request, "guide_account_exists")
        assert error.value.sqlstate == "22023"

        with pytest.raises(asyncpg.PostgresError) as error:
            await _step(
                connection,
                sign_request,
                "guide_shared",
                column_names=["name"],
                column_values=["generic writes are closed"],
            )
        assert error.value.sqlstate == "22023"

        wrong_selectors = dict(request)
        wrong_selectors["selectors"] = {
            **request["selectors"],
            "scope_present": True,
        }
        with pytest.raises(asyncpg.PostgresError) as error:
            await _step(connection, wrong_selectors, "guide_account_exists")
        assert error.value.sqlstate == "22023"

        with pytest.raises(asyncpg.PostgresError) as error:
            await _step(
                connection,
                request,
                "guide_apply",
                {"guide_assets_ascii": "{}"},
            )
        assert error.value.sqlstate == "22023"

        with pytest.raises(asyncpg.PostgresError) as error:
            await _step(
                connection,
                request,
                "guide_apply",
                {"guide_assets_ascii": '[{"name":"指引"}]'},
            )
        assert error.value.sqlstate == "22023"
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup(maker, world)


async def _function_metadata(connection):
    result = await connection.execute(
        text(
            """SELECT function.oid::bigint AS oid,
                      function.oid::regprocedure::text AS signature,
                      pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                      function.proacl::text AS acl,
                      function.prosecdef AS security_definer,
                      function.proconfig AS settings,
                      pg_catalog.pg_get_functiondef(function.oid) AS definition
               FROM pg_catalog.pg_proc AS function
               WHERE function.oid=ANY(ARRAY[
                   pg_catalog.to_regprocedure(:config_step)::oid,
                   pg_catalog.to_regprocedure(:query_step)::oid,
                   pg_catalog.to_regprocedure(:read_project)::oid,
                   pg_catalog.to_regprocedure(:read_member)::oid
               ]) ORDER BY function.oid"""
        ),
        {
            "config_step": _CONFIG_STEP,
            "query_step": _PROJECT_STEP_SIGNATURE,
            "read_project": "aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)",
            "read_member": "aap_workspace_configuration_v1._guide_asset_read_member_rows(uuid,uuid)",
        },
    )
    return {row["signature"]: dict(row) for row in result.mappings()}


async def test_guide_native_replacement_retains_existing_function_oids_and_acls(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    sql_dir = _ROOT / "apps/api/alembic/sql"
    old_config_sql = (sql_dir / "0268_workspace_class_rename.sql").read_text()
    old_config_step = next(
        statement.strip()
        for statement in old_config_sql.split("-- aap-statement-break")
        if statement.strip().startswith(
            "CREATE OR REPLACE FUNCTION aap_workspace_configuration_v1.configuration_step("
        )
    )
    old_query_sql = (sql_dir / "0265_workspace_manager_queries.sql").read_text()
    old_query_step = old_query_sql[
        old_query_sql.index(
            "CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1.project_query_step("
        ) :
    ].strip()
    if not old_query_step.endswith("$function$;"):
        raise AssertionError("Frozen 0265 Project query wrapper is incomplete")
    new_statements = (
        (sql_dir / "0269_workspace_guide_assets.sql")
        .read_text()
        .split("-- aap-statement-break")
    )

    async with test_engine.connect() as owner:
        transaction = await owner.begin()
        try:
            before = await _function_metadata(owner)
            assert len(before) == 4
            for metadata in before.values():
                assert metadata["owner"] == "workspace_code"
                assert metadata["security_definer"] is True
                assert "search_path=pg_catalog, pg_temp" in metadata["settings"]

            await owner.exec_driver_sql(old_config_step)
            await owner.exec_driver_sql(old_query_step)
            restored = await _function_metadata(owner)
            for signature, metadata in before.items():
                for key in ("oid", "owner", "acl"):
                    assert restored[signature][key] == metadata[key]

            for statement in new_statements:
                if statement.strip():
                    await owner.exec_driver_sql(statement.strip())
            assert await _function_metadata(owner) == before
        finally:
            await transaction.rollback()
