"""Owned-role tests for the fixed Workspace Project access lock phases."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace_contract.member_values import decode_project_member_rows
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

_FUNCTION = "aap_workspace_v1.project_access_step(uuid,uuid,text)"
_BUSY_HINTS = {
    "lock_actor_share": "aap.workspace.project_access.account_busy",
    "lock_project_key_share": "aap.workspace.project_access.project_busy",
    "lock_member_share": "aap.workspace.project_access.membership_busy",
}


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _seed(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = {"actor_ids": [], "project_ids": []}
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"access-step-owner-{uuid.uuid4().hex}@example.test",
                "Access step owner",
            )
            member = await create_user(
                db,
                "employee",
                f"access-step-member-{uuid.uuid4().hex}@example.test",
                "Access step member",
            )
            world["actor_ids"].extend((owner.id, member.id))
            project = await create_project(db, owner_id=owner.id)
            world["project_ids"].append(project.id)
            membership = await create_membership(
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
                membership_id=membership.id,
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


async def _connect_inference(test_engine):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    await connection.execute("SET ROLE inference_code")
    assert await connection.fetchval("SELECT current_user") == "inference_code"
    return connection


async def _step(connection, actor_id, project_id, phase):
    raw = await connection.fetchval(
        "SELECT aap_workspace_v1.project_access_step($1::uuid,$2::uuid,$3::text)",
        actor_id,
        project_id,
        phase,
    )
    reply = json.loads(raw) if isinstance(raw, str) else raw
    assert set(reply) == {"schema", "actor_id", "project_id", "phase", "facts"}
    assert reply["schema"] == "aap-workspace-project-access-step/v1"
    assert reply["actor_id"] == str(actor_id)
    assert reply["project_id"] == str(project_id)
    assert reply["phase"] == phase
    return reply["facts"]


async def _assert_nowait_conflict(test_engine, sql, *args):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=5
    )
    transaction = None
    try:
        transaction = connection.transaction()
        await transaction.start()
        with pytest.raises(asyncpg.PostgresError) as caught:
            await connection.fetchval(sql, *args)
        assert caught.value.sqlstate == "55P03"
    finally:
        try:
            if transaction is not None:
                await transaction.rollback()
        finally:
            await connection.close()


async def _assert_busy_phase(test_engine, world, phase, lock_sql, *lock_args):
    blocker = None
    inference = None
    transaction = None
    try:
        blocker = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=10
        )
        await blocker.execute("BEGIN")
        await blocker.fetchval(lock_sql, *lock_args)
        inference = await _connect_inference(test_engine)
        transaction = inference.transaction()
        await transaction.start()
        if phase in {"lock_project_key_share", "lock_member_share"}:
            actor = await _step(
                inference, world["member_id"], world["project_id"], "lock_actor_share"
            )
            assert actor["actor"]["is_active"] is True
        if phase == "lock_member_share":
            project = await _step(
                inference,
                world["member_id"],
                world["project_id"],
                "lock_project_key_share",
            )
            assert project["row_json"] is not None

        with pytest.raises(asyncpg.PostgresError) as caught:
            await _step(inference, world["member_id"], world["project_id"], phase)
        assert caught.value.sqlstate in {"55P03", "40001", "40P01"}
        assert caught.value.hint == _BUSY_HINTS[phase]
    finally:
        try:
            if transaction is not None:
                await transaction.rollback()
        finally:
            try:
                if inference is not None:
                    await inference.close()
            finally:
                if blocker is not None:
                    try:
                        await blocker.execute("ROLLBACK")
                    finally:
                        await blocker.close()


async def test_inference_role_gets_only_bound_phase_facts_and_no_raw_reads(test_engine):
    maker, world = await _seed(test_engine)
    connection = None
    metadata = None
    try:
        metadata = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=5
        )
        acl = await metadata.fetchrow(
            """SELECT pg_catalog.has_function_privilege(
                          'inference_code', function.oid, 'EXECUTE'
                      ) AS inference_execute,
                      EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(COALESCE(
                              function.proacl,
                              pg_catalog.acldefault('f', function.proowner)
                          )) AS privilege
                          WHERE privilege.grantee = 0
                            AND privilege.privilege_type = 'EXECUTE'
                      ) AS public_execute
               FROM pg_catalog.pg_proc AS function
               WHERE function.oid = pg_catalog.to_regprocedure($1)""",
            _FUNCTION,
        )
        assert acl["inference_execute"] is True
        assert acl["public_execute"] is False

        connection = await _connect_inference(test_engine)
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
            (
                "SELECT * FROM aap_labeling_v1.workspace_project_query_counters("
                "$1::uuid[])",
                ([world["project_id"]],),
            ),
        )
        for statement, parameters in raw_reads:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch(statement, *parameters)

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
                "is_active": True,
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
            assert len(json.loads(project["row_json"])) == 43

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
            assert len(decoded_members) == 1
            assert decoded_members[0].id == world["membership_id"]
            assert decoded_members[0].role == "annotator"

        missing_actor = await _step(
            connection, uuid.uuid4(), world["project_id"], "lock_actor_share"
        )
        assert missing_actor == {"actor": None}

        missing_project = await _step(
            connection,
            world["owner_id"],
            uuid.uuid4(),
            "lock_project_key_share",
        )
        assert missing_project == {"row_json": None}

        missing_member = await _step(
            connection,
            world["owner_id"],
            world["project_id"],
            "lock_member_share",
        )
        assert json.loads(missing_member["rows_json"]) == []

        with pytest.raises(asyncpg.PostgresError) as caught:
            await connection.fetchval(
                "SELECT aap_workspace_v1.project_access_step(NULL::uuid,$1::uuid,$2::text)",
                world["project_id"],
                "lock_actor_share",
            )
        assert caught.value.sqlstate == "22023"
        with pytest.raises(asyncpg.PostgresError) as caught:
            await connection.fetchval(
                "SELECT aap_workspace_v1.project_access_step($1::uuid,$2::uuid,$3::text)",
                world["member_id"],
                world["project_id"],
                "lock_project_update",
            )
        assert caught.value.sqlstate == "22023"
    finally:
        try:
            if connection is not None:
                await connection.close()
        finally:
            try:
                if metadata is not None:
                    await metadata.close()
            finally:
                await _cleanup(maker, world)


async def test_project_access_lock_modes_busy_markers_and_postrelease_revocation(
    test_engine,
):
    maker, world = await _seed(test_engine)
    connection = None
    owner_connection = None
    try:
        connection = await _connect_inference(test_engine)
        owner_connection = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=10
        )
        async with connection.transaction():
            await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_actor_share",
            )
            await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_project_key_share",
            )
            await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_member_share",
            )

            await _assert_nowait_conflict(
                test_engine,
                "SELECT id FROM public.users WHERE id=$1::uuid FOR UPDATE NOWAIT",
                world["member_id"],
            )
            await _assert_nowait_conflict(
                test_engine,
                "SELECT id FROM public.projects WHERE id=$1::uuid FOR UPDATE NOWAIT",
                world["project_id"],
            )
            await _assert_nowait_conflict(
                test_engine,
                "SELECT id FROM public.project_members "
                "WHERE project_id=$1::uuid AND user_id=$2::uuid FOR UPDATE NOWAIT",
                world["project_id"],
                world["member_id"],
            )

            no_key_update = owner_connection.transaction()
            await no_key_update.start()
            try:
                await owner_connection.fetchval(
                    "SELECT id FROM public.projects WHERE id=$1::uuid "
                    "FOR NO KEY UPDATE NOWAIT",
                    world["project_id"],
                )
            finally:
                await no_key_update.rollback()

        await _assert_busy_phase(
            test_engine,
            world,
            "lock_actor_share",
            "SELECT id FROM public.users WHERE id=$1::uuid FOR UPDATE NOWAIT",
            world["member_id"],
        )
        await _assert_busy_phase(
            test_engine,
            world,
            "lock_project_key_share",
            "SELECT id FROM public.projects WHERE id=$1::uuid FOR UPDATE NOWAIT",
            world["project_id"],
        )
        await _assert_busy_phase(
            test_engine,
            world,
            "lock_member_share",
            "SELECT id FROM public.project_members "
            "WHERE project_id=$1::uuid AND user_id=$2::uuid FOR UPDATE NOWAIT",
            world["project_id"],
            world["member_id"],
        )

        await owner_connection.execute(
            "UPDATE public.project_members SET role='reviewer' WHERE id=$1::uuid",
            world["membership_id"],
        )
        async with connection.transaction():
            actor = await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_actor_share",
            )
            await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_project_key_share",
            )
            member = await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_member_share",
            )
            assert actor["actor"]["is_active"] is True
            decoded_members = decode_project_member_rows(
                member["rows_json"],
                target_id=world["project_id"],
                user_id=world["member_id"],
                max_rows=1,
            )
            assert decoded_members[0].role == "reviewer"

        await owner_connection.execute(
            "UPDATE public.users SET is_active=false WHERE id=$1::uuid",
            world["member_id"],
        )
        async with connection.transaction():
            inactive = await _step(
                connection,
                world["member_id"],
                world["project_id"],
                "lock_actor_share",
            )
            assert inactive["actor"]["is_active"] is False
    finally:
        try:
            if connection is not None:
                await connection.close()
        finally:
            try:
                if owner_connection is not None:
                    await owner_connection.close()
            finally:
                await _cleanup(maker, world)


async def test_full_project_and_member_datetime_decode_fail_before_next_phase(
    test_engine,
):
    maker, world = await _seed(test_engine)
    owner_connection = None
    inference = None
    try:
        owner_connection = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=10
        )
        inference = await _connect_inference(test_engine)
        await owner_connection.execute(
            "UPDATE public.projects SET due_date=DATE '10000-01-01' WHERE id=$1::uuid",
            world["project_id"],
        )
        async with inference.transaction():
            await _step(
                inference,
                world["member_id"],
                world["project_id"],
                "lock_actor_share",
            )
            project = await _step(
                inference,
                world["member_id"],
                world["project_id"],
                "lock_project_key_share",
            )
            with pytest.raises(ValueError):
                decode_project_row(project["row_json"], target_id=world["project_id"])

        await owner_connection.execute(
            "UPDATE public.projects SET due_date=NULL WHERE id=$1::uuid",
            world["project_id"],
        )
        await owner_connection.execute(
            "UPDATE public.project_members "
            "SET assigned_at=TIMESTAMPTZ '10000-01-01 00:00:00+00' "
            "WHERE id=$1::uuid",
            world["membership_id"],
        )
        async with inference.transaction():
            await _step(
                inference,
                world["member_id"],
                world["project_id"],
                "lock_actor_share",
            )
            project = await _step(
                inference,
                world["member_id"],
                world["project_id"],
                "lock_project_key_share",
            )
            decode_project_row(project["row_json"], target_id=world["project_id"])
            member = await _step(
                inference,
                world["member_id"],
                world["project_id"],
                "lock_member_share",
            )
            with pytest.raises(OverflowError):
                decode_project_member_rows(
                    member["rows_json"],
                    target_id=world["project_id"],
                    user_id=world["member_id"],
                    max_rows=1,
                )
    finally:
        try:
            if owner_connection is not None:
                try:
                    await owner_connection.execute(
                        "UPDATE public.projects SET due_date=NULL WHERE id=$1::uuid",
                        world["project_id"],
                    )
                finally:
                    await owner_connection.close()
        finally:
            try:
                if inference is not None:
                    await inference.close()
            finally:
                await _cleanup(maker, world)
