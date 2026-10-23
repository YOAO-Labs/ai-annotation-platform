"""Old-behavior replay for Identity selection in operational seed consumers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import event, text

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.identity_commands import IdentityCommandService
from scripts import import_images, seed_scale
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = ROOT / "apps/api/tests/fixtures/identity_seed_selection_legacy.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text())["snapshots"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


def _replay(name, value, record_property):
    assert value == GOLDEN[name]
    record_property("identity_seed_selection_replay", name)


@pytest.fixture
async def selection_world(test_engine):
    async with _world(test_engine) as (owner, consumer, role, _url):
        # Match the existing worktree CLI's two Assets capabilities used here;
        # the LAN application profile intentionally does not include them.
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "GRANT EXECUTE ON FUNCTION "
                    "aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text), "
                    "aap_assets_v1.append_operator_items(uuid,uuid,jsonb) TO " + role
                )
            )
        result = await _grants(
            test_engine, ROOT / "infra/docker/identity-scale-seed-grants.sql", role
        )
        assert result.returncode == 0, result.stderr
        result = await _grants(
            test_engine,
            ROOT / "infra/docker/workspace-scale-seed-grants.sql",
            role,
            assets_owner="assets_code",
            labeling_owner="labeling_code",
        )
        assert result.returncode == 0, result.stderr
        async with consumer() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,REFERENCES')"
                )
            )
        yield owner, consumer


async def _scale_actors(owner, name, scenario):
    ids = {
        key: uuid.uuid4()
        for key in (
            "admin",
            "later_admin",
            "annotator",
            "later_annotator",
            "inactive",
            "reviewer",
            "unlinked",
        )
    }
    async with owner.begin() as db:
        for key, role, active, year in (
            (
                "admin",
                "viewer" if scenario == "no_admin" else "super_admin",
                False,
                2001,
            ),
            (
                "later_admin",
                "viewer" if scenario == "no_admin" else "super_admin",
                True,
                2001 if scenario == "ties" else 2002,
            ),
            ("annotator", "viewer", scenario != "inactive_only", 2003),
            (
                "later_annotator",
                "employee",
                scenario != "inactive_only",
                2003 if scenario == "ties" else 2004,
            ),
            ("inactive", "employee", False, 1990),
            ("reviewer", "employee", True, 1991),
            ("unlinked", "employee", True, 1980),
        ):
            db.add(
                User(
                    id=ids[key],
                    email=f"{name}-{key}@seed.test",
                    name=key,
                    password_hash="owned-seed-fixture",
                    role=role,
                    is_active=active,
                )
            )
            await db.flush()
            await db.execute(
                text(
                    f"UPDATE public.users SET created_at='{year}-01-01T00:00:00Z' WHERE id=:id"
                ),
                {"id": ids[key]},
            )
        projects = []
        for n in range(2 if scenario in {"cross_project", "duplicates"} else 1):
            project = Project(
                display_id="P-" + uuid.uuid4().hex[:12],
                name=f"support-{name}-{n}",
                type_key="image-det",
                type_label="fixture",
                owner_id=ids["later_admin"],
            )
            db.add(project)
            await db.flush()
            projects.append(project)
        for key in ("annotator", "later_annotator", "inactive", "reviewer"):
            db.add(
                ProjectMember(
                    project_id=projects[0].id,
                    user_id=ids[key],
                    role="reviewer"
                    if key == "reviewer" or scenario == "no_duty"
                    else "annotator",
                    assigned_by=ids["later_admin"],
                )
            )
        if scenario == "cross_project":
            db.add(
                ProjectMember(
                    project_id=projects[1].id,
                    user_id=ids["unlinked"],
                    role="annotator",
                    assigned_by=ids["later_admin"],
                )
            )
        elif scenario == "duplicates":
            db.add(
                ProjectMember(
                    project_id=projects[1].id,
                    user_id=ids["annotator"],
                    role="annotator",
                    assigned_by=ids["later_admin"],
                )
            )
        if scenario == "infinite":
            await db.execute(
                text(
                    "UPDATE public.users SET created_at='-infinity'::timestamptz WHERE id IN (:admin,:annotator)"
                ),
                {"admin": ids["admin"], "annotator": ids["annotator"]},
            )
    return ids


async def _remove_scale_actors(owner, name, ids):
    async with owner() as cleanup:
        await cleanup.execute(
            text(
                "DELETE FROM project_members WHERE project_id IN (SELECT id FROM projects WHERE name LIKE :prefix)"
            ),
            {"prefix": "support-" + name + "-%"},
        )
        await cleanup.execute(
            text("DELETE FROM projects WHERE name LIKE :prefix"),
            {"prefix": "support-" + name + "-%"},
        )
        await cleanup.execute(
            text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": list(ids.values())},
        )
        await cleanup.commit()


async def test_node_selectors_are_read_only_and_fenced(selection_world):
    owner, consumer = selection_world
    name = "node-selection"
    ids = await _scale_actors(owner, name, "normal")
    try:
        url = (
            consumer.kw["bind"]
            .url.set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        result = subprocess.run(
            [
                "node",
                str(ROOT / "apps/api/tests/fixtures/identity_seed_selection_peer.mjs"),
            ],
            input=json.dumps(
                {
                    "database_url": url,
                    "admin_id": str(ids["admin"]),
                    "annotator_id": str(ids["annotator"]),
                }
            ),
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "ok": True,
            "read_only": True,
            "private_user_denied": True,
        }
    finally:
        await _remove_scale_actors(owner, name, ids)


async def test_combined_selector_uses_statement_start_snapshot(
    test_engine, selection_world, record_property
):
    owner, consumer = selection_world
    name = "snapshot-selection"
    ids = await _scale_actors(owner, name, "normal")
    key = uuid.uuid4().int % 2147483647
    tag = "seed-snapshot-" + uuid.uuid4().hex
    target = consumer.kw["bind"].sync_engine

    def gate(_connection, _cursor, statement, parameters, _context, _many):
        if "read_scale_seed_annotator" in statement:
            assert "aap_workspace_v1.project_member_facts" in statement
            statement = (
                f"WITH aap_seed_gate AS MATERIALIZED (SELECT pg_catalog.pg_advisory_lock({key})) "
                + statement
                + " FROM aap_seed_gate"
            )
        return statement, parameters

    event.listen(target, "before_cursor_execute", gate, retval=True)
    task = None
    try:
        async with test_engine.connect() as lock:
            await lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                async with consumer() as db:
                    await db.execute(
                        text("SELECT set_config('application_name', :tag, true)"),
                        {"tag": tag},
                    )
                    task = asyncio.create_task(
                        IdentityCommandService(db).resolve_scale_seed_actors()
                    )
                    deadline = asyncio.get_running_loop().time() + 10
                    while asyncio.get_running_loop().time() < deadline:
                        async with owner() as observer:
                            waiting = await observer.scalar(
                                text(
                                    "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND application_name=:tag AND wait_event_type='Lock' AND query LIKE 'WITH aap_seed_gate%'"
                                ),
                                {"tag": tag},
                            )
                        if waiting is not None:
                            break
                        if task.done():
                            pytest.fail(
                                f"selector exited before the owned statement gate: {task.result()}"
                            )
                        await asyncio.sleep(0.02)
                    else:
                        pytest.fail("selector did not reach the owned statement gate")
                    async with owner.begin() as external:
                        await external.execute(
                            text(
                                "UPDATE public.users SET is_active=false WHERE id=:id"
                            ),
                            {"id": ids["annotator"]},
                        )
                        await external.execute(
                            text(
                                "UPDATE project_members SET role='reviewer' WHERE user_id=:id"
                            ),
                            {"id": ids["annotator"]},
                        )
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
                    assert await asyncio.wait_for(asyncio.shield(task), 15) == (
                        ids["admin"],
                        ids["annotator"],
                    )
                    event.remove(target, "before_cursor_execute", gate)
                    assert await IdentityCommandService(
                        db
                    ).resolve_scale_seed_actors() == (
                        ids["admin"],
                        ids["later_annotator"],
                    )
                    record_property(
                        "scale_seed_statement_snapshot",
                        {
                            "backend_pid": waiting,
                            "role": consumer.kw["bind"].url.username,
                            "statement_start_candidate": str(ids["annotator"]),
                            "next_statement_candidate": str(ids["later_annotator"]),
                            "membership_and_activity_changed_together": True,
                        },
                    )
            finally:
                await lock.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
                if task is not None:
                    await asyncio.wait_for(asyncio.shield(task), 15)
    finally:
        if event.contains(target, "before_cursor_execute", gate):
            event.remove(target, "before_cursor_execute", gate)
        await _remove_scale_actors(owner, name, ids)


class _Objects:
    def __init__(self):
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append("head")

    def upload_file(self, path, bucket, key, **kwargs):
        self.calls.append([key, Path(path).read_bytes().decode()])


@pytest.mark.parametrize("scenario", ["empty", "inactive_viewer", "multiple", "limit"])
async def test_image_import_identity_behavior(
    selection_world, scenario, monkeypatch, tmp_path, record_property
):
    owner, consumer = selection_world
    name = "identity-import-" + scenario
    ids = set()
    if scenario != "empty":
        async with owner.begin() as db:
            for n in range(2 if scenario == "multiple" else 1):
                user = User(
                    email=f"{name}-{n}@seed.test",
                    name="import fixture",
                    password_hash="fixture",
                    role="viewer",
                    is_active=False,
                )
                db.add(user)
                await db.flush()
                ids.add(user.id)
    objects = _Objects()
    monkeypatch.setattr(import_images, "get_s3_client", lambda: objects)
    monkeypatch.setattr(
        import_images.settings, "minio_datasets_bucket", "fixture-datasets"
    )
    dispose = AsyncMock()
    monkeypatch.setattr(import_images, "engine", SimpleNamespace(dispose=dispose))
    async with owner() as before:
        ids = set((await before.execute(text("SELECT id FROM public.users"))).scalars())
    session = consumer
    monkeypatch.setattr(import_images, "Session", session)
    images = tmp_path / "pictures"
    images.mkdir()
    for filename in ("a.png", "b.jpg"):
        (images / filename).write_text(filename)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        await import_images.import_to_dataset(
            str(images), name, "identity-input", 1 if scenario == "limit" else None
        )
    async with owner() as db:
        datasets = list(
            (
                await db.execute(
                    text(
                        "SELECT id,created_by,file_count FROM datasets WHERE name=:name"
                    ),
                    {"name": name},
                )
            ).mappings()
        )
        files = list(
            (
                await db.execute(
                    text(
                        "SELECT file_name,file_path FROM dataset_items WHERE dataset_id IN (SELECT id FROM datasets WHERE name=:name) ORDER BY file_name"
                    ),
                    {"name": name},
                )
            ).mappings()
        )
    _replay(
        name,
        {
            "dataset_count": len(datasets),
            "owner_in_candidates": all(row["created_by"] in ids for row in datasets),
            "file_counts": [row["file_count"] for row in datasets],
            "files": [dict(row) for row in files],
            "objects": objects.calls,
            "disposed": dispose.await_count,
            "missing_user": "数据库中没有用户" in output.getvalue(),
            "finished": "导入完成" in output.getvalue(),
        },
        record_property,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "no_admin",
        "no_duty",
        "inactive_only",
        "cross_project",
        "duplicates",
        "ties",
        "infinite",
    ],
)
async def test_scale_seed_selection_behavior(
    selection_world, scenario, monkeypatch, record_property
):
    owner, consumer = selection_world
    name = "selection-" + scenario
    ids = await _scale_actors(owner, name, scenario)
    try:
        monkeypatch.setattr(seed_scale, "MARKER", name)
        dispose = AsyncMock()
        monkeypatch.setattr(seed_scale, "engine", SimpleNamespace(dispose=dispose))
        observed = {"actors": [], "commits": 0}

        @contextlib.asynccontextmanager
        async def session():
            async with consumer() as db:
                real_scalar = db.scalar

                async def scalar(statement, *args, **kwargs):
                    raw = await real_scalar(statement, *args, **kwargs)
                    if "read_scale_seed_" in str(statement):
                        reply = raw if isinstance(raw, dict) else json.loads(raw)
                        value = reply["account_id"]
                        actor = uuid.UUID(value) if value is not None else None
                        key = next(
                            (key for key, value in ids.items() if value == actor), None
                        )
                        if scenario == "ties" and key in {"admin", "later_admin"}:
                            key = "admin_tie"
                        if scenario == "ties" and key in {
                            "annotator",
                            "later_annotator",
                        }:
                            key = "annotator_tie"
                        observed["actors"].append(key)
                    return raw

                db.scalar = scalar

                def committed(_db):
                    observed["commits"] += 1

                event.listen(db.sync_session, "after_commit", committed)
                yield db

        monkeypatch.setattr(seed_scale, "Session", session)
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                await seed_scale.seed(4, 2, 1)
            observed["outcome"] = "completed"
        except SystemExit as exc:
            observed["outcome"] = {"exit": exc.code}
        observed["disposed"] = dispose.await_count
        observed["printed_done"] = (
            "done: 4 tasks / 2 batches / 1 annotated" in stdout.getvalue()
        )
        observed["printed_missing_actors"] = (
            "need at least 1 super_admin" in stdout.getvalue()
        )
        async with owner() as db:
            project = (
                (
                    await db.execute(
                        text(
                            "SELECT id,owner_id,total_tasks FROM projects WHERE name=:name"
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if project:
                owner_key = next(
                    key for key, value in ids.items() if value == project["owner_id"]
                )
                observed["project_owner"] = (
                    "admin_tie" if scenario == "ties" else owner_key
                )
                observed["total_tasks"] = project["total_tasks"]
                observed["tasks"] = await db.scalar(
                    text("SELECT count(*) FROM tasks WHERE project_id=:id"),
                    {"id": project["id"]},
                )
                observed["batches"] = await db.scalar(
                    text("SELECT count(*) FROM task_batches WHERE project_id=:id"),
                    {"id": project["id"]},
                )
                observed["annotations"] = await db.scalar(
                    text("SELECT count(*) FROM annotations WHERE project_id=:id"),
                    {"id": project["id"]},
                )
            else:
                observed["project_owner"] = None
        _replay(name, observed, record_property)
    finally:
        async with owner() as cleanup:
            await seed_scale._purge(cleanup)
            await cleanup.execute(
                text(
                    "DELETE FROM project_members WHERE project_id IN (SELECT id FROM projects WHERE name LIKE :prefix)"
                ),
                {"prefix": "support-" + name + "-%"},
            )
            await cleanup.execute(
                text("DELETE FROM projects WHERE name LIKE :prefix"),
                {"prefix": "support-" + name + "-%"},
            )
            await cleanup.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": list(ids.values())},
            )
            await cleanup.commit()
