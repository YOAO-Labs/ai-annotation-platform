"""Original screenshot tool behavior at the Identity owner boundary."""

from __future__ import annotations
import copy
import io
import json
import os
from contextlib import redirect_stderr
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text
from app.config import settings
from scripts import screenshot_job_recovery_fixture as jobs
from scripts import screenshot_review_reject_fixture as review
from scripts import restore_screenshot_user_preferences as preferences
from tests.identity_recording_fixture_helpers import (
    INITIAL_PREFERENCES,
    recording_graph,
    recording_snapshot,
    recording_owner_session,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]


async def outcome(operation):
    try:
        return {"result": await operation}
    except Exception as error:
        code = getattr(getattr(error, "orig", None), "sqlstate", None)
        return {
            "error": type(error).__name__,
            "sqlstate": code,
            "message": str(error)
            if isinstance(error, (RuntimeError, ValueError, FileNotFoundError))
            and code is None
            else None,
        }


@pytest.mark.parametrize(
    "case",
    [
        "happy",
        "repeat",
        "cleanup",
        "missing_user",
        "missing_argument",
        "bad_project",
        "unmanaged",
        "undeclared_task",
        "bad_backend",
        "no_stage",
    ],
)
async def test_jobs_recording_identity(test_engine, case, record_property):
    async with recording_graph(test_engine) as ids:
        args = SimpleNamespace(
            action="seed",
            project_id=ids["ocr_project"],
            task_id=ids["ocr_task"],
            user_email=ids["recorder_email"],
        )
        if case in ("repeat", "cleanup", "missing_user"):
            first = await jobs.run(args)
        async with test_engine.begin() as db:
            if case == "bad_project":
                args.project_id = uuid.uuid4()
            if case == "unmanaged":
                await db.execute(
                    text("UPDATE datasets SET metadata='{}'::jsonb WHERE id=:id"),
                    {"id": ids["ocr_dataset"]},
                )
            if case == "undeclared_task":
                await db.execute(
                    text("UPDATE tasks SET file_path='outside.png' WHERE id=:id"),
                    {"id": ids["ocr_task"]},
                )
            if case == "bad_backend":
                await db.execute(
                    text(
                        "UPDATE ml_backend_registry SET name='unrelated' WHERE id=:id"
                    ),
                    {"id": ids["backend"]},
                )
            if case == "no_stage":
                await db.execute(
                    text(
                        "UPDATE projects SET preannotate_pipeline='[]'::jsonb WHERE id=:id"
                    ),
                    {"id": ids["ocr_project"]},
                )
        if case == "missing_user":
            args.user_email = "missing@fixture.test"
        if case == "missing_argument":
            args.user_email = None
        if case == "cleanup":
            args.action = "cleanup"
        before = await recording_snapshot(test_engine, ids)
        result = await outcome(jobs.run(args))
        assert ("result" in result) == (case in ("happy", "repeat", "cleanup")), result
        snapshot = await recording_snapshot(test_engine, ids)
        if case == "repeat":
            assert result["result"]["async_job_id"] != first["async_job_id"]
        if "error" in result:
            assert result["error"] == "RuntimeError", result
            assert snapshot == before
        elif case == "cleanup":
            assert snapshot["jobs"] == [] and snapshot["failed"] == []
        else:
            assert len(snapshot["jobs"]) == len(snapshot["failed"]) == 1
            assert snapshot["jobs"][0]["kind"] == "batch_predict"
            assert snapshot["jobs"][0]["status"] == "failed"
            assert snapshot["jobs"][0]["user_id"] == "recorder"
            assert snapshot["jobs"][0]["payload"]["task_id"] == "ocr_task"
            assert snapshot["failed"][0]["error_type"] == "BackendTimeout"
        record_property("identity_recording_case", "jobs-" + case)


