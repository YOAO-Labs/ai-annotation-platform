"""Freeze atomic lifecycle counter writes at their original ORM boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import event, inspect, text

from app.db.models.project import Project
from app.services.labeling_tasks import LabelingTaskService
from tests.test_labeling_project_counter_owner_native import _state
from tests.test_workspace_member_facts_legacy import _pending
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]
_BASE = "af9c604f015f98f22020661c4df5f8d7c561fdd3"
_GOLDEN = Path(__file__).parent / "fixtures/labeling_counter_deltas_legacy.json"
_STAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)
_EXPLICIT = datetime(2001, 1, 1, tzinfo=timezone.utc)
_METHODS = {
    "review": "decrement_project_review_count",
    "completed": "decrement_project_completed_count",
    "approval": "record_project_review_approval",
}


def _compare(key, observed):
    if os.getenv("AAP_COUNTER_DELTA_CAPTURE") == "1":
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
        assert key not in document["cases"], (
            "never overwrite an atomic counter baseline"
        )
        document["cases"][key] = observed
        _GOLDEN.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        assert observed == json.loads(_GOLDEN.read_text())["cases"][key]


def _stamp(value, tx_now):
    if value == tx_now:
        return "transaction_now"
    if value == _STAMP:
        return "initial"
    if value == _EXPLICIT:
        return "explicit"
    assert value == "__unloaded__", "unexpected counter timestamp"
    return "unloaded"


def _cache(project, tx_now):
    state = _state(project)
    state["updated_at"] = _stamp(
        inspect(project).dict.get("updated_at", "__unloaded__"), tx_now
    )
    return state


async def _database(db, project_id, tx_now):
    with db.no_autoflush:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT name,total_tasks,completed_tasks,review_tasks,in_progress_tasks,"
                        "batch_summary,updated_at FROM projects WHERE id=:id"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    result = dict(row)
    result["updated_at"] = _stamp(result["updated_at"], tx_now)
    return result


async def _check(test_engine, method, kind, autoflush):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as setup:
            completed, review = 11, 7
            if kind == "zero":
                completed, review = 0, 0
            elif kind == "negative":
                completed, review = -2, -2
            elif kind == "overflow":
                if method == "review":
                    review = -2147483648
                elif method == "completed":
                    completed = -2147483648
                else:
                    completed = 2147483647
            await setup.execute(
                text(
                    "UPDATE projects SET name='initial',total_tasks=22,completed_tasks=:completed,"
                    "review_tasks=:review,in_progress_tasks=4,batch_summary=CAST(:summary AS jsonb),"
                    "updated_at=:stamp WHERE id=:id"
                ),
                {
                    "id": world["project_id"],
                    "completed": completed,
                    "review": review,
                    "summary": json.dumps({"old": 1}),
                    "stamp": _STAMP,
                },
            )
        async with owner() as db:
            project = await db.get(Project, world["project_id"])
            if kind == "stale":
                async with owner.begin() as change:
                    await change.execute(
                        text(
                            "UPDATE projects SET completed_tasks=111,review_tasks=107,total_tasks=122 WHERE id=:id"
                        ),
                        {"id": world["project_id"]},
                    )
            elif kind == "deleted":
                async with owner.begin() as change:
                    await change.execute(
                        text("DELETE FROM projects WHERE id=:id"),
                        {"id": world["project_id"]},
                    )
            if kind == "dirty_other":
                project.name = "pending other field"
            elif kind == "dirty_target":
                setattr(
                    project,
                    "review_tasks" if method == "review" else "completed_tasks",
                    42,
                )
            elif kind == "dirty_other_counter":
                field = (
                    "completed_tasks"
                    if method == "review"
                    else "review_tasks"
                    if method == "completed"
                    else "total_tasks"
                )
                setattr(project, field, 42)
            elif kind == "partial":
                db.expire(project, ["review_tasks", "completed_tasks", "updated_at"])
            elif kind == "expired_all":
                db.expire(project)
            elif kind == "explicit_timestamp":
                project.updated_at = _EXPLICIT
            pending = None
            if kind in {
                "pending_task",
                "dirty_other",
                "dirty_target",
                "dirty_other_counter",
                "partial",
                "expired_all",
                "explicit_timestamp",
            }:
                pending = _pending(world)
                db.add(pending)
            db.autoflush = autoflush
            tx_now = await db.scalar(text("SELECT now()"))
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            flushes = []

            def mark_flush(session, _context, _instances):
                flushes.append(
                    {
                        "new": sorted(type(obj).__name__ for obj in session.new),
                        "dirty": sorted(type(obj).__name__ for obj in session.dirty),
                    }
                )

            event.listen(db.sync_session, "before_flush", mark_flush)
            error = None
            result = None
            try:
                result = await getattr(LabelingTaskService(db), _METHODS[method])(
                    project
                )
            except Exception as exc:
                error = {
                    "type": type(exc).__name__,
                    "sqlstate": getattr(getattr(exc, "orig", None), "sqlstate", None),
                }
            finally:
                event.remove(db.sync_session, "before_flush", mark_flush)
            observed = {
                "error": error,
                "return_is_none": result is None,
                "cache": _cache(project, tx_now),
                "pending_task": pending is not None and pending in db.new,
                "flushes": flushes,
                "session_active": db.is_active,
            }
            if error is None:
                observed["database_before_flush"] = await _database(
                    db, world["project_id"], tx_now
                )
                assert (
                    await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
                ).one() == before
                await db.flush()
                observed["database_after_flush"] = await _database(
                    db, world["project_id"], tx_now
                )
                observed["cache_after_flush"] = _cache(project, tx_now)
            await db.rollback()
            observed["database_after_rollback"] = await _database(
                db, world["project_id"], tx_now
            )
            _compare(f"{method}:{kind}:{autoflush}", observed)
            await db.rollback()


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize(
    "cache",
    [
        "clean",
        "pending_task",
        "dirty_other",
        "dirty_target",
        "dirty_other_counter",
        "partial",
        "expired_all",
        "stale",
        "explicit_timestamp",
        "deleted",
    ],
)
async def test_counter_delta_cache_and_flush_match_legacy(
    test_engine, method, autoflush, cache
):
    await _check(test_engine, method, cache, autoflush)


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("kind", ["zero", "negative", "overflow"])
async def test_counter_delta_numeric_and_error_edges_match_legacy(
    test_engine, method, kind
):
    await _check(test_engine, method, kind, True)
