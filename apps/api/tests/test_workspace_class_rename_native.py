"""Exercise the stateless class-rename capability with its actual narrow LOGIN."""

from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.configuration_contract import configuration_request
from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from aap_workspace_contract.project_query_values import decode_project_row
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import create_project, create_task, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_project_class_rename_legacy import _binding, _snapshot
from tests.test_workspace_configuration_native import _configuration_dsn


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_SCHEMA = "aap_workspace_configuration_v1"
_STEP_SIGNATURE = f"{_SCHEMA}.configuration_step(jsonb,text,jsonb,text[],text[])"
_HELPERS = (
    f"{_SCHEMA}._class_rename_project_row(uuid,text)",
    f"{_SCHEMA}._class_rename_member_rows(uuid,uuid)",
)
_ROOT = Path(__file__).resolve().parents[3]


async def _step(connection, request, action, args=None):
    raw = await connection.fetchval(
        f"SELECT {_SCHEMA}.configuration_step("
        "$1::jsonb,$2::text,$3::jsonb,$4::text[],$5::text[])",
        request.model_dump_json(by_alias=True),
        action,
        json.dumps({} if args is None else args, ensure_ascii=True),
        [],
        [],
    )
    result = json.loads(raw)
    assert result["schema"] == "aap-workspace-configuration/v1"
    assert result["operation_id"] == str(request.operation_id)
    assert result["operation"] == request.operation
    assert result["action"] == action
    return result


def _request(world, *, actor=None):
    return configuration_request(
        "rename_project_class",
        world["owner_id"] if actor is None else actor,
        world["project_id"],
        {"old_name": "Car", "new_name": "Truck", "tool_unit_id": "bbox"},
    )


def _apply_args():
    return {
        "old_name": "Car",
        "new_name": "Truck",
        "tool_bindings_ascii": json.dumps(
            {"bbox": _binding(["Truck"])},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }


async def _seed(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = {"actor_ids": [], "project_ids": [], "task_ids": []}
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"rename-native-{uuid.uuid4().hex}@example.test",
                "Rename native owner",
            )
            other = await create_user(
                db,
                "project_admin",
                f"rename-member-{uuid.uuid4().hex}@example.test",
                "Rename native member",
            )
            world["actor_ids"].extend((owner.id, other.id))
            project = await create_project(db, owner_id=owner.id)
            world["project_ids"].append(project.id)
            project.tool_bindings = {"bbox": _binding(["Car"])}
            task = await create_task(db, project_id=project.id)
            world["task_ids"].append(task.id)
            annotation = Annotation(
                id=uuid.uuid4(),
                task_id=task.id,
                project_id=project.id,
                user_id=owner.id,
                annotation_type="bbox",
                tool_unit_id="bbox",
                source="manual",
                class_name="Car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                attributes={},
                version=7,
                is_active=True,
            )
            db.add(annotation)
            db.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    user_id=other.id,
                    role="viewer",
                    assigned_by=owner.id,
                )
            )
            world.update(
                owner_id=owner.id,
                other_id=other.id,
                project_id=project.id,
                annotation_id=annotation.id,
            )
        return maker, world
    except BaseException:
        await _cleanup(maker, world)
        raise


async def _cleanup(maker, world):
    async with maker.begin() as db:
        if world["project_ids"]:
            await db.execute(
                delete(Annotation).where(
                    Annotation.project_id.in_(world["project_ids"])
                )
            )
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_(world["project_ids"])
                )
            )
        if world["task_ids"]:
            await db.execute(delete(Task).where(Task.id.in_(world["task_ids"])))
        if world["project_ids"]:
            await db.execute(
                delete(Project).where(Project.id.in_(world["project_ids"]))
            )
        if world["actor_ids"]:
            await db.execute(delete(User).where(User.id.in_(world["actor_ids"])))


async def _operation_rows(maker, operation_ids):
    counts = {}
    async with maker() as db:
        for suffix in ("states", "attempts", "receipts"):
            table = f"public.workspace_configuration_operation_{suffix}"
            counts[suffix] = await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE operation_id=ANY(:ids)"),
                {"ids": list(operation_ids)},
            )
    return counts


