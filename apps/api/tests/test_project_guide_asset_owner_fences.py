"""Real PostgreSQL interleavings for the Workspace guide-asset owner.

Controlled callbacks pause around SQL fences; they do not qualify object storage.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.configuration import (
    ConfigurationManager,
    ConfigurationPostCommitFailure,
)
from aap_workspace.configuration_contract import configuration_request
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from aap_workspace.guide_asset_policy import MAX_GUIDE_ASSET_SIZE_BYTES
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.workspace_configuration_helpers import configuration_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


class _ControlledGuideObjects:
    def __init__(
        self,
        *,
        blocked_method: str,
        result=None,
        error: Exception | None = None,
    ):
        self.blocked_method = blocked_method
        self.result = result
        self.error = error
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, uuid.UUID, str]] = []

    async def _call(self, method: str, project_id: uuid.UUID, key: str, default):
        self.calls.append((method, project_id, key))
        if method != self.blocked_method:
            return default
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=20)
        if self.error is not None:
            raise self.error
        return self.result

    async def generate_upload_url(self, project_id, key, *, content_type):
        assert content_type == "image/png"
        return await self._call(
            "generate_upload_url", project_id, key, "https://objects.example.test/put"
        )

    async def verify_upload(self, project_id, key):
        return await self._call("verify_upload", project_id, key, 128)

    async def delete_object(self, project_id, key):
        await self._call("delete_object", project_id, key, None)

    async def generate_download_url(self, project_id, key):
        return await self._call(
            "generate_download_url",
            project_id,
            key,
            "https://objects.example.test/get",
        )


async def _seed(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = {"actor_ids": [], "project_ids": []}
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"guide-fence-owner-{uuid.uuid4().hex}@example.test",
                "Guide fence owner",
            )
            member = await create_user(
                db,
                "employee",
                f"guide-fence-member-{uuid.uuid4().hex}@example.test",
                "Guide fence member",
            )
            world["actor_ids"].extend((owner.id, member.id))
            project = await create_project(db, owner_id=owner.id)
            world["project_ids"].append(project.id)
            key = f"projects/{project.id}/guide/{uuid.uuid4()}-registered.png"
            project.guide_assets = [
                {
                    "key": key,
                    "original_name": "registered.png",
                    "content_type": "image/png",
                    "size": 128,
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


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


def _manager(test_engine, objects):
    return ConfigurationManager(
        ConfigurationPostgresData(configuration_dsn(test_engine), command_timeout=15),
        guide_objects=objects,
    )


def _request(operation, actor_id, project_id, payload):
    return configuration_request(operation, actor_id, project_id, payload)


async def _project_state(maker, project_id):
    async with maker() as db:
        row = (
            (
                await db.execute(
                    text(
                        """SELECT id::text AS id, name, owner_id::text AS owner_id,
                              tool_bindings::text AS tool_bindings,
                              guide_assets::text AS guide_assets,
                              due_date::text AS due_date,
                              updated_at::text AS updated_at
                       FROM public.projects WHERE id=:project_id"""
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _operation_counts(maker, operation_ids):
    counts = {}
    async with maker() as db:
        for suffix in ("states", "attempts", "receipts"):
            counts[suffix] = await db.scalar(
                text(
                    f"SELECT count(*) FROM public.workspace_configuration_operation_{suffix} "
                    "WHERE operation_id=ANY(:ids)"
                ),
                {"ids": list(operation_ids)},
            )
    return counts


async def _assert_nowait_conflict(test_engine, statement, *args):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=5
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        try:
            await connection.fetchval(statement, *args)
        except asyncpg.PostgresError as error:
            assert error.sqlstate == "55P03"
        else:
            raise AssertionError(f"Expected a held row lock for {statement}")
    finally:
        await transaction.rollback()
        await connection.close()


async def _assert_waiting_on_database_lock(test_engine, application_name):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=5
    )
    try:
        for _attempt in range(100):
            wait_event_type = await connection.fetchval(
                "SELECT wait_event_type FROM pg_catalog.pg_stat_activity "
                "WHERE application_name=$1",
                application_name,
            )
            if wait_event_type == "Lock":
                return
            await asyncio.sleep(0.025)
        raise AssertionError(
            f"Connection {application_name!r} never waited on a database lock"
        )
    finally:
        await connection.close()


async def _settle(task, objects=None):
    if objects is not None:
        objects.release.set()
    if task is None:
        return
    done, _pending = await asyncio.wait({task}, timeout=15)
    if not done:
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=5)
    if done:
        await asyncio.gather(task, return_exceptions=True)


async def test_complete_rechecks_revocation_after_head_before_oversize_cleanup(
    test_engine,
):
    maker, world = await _seed(test_engine)
    objects = _ControlledGuideObjects(
        blocked_method="verify_upload", result=MAX_GUIDE_ASSET_SIZE_BYTES + 1
    )
    manager = _manager(test_engine, objects)
    key = f"projects/{world['project_id']}/guide/{uuid.uuid4()}-oversized.png"
    request = _request(
        "guide_asset_upload_complete",
        world["owner_id"],
        world["project_id"],
        {"key": key, "original_name": "oversized.png", "content_type": "image/png"},
    )
    task = None
    operation_id = request.operation_id
    before = await _project_state(maker, world["project_id"])
    try:
        task = asyncio.create_task(manager.execute(request))
        await asyncio.wait_for(objects.entered.wait(), timeout=10)

        # The pre-HEAD reads are deliberately unlocked: revocation can commit here.
        async def disable_actor():
            async with maker.begin() as db:
                result = await db.execute(
                    text("UPDATE public.users SET is_active=false WHERE id=:actor_id"),
                    {"actor_id": world["owner_id"]},
                )
                assert result.rowcount == 1

        await asyncio.wait_for(disable_actor(), timeout=5)

        objects.release.set()
        reply = await asyncio.wait_for(task, timeout=15)
        assert reply.ok is False
        assert reply.error.status_code == 401
        assert [call[0] for call in objects.calls] == ["verify_upload"]
        assert await _project_state(maker, world["project_id"]) == before
        assert await _operation_counts(maker, [operation_id]) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
    finally:
        await _settle(task, objects)
        await _cleanup(maker, world)


async def test_member_sign_holds_authority_locks_until_sign_finishes_then_delete_wins(
    test_engine,
):
    maker, world = await _seed(test_engine)
    objects = _ControlledGuideObjects(
        blocked_method="generate_download_url",
        result="https://objects.example.test/registered",
    )
    manager = _manager(test_engine, objects)
    request = _request(
        "guide_asset_sign",
        world["member_id"],
        world["project_id"],
        {"key": world["guide_key"]},
    )
    task = None
    delete_task = None
    delete_application = f"guide-fence-delete-{uuid.uuid4().hex}"

    async def delete_member(started):
        connection = await asyncpg.connect(
            _owner_dsn(test_engine),
            timeout=5,
            command_timeout=10,
            server_settings={"application_name": delete_application},
        )
        try:
            async with connection.transaction():
                started.set()
                await connection.execute(
                    "DELETE FROM public.project_members "
                    "WHERE project_id=$1::uuid AND user_id=$2::uuid",
                    world["project_id"],
                    world["member_id"],
                )
        finally:
            await connection.close()

    try:
        task = asyncio.create_task(manager.execute(request))
        await asyncio.wait_for(objects.entered.wait(), timeout=10)
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

        started = asyncio.Event()
        delete_task = asyncio.create_task(delete_member(started))
        await asyncio.wait_for(started.wait(), timeout=5)
        await _assert_waiting_on_database_lock(test_engine, delete_application)

        objects.release.set()
        reply = await asyncio.wait_for(task, timeout=15)
        assert reply.ok is True
        assert reply.result.refreshed is False
        assert json.loads(reply.result.data_json) == {
            "url": "https://objects.example.test/registered",
            "expires_in": 3600,
        }
        await asyncio.wait_for(delete_task, timeout=10)

        after_delete = await manager.execute(
            _request(
                "guide_asset_sign",
                world["member_id"],
                world["project_id"],
                {"key": world["guide_key"]},
            )
        )
        assert after_delete.ok is False
        assert after_delete.error.status_code == 404
    finally:
        await _settle(task, objects)
        if delete_task is not None and not delete_task.done():
            await _settle(delete_task)
        await _cleanup(maker, world)


async def test_init_holds_account_and_project_fences_but_rolls_back_without_mutation(
    test_engine,
):
    maker, world = await _seed(test_engine)
    objects = _ControlledGuideObjects(
        blocked_method="generate_upload_url",
        result="https://objects.example.test/upload",
    )
    manager = _manager(test_engine, objects)
    request = _request(
        "guide_asset_upload_init",
        world["owner_id"],
        world["project_id"],
        {"filename": "new.png", "content_type": "image/png", "size": 128},
    )
    task = None
    before = await _project_state(maker, world["project_id"])
    try:
        task = asyncio.create_task(manager.execute(request))
        await asyncio.wait_for(objects.entered.wait(), timeout=10)
        await _assert_nowait_conflict(
            test_engine,
            "SELECT id FROM public.users WHERE id=$1::uuid FOR UPDATE NOWAIT",
            world["owner_id"],
        )
        await _assert_nowait_conflict(
            test_engine,
            "SELECT id FROM public.projects WHERE id=$1::uuid FOR UPDATE NOWAIT",
            world["project_id"],
        )
        objects.release.set()
        reply = await asyncio.wait_for(task, timeout=15)
        assert reply.ok is True
        assert reply.result.refreshed is False
        assert json.loads(reply.result.data_json)["upload_url"] == (
            "https://objects.example.test/upload"
        )
        assert await _project_state(maker, world["project_id"]) == before
        assert await _operation_counts(maker, [request.operation_id]) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
    finally:
        await _settle(task, objects)
        await _cleanup(maker, world)


async def test_delete_keeps_write_fences_through_best_effort_object_failure(
    test_engine,
):
    maker, world = await _seed(test_engine)
    objects = _ControlledGuideObjects(
        blocked_method="delete_object",
        error=RuntimeError("simulated object-store delete failure"),
    )
    manager = _manager(test_engine, objects)
    request = _request(
        "guide_asset_delete",
        world["owner_id"],
        world["project_id"],
        {"key": world["guide_key"]},
    )
    task = None
    try:
        task = asyncio.create_task(manager.execute(request))
        await asyncio.wait_for(objects.entered.wait(), timeout=10)
        await _assert_nowait_conflict(
            test_engine,
            "SELECT id FROM public.users WHERE id=$1::uuid FOR UPDATE NOWAIT",
            world["owner_id"],
        )
        await _assert_nowait_conflict(
            test_engine,
            "SELECT id FROM public.projects WHERE id=$1::uuid FOR UPDATE NOWAIT",
            world["project_id"],
        )
        objects.release.set()
        reply = await asyncio.wait_for(task, timeout=15)
        assert reply.ok is True
        assert reply.result.refreshed is True
        assert json.loads(reply.result.data_json) == {"deleted": world["guide_key"]}
        assert [call[0] for call in objects.calls] == ["delete_object"]
        after = await _project_state(maker, world["project_id"])
        assert json.loads(after["guide_assets"]) == []
        assert await _operation_counts(maker, [request.operation_id]) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
    finally:
        await _settle(task, objects)
        await _cleanup(maker, world)


async def test_complete_commits_before_failing_postcommit_full_row_refresh(test_engine):
    maker, world = await _seed(test_engine)
    objects = _ControlledGuideObjects(blocked_method="unused")
    manager = _manager(test_engine, objects)
    key = f"projects/{world['project_id']}/guide/{uuid.uuid4()}-late-refresh.png"
    request = _request(
        "guide_asset_upload_complete",
        world["owner_id"],
        world["project_id"],
        {
            "key": key,
            "original_name": "late-refresh.png",
            "content_type": "image/png",
        },
    )
    suffix = uuid.uuid4().hex
    trigger_name = f"test_guide_bad_due_{suffix}"
    function_name = f"test_guide_bad_due_{suffix}"
    owner_connection = None
    try:
        owner_connection = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=10
        )
        await owner_connection.execute(
            f"""CREATE FUNCTION public.{function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $body$
                BEGIN
                    IF NEW.guide_assets IS DISTINCT FROM OLD.guide_assets THEN
                        NEW.due_date := DATE '10000-01-01';
                    END IF;
                    RETURN NEW;
                END
                $body$"""
        )
        await owner_connection.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE UPDATE ON public.projects "
            f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
        )

        with pytest.raises(ConfigurationPostCommitFailure):
            await manager.execute(request)

        row = await owner_connection.fetchrow(
            "SELECT guide_assets::text AS guide_assets, due_date::text AS due_date "
            "FROM public.projects WHERE id=$1::uuid",
            world["project_id"],
        )
        assets = json.loads(row["guide_assets"])
        assert any(entry["key"] == key for entry in assets)
        assert row["due_date"] == "10000-01-01"
        assert await _operation_counts(maker, [request.operation_id]) == {
            "states": 0,
            "attempts": 0,
            "receipts": 0,
        }
    finally:
        try:
            if owner_connection is not None:
                await owner_connection.execute(
                    "UPDATE public.projects SET due_date=NULL WHERE id=$1::uuid",
                    world["project_id"],
                )
                await owner_connection.execute(
                    f"DROP TRIGGER IF EXISTS {trigger_name} ON public.projects"
                )
                await owner_connection.execute(
                    f"DROP FUNCTION IF EXISTS public.{function_name}()"
                )
        finally:
            if owner_connection is not None:
                await owner_connection.close()
            await _cleanup(maker, world)
