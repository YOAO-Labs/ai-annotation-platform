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
from tests.test_workspace_consumer_profile_native import _world, _grants, _assert_closed
from app.config import settings
from scripts import screenshot_job_recovery_fixture as jobs
from scripts import screenshot_review_reject_fixture as review
from scripts import restore_screenshot_user_preferences as preferences
from tests.identity_recording_fixture_helpers import (
    ROOT,
    RECORDER_EMAIL,
    INITIAL_PREFERENCES,
    recording_graph,
    recording_snapshot,
    canonical,
)

CAPTURE = os.getenv("AAP_IDENTITY_RECORDING_CAPTURE") == "1"
ORACLE = ROOT / "apps/api/tests/fixtures/identity_recording_legacy.json"
GOLDEN = {} if CAPTURE else json.loads(ORACLE.read_text())["snapshots"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]


def replay(name, value, record_property):
    if CAPTURE:
        directory = ROOT / ".worktree/identity-recording-baseline-cases"
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        )
    else:
        if value != GOLDEN[name]:
            directory = ROOT / ".worktree/recording-replay-mismatches"
            directory.mkdir(exist_ok=True)
            (directory / f"{name}.json").write_text(
                json.dumps(
                    {"actual": value, "expected": GOLDEN[name]},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        assert value == GOLDEN[name]
    record_property("identity_recording_replay", name)


@pytest.fixture
async def recording_consumer(test_engine):
    if CAPTURE:
        yield test_engine.url, test_engine
        return
    async with _world(test_engine) as (_owner, consumer, role, url):
        result = await _grants(
            test_engine,
            ROOT / "infra/docker/identity-recording-fixture-grants.sql",
            role,
        )
        assert result.returncode == 0, result.stderr
        async with consumer() as db:
            await _assert_closed(db)
        yield url, consumer.kw["bind"]


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
async def test_jobs_recording_identity(
    test_engine, recording_consumer, monkeypatch, case, record_property
):
    async with recording_graph(test_engine) as ids:
        monkeypatch.setattr(
            settings,
            "database_url",
            recording_consumer[0].render_as_string(hide_password=False),
        )
        args = SimpleNamespace(
            action="seed",
            project_id=ids["ocr_project"],
            task_id=ids["ocr_task"],
            user_email=RECORDER_EMAIL,
        )
        if case in ("repeat", "cleanup", "missing_user"):
            first = await jobs.run(args)
        async with test_engine.begin() as db:
            if case == "bad_project":
                await db.execute(
                    text("UPDATE projects SET display_id='P-UNMANAGED' WHERE id=:id"),
                    {"id": ids["ocr_project"]},
                )
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
        before = await recording_snapshot(test_engine)
        result = await outcome(jobs.run(args))
        assert ("result" in result) == (case in ("happy", "repeat", "cleanup")), result
        snapshot, aliases = await recording_snapshot(test_engine, with_aliases=True)
        if case == "repeat":
            assert result["result"]["async_job_id"] != first["async_job_id"]
        if "error" in result:
            assert snapshot == before
        replay(
            "jobs-" + case,
            {"outcome": canonical(result, aliases), "state": snapshot},
            record_property,
        )


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
async def test_review_recording_identity(
    test_engine, recording_consumer, monkeypatch, case, record_property
):
    async with recording_graph(test_engine) as ids:
        monkeypatch.setattr(
            settings,
            "database_url",
            recording_consumer[0].render_as_string(hide_password=False),
        )
        args = SimpleNamespace(
            action="cleanup" if case == "cleanup" else "prepare",
            project_id=ids["image_project"],
            task_id=ids["image_task"],
            reviewer_email=RECORDER_EMAIL,
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
                await db.execute(
                    text("UPDATE projects SET display_id='P-UNMANAGED' WHERE id=:id"),
                    {"id": ids["image_project"]},
                )
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
            args.reviewer_email = "annotator@fixture.test"
        if case == "repeat":
            await review.restore(args)
        before = await recording_snapshot(test_engine)
        result = await outcome(review.restore(args))
        assert ("result" in result) == (case in ("happy", "repeat", "cleanup")), result
        snapshot = await recording_snapshot(test_engine)
        if "error" in result:
            assert snapshot == before
        replay(
            "review-" + case, {"outcome": result, "state": snapshot}, record_property
        )


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
        "preferred_dsn",
        "fallback_dsn",
    ],
)
async def test_recording_preferences(
    test_engine, recording_consumer, monkeypatch, tmp_path, case, record_property
):
    async with recording_graph(test_engine) as ids:
        url = recording_consumer[0].render_as_string(hide_password=False)
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("SCREENSHOT_DATABASE_URL", raising=False)
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
            target = uuid.uuid5(uuid.NAMESPACE_URL, "missing-recording-user")
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
            monkeypatch.delenv("DATABASE_URL")
            monkeypatch.delenv("SCREENSHOT_DATABASE_URL", raising=False)
        if case == "wrong_database":
            monkeypatch.setenv(
                "DATABASE_URL",
                test_engine.url.set(database="never_connect_dev").render_as_string(
                    hide_password=False
                ),
            )
        if case == "preferred_dsn":
            monkeypatch.setenv("SCREENSHOT_DATABASE_URL", url)
            monkeypatch.setenv("DATABASE_URL", "never-connect")
        if case == "fallback_dsn":
            monkeypatch.setenv("SCREENSHOT_DATABASE_URL", "")
        calls = []
        make_engine = preferences.create_async_engine

        def real_engine(*args, **kwargs):
            calls.append(kwargs)
            return make_engine(*args, **kwargs)

        monkeypatch.setattr(preferences, "create_async_engine", real_engine)
        before = await recording_snapshot(test_engine)
        result = await outcome(preferences.restore(target, path))
        assert ("result" in result) == (
            case in ("nested", "empty", "same", "preferred_dsn", "fallback_dsn")
        ), result
        if result.get("error") == "FileNotFoundError":
            result["message"] = "<missing preferences file>"
        snapshot = await recording_snapshot(test_engine)
        if "error" in result:
            assert snapshot == before
        replay(
            "preferences-" + case,
            {"outcome": result, "engine_options": calls, "users": snapshot["users"]},
            record_property,
        )


@pytest.mark.parametrize("kind", ["jobs", "review"])
async def test_recording_production_guards(monkeypatch, kind, record_property):
    monkeypatch.setattr(settings, "environment", "production")
    buffer = io.StringIO()
    with redirect_stderr(buffer):
        code = (jobs if kind == "jobs" else review).main()
    replay(
        "guard-" + kind,
        {"exit_code": code, "stderr": buffer.getvalue()},
        record_property,
    )


async def test_jobs_cached_email_keeps_original_id(
    test_engine, recording_consumer, record_property
):
    from sqlalchemy import select
    from sqlalchemy.orm import aliased, load_only
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.user import User
    from app.services.identity_read_relations import account_profiles

    async with recording_graph(test_engine) as ids:
        profile = aliased(User, account_profiles, adapt_on_names=True)
        async with async_sessionmaker(
            recording_consumer[1], expire_on_commit=False
        )() as db:
            cached = await db.scalar(
                select(profile)
                .options(load_only(profile.id, profile.email))
                .where(profile.id == ids["recorder"])
            )
            assert cached.email == RECORDER_EMAIL
            changed_email = "changed-recording@fixture.test"
            async with test_engine.begin() as writer:
                await writer.execute(
                    text("UPDATE public.users SET email=:email WHERE id=:id"),
                    {"email": changed_email, "id": ids["recorder"]},
                )
            result = await jobs.seed(
                db,
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email=changed_email,
            )
            assert cached.email == RECORDER_EMAIL
            await db.commit()
        snapshot, aliases = await recording_snapshot(test_engine, with_aliases=True)
        replay(
            "jobs-cached-email",
            {"result": canonical(result, aliases), "state": snapshot},
            record_property,
        )


async def test_jobs_cached_project_preserves_original_pipeline_and_name(
    test_engine, recording_consumer
):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services.project_access import get_workspace_project

    async with recording_graph(test_engine) as ids:
        async with async_sessionmaker(
            recording_consumer[1], expire_on_commit=False
        )() as db:
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
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email=RECORDER_EMAIL,
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