async def test_class_rename_native_retains_atomic_caller_control_and_no_receipts(
    test_engine,
):
    maker, world = await _seed(test_engine)
    connection = None
    try:
        before = await _snapshot(maker, world["project_id"])
        request = _request(world)
        direct = _request(world)
        rejected = _request(world, actor=world["other_id"])
        operation_ids = (
            request.operation_id,
            direct.operation_id,
            rejected.operation_id,
        )
        assert await _operation_rows(maker, operation_ids) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
        connection = await asyncpg.connect(_configuration_dsn(test_engine))
        transaction = connection.transaction()
        await transaction.start()
        prepared = await _step(connection, request, "class_rename_prepare")
        assert prepared["ok"] is True
        assert set(prepared["facts"]) == {"actor", "row_json"}
        assert prepared["facts"]["actor"] == {
            "actor_id": str(world["owner_id"]),
            "platform_role": "project_admin",
            "is_active": True,
        }
        row = decode_project_row(
            prepared["facts"]["row_json"], target_id=world["project_id"]
        )
        assert json.loads(row["tool_bindings"])["bbox"]["classes"][0]["name"] == "Car"
        locked = await _step(connection, request, "class_rename_lock")
        assert locked["ok"] is True and set(locked["facts"]) == {"row_json"}
        assert (
            decode_project_row(
                locked["facts"]["row_json"], target_id=world["project_id"]
            )
            == row
        )
        applied = await _step(connection, request, "class_rename_apply", _apply_args())
        assert applied["ok"] is True and applied["facts"] == {}
        assert await _snapshot(maker, world["project_id"]) == before
        await transaction.rollback()
        assert await _snapshot(maker, world["project_id"]) == before

        async with connection.transaction():
            denied = await _step(
                connection, rejected, "class_rename_apply", _apply_args()
            )
            assert denied["ok"] is False
            assert denied["error"]["status_code"] == 403
        assert await _snapshot(maker, world["project_id"]) == before

        # Calling the typed effect action directly must still acquire live
        # authority/configuration fences instead of trusting earlier actions.
        async with connection.transaction():
            applied = await _step(
                connection, direct, "class_rename_apply", _apply_args()
            )
            assert applied["ok"] is True
            assert await _snapshot(maker, world["project_id"]) == before
        after = await _snapshot(maker, world["project_id"])
        assert after["tool_bindings"]["bbox"]["classes"][0]["name"] == "Truck"
        assert [row["class_name"] for row in after["annotations"]] == ["Truck"]
        assert after["annotations"][0]["version"] == before["annotations"][0]["version"]
        assert (
            after["annotations"][0]["updated_at"]
            == before["annotations"][0]["updated_at"]
        )
        assert await _operation_rows(maker, operation_ids) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup(maker, world)