@pytest.mark.parametrize(
    "case",
    [
        "happy",
        "repeat",
        "cleanup",
        "missing_user",
        "wrong_reviewer",
        "self_review",
        "unknown_evidence",
        "missing_assignee",
        "bad_project",
        "unmanaged",
        "undeclared_task",
    ],
)
async def test_review_recording_identity(test_engine, case, record_property):
    async with recording_graph(test_engine) as ids:
        args = SimpleNamespace(
            action="cleanup" if case == "cleanup" else "prepare",
            project_id=ids["image_project"],
            task_id=ids["image_task"],
            reviewer_email=ids["recorder_email"],
        )
        async with test_engine.begin() as db:
            if case == "self_review":
                await db.execute(
                    text(
                        "UPDATE tasks SET annotation_contributor_ids=CAST(:ids AS jsonb) WHERE id=:id"
                    ),
                    {
                        "id": ids["image_task"],
                        "ids": json.dumps(
                            [str(ids["annotator"]), str(ids["recorder"])]
                        ),
                    },
                )
            if case == "unknown_evidence":
                await db.execute(
                    text(
                        "UPDATE tasks SET annotation_contributor_ids=NULL WHERE id=:id"
                    ),
                    {"id": ids["image_task"]},
                )
            if case == "missing_assignee":
                await db.execute(
                    text("UPDATE tasks SET assignee_id=NULL WHERE id=:id"),
                    {"id": ids["image_task"]},
                )
            if case == "bad_project":
                args.project_id = uuid.uuid4()
            if case == "unmanaged":
                await db.execute(
                    text("UPDATE datasets SET metadata='{}'::jsonb WHERE id=:id"),
                    {"id": ids["image_dataset"]},
                )
            if case == "undeclared_task":
                await db.execute(
                    text("UPDATE tasks SET file_path='outside.png' WHERE id=:id"),
                    {"id": ids["image_task"]},
                )
        if case == "missing_user":
            args.reviewer_email = "missing@fixture.test"
        if case == "wrong_reviewer":
            args.reviewer_email = ids["annotator_email"]
        if case == "repeat":
            await review.restore(args)
        before = await recording_snapshot(test_engine, ids)
        result = await outcome(review.restore(args))
        assert ("result" in result) == (case in ("happy", "repeat", "cleanup")), result
        snapshot = await recording_snapshot(test_engine, ids)
        if "error" in result:
            assert result["error"] == "RuntimeError", result
            assert snapshot == before
        else:
            task = next(row for row in snapshot["tasks"] if row["id"] == "image_task")
            assert task["status"] == "review"
            assert task["review_contributor_ids"] == ["annotator"]
            assert task["review_submitter_id"] == "annotator"
            assert [row["body"] for row in snapshot["feedback"]] == [
                "unrelated recording feedback"
            ]
            assert len(snapshot["notifications"]) == 1
        record_property("identity_recording_case", "review-" + case)


@pytest.mark.parametrize(
    "case",
    [
        "nested",
        "empty",
        "same",
        "missing_user",
        "null_user",
        "invalid_list",
        "invalid_json",
        "nan",
        "missing_file",
        "missing_dsn",
        "wrong_database",
        "ignored_screenshot_dsn",
        "verified_runtime",
    ],
)
async def test_recording_preferences(
    test_engine, monkeypatch, tmp_path, case, record_property
):
    async with recording_graph(test_engine) as ids:
        runtime_url = (
            os.environ.get("FIXTURE_RUNTIME_DATABASE_URL") or os.environ["DATABASE_URL"]
        )
        monkeypatch.setenv("FIXTURE_RUNTIME_DATABASE_URL", runtime_url)
        target = ids["recorder"]
        value = {
            "workbench": {
                "layout": {"workspace": {"contexts": {"fresh": {"shown": False}}}}
            },
            "未知": {"number": 1.25, "none": None},
        }
        if case == "empty":
            value = {}
        if case == "same":
            value = copy.deepcopy(INITIAL_PREFERENCES)
        if case == "missing_user":
            target = uuid.uuid4()
        if case == "null_user":
            target = None
        if case == "invalid_list":
            value = []
        path = tmp_path / "preferences.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if case == "invalid_json":
            path.write_text("{", encoding="utf-8")
        if case == "nan":
            path.write_text('{"invalid":NaN}', encoding="utf-8")
        if case == "missing_file":
            path.unlink()
        if case == "missing_dsn":
            monkeypatch.delenv("DATABASE_URL", raising=False)
            monkeypatch.delenv("FIXTURE_RUNTIME_DATABASE_URL", raising=False)
        if case == "wrong_database":
            monkeypatch.setenv(
                "FIXTURE_RUNTIME_DATABASE_URL",
                test_engine.url.set(database="never_connect_dev").render_as_string(
                    hide_password=False
                ),
            )
        if case == "ignored_screenshot_dsn":
            monkeypatch.setenv("SCREENSHOT_DATABASE_URL", "never-connect")
        if case == "verified_runtime":
            monkeypatch.setenv("DATABASE_URL", "never-connect")
        before = await recording_snapshot(test_engine, ids)
        result = await outcome(preferences.restore(target, path))
        assert ("result" in result) == (
            case
            in ("nested", "empty", "same", "ignored_screenshot_dsn", "verified_runtime")
        ), result
        snapshot = await recording_snapshot(test_engine, ids)
        if "error" in result:
            if case == "nan":
                assert result["sqlstate"] == "22P02", result
            else:
                expected_error = {
                    "missing_user": "RuntimeError",
                    "null_user": "RuntimeError",
                    "invalid_list": "ValueError",
                    "invalid_json": "JSONDecodeError",
                    "missing_file": "FileNotFoundError",
                    "missing_dsn": "KeyError",
                    "wrong_database": "FixtureSetupRefused",
                }[case]
                assert result["error"] == expected_error, result
            assert snapshot == before
        else:
            for section in (
                "jobs",
                "failed",
                "tasks",
                "feedback",
                "notifications",
                "batches",
            ):
                assert snapshot[section] == before[section]
            old_users = {user["id"]: user for user in before["users"]}
            new_users = {user["id"]: user for user in snapshot["users"]}
            assert set(new_users) == set(old_users)
            for name, previous in old_users.items():
                expected = dict(previous)
                if name == "recorder":
                    expected["preferences"] = value
                assert new_users[name] == expected
        record_property("identity_recording_case", "preferences-" + case)


