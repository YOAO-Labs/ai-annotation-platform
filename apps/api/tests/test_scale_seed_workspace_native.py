"""Freeze raw SQL scale-seed defaults, staging and cleanup before migration."""

from __future__ import annotations

from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text

from scripts import seed_scale
from tests.test_identity_seed_selection_native import (
    _remove_scale_actors,
    _scale_actors,
)
from tests.test_workspace_consumer_profile_native import _assert_closed, _grants, _world

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = os.getenv("AAP_WORKSPACE_SCALE_CAPTURE") == "1"
ORACLE = ROOT / "apps/api/tests/fixtures/workspace_scale_seed_legacy.json"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL",
    ),
]


@pytest.fixture
async def scale_world(test_engine):
    async with _world(test_engine, apply=not CAPTURE) as (owner, consumer, role, _):
        profiles = ["identity-scale-seed-grants.sql"]
        if not CAPTURE:
            profiles += ["workspace-scale-seed-grants.sql"]
        for profile in profiles:
            result = await _grants(
                test_engine,
                ROOT / "infra/docker" / profile,
                role,
                assets_owner="assets_code",
                labeling_owner="labeling_code",
            )
            assert result.returncode == 0, result.stderr
        if not CAPTURE:
            async with consumer() as db:
                await _assert_closed(db)
        yield owner, consumer


