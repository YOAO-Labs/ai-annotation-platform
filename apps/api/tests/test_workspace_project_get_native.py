"""Frozen real Session.get compatibility for owner-backed Project snapshots."""

from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest
from sqlalchemy import delete, event, inspect, text

from app.db.models.project import Project
from app.db.models.task import Task
from app.services.labeling_core_read_relations import get_core_row, task_row
from tests.test_workspace_project_authority_locking import _world
from tests.test_workspace_project_authority_locking import _assert_private_denied

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]
_BASE = "a6ebe369bbb3d6a966ca05b9e40a6440ea2544f3"
_GOLDEN = Path(__file__).parent / "fixtures/workspace_project_get_legacy.json"


async def _read(db, project_id, actor_id, refresh):
    if os.getenv("AAP_PROJECT_GET_CAPTURE") == "1":
        root = Path(__file__).resolve().parents[3]
        assert (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            == _BASE
        )
        return await db.get(Project, project_id, populate_existing=refresh)
    from app.services.project_access import get_workspace_project

    return await get_workspace_project(
        db, actor_id=actor_id, project_id=project_id, populate_existing=refresh
    )


def _state(obj):
    if obj is None:
        return None
    state = inspect(obj)
    return {
        "values": {
            key: state.dict.get(key, "__unloaded__")
            for key in ("name", "status", "tool_bindings")
        },
        "expired": state.expired,
        "unloaded": sorted(state.unloaded),
        "deleted": state.deleted,
        "detached": state.detached,
        "persistent": state.persistent,
        "modified": state.modified,
    }


@pytest.mark.parametrize("actorless", [False, True], ids=["actor-facts", "actor-free"])
@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize(
    "cache",
    [
        "miss",
        "clean",
        "dirty",
        "partial",
        "expired",
        "expired_dirty",
        "deleted_expired",
        "deleted_refresh",
        "missing",
        "invalid_date_cached",
    ],
)
async def test_project_get_matches_frozen_session_behavior(
    test_engine, refresh, autoflush, cache, actorless
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as seed:
            await seed.execute(
                text(
                    "UPDATE public.projects SET name='initial',status='in_progress',tool_bindings=CAST(:tools AS jsonb) WHERE id=:id"
                ),
                {"id": world["project_id"], "tools": json.dumps({"initial": True})},
            )
        async with owner() as db:
            project_id = uuid.uuid4() if cache == "missing" else world["project_id"]
            cached = (
                None
                if cache in {"miss", "missing"}
                else await db.get(Project, project_id)
            )
            async with owner.begin() as external:
                if cache in {"deleted_expired", "deleted_refresh"}:
                    await external.execute(
                        text("DELETE FROM public.projects WHERE id=:id"),
                        {"id": project_id},
                    )
                else:
                    await external.execute(
                        text(
                            "UPDATE public.projects SET name='committed',status='completed',tool_bindings=CAST(:tools AS jsonb) WHERE id=:id"
                        ),
                        {"id": project_id, "tools": json.dumps({"committed": True})},
                    )
                    if cache == "invalid_date_cached":
                        await external.execute(
                            text(
                                "UPDATE public.projects SET due_date=DATE '12000-01-01' WHERE id=:id"
                            ),
                            {"id": project_id},
                        )
            if cache == "dirty":
                cached.name = "pending local"
            elif cache == "partial":
                db.expire(cached, ["status"])
            elif cache in {"expired", "expired_dirty", "deleted_expired"}:
                db.expire(cached)
                if cache == "expired_dirty":
                    cached.name = "pending local"
            pending = Task(
                id=uuid.uuid4(),
                project_id=world["project_id"],
                display_id=f"T-GET-{uuid.uuid4().hex[:12]}",
                file_name="pending.png",
                file_path=f"get/{uuid.uuid4().hex}.png",
                file_type="image",
                status="pending",
            )
            # Deleted Project cases have no valid pending Task foreign key.
            if cache not in {"deleted_expired", "deleted_refresh"}:
                db.add(pending)
            trace = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                lowered = statement.lower()
                if (
                    "project_access_step(" in lowered
                    or "aap_workspace_v1.read_project_row(" in lowered
                    or ("select " in lowered and "from projects" in lowered)
                ):
                    trace.append("project_read")
                elif "insert into tasks" in lowered:
                    trace.append("task_insert")
                elif "update projects" in lowered:
                    trace.append("project_update")

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            result, error = None, None
            try:
                with nullcontext() if autoflush else db.no_autoflush:
                    result = await _read(
                        db,
                        project_id,
                        None if actorless else world["member_id"],
                        refresh,
                    )
            except Exception as exc:
                error = type(exc).__name__
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            observed = {
                "error": error,
                "result": _state(result),
                "cached": _state(cached),
                "same_identity": cached is not None and result is cached,
                "pending_task": pending in db.new,
                "trace": trace,
            }
            key = f"{cache}:autoflush={autoflush}:refresh={refresh}"
            if os.getenv("AAP_PROJECT_GET_CAPTURE") == "1":
                existing = (
                    json.loads(_GOLDEN.read_text())
                    if _GOLDEN.exists()
                    else {"source_commit": _BASE, "cases": {}}
                )
                assert key not in existing["cases"], (
                    "legacy capture must never overwrite an existing case"
                )
                existing["cases"][key] = observed
                _GOLDEN.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                )
            else:
                assert observed == json.loads(_GOLDEN.read_text())["cases"][key]
            await db.rollback()


async def test_restricted_api_task_access_uses_read_only_owner_snapshots(test_engine):
    from app.services.task_access import resolve_task_access
    from tests.factory import create_task

    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as seed:
            task = await create_task(seed, project_id=world["project_id"])
            task_id = task.id
        try:
            async with api() as db:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await _assert_private_denied(db)
                before = (
                    await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
                ).one()
                task = await get_core_row(db, task_row, task_id)
                access = await resolve_task_access(
                    db, task=task, actor_id=world["member_id"]
                )
                assert (
                    access.project_id == world["project_id"]
                    and access.project_role == "annotator"
                )
                assert (
                    await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
                ).one() == before
        finally:
            async with owner.begin() as cleanup:
                await cleanup.execute(delete(Task).where(Task.id == task_id))


async def test_restricted_api_training_authority_keeps_existing_owner_fences(
    test_engine,
):
    from app.services.training_datasets import bind_training_authority
    from tests.test_workspace_project_access_step_native import _assert_nowait_conflict

    async with _world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            await bind_training_authority(db).assert_authority(
                project_id=world["project_id"], actor_id=world["owner_id"]
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for table, identity in (
                ("users", world["owner_id"]),
                ("projects", world["project_id"]),
            ):
                await _assert_nowait_conflict(
                    test_engine,
                    f"SELECT id FROM public.{table} WHERE id=$1 FOR UPDATE NOWAIT",
                    identity,
                )