@pytest.mark.parametrize("kind", ["jobs", "review"])
async def test_recording_production_guards(monkeypatch, kind, record_property):
    monkeypatch.setattr(settings, "environment", "production")
    buffer = io.StringIO()
    with redirect_stderr(buffer):
        code = (jobs if kind == "jobs" else review).main()
    assert code == 2
    record_property("identity_recording_production_guard", kind)


async def test_jobs_cached_email_keeps_original_id(test_engine, record_property):
    from sqlalchemy import select
    from sqlalchemy.orm import aliased, load_only
    from app.db.models.user import User
    from app.services.identity_read_relations import account_profiles

    async with recording_graph(test_engine) as ids:
        profile = aliased(User, account_profiles, adapt_on_names=True)
        async with recording_owner_session() as db:
            cached = await db.scalar(
                select(profile)
                .options(load_only(profile.id, profile.email))
                .where(profile.id == ids["recorder"])
            )
            assert cached.email == ids["recorder_email"]
            changed_email = f"changed-{ids['run_id'].hex}@fixture.test"
            async with test_engine.begin() as writer:
                await writer.execute(
                    text("UPDATE public.users SET email=:email WHERE id=:id"),
                    {"email": changed_email, "id": ids["recorder"]},
                )
            result = await jobs.seed(
                db,
                run_id=ids["run_id"],
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email=changed_email,
            )
            assert result["project_id"] == str(ids["ocr_project"])
            assert cached.email == ids["recorder_email"]
            await db.commit()
        snapshot = await recording_snapshot(test_engine, ids)
        assert len(snapshot["jobs"]) == 1
        assert snapshot["jobs"][0]["user_id"] == "recorder"
        record_property("identity_recording_case", "jobs-cached-email")


async def test_jobs_cached_project_preserves_original_pipeline_and_name(test_engine):
    from app.services.project_access import get_workspace_project

    async with recording_graph(test_engine) as ids:
        async with recording_owner_session() as db:
            cached = await get_workspace_project(db, project_id=ids["ocr_project"])
            original_name = cached.name
            async with test_engine.begin() as writer:
                await writer.execute(
                    text(
                        "UPDATE projects SET name='Changed externally',preannotate_pipeline='[]'::jsonb WHERE id=:id"
                    ),
                    {"id": ids["ocr_project"]},
                )
            result = await jobs.seed(
                db,
                run_id=ids["run_id"],
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email=ids["recorder_email"],
            )
            assert (
                await get_workspace_project(db, project_id=ids["ocr_project"]) is cached
            )
            assert cached.name == original_name
            await db.commit()
        async with test_engine.connect() as owner:
            payload = await owner.scalar(
                text("SELECT payload FROM async_jobs WHERE id=:id"),
                {"id": uuid.UUID(result["async_job_id"])},
            )
            assert payload["project_name"] == original_name
            assert payload["ml_backend_id"] == str(ids["backend"])
