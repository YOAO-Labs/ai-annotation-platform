"""Borrowed authority locks on a real API LOGIN without private row reads."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.services.project_access import lock_current_project_access
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_workspace_project_access_step_native import (
    _assert_nowait_conflict,
    _cleanup,
    _seed,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier and actual API LOGIN",
    ),
]
_ROLE = "identity_api_consumer"
_PRIVATE = ("projects", "project_members")
_FUNCTION = "aap_workspace_v1.project_access_step(uuid,uuid,text)"


def _api_url(test_engine):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert url.username == _ROLE
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return url


@asynccontextmanager
async def _world(test_engine, *, retain_audited=False):
    owner, world = await _seed(test_engine)
    url = _api_url(test_engine)
    engine = create_async_engine(
        url, hide_parameters=True, connect_args={"timeout": 5, "command_timeout": 10}
    )
    before = None
    try:
        async with test_engine.begin() as db:
            before = (
                await db.execute(
                    text(
                        "SELECT relname,relacl::text FROM pg_class "
                        "WHERE oid IN ('public.projects'::regclass,'public.project_members'::regclass) "
                        "ORDER BY relname"
                    )
                )
            ).all()
            for role, expected in (
                (_ROLE, True),
                ("legacy_runtime", True),
                ("append_client", False),
            ):
                assert (
                    await db.scalar(
                        text(
                            "SELECT has_function_privilege(:role,:function,'EXECUTE')"
                        ),
                        {"role": role, "function": _FUNCTION},
                    )
                ) is expected
            for table in _PRIVATE:
                assert not await db.scalar(
                    text("SELECT has_any_column_privilege(:role,:table,'SELECT')"),
                    {"role": _ROLE, "table": "public." + table},
                )
        yield owner, async_sessionmaker(engine, expire_on_commit=False), world, url
    finally:
        await engine.dispose()
        try:
            if before is not None:
                async with test_engine.begin() as db:
                    after = (
                        await db.execute(
                            text(
                                "SELECT relname,relacl::text FROM pg_class "
                                "WHERE oid IN ('public.projects'::regclass,'public.project_members'::regclass) "
                                "ORDER BY relname"
                            )
                        )
                    ).all()
                    assert after == before
        finally:
            # Audit rows stay immutable; audited fixtures leave with the owned database.
            if not retain_audited:
                await _cleanup(owner, world)


async def _assert_private_denied(db):
    assert await db.scalar(text("SELECT current_user")) == _ROLE
    for table in ("users", *_PRIVATE):
        assert not await db.scalar(
            text("SELECT has_any_column_privilege(current_user,:table,'SELECT')"),
            {"table": "public." + table},
        )
        with pytest.raises(DBAPIError) as denied:
            async with db.begin_nested():
                await db.execute(text(f"SELECT id FROM public.{table} LIMIT 1"))
        assert denied.value.orig.sqlstate == "42501"


def _capture_steps(engine):
    steps = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "aap_identity_v1.lock_accounts_share" in statement:
            steps.append("actor")
        elif "aap_workspace_v1.project_access_step" in statement:
            steps.extend(
                value
                for value in parameters
                if value in ("lock_project_key_share", "lock_member_share")
            )

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    return steps, capture


async def test_api_login_holds_current_authority_without_private_row_reads(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            steps, listener = _capture_steps(db.bind)
            try:
                actor, project, access = await lock_current_project_access(
                    db, actor_id=world["member_id"], project_id=world["project_id"]
                )
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", listener)
            assert steps == ["actor", "lock_project_key_share", "lock_member_share"]
            assert (actor.id, actor.name) == (world["member_id"], "Access step member")
            assert actor.email.startswith("access-step-member-")
            assert (access.project_id, access.membership_id, access.project_role) == (
                world["project_id"],
                world["membership_id"],
                "annotator",
            )
            assert access.membership_version == 1 and access.has("annotation.write")
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            assert project.id == world["project_id"]
            assert project is await db.get(Project, world["project_id"])
            for table, identity in (
                ("users", world["member_id"]),
                ("projects", world["project_id"]),
                ("project_members", world["membership_id"]),
            ):
                await _assert_nowait_conflict(
                    test_engine,
                    f"SELECT id FROM public.{table} WHERE id=$1 FOR UPDATE NOWAIT",
                    identity,
                )
            async with owner() as counter:
                await counter.execute(text("SET LOCAL lock_timeout='1s'"))
                await counter.execute(
                    text(
                        "UPDATE public.projects SET total_tasks=total_tasks+1 WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await counter.rollback()


async def test_manager_skips_membership_phase_under_the_same_api_transaction(
    test_engine,
):
    async with _world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            steps, listener = _capture_steps(db.bind)
            try:
                actor, project, access = await lock_current_project_access(
                    db, actor_id=world["owner_id"], project_id=world["project_id"]
                )
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", listener)
            assert actor.id == world["owner_id"]
            assert access.is_manager and access.membership_id is None
            assert steps == ["actor", "lock_project_key_share"]


async def test_locked_snapshots_refresh_dirty_cached_rows_without_flushing_other_work(
    test_engine,
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as writer:
            cached_project = await writer.get(Project, world["project_id"])
            cached_member = await writer.get(ProjectMember, world["membership_id"])
            fresh_tools = {"bbox": {"classes": ["fresh owner configuration"]}}
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE public.projects SET name='fresh locked snapshot', "
                        "status='completed',tool_bindings=CAST(:tools AS jsonb) WHERE id=:id"
                    ),
                    {"id": world["project_id"], "tools": json.dumps(fresh_tools)},
                )
                await change.execute(
                    text(
                        "UPDATE public.project_members SET role='reviewer',version=2,weekly_target=23 "
                        "WHERE id=:id"
                    ),
                    {"id": world["membership_id"]},
                )
            cached_project.name = "discarded pending project edit"
            cached_project.tool_bindings = {"discarded": True}
            cached_member.role = "viewer"
            pending = Task(
                id=uuid.uuid4(),
                project_id=world["project_id"],
                display_id=f"T-AUTH-{uuid.uuid4().hex[:12]}",
                file_name="pending.png",
                file_path=f"authority/{uuid.uuid4().hex}.png",
                file_type="image",
                status="pending",
            )
            writer.add(pending)
            actor, project, access = await lock_current_project_access(
                writer, actor_id=world["member_id"], project_id=world["project_id"]
            )
            assert actor.id == world["member_id"]
            assert project is cached_project
            assert (project.name, project.status, project.tool_bindings) == (
                "fresh locked snapshot",
                "completed",
                fresh_tools,
            )
            assert (
                cached_member.role,
                cached_member.version,
                cached_member.weekly_target,
            ) == (
                "reviewer",
                2,
                23,
            )
            assert access.project_role == "reviewer" and access.membership_version == 2
            assert (
                cached_project not in writer.dirty and cached_member not in writer.dirty
            )
            assert pending in writer.new
            async with owner() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM public.tasks WHERE id=:id"),
                        {"id": pending.id},
                    )
                    == 0
                )
            await writer.rollback()


@pytest.mark.parametrize(
    ("table", "identity", "reason"),
    [
        ("users", "member_id", "account_busy"),
        ("projects", "project_id", "project_busy"),
        ("project_members", "membership_id", "membership_busy"),
    ],
)
async def test_api_authority_maps_nowait_contention_and_leaves_caller_rollback(
    test_engine, table, identity, reason
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner() as blocker:
            await blocker.execute(
                text(f"SELECT id FROM public.{table} WHERE id=:id FOR UPDATE"),
                {"id": world[identity]},
            )
            async with api() as db:
                with pytest.raises(HTTPException) as busy:
                    await lock_current_project_access(
                        db, actor_id=world["member_id"], project_id=world["project_id"]
                    )
                assert (busy.value.status_code, busy.value.detail) == (
                    409,
                    {"reason": reason},
                )
                with pytest.raises(DBAPIError) as aborted:
                    await db.execute(text("SELECT 1"))
                assert aborted.value.orig.sqlstate == "25P02"
                await db.rollback()


async def test_legacy_project_date_failure_precedes_membership_phase(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.projects SET due_date=DATE '12000-01-01' WHERE id=:id"
                ),
                {"id": world["project_id"]},
            )
        async with api() as db:
            steps, listener = _capture_steps(db.bind)
            try:
                with pytest.raises((ValueError, OverflowError)):
                    await lock_current_project_access(
                        db, actor_id=world["member_id"], project_id=world["project_id"]
                    )
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", listener)
            assert steps == ["actor", "lock_project_key_share"]
            assert await db.scalar(text("SELECT 1")) == 1
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT due_date::text FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == "12000-01-01"
            )


async def test_bad_native_scope_aborts_the_callers_prior_write(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            original = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )

        class WrongScope:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, name):
                return getattr(self.db, name)

            async def scalar(self, statement, *args, **kwargs):
                result = await self.db.scalar(statement, *args, **kwargs)
                if "aap_workspace_v1.project_access_step" in str(statement):
                    result = json.loads(result) if isinstance(result, str) else result
                    return {**result, "project_id": str(uuid.uuid4())}
                return result

            async def execute(self, statement, *args, **kwargs):
                result = await self.db.execute(statement, *args, **kwargs)
                if "aap_workspace_v1.project_access_step" not in str(statement):
                    return result

                class WrongScopeResult:
                    def scalar_one_or_none(self):
                        raw = result.scalar_one_or_none()
                        raw = json.loads(raw) if isinstance(raw, str) else raw
                        return {**raw, "project_id": str(uuid.uuid4())}

                return WrongScopeResult()

        with pytest.raises(ValueError):
            async with owner.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='uncommitted authority probe' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await lock_current_project_access(
                    WrongScope(db),
                    actor_id=world["member_id"],
                    project_id=world["project_id"],
                )
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == original
            )


async def test_node_api_login_uses_owner_capabilities_without_python_proxy(test_engine):
    async with _world(test_engine) as (_owner, _api, world, url):
        peer = Path(__file__).parent / "fixtures/workspace_project_authority_peer.mjs"
        process = await asyncio.create_subprocess_exec(
            "node",
            str(peer),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(
                    json.dumps(
                        {
                            "database_url": url.set(
                                drivername="postgresql"
                            ).render_as_string(hide_password=False),
                            "actor_id": str(world["member_id"]),
                            "project_id": str(world["project_id"]),
                            "owner_id": str(world["owner_id"]),
                            "membership_id": str(world["membership_id"]),
                        }
                    ).encode()
                ),
                timeout=20,
            )
            assert process.returncode == 0, err.decode()
            assert json.loads(out) == {"ok": True, "transaction_unchanged": True}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
