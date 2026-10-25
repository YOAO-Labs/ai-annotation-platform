"""Freeze actual Project set/projection callers before owner-view migration."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect, text

from app.api.v1.training_hub import projects as training_projects
from app.application.task_dataset_backfill import list_project_ids
from app.db.models.project import Project
from app.services.annotation_comment import AnnotationCommentService
from app.services.notification import NotificationService
from app.workers.export import _scope_fingerprint, _scope_naming
from tests.factory import create_project
from tests.test_workspace_member_facts_legacy import _pending
from tests.test_workspace_project_authority_locking import _world
from tests.test_workspace_project_get_native import _state

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]
_BASE = "22fba052f23a83d7c7acdd10ae8c044e8a20d825"
_GOLDEN = Path(__file__).parent / "fixtures/workspace_project_facts_legacy.json"
_STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _compare(key, observed):
    if os.getenv("AAP_PROJECT_FACTS_CAPTURE") == "1":
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
        assert key not in document["cases"], "never overwrite a captured Project case"
        document["cases"][key] = observed
        _GOLDEN.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        assert observed == json.loads(_GOLDEN.read_text())["cases"][key]


def _project_state(obj):
    if obj is None:
        return None
    result = _state(obj)
    result["total_tasks"] = inspect(obj).dict.get("total_tasks", "__unloaded__")
    return result


def _actor(world):
    return SimpleNamespace(id=world["owner_id"], role="project_admin", is_active=True)


@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize(
    "cache",
    [
        "miss",
        "clean",
        "dirty_name",
        "dirty_counter",
        "partial",
        "expired",
        "expired_dirty",
        "stale",
        "stale_partial",
        "deleted_database",
    ],
)
async def test_training_project_cache_defer_and_autoflush_match_legacy(
    test_engine, cache, autoflush
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as setup:
            pending_project = await create_project(setup, owner_id=world["member_id"])
            pending_project_id = pending_project.id
            world["project_ids"].append(pending_project_id)
            await setup.execute(
                text(
                    "UPDATE projects SET name='initial',status='in_progress',total_tasks=7,"
                    "annotation_guide='guide',guide_assets='[]'::jsonb,updated_at=:stamp WHERE id=:id"
                ),
                {"id": world["project_id"], "stamp": _STAMP},
            )
        async with owner() as db:
            cached = (
                None if cache == "miss" else await db.get(Project, world["project_id"])
            )
            if cache.startswith("stale"):
                async with owner.begin() as change:
                    await change.execute(
                        text(
                            "UPDATE projects SET name='committed',status='completed',total_tasks=31,"
                            "tool_bindings='{}'::jsonb WHERE id=:id"
                        ),
                        {"id": world["project_id"]},
                    )
            elif cache == "deleted_database":
                async with owner.begin() as change:
                    await change.execute(
                        text("DELETE FROM projects WHERE id=:id"),
                        {"id": world["project_id"]},
                    )
            if cache == "dirty_name":
                cached.name = "pending local"
            elif cache == "dirty_counter":
                cached.total_tasks = 23
            elif cache in {"expired", "expired_dirty"}:
                db.expire(cached)
                if cache == "expired_dirty":
                    cached.name = "pending local"
            elif cache in {"partial", "stale_partial"}:
                db.expire(cached, ["name", "total_tasks", "annotation_guide"])
            pending = _pending({**world, "project_id": pending_project_id})
            db.add(pending)
            db.autoflush = autoflush
            trace = []
            loaded = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                lowered = statement.lower()
                if "write_project_counter_projection" in lowered:
                    trace.append("counter_patch")
                elif lowered.startswith("update projects"):
                    trace.append("project_update")
                elif "select " in lowered and (
                    "from projects" in lowered or ".project_facts" in lowered
                ):
                    trace.append("project_read")
                elif "insert into tasks" in lowered:
                    trace.append("task_insert")

            def retain(_session, obj):
                if isinstance(obj, Project):
                    loaded.append(obj)

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            event.listen(db.sync_session, "loaded_as_persistent", retain)
            before = tuple(
                (await db.execute(text("SELECT pg_backend_pid(),txid_current()"))).one()
            )
            try:
                result = await training_projects(
                    search="", limit=20, offset=0, user=_actor(world), db=db
                )
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
                Project.__mapper__.identity_key_from_primary_key((world["project_id"],))
            )
            _compare(
                f"training:{cache}:{autoflush}",
                {
                    "total": result["total"],
                    "items": [
                        {
                            field: row[field]
                            for field in [
                                "name",
                                "status",
                                "can_prepare_dataset",
                                "ready_dataset_count",
                                "active_run_count",
                            ]
                        }
                        for row in result["items"]
                    ],
                    "cached": _project_state(cached),
                    "loaded": [_project_state(obj) for obj in loaded],
                    "same_identity": cached is not None and identity is cached,
                    "pending_task": pending in db.new,
                    "trace": trace,
                },
            )
            await db.rollback()


@pytest.mark.parametrize("autoflush", [False, True])
async def test_id_backfill_keeps_autoflush_and_filter_precedence(
    test_engine, autoflush
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            pending = _pending(world)
            db.add(pending)
            db.autoflush = autoflush
            ids = await list_project_ids(
                db, project_id=world["project_id"], after=world["project_id"]
            )
            _compare(
                f"ids:{autoflush}",
                {
                    "matches": ids == [world["project_id"]],
                    "pending_task": pending in db.new,
                },
            )
            await db.rollback()


@pytest.mark.parametrize("date_column", ["due_date", "created_at", "updated_at"])
@pytest.mark.parametrize(
    "operation",
    ["ids", "mentions", "delivery", "training", "export_naming", "export_fingerprint"],
)
async def test_projection_date_decoding_matches_legacy(
    test_engine, date_column, operation
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as change:
            kind = "DATE" if date_column == "due_date" else "TIMESTAMPTZ"
            await change.execute(
                text(
                    f"UPDATE projects SET {date_column}={kind} '10000-01-01' WHERE id=:id"
                ),
                {"id": world["project_id"]},
            )
        async with owner() as db:
            result = None
            error = None
            try:
                if operation == "ids":
                    result = await list_project_ids(
                        db, project_id=world["project_id"]
                    ) == [world["project_id"]]
                elif operation == "mentions":
                    await AnnotationCommentService(db).validate_mentions(
                        world["project_id"], [world["owner_id"]]
                    )
                elif operation == "delivery":
                    result = sorted(
                        await NotificationService(db)._allowed_delivery_indices(
                            [SimpleNamespace(user_id=world["owner_id"])],
                            [(True, world["project_id"], True)],
                        )
                    )
                elif operation == "training":
                    await training_projects(
                        search="", limit=20, offset=0, user=_actor(world), db=db
                    )
                elif operation == "export_naming":
                    media, dataset, display = await _scope_naming(
                        db, world["project_id"], None
                    )
                    result = [media, dataset, display.startswith("P-E2E-")]
                else:
                    stamp, count = await _scope_fingerprint(
                        db, world["project_id"], None
                    )
                    result = [isinstance(stamp, datetime), count]
            except Exception as exc:
                error = type(exc).__name__
            _compare(
                f"date:{date_column}:{operation}", {"result": result, "error": error}
            )
            await db.rollback()


def _immutable_training_projects():
    # Only newly discovered baseline cases use this exact old function body.
    # Root may already contain integrated source while HEAD remains the base.
    from app.api.v1 import training_hub

    root = Path(__file__).resolve().parents[3]
    source = subprocess.check_output(
        ["git", "show", _BASE + ":apps/api/app/api/v1/training_hub.py"],
        cwd=root,
        text=True,
    )
    definitions = [
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"project_scope", "projects"}
    ]
    assert {node.name for node in definitions} == {"project_scope", "projects"}
    for node in definitions:
        node.decorator_list = []
    namespace = dict(vars(training_hub))
    exec(
        compile(
            ast.Module(body=definitions, type_ignores=[]),
            _BASE + ":training_hub.py",
            "exec",
        ),
        namespace,
    )
    return namespace["projects"]


@pytest.mark.parametrize("autoflush", [False, True])
async def test_super_admin_total_and_page_share_autoflush(test_engine, autoflush):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner.begin() as setup:
            await setup.execute(
                text("UPDATE users SET role='super_admin' WHERE id=:id"),
                {"id": world["owner_id"]},
            )
        async with owner() as db:
            pending = Project(
                display_id="P-SA-PENDING",
                name="pending-catalog",
                type_key="image-det",
                type_label="Image",
                owner_id=world["owner_id"],
                tool_bindings={},
            )
            db.add(pending)
            db.autoflush = autoflush
            function = (
                _immutable_training_projects()
                if os.getenv("AAP_PROJECT_FACTS_CAPTURE") == "1"
                else training_projects
            )
            result = await function(
                search="",
                limit=20,
                offset=0,
                user=SimpleNamespace(
                    id=world["owner_id"], role="super_admin", is_active=True
                ),
                db=db,
            )
            _compare(
                f"training:super_admin:{autoflush}",
                {
                    "total": result["total"],
                    "names": sorted(row["name"] for row in result["items"]),
                    "pending_project": pending in db.new,
                },
            )
            await db.rollback()