async def test_class_rename_rolls_back_annotation_when_project_update_fails(
    test_engine,
):
    maker, world = await _seed(test_engine)
    suffix = uuid.uuid4().hex
    trigger = f"rename_failure_{suffix}"
    function = f"public.rename_failure_{suffix}"
    installed = False
    connection = None
    try:
        before = await _snapshot(maker, world["project_id"])
        # The exact owned row alone fails after the Labeling effect. Observing
        # Truck here proves the failure is between the two business writes.
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(
                f"""CREATE FUNCTION {function}() RETURNS trigger
                LANGUAGE plpgsql SECURITY DEFINER
                SET search_path=pg_catalog,pg_temp AS $probe$
                BEGIN
                    IF NEW.id = '{world["project_id"]}'::uuid THEN
                        IF (SELECT class_name FROM public.annotations
                            WHERE id='{world["annotation_id"]}'::uuid) IS DISTINCT FROM 'Truck'
                        THEN RAISE EXCEPTION 'Labeling effect has not run first'; END IF;
                        RAISE EXCEPTION USING ERRCODE='23514',
                            MESSAGE='owned project failure after Labeling effect';
                    END IF;
                    RETURN NEW;
                END; $probe$"""
            )
            await owner.exec_driver_sql(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF tool_bindings "
                f"ON public.projects FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
        installed = True
        connection = await asyncpg.connect(_configuration_dsn(test_engine))
        with pytest.raises(asyncpg.CheckViolationError, match="after Labeling effect"):
            async with connection.transaction():
                await _step(
                    connection, _request(world), "class_rename_apply", _apply_args()
                )
        assert await _snapshot(maker, world["project_id"]) == before
    finally:
        if connection is not None:
            await connection.close()
        if installed:
            async with test_engine.begin() as owner:
                await owner.exec_driver_sql(
                    f"DROP TRIGGER {trigger} ON public.projects"
                )
                await owner.exec_driver_sql(f"DROP FUNCTION {function}() RESTRICT")
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
                   pg_catalog.to_regprocedure(:step)::oid,
                   pg_catalog.to_regprocedure(:project)::oid,
                   pg_catalog.to_regprocedure(:member)::oid
               ]) ORDER BY function.oid"""
        ),
        {"step": _STEP_SIGNATURE, "project": _HELPERS[0], "member": _HELPERS[1]},
    )
    return {row["signature"]: dict(row) for row in result.mappings()}


async def test_class_rename_replacement_retains_live_oid_acl_and_function_owners(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    old_sql = (
        _ROOT / "apps/api/alembic/sql/0257_workspace_configuration.sql"
    ).read_text()
    old_step = next(
        statement.strip()
        for statement in old_sql.split("-- aap-statement-break")
        if statement.strip().startswith(
            f"CREATE FUNCTION {_SCHEMA}.configuration_step("
        )
    ).replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    new_statements = (
        (_ROOT / "apps/api/alembic/sql/0268_workspace_class_rename.sql")
        .read_text()
        .split("-- aap-statement-break")
    )
    async with test_engine.connect() as owner:
        transaction = await owner.begin()
        try:
            before = await _function_metadata(owner)
            assert len(before) == 3
            for metadata in before.values():
                assert metadata["owner"] == "workspace_code"
                assert metadata["security_definer"] is True
                assert "search_path=pg_catalog, pg_temp" in metadata["settings"]
            await owner.exec_driver_sql(old_step)
            legacy = await _function_metadata(owner)
            step_key = next(key for key in before if ".configuration_step(" in key)
            for key in ("oid", "owner", "acl"):
                assert legacy[step_key][key] == before[step_key][key]
            assert legacy[step_key]["definition"] != before[step_key]["definition"]
            for statement in new_statements:
                if statement.strip():
                    await owner.exec_driver_sql(statement.strip())
            assert await _function_metadata(owner) == before
        finally:
            await transaction.rollback()


@pytest.mark.parametrize(
    "field,sql_type,value,error_type",
    (
        ("due_date", "date", "10000-01-01", ValueError),
        ("created_at", "timestamptz", "10000-01-01 00:00:00+00", OverflowError),
    ),
)
async def test_full_project_decode_precedes_the_changed_exclusive_upgrade(
    test_engine, field, sql_type, value, error_type
):
    maker, world = await _seed(test_engine)
    original = None
    changed = False
    try:
        before = await _snapshot(maker, world["project_id"])
        async with test_engine.begin() as owner:
            original = await owner.scalar(
                text(f"SELECT {field}::text FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
            await owner.execute(
                text(
                    f"UPDATE public.projects SET {field}=CAST(CAST(:value AS text) AS {sql_type}) WHERE id=:id"
                ),
                {"id": world["project_id"], "value": value},
            )
        changed = True
        async with test_engine.connect() as holder:
            async with holder.begin():
                await holder.execute(
                    text("SELECT id FROM public.projects WHERE id=:id FOR KEY SHARE"),
                    {"id": world["project_id"]},
                )
                # Initial KEY SHARE is compatible; the later exclusive upgrade
                # would fail409. Full-row driver decoding must fail first.
                manager = ConfigurationManager(
                    ConfigurationPostgresData(_configuration_dsn(test_engine))
                )
                with pytest.raises(error_type):
                    await manager.execute(_request(world))
        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    f"UPDATE public.projects SET {field}=CAST(CAST(:value AS text) AS {sql_type}) WHERE id=:id"
                ),
                {"id": world["project_id"], "value": original},
            )
        changed = False
        assert await _snapshot(maker, world["project_id"]) == before
    finally:
        if changed:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(
                        f"UPDATE public.projects SET {field}=CAST(CAST(:value AS text) AS {sql_type}) WHERE id=:id"
                    ),
                    {"id": world["project_id"], "value": original},
                )
        await _cleanup(maker, world)


async def test_full_member_decode_precedes_the_nonmanager_rejection(test_engine):
    maker, world = await _seed(test_engine)
    try:
        before = await _snapshot(maker, world["project_id"])
        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.project_members SET assigned_at=TIMESTAMPTZ '10000-01-01 00:00:00+00' WHERE project_id=:project AND user_id=:actor"
                ),
                {"project": world["project_id"], "actor": world["other_id"]},
            )
        manager = ConfigurationManager(
            ConfigurationPostgresData(_configuration_dsn(test_engine))
        )
        with pytest.raises(OverflowError):
            await manager.execute(_request(world, actor=world["other_id"]))
        assert await _snapshot(maker, world["project_id"]) == before
    finally:
        await _cleanup(maker, world)
