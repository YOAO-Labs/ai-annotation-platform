"""Caller-affine unlocked access against owned Workspace and Identity facts."""

from __future__ import annotations

from contextlib import nullcontext
import asyncio
import json
import os
from pathlib import Path
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import event, text

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.project_access import resolve_project_access_by_id
from tests.test_workspace_project_authority_locking import (
    _assert_private_denied,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier and actual API LOGIN",
    ),
]


async def test_read_only_api_login_uses_same_transaction_without_authority_locks(
    test_engine,
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            actions = []

            def capture(_conn, _cursor, statement, parameters, _context, _many):
                if "aap_identity_v1.read_actor_facts" in statement:
                    actions.append("actor_status")
                elif "project_query_policy()" in statement:
                    actions.append("policy")
                elif (
                    "project_access_step(" in statement
                    or "project_query_step(" in statement
                ):
                    actions.extend(
                        value
                        for value in parameters
                        if value
                        in ("read_project_row", "get_row", "access_member_rows")
                    )

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            try:
                project, access = await resolve_project_access_by_id(
                    db, actor_id=world["member_id"], project_id=world["project_id"]
                )
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            assert actions == [
                "read_project_row",
                "actor_status",
                "policy",
                "get_row",
                "access_member_rows",
            ]
            assert project is await db.get(Project, world["project_id"])
            assert (access.membership_id, access.project_role) == (
                world["membership_id"],
                "annotator",
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            async with owner() as contender:
                for table, identity in (
                    ("users", world["member_id"]),
                    ("projects", world["project_id"]),
                    ("project_members", world["membership_id"]),
                ):
                    assert (
                        await contender.scalar(
                            text(
                                f"SELECT id FROM public.{table} WHERE id=:id FOR UPDATE NOWAIT"
                            ),
                            {"id": identity},
                        )
                        == identity
                    )


@pytest.mark.parametrize("project_exists", [False, True])
async def test_unlocked_missing_project_precedes_disabled_account(
    test_engine, monkeypatch, project_exists
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as change:
            await change.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": world["member_id"]},
            )
        calls = []
        original = IdentityAccountLockData.read_actor_status

        async def status(self, actor_id):
            calls.append(actor_id)
            return await original(self, actor_id)

        monkeypatch.setattr(IdentityAccountLockData, "read_actor_status", status)
        async with api() as db:
            with pytest.raises(HTTPException) as caught:
                await resolve_project_access_by_id(
                    db,
                    actor_id=world["member_id"],
                    project_id=world["project_id"] if project_exists else uuid.uuid4(),
                )
        assert caught.value.status_code == (401 if project_exists else 404)
        assert calls == ([world["member_id"]] if project_exists else [])
        if project_exists:
            assert caught.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("change", ["refresh", "inactive_and_missing"])
async def test_unlocked_reads_committed_changes_between_project_and_account(
    test_engine, monkeypatch, change
):
    async with _world(test_engine) as (owner, api, world, _url):
        original = IdentityAccountLockData.read_actor_status

        async def status(self, actor_id):
            if change == "refresh":
                actor = await original(self, actor_id)
            async with owner.begin() as writer:
                if change == "refresh":
                    await writer.execute(
                        text(
                            "UPDATE public.projects SET name='changed after actor',status='completed' WHERE id=:id"
                        ),
                        {"id": world["project_id"]},
                    )
                    await writer.execute(
                        text(
                            "UPDATE public.project_members SET role='reviewer',version=2,weekly_target=23 WHERE id=:id"
                        ),
                        {"id": world["membership_id"]},
                    )
                else:
                    await writer.execute(
                        text("UPDATE public.users SET is_active=false WHERE id=:id"),
                        {"id": actor_id},
                    )
                    await writer.execute(
                        text("DELETE FROM public.projects WHERE id=:id"),
                        {"id": world["project_id"]},
                    )
            return actor if change == "refresh" else await original(self, actor_id)

        monkeypatch.setattr(IdentityAccountLockData, "read_actor_status", status)
        async with api() as db:
            if change == "inactive_and_missing":
                with pytest.raises(HTTPException) as caught:
                    await resolve_project_access_by_id(
                        db, actor_id=world["member_id"], project_id=world["project_id"]
                    )
                assert caught.value.status_code == 401
            else:
                project, access = await resolve_project_access_by_id(
                    db, actor_id=world["member_id"], project_id=world["project_id"]
                )
                assert (project.name, project.status) == (
                    "changed after actor",
                    "completed",
                )
                assert (access.project_role, access.membership_version) == (
                    "reviewer",
                    2,
                )
                assert project is await db.get(Project, world["project_id"])


async def test_initial_unlocked_temporal_error_precedes_identity(
    test_engine, monkeypatch
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as change:
            await change.execute(
                text(
                    "UPDATE public.projects SET due_date=DATE '12000-01-01' WHERE id=:id"
                ),
                {"id": world["project_id"]},
            )

        async def unexpected_actor(*_args):
            pytest.fail("initial Project materialization must precede Identity")

        monkeypatch.setattr(
            IdentityAccountLockData, "read_actor_status", unexpected_actor
        )
        async with api() as db:
            with pytest.raises((ValueError, OverflowError)):
                await resolve_project_access_by_id(
                    db, actor_id=world["member_id"], project_id=world["project_id"]
                )
            assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("phase", ["initial", "query"])
async def test_unlocked_bad_scope_rolls_back_callers_prior_write(test_engine, phase):
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
                if phase == "initial" and "project_access_step(" in str(statement):
                    raw = json.loads(result) if isinstance(result, str) else result
                    return {**raw, "project_id": str(uuid.uuid4())}
                if phase == "query" and "project_query_step(" in str(statement):
                    raw = json.loads(result) if isinstance(result, str) else result
                    return {
                        **raw,
                        "request": {**raw["request"], "target_id": str(uuid.uuid4())},
                    }
                return result

        with pytest.raises(
            ValueError if phase == "initial" else HTTPException
        ) as caught:
            async with owner.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='uncommitted unlocked probe' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await resolve_project_access_by_id(
                    WrongScope(db),
                    actor_id=world["member_id"],
                    project_id=world["project_id"],
                )
        if phase == "query":
            assert caught.value.status_code == 503
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == original
            )


async def test_node_reads_unlocked_facts_directly_in_read_only_transaction(test_engine):
    async with _world(test_engine) as (_owner, _api, world, url):
        process = await asyncio.create_subprocess_exec(
            "node",
            str(
                Path(__file__).parent
                / "fixtures/workspace_project_access_unlocked_peer.mjs"
            ),
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
                            "membership_id": str(world["membership_id"]),
                        }
                    ).encode()
                ),
                timeout=20,
            )
            assert process.returncode == 0, err.decode()
            assert json.loads(out) == {
                "ok": True,
                "transaction_unchanged": True,
                "read_only": True,
            }
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize("autoflush", [True, False])
async def test_unlocked_snapshots_preserve_caller_autoflush_and_cache(
    test_engine, autoflush
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            member = await db.get(ProjectMember, world["membership_id"])
            fresh_tools = {"bbox": {"classes": ["fresh owner configuration"]}}
            local_tools = {"bbox": {"classes": ["pending local configuration"]}}
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE public.projects SET name='fresh unlocked snapshot', "
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
            project.name = "pending project edit"
            project.tool_bindings = local_tools
            member.role = "viewer"
            pending = Task(
                id=uuid.uuid4(),
                project_id=world["project_id"],
                display_id=f"T-UNL-{uuid.uuid4().hex[:12]}",
                file_name="pending.png",
                file_path=f"unlocked/{uuid.uuid4().hex}.png",
                file_type="image",
                status="pending",
            )
            db.add(pending)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            with nullcontext() if autoflush else db.no_autoflush:
                actual, access = await resolve_project_access_by_id(
                    db, actor_id=world["member_id"], project_id=world["project_id"]
                )
            assert actual is project
            assert (project.name, project.status, project.tool_bindings) == (
                "pending project edit" if autoflush else "fresh unlocked snapshot",
                "completed",
                local_tools if autoflush else fresh_tools,
            )
            assert (member.role, member.version, member.weekly_target) == (
                "viewer" if autoflush else "reviewer",
                2,
                23,
            )
            assert (access.project_role, access.membership_version) == (member.role, 2)
            assert project not in db.dirty and member not in db.dirty
            assert (pending in db.new) is not autoflush
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.tasks WHERE id=:id"),
                    {"id": pending.id},
                )
            ) == int(autoflush)
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            async with owner() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM public.tasks WHERE id=:id"),
                        {"id": pending.id},
                    )
                ) == 0
            await db.rollback()
