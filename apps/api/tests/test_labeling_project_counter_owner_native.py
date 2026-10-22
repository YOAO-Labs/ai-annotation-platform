"""Frozen counter persistence at the existing Session flush boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest
from sqlalchemy import delete, event, inspect, text

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.batch import BatchService
from app.services.labeling_tasks import LabelingTaskService
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]
_BASE = "9e37fe5b7cd358ae199db2104c11299b77e1a872"
_GOLDEN = Path(__file__).parent / "fixtures/labeling_project_counters_legacy.json"
_COUNTERS = (
    "total_tasks",
    "completed_tasks",
    "review_tasks",
    "in_progress_tasks",
    "batch_summary",
)


def _capture(key, observed):
    if os.getenv("AAP_COUNTER_OWNER_CAPTURE") == "1":
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
            else {"source_commit": _BASE, "cases": {}}
        )
        assert key not in document["cases"], "never overwrite a captured legacy case"
        document["cases"][key] = observed
        _GOLDEN.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        expected = json.loads(_GOLDEN.read_text())["cases"][key]

        # The legacy batch query has no ORDER BY. Either batch may be the final
        # deferred dirty row, so compare the persisted + pending projection and
        # the number of deferred rows, not a random UUID-dependent query order.
        def comparable(value):
            value = dict(value)
            rows = value.pop("batch_rows")
            dirty = value.pop("dirty_batches")
            value["batch_flush_state"] = {
                "projections": sorted(rows + dirty),
                "pending_count": len(dirty),
            }
            return value

        assert comparable(observed) == comparable(expected)


def _state(project):
    if project is None:
        return None
    state = inspect(project)
    return {
        "values": {
            name: state.dict.get(name, "__unloaded__") for name in (*_COUNTERS, "name")
        },
        "dirty_fields": sorted(
            name
            for name in (*_COUNTERS, "name", "updated_at")
            if state.attrs[name].history.has_changes()
        ),
        "unloaded": sorted(state.unloaded),
        "modified": state.modified,
    }


async def _seed_counters(owner, world, *, correct=False):
    batch_ids = [uuid.uuid4(), uuid.uuid4()]
    async with owner.begin() as db:
        for index, batch_id in enumerate(batch_ids):
            db.add(
                TaskBatch(
                    id=batch_id,
                    project_id=world["project_id"],
                    display_id=f"B-CNT-{batch_id.hex[:12]}",
                    name="counter",
                    status="reviewing" if index == 0 else "active",
                    annotator_id=world["owner_id"] if index == 0 else None,
                    total_tasks=9,
                    completed_tasks=7,
                    review_tasks=6,
                )
            )
        await db.flush()
        for index, batch_id in enumerate(batch_ids):
            task_id = uuid.uuid4()
            db.add(
                Task(
                    id=task_id,
                    project_id=world["project_id"],
                    batch_id=batch_id,
                    display_id=f"T-CNT-{task_id.hex[:12]}",
                    file_name="counter.png",
                    file_path=f"counter/{task_id}.png",
                    file_type="image",
                    status="completed" if index == 0 else "in_progress",
                )
            )
        await db.flush()
        await db.execute(
            text(
                "UPDATE public.projects SET name='initial',total_tasks=:total,completed_tasks=:completed,review_tasks=:review,in_progress_tasks=:progress,batch_summary=CAST(:summary AS jsonb),updated_at=TIMESTAMPTZ '2000-01-01' WHERE id=:id"
            ),
            {
                "id": world["project_id"],
                "total": 2 if correct else 9,
                "completed": 1 if correct else 7,
                "review": 0 if correct else 6,
                "progress": 1 if correct else 8,
                "summary": json.dumps(
                    {"total": 2, "assigned": 1, "in_review": 1}
                    if correct
                    else {"old": 1}
                ),
            },
        )
    return batch_ids


@pytest.mark.parametrize("method", ["batch", "source_less"])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize(
    "cache",
    [
        "miss",
        "clean",
        "dirty_other",
        "no_change",
        "explicit_timestamp",
        "stale_same",
        "stale_partial",
        "expired",
    ],
)
async def test_project_counters_match_legacy_flush_state(
    test_engine, method, autoflush, cache
):
    async with _world(test_engine) as (owner, _api, world, _url):
        batch_ids = await _seed_counters(
            owner, world, correct=cache in {"no_change", "stale_same", "stale_partial"}
        )
        try:
            async with owner() as db:
                cached = (
                    None
                    if cache == "miss"
                    else await db.get(Project, world["project_id"])
                )
                if cache in {"stale_same", "stale_partial"}:
                    if cache == "stale_partial":
                        cached.total_tasks = 9
                    async with owner.begin() as external:
                        await external.execute(
                            text(
                                "UPDATE public.projects SET total_tasks=99,completed_tasks=99,review_tasks=99,in_progress_tasks=99 WHERE id=:id"
                            ),
                            {"id": world["project_id"]},
                        )
                if cache == "dirty_other":
                    cached.name = "pending other field"
                elif cache == "explicit_timestamp":
                    from datetime import datetime, timezone

                    cached.updated_at = datetime(2001, 1, 1, tzinfo=timezone.utc)
                elif cache == "expired":
                    db.expire(cached)
                pending_id = uuid.uuid4()
                pending = Task(
                    id=pending_id,
                    project_id=world["project_id"],
                    batch_id=batch_ids[0],
                    display_id=f"T-CNT-P-{pending_id.hex[:12]}",
                    file_name="pending.png",
                    file_path=f"counter/{pending_id}.png",
                    file_type="image",
                    status="pending",
                )
                if cache not in {"no_change", "stale_same", "stale_partial"}:
                    db.add(pending)
                db.autoflush = autoflush
                tx_now = await db.scalar(text("SELECT now()"))
                before_flush = []

                def mark_flush(session, _context, _instances):
                    before_flush.append(
                        {
                            "new": sorted(type(obj).__name__ for obj in session.new),
                            "dirty": sorted(
                                type(obj).__name__ for obj in session.dirty
                            ),
                        }
                    )

                event.listen(db.sync_session, "before_flush", mark_flush)
                try:
                    if method == "batch":
                        await BatchService(db)._sync_project_counters(
                            world["project_id"]
                        )
                    else:
                        await LabelingTaskService(
                            db
                        ).recalculate_source_less_task_counters(world["project_id"])
                    result_project = db.sync_session.identity_map.get(
                        Project.__mapper__.identity_key_from_primary_key(
                            (world["project_id"],)
                        )
                    )

                    async def snapshot():
                        row = (
                            (
                                await db.execute(
                                    text(
                                        "SELECT total_tasks,completed_tasks,review_tasks,in_progress_tasks,batch_summary,name,updated_at FROM public.projects WHERE id=:id"
                                    ),
                                    {"id": world["project_id"]},
                                )
                            )
                            .mappings()
                            .one()
                        )
                        values = dict(row)
                        timestamp = values.pop("updated_at")
                        values["updated_at"] = (
                            "transaction_now"
                            if timestamp == tx_now
                            else timestamp.isoformat()
                        )
                        return values

                    with db.no_autoflush:
                        pending_before = pending in db.new
                        before = await snapshot()
                        state = _state(cached or result_project)
                        batch_rows = list(
                            (
                                await db.execute(
                                    text(
                                        "SELECT total_tasks,completed_tasks,review_tasks FROM public.task_batches WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY display_id"
                                    ),
                                    {"ids": batch_ids},
                                )
                            ).all()
                        )
                        # Batch identities may be weakly released after the method returns.
                        dirty_batches = sorted(
                            (
                                inspect(obj).dict.get("total_tasks"),
                                inspect(obj).dict.get("completed_tasks"),
                                inspect(obj).dict.get("review_tasks"),
                            )
                            for obj in db.dirty
                            if isinstance(obj, TaskBatch)
                        )
                    await db.flush()
                    after = await snapshot()
                    observed = {
                        "before": before,
                        "after": after,
                        "project": state if cached is not None else None,
                        "pending_before": pending_before,
                        "before_flush": before_flush,
                        "batch_rows": sorted([list(row) for row in batch_rows]),
                        "dirty_batches": [list(values) for values in dirty_batches],
                    }
                    _capture(f"{method}:{cache}:autoflush={autoflush}", observed)
                finally:
                    event.remove(db.sync_session, "before_flush", mark_flush)
                    await db.rollback()
        finally:
            async with owner.begin() as cleanup:
                await cleanup.execute(
                    delete(Task).where(Task.project_id == world["project_id"])
                )
                await cleanup.execute(
                    delete(TaskBatch).where(TaskBatch.project_id == world["project_id"])
                )
