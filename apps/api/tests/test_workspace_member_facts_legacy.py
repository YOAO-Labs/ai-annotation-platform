"""Frozen caller behavior before replacing nonlocking private membership reads."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import event, text

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.services.annotation_comment import AnnotationCommentService
from app.services.notification import NotificationService
from app.services.project_aggregates import member_user_ids
from app.services.project_performance import ResolvedScope, _build_roster
from tests.factory import create_user
from tests.test_workspace_member_locks_native import _state
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]
_BASE = "f109006597df34af5af632439d91698e76e6b5f0"
_GOLDEN = Path(__file__).parent / "fixtures/workspace_member_facts_legacy.json"
_STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SCOPE = ResolvedScope(_STAMP, _STAMP, "UTC", _STAMP)


def _compare(key, observed):
    if os.getenv("AAP_MEMBER_FACTS_CAPTURE") == "1":
        assert (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[3],
                text=True,
            ).strip()
            == _BASE
        )
        document = (
            json.loads(_GOLDEN.read_text())
            if _GOLDEN.exists()
            else {
                "source_commit": _BASE,
                "cases": {},
            }
        )
        assert key not in document["cases"], "never overwrite frozen caller behavior"
        document["cases"][key] = observed
        _GOLDEN.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        assert observed == json.loads(_GOLDEN.read_text())["cases"][key]


def _pending(world):
    identity = uuid.uuid4()
    return Task(
        id=identity,
        project_id=world["project_id"],
        display_id=f"MF-{identity.hex[:20]}",
        file_name="pending.png",
        file_path=f"member-facts/{identity}.png",
        file_type="image",
        status="pending",
    )


async def _roster(db, project):
    return await _build_roster(
        db,
        project,
        include_historical=False,
        account_status="all",
        query=None,
        scope=_SCOPE,
    )


@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize(
    "cache",
    [
        "miss",
        "clean",
        "dirty_role",
        "dirty_target",
        "expired",
        "expired_dirty",
        "stale",
        "stale_partial",
        "deleted_database",
    ],
)
async def test_roster_entity_cache_matches_legacy(test_engine, cache, autoflush):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as seed:
            await seed.execute(
                text(
                    "UPDATE public.project_members SET assigned_at=:stamp,updated_at=:stamp "
                    "WHERE id=:id"
                ),
                {"id": world["membership_id"], "stamp": _STAMP},
            )
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            cached = (
                None
                if cache == "miss"
                else await db.get(ProjectMember, world["membership_id"])
            )
            if cache.startswith("stale"):
                async with owner.begin() as change:
                    await change.execute(
                        text(
                            "UPDATE public.project_members SET role='reviewer',weekly_target=31,version=2 "
                            "WHERE id=:id"
                        ),
                        {"id": world["membership_id"]},
                    )
            elif cache == "deleted_database":
                async with owner.begin() as change:
                    await change.execute(
                        text("DELETE FROM public.project_members WHERE id=:id"),
                        {"id": world["membership_id"]},
                    )
            if cache == "dirty_role":
                cached.role = "viewer"
            elif cache == "dirty_target":
                cached.weekly_target = 23
            elif cache in {"expired", "expired_dirty"}:
                db.expire(cached)
                if cache == "expired_dirty":
                    cached.role = "viewer"
            elif cache == "stale_partial":
                db.expire(cached, ["role", "weekly_target"])
            pending = _pending(world)
            db.add(pending)
            db.autoflush = autoflush
            trace = []
            loaded = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                lowered = statement.lower()
                if "select " in lowered and (
                    "from project_members" in lowered
                    or ".project_member_facts" in lowered
                ):
                    trace.append("member_read")
                elif "update project_members" in lowered:
                    trace.append("member_update")
                elif "insert into tasks" in lowered:
                    trace.append("task_insert")

            def retain(_session, obj):
                if isinstance(obj, ProjectMember):
                    loaded.append(obj)

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            event.listen(db.sync_session, "loaded_as_persistent", retain)
            before = tuple(
                (await db.execute(text("SELECT pg_backend_pid(),txid_current()"))).one()
            )
            try:
                rows = await _roster(db, project)
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
                event.remove(db.sync_session, "loaded_as_persistent", retain)
            assert (
                tuple(
                    (
                        await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
                    ).one()
                )
                == before
            )
            identity = db.sync_session.identity_map.get(
                ProjectMember.__mapper__.identity_key_from_primary_key(
                    (world["membership_id"],)
                )
            )
            _compare(
                f"roster:{cache}:{autoflush}",
                {
                    "members": sorted(
                        [
                            "owner" if row.user.id == world["owner_id"] else "member",
                            row.project_role,
                            row.is_owner,
                            row.is_current_member,
                            row.member_since == _STAMP
                            if row.user.id == world["member_id"]
                            else True,
                        ]
                        for row in rows
                    ),
                    "cached": _state(cached),
                    "loaded": [_state(obj) for obj in loaded],
                    "same_identity": cached is not None and identity is cached,
                    "pending_task": pending in db.new,
                    "trace": trace,
                },
            )
            await db.rollback()
        async with owner() as check:
            assert (
                await check.scalar(
                    text("SELECT count(*) FROM tasks WHERE id=:id"), {"id": pending.id}
                )
                == 0
            )


@pytest.mark.parametrize("autoflush", [False, True])
async def test_standalone_member_ids_preserve_autoflush(test_engine, autoflush):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            pending = _pending(world)
            db.add(pending)
            db.autoflush = autoflush
            result = list(
                (await db.scalars(member_user_ids(world["project_id"]))).all()
            )
            _compare(
                f"member_ids:{autoflush}",
                {
                    "result_is_member": result == [world["member_id"]],
                    "pending_task": pending in db.new,
                },
            )
            await db.rollback()


async def test_delivery_keeps_four_statement_snapshots(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as seed:
            outsider = await create_user(
                seed, "employee", f"mf-{uuid.uuid4().hex}@example.test", "Outsider"
            )
            outsider_id = outsider.id
            world["actor_ids"].append(outsider_id)
        async with owner() as db:
            statements = []

            class BetweenStatements:
                async def execute(self, statement):
                    result = await db.execute(statement)
                    sql = str(statement).lower()
                    statements.append(
                        "member"
                        if "project_members" in sql or "project_member_facts" in sql
                        else "project"
                    )
                    async with owner.begin() as change:
                        if len(statements) == 1:
                            await change.execute(
                                text(
                                    "UPDATE project_members SET role='reviewer' WHERE id=:id"
                                ),
                                {"id": world["membership_id"]},
                            )
                        elif len(statements) == 2:
                            await change.execute(
                                text("UPDATE users SET is_active=false WHERE id=:id"),
                                {"id": world["owner_id"]},
                            )
                        elif len(statements) == 3:
                            await change.execute(
                                text(
                                    "UPDATE users SET role='super_admin' WHERE id=:id"
                                ),
                                {"id": outsider_id},
                            )
                    return result

            ids = [
                world["member_id"],
                world["member_id"],
                world["owner_id"],
                outsider_id,
                outsider_id,
            ]
            scopes = [
                (True, world["project_id"], value)
                for value in [False, True, True, True]
            ] + [(False, None, False)]
            allowed = await NotificationService(
                BetweenStatements()
            )._allowed_delivery_indices(
                [SimpleNamespace(user_id=identity) for identity in ids],
                scopes,
            )
            _compare(
                "delivery:snapshots",
                {"statements": statements, "allowed": sorted(allowed)},
            )
            await db.rollback()


@pytest.mark.parametrize("date_column", ["assigned_at", "updated_at"])
@pytest.mark.parametrize("operation", ["member_ids", "mentions", "delivery", "roster"])
async def test_partial_reads_keep_native_date_decoding(
    test_engine, date_column, operation
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as change:
            await change.execute(
                text(
                    f"UPDATE project_members SET {date_column}=TIMESTAMPTZ '10000-01-01' WHERE id=:id"
                ),
                {"id": world["membership_id"]},
            )
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            error = None
            result = None
            try:
                if operation == "member_ids":
                    result = list(await db.scalars(member_user_ids(project.id))) == [
                        world["member_id"]
                    ]
                elif operation == "mentions":
                    result = await AnnotationCommentService(db).validate_mentions(
                        project.id, [world["member_id"]]
                    )
                elif operation == "delivery":
                    result = sorted(
                        await NotificationService(db)._allowed_delivery_indices(
                            [SimpleNamespace(user_id=world["member_id"])],
                            [(True, project.id, False)],
                        )
                    )
                else:
                    await _roster(db, project)
            except Exception as exc:
                error = type(exc).__name__
            _compare(
                f"date:{date_column}:{operation}", {"result": result, "error": error}
            )
            await db.rollback()


async def test_mentions_keep_any_role_inactive_and_missing_order(test_engine):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as change:
            await change.execute(
                text("UPDATE project_members SET role='unknown' WHERE id=:id"),
                {"id": world["membership_id"]},
            )
            await change.execute(
                text("UPDATE users SET is_active=false WHERE id IN (:member,:owner)"),
                {"member": world["member_id"], "owner": world["owner_id"]},
            )
        async with owner() as db:
            service = AnnotationCommentService(db)
            await service.validate_mentions(
                world["project_id"], [world["member_id"], world["owner_id"]]
            )
            missing = [uuid.uuid4(), uuid.uuid4()]
            with pytest.raises(HTTPException) as invalid:
                await service.validate_mentions(
                    world["project_id"],
                    [missing[1], world["member_id"], missing[0], missing[1]],
                )
            detail = invalid.value.detail
            _compare(
                "mentions:any_role_order",
                {
                    "status": invalid.value.status_code,
                    "error": detail["error"],
                    "order": [
                        missing.index(uuid.UUID(value))
                        for value in detail["non_member_user_ids"]
                    ],
                },
            )
            await db.rollback()