async def _state(owner, marker, ids):
    async with owner() as db:
        project = await db.scalar(
            text("SELECT to_jsonb(p) FROM projects p WHERE name=:marker"),
            {"marker": marker},
        )
        if project is None:
            return {"project": None}
        project_id = project.pop("id")
        assert project.pop("created_at") and project.pop("updated_at")
        assert project.pop("display_id").startswith("P-")
        project["name"] = "<marker>"
        project["owner_id"] = next(
            key for key, value in ids.items() if str(value) == project["owner_id"]
        )
        members = (
            (
                await db.execute(
                    text(
                        "SELECT user_id,role,assigned_by,version,weekly_target FROM project_members WHERE project_id=:id"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .all()
        )
        members = [
            {
                key: next(
                    (label for label, identifier in ids.items() if identifier == value),
                    value,
                )
                for key, value in row.items()
            }
            for row in members
        ]
        counts = {}
        for table in ("tasks", "task_batches", "annotations"):
            counts[table] = await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE project_id=:id"),
                {"id": project_id},
            )
        task_values = (
            (
                await db.execute(
                    text(
                        "SELECT file_name,file_path,file_type,status,sequence_order,is_labeled,total_annotations,total_predictions FROM tasks WHERE project_id=:id ORDER BY sequence_order"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .all()
        )
        batch_values = (
            (
                await db.execute(
                    text(
                        "SELECT name,status FROM task_batches WHERE project_id=:id ORDER BY name"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .all()
        )
        annotations = (
            (
                await db.execute(
                    text(
                        "SELECT class_name,geometry,is_active FROM annotations WHERE project_id=:id ORDER BY id"
                    ),
                    {"id": project_id},
                )
            )
            .mappings()
            .all()
        )
        dataset_count = await db.scalar(
            text("SELECT count(*) FROM datasets WHERE name=:marker"), {"marker": marker}
        )
        item_count = await db.scalar(
            text(
                "SELECT count(*) FROM dataset_items WHERE dataset_id IN (SELECT id FROM datasets WHERE name=:marker)"
            ),
            {"marker": marker},
        )
    return {
        "project": project,
        "members": members,
        "counts": counts,
        "tasks": [dict(row) for row in task_values],
        "batches": [dict(row) for row in batch_values],
        "annotations": [dict(row) for row in annotations],
        "datasets": dataset_count,
        "items": item_count,
    }


@pytest.mark.parametrize(
    "case", ["normal", "zero", "tasks_fail", "annotations_fail", "no_admin"]
)
async def test_scale_seed_original_raw_defaults_and_commit_stages(
    scale_world, case, monkeypatch, record_property
):
    owner, consumer = scale_world
    marker = "workspace-scale-" + case
    ids = await _scale_actors(
        owner, marker, "no_admin" if case == "no_admin" else "normal"
    )
    commits = []
    dispose = AsyncMock()

    @asynccontextmanager
    async def session():
        async with consumer() as db:

            def committed(_):
                commits.append(True)

            event.listen(db.sync_session, "after_commit", committed)
            yield db

    def fail_stage(connection, cursor, statement, parameters, context, executemany):
        if (
            case == "tasks_fail"
            and "aap_labeling_v1.create_scale_seed_tasks" in statement
        ) or (
            case == "annotations_fail"
            and "aap_labeling_v1.create_scale_seed_annotations" in statement
        ):
            raise RuntimeError("controlled scale stage failure")

    target = consumer.kw["bind"].sync_engine
    event.listen(target, "before_cursor_execute", fail_stage)
    monkeypatch.setattr(seed_scale, "MARKER", marker)
    monkeypatch.setattr(seed_scale, "Session", session)
    monkeypatch.setattr(seed_scale, "engine", SimpleNamespace(dispose=dispose))
    try:
        from contextlib import redirect_stdout

        output = io.StringIO()
        outcome = "completed"
        try:
            with redirect_stdout(output):
                await seed_scale.seed(0 if case == "zero" else 4, 2, 1)
        except SystemExit as error:
            assert case == "no_admin"
            outcome = {"exit": error.code}
        except RuntimeError as error:
            assert (
                case in {"tasks_fail", "annotations_fail"}
                and str(error) == "controlled scale stage failure"
            )
            outcome = str(error)
        state = await _state(owner, marker, ids)
        state.update(
            commits=len(commits),
            disposed=dispose.await_count,
            outcome=outcome,
            printed_done="[seed_scale] done:" in output.getvalue(),
        )
        if CAPTURE:
            folder = ROOT / ".worktree/workspace-scale-baseline-cases"
            folder.mkdir(exist_ok=True)
            (folder / (case + ".json")).write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            assert state == json.loads(ORACLE.read_text())["snapshots"][case]
        record_property("workspace_scale_original", case)
    finally:
        event.remove(target, "before_cursor_execute", fail_stage)
        async with owner() as db:
            await seed_scale._purge(db)
        await _remove_scale_actors(owner, marker, ids)


@asynccontextmanager
async def seeded_scale(scale_world, monkeypatch, marker):
    owner, consumer = scale_world
    ids = await _scale_actors(owner, marker, "normal")
    monkeypatch.setattr(seed_scale, "MARKER", marker)
    monkeypatch.setattr(seed_scale, "Session", consumer)
    monkeypatch.setattr(seed_scale, "engine", SimpleNamespace(dispose=AsyncMock()))
    try:
        await seed_scale.seed(4, 2, 1)
        async with owner() as db:
            project_id = await db.scalar(
                text("SELECT id FROM projects WHERE name=:m"), {"m": marker}
            )
            dataset_id = await db.scalar(
                text("SELECT id FROM datasets WHERE name=:m"), {"m": marker}
            )
            task_id, item_id = (
                await db.execute(
                    text(
                        "SELECT id,dataset_item_id FROM tasks WHERE project_id=:id ORDER BY sequence_order LIMIT 1"
                    ),
                    {"id": project_id},
                )
            ).one()
        yield owner, consumer, ids, project_id, dataset_id, task_id, item_id
    finally:
        async with owner() as db:
            await seed_scale._purge(db)
        await _remove_scale_actors(owner, marker, ids)


@pytest.mark.parametrize("kind", ["video", "learning", "camera_link", "chapter"])
async def test_purge_retains_execution_learning_and_auxiliary_evidence(
    scale_world, monkeypatch, kind
):
    import uuid
    from datetime import UTC, datetime
    from fastapi import HTTPException
    from app.db.models.learning_policy import LearningPolicy
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.db.models.video_chapter import VideoChapter
    from app.db.models.video_tracker_job import VideoTrackerJob

    marker = "scale-retain-" + kind
    async with seeded_scale(scale_world, monkeypatch, marker) as (
        owner,
        consumer,
        ids,
        project_id,
        dataset_id,
        task_id,
        item_id,
    ):
        reference = uuid.uuid4()
        foreign_task = uuid.uuid4()
        async with owner.begin() as db:
            if kind == "video":
                db.add(
                    VideoTrackerJob(
                        id=reference,
                        task_id=task_id,
                        dataset_item_id=item_id,
                        status="running",
                        started_at=datetime.now(UTC),
                        model_key="synthetic-unretired",
                        direction="forward",
                        from_frame=0,
                        to_frame=1,
                        prompt={},
                        event_channel="scale-retirement",
                    )
                )
            elif kind == "learning":
                db.add(
                    LearningPolicy(
                        id=reference,
                        project_id=project_id,
                        creator_id=ids["admin"],
                        config={},
                    )
                )
            elif kind == "camera_link":
                support = await db.scalar(
                    text("SELECT id FROM projects WHERE name=:name"),
                    {"name": "support-" + marker + "-0"},
                )
                db.add(
                    Task(
                        id=foreign_task,
                        project_id=support,
                        display_id="CAM-" + uuid.uuid4().hex[:10],
                        file_name="camera.png",
                        file_path="camera.png",
                        file_type="image",
                    )
                )
                await db.flush()
                db.add(
                    TaskDatasetItemLink(
                        id=reference,
                        task_id=foreign_task,
                        dataset_item_id=item_id,
                        role="camera_front",
                        sensor_name="front",
                    )
                )
            else:
                db.add(
                    VideoChapter(
                        id=reference,
                        dataset_item_id=item_id,
                        start_frame=0,
                        end_frame=1,
                        title="Retained",
                        created_by=ids["admin"],
                    )
                )
        table = {
            "video": "video_tracker_jobs",
            "learning": "learning_policies",
            "camera_link": "task_dataset_item_links",
            "chapter": "video_chapters",
        }[kind]
        before = await _state(owner, marker, ids)
        try:
            async with consumer() as db:
                with pytest.raises(HTTPException) as error:
                    await seed_scale._purge(db)
                assert error.value.status_code == 409
                await db.rollback()
            assert await _state(owner, marker, ids) == before
            async with owner() as db:
                assert (
                    await db.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE id=:id"),
                        {"id": reference},
                    )
                    == 1
                )
                if kind == "video":
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=:id"
                            ),
                            {"id": reference},
                        )
                        == 0
                    )
        finally:
            async with owner.begin() as db:
                await db.execute(
                    text(f"DELETE FROM {table} WHERE id=:id"), {"id": reference}
                )
                if kind == "camera_link":
                    await db.execute(
                        text("DELETE FROM tasks WHERE id=:id"), {"id": foreign_task}
                    )


@pytest.mark.parametrize("scope", ["project", "dataset"])
async def test_purge_busy_scope_stops_before_committed_deletion(
    scale_world, monkeypatch, scope
):
    from fastapi import HTTPException

    marker = "scale-busy-" + scope
    async with seeded_scale(scale_world, monkeypatch, marker) as (
        owner,
        consumer,
        ids,
        project_id,
        dataset_id,
        task_id,
        item_id,
    ):
        before = await _state(owner, marker, ids)
        async with owner() as locked:
            table, identifier = (
                ("projects", project_id)
                if scope == "project"
                else ("datasets", dataset_id)
            )
            await locked.execute(
                text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE"),
                {"id": identifier},
            )
            async with consumer() as db:
                with pytest.raises(HTTPException) as error:
                    await seed_scale._purge(db)
                assert error.value.status_code == 409
                assert error.value.detail["reason"] == (
                    "project_busy" if scope == "project" else "source_busy"
                )
                await db.rollback()
            await locked.rollback()
        assert await _state(owner, marker, ids) == before


async def test_node_scale_capabilities_sequence_conflict_and_rollback(
    scale_world, record_property
):
    import asyncio
    import subprocess

    owner, consumer = scale_world
    marker = "scale-node-actors"
    ids = await _scale_actors(owner, marker, "normal")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                str(ROOT / "apps/api/tests/fixtures/workspace_scale_seed_peer.mjs"),
            ],
            input=json.dumps(
                {
                    "database_url": consumer.kw["bind"]
                    .url.set(drivername="postgresql")
                    .render_as_string(hide_password=False),
                    "owner_id": str(ids["admin"]),
                    "annotator_id": str(ids["annotator"]),
                }
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value == {
            "ok": True,
            "fixed_capabilities": 5,
            "conflict_unchanged": True,
            "sequence_preserved": True,
            "rolled_back": True,
        }
        record_property("workspace_scale_node_peer", value)
    finally:
        await _remove_scale_actors(owner, marker, ids)


async def test_scale_profile_is_explicit_and_does_not_expose_private_helpers(
    test_engine,
):
    async with _world(test_engine) as (_, consumer, role, _):
        workspace_signature = "aap_workspace_v1.create_scale_seed_project(uuid,text)"
        async with consumer() as db:
            assert not await db.scalar(
                text("SELECT has_function_privilege(current_user,:s,'EXECUTE')"),
                {"s": workspace_signature},
            )
            assert not await db.scalar(
                text(
                    "SELECT has_function_privilege(current_user,'aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)','EXECUTE')"
                )
            )
        result = await _grants(
            test_engine,
            ROOT / "infra/docker/workspace-scale-seed-grants.sql",
            role,
            assets_owner="assets_code",
            labeling_owner="labeling_code",
        )
        assert result.returncode == 0, result.stderr
        async with consumer() as db:
            await _assert_closed(db)
            for labeling_signature in (
                "aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])",
                "aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)",
                "aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)",
                "aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)",
            ):
                assert await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,:signature,'EXECUTE')"
                    ),
                    {"signature": labeling_signature},
                )
            for relation in (
                "public.tasks",
                "public.task_batches",
                "public.annotations",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,:relation,"
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "pg_catalog.has_any_column_privilege(current_user,:relation,"
                        "'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"relation": relation},
                )
            for sequence in (
                "public.display_seq_tasks",
                "public.display_seq_batches",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_sequence_privilege(current_user,"
                        ":sequence,'USAGE,SELECT,UPDATE')"
                    ),
                    {"sequence": sequence},
                )
            assert await db.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(current_user,"
                    "'aap_labeling_v1.batch_rows','SELECT')"
                )
            )
            await db.execute(
                text(
                    "SELECT id FROM aap_labeling_v1.batch_rows "
                    "WHERE project_id IS NULL ORDER BY display_id LIMIT 1"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT has_function_privilege(current_user,'aap_workspace_v1.read_project_row(uuid)','EXECUTE')"
                )
            )
            assert await db.scalar(
                text("SELECT has_function_privilege(current_user,:s,'EXECUTE')"),
                {"s": workspace_signature},
            )
            assert not await db.scalar(
                text(
                    "SELECT has_function_privilege(current_user,'aap_assets_v1._lock_dataset_source_deletion(uuid,uuid)','EXECUTE')"
                )
            )
        result = await _grants(
            test_engine, ROOT / "infra/docker/workspace-consumer-grants.sql", role
        )
        assert result.returncode == 0, result.stderr
        async with consumer() as db:
            assert not await db.scalar(
                text("SELECT has_function_privilege(current_user,:s,'EXECUTE')"),
                {"s": workspace_signature},
            )
            assert not await db.scalar(
                text(
                    "SELECT has_function_privilege(current_user,'aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)','EXECUTE')"
                )
            )
