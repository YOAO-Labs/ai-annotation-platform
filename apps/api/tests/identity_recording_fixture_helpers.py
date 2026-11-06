"""Owned minimal screenshot graphs for actual Identity consumer acceptance."""

from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import json
import os
import sys
import uuid

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.notification import Notification
from app.services.screenshot_seed_spec import PROJECT_SPECS, SEED_MANAGED_BY
from scripts.screenshot_review_reject_fixture import REASON_TEXT
from tests.seed_owner_support import private_seed_owner
from tests.test_workspace_consumer_profile_native import _assert_closed, _grants, _world

ROOT = Path(__file__).resolve().parents[3]
ID_NAMES = (
    "owner",
    "recorder",
    "annotator",
    "backend",
    "image_project",
    "ocr_project",
    "image_task",
    "ocr_task",
    "image_dataset",
    "ocr_dataset",
    "image_item",
    "ocr_item",
    "batch",
)
RECORDER_EMAIL = "recording-inactive-viewer@fixture.test"
INITIAL_PREFERENCES = {
    "workbench": {"layout": {"workspace": {"contexts": {"old": {"visible": True}}}}},
    "existing": {"kept": 1},
}


@pytest.fixture
async def recording_consumer(test_engine):
    """A fixture-only LOGIN for the recording preference capability checks."""
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


@asynccontextmanager
async def recording_owner_session():
    """Use only the coordinator-verified private owner for fixture commands."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from fixture_suite_coordinator import verified_fixture_owner_url

    engine = create_async_engine(
        await verified_fixture_owner_url(), hide_parameters=True
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


def recording_sources():
    """Resolve qualified screenshot assets without downloading or rewriting them."""
    from scripts.seed_assets import default_cache_dir, ensure_profile, select_profile
    from scripts.seed_screenshot_assets import (
        GENERATED_REVISION,
        REQUIRED_SOURCE_IDS,
        _source_digest,
        _validate,
    )

    selected = select_profile(
        "screenshots", required_ids={"rapidocr-image", *REQUIRED_SOURCE_IDS}
    )
    assets = ensure_profile("screenshots", offline=True, assets=selected)
    sources = {
        name: assets[name].root.joinpath(*assets[name].asset.required_files[0].parts)
        for name in REQUIRED_SOURCE_IDS
    }
    prepared = _validate(
        default_cache_dir() / "prepared" / GENERATED_REVISION,
        source_sha256=_source_digest(sources),
    )
    spec = PROJECT_SPECS["image_demo"]
    task = next(task for task in spec.tasks if task.status == "review")
    relative = task.file_path.removeprefix(spec.storage_prefix)
    return {
        "image": prepared.image_root / "images" / relative,
        "ocr": assets["rapidocr-image"].root / "ch_en_num.jpg",
    }


@asynccontextmanager
async def recording_graph(engine):
    from app.services import fixture_run_ownership as ownership

    sources = recording_sources()
    for path in sources.values():
        if not path.is_file():
            raise FileNotFoundError(f"qualified screenshot source is missing: {path}")
    run_id = uuid.uuid4()
    ids = {name: uuid.uuid4() for name in ID_NAMES}
    ids["run_id"] = run_id
    ids["recorder_email"] = f"recording-{run_id.hex}-reviewer@fixture.test"
    ids["owner_email"] = f"recording-{run_id.hex}-owner@fixture.test"
    ids["annotator_email"] = f"recording-{run_id.hex}-annotator@fixture.test"
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from fixture_suite_coordinator import verified_fixture_owner_url
    from tests.identity_e2e_support import fixture_routing_owner

    monkeypatch = pytest.MonkeyPatch()

    async with fixture_routing_owner(engine, monkeypatch), private_seed_owner() as owner:
        owner_url = await verified_fixture_owner_url()
        parsed = make_url(owner_url)
        assert (parsed.host, parsed.port or 5432, parsed.database) == (
            engine.url.host,
            engine.url.port or 5432,
            engine.url.database,
        )
        await owner.prepare_suite(run_id, "screenshot")
        owner_engine = create_async_engine(owner_url, hide_parameters=True)
        previous_run = os.environ.get("SCREENSHOT_FIXTURE_RUN_ID")
        originals = {}
        failure = None
        try:
            async with async_sessionmaker(owner_engine, expire_on_commit=False)() as db:
                admission = await ownership.admit(db, run_id, "recording_fixture")
                await db.commit()
                try:
                    for name, role, active, key in (
                        ("owner", "super_admin", True, "admin"),
                        ("recorder", "viewer", False, "reviewer"),
                        ("annotator", "employee", True, "annotator"),
                    ):
                        db.add(
                            User(
                                id=ids[name],
                                email=ids[name + "_email"],
                                name=name,
                                password_hash="fixture-not-for-login",
                                role=role,
                                is_active=active,
                            )
                        )
                        await db.flush()
                        await ownership.reserve(
                            db,
                            run_id,
                            "user",
                            ids[name],
                            logical_key=key,
                            published=True,
                        )
                    await db.execute(
                        text(
                            "UPDATE public.identity_account_preferences "
                            "SET preferences=CAST(:preferences AS jsonb) "
                            "WHERE user_id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {
                            "preferences": json.dumps(INITIAL_PREFERENCES),
                            "ids": [
                                ids[name] for name in ("owner", "recorder", "annotator")
                            ],
                        },
                    )
                    db.add(
                        MLBackendRegistry(
                            id=ids["backend"],
                            name="RapidOCR isolated recording fixture",
                            url=f"http://recording-{run_id.hex}.invalid:8765",
                            state="connected",
                        )
                    )
                    await db.flush()
                    await ownership.reserve(
                        db,
                        run_id,
                        "registry",
                        ids["backend"],
                        logical_key="recording-ocr",
                        published=True,
                    )
                    await db.commit()
                    for prefix, key in (("image", "image_demo"), ("ocr", "ocr_demo")):
                        spec = PROJECT_SPECS[key]
                        task_spec = (
                            next(task for task in spec.tasks if task.status == "review")
                            if prefix == "image"
                            else spec.tasks[0]
                        )
                        relative = task_spec.file_path.removeprefix(spec.storage_prefix)
                        payload = sources[prefix].read_bytes()
                        with Image.open(sources[prefix]) as image:
                            width, height = image.size
                        metadata = {
                            "seed": {
                                "managed_by": SEED_MANAGED_BY,
                                "logical_key": key,
                                "run_id": str(run_id),
                            }
                        }
                        project = Project(
                            id=ids[prefix + "_project"],
                            display_id="P-" + ids[prefix + "_project"].hex[:12],
                            name="Owned recording " + prefix,
                            type_label="Image",
                            type_key="image-det",
                            owner_id=ids["owner"],
                            preannotate_pipeline=[
                                {
                                    "stage": 1,
                                    "ml_backend_id": str(ids["backend"]),
                                    "task_type": "ocr",
                                    "model_id": "ocr-e2e",
                                }
                            ],
                        )
                        dataset = Dataset(
                            id=ids[prefix + "_dataset"],
                            display_id="DS-" + ids[prefix + "_dataset"].hex[:12],
                            name="Owned recording " + prefix,
                            created_by=ids["owner"],
                            metadata_=metadata,
                        )
                        db.add_all([project, dataset])
                        await db.flush()
                        await ownership.reserve(
                            db,
                            run_id,
                            "project",
                            project.id,
                            logical_key=key,
                            published=True,
                        )
                        await ownership.reserve_original_dataset(
                            db, run_id, dataset.id, logical_key=key
                        )
                        await ownership.publish_resource(
                            db, run_id, "dataset", dataset.id
                        )
                        db.add(
                            ProjectDataset(project_id=project.id, dataset_id=dataset.id)
                        )
                        await ownership.reserve(
                            db,
                            run_id,
                            "item",
                            ids[prefix + "_item"],
                            logical_key=key + "/" + relative,
                        )
                        await db.commit()
                        item = DatasetItem(
                            id=ids[prefix + "_item"],
                            dataset_id=dataset.id,
                            file_name=Path(relative).name,
                            file_path="",
                            file_type="image",
                            file_size=len(payload),
                            width=width,
                            height=height,
                            metadata_={"storage_relative_path": relative},
                        )
                        allocation, original = await ownership.create_original(
                            db,
                            run_id=run_id,
                            admission_id=admission,
                            item=item,
                            logical_path=relative,
                            payload=payload,
                            content_type="image/jpeg",
                            requested_by=ids["owner"],
                        )
                        item.content_hash = original.sha256
                        await ownership.attach_original(db, item, allocation, original)
                        await ownership.publish_resource(db, run_id, "item", item.id)
                        if prefix == "image":
                            db.add(
                                TaskBatch(
                                    id=ids["batch"],
                                    project_id=project.id,
                                    display_id="B-" + ids["batch"].hex[:12],
                                    name="Recording review",
                                    status="draft",
                                )
                            )
                            await db.flush()
                        task = Task(
                            id=ids[prefix + "_task"],
                            project_id=project.id,
                            dataset_item_id=item.id,
                            batch_id=ids["batch"] if prefix == "image" else None,
                            display_id="T-" + ids[prefix + "_task"].hex[:12],
                            file_name=item.file_name,
                            file_path=item.file_path,
                            file_type="image",
                            status="rejected"
                            if prefix == "image"
                            else task_spec.status,
                            assignee_id=ids["annotator"] if prefix == "image" else None,
                            reviewer_id=ids["recorder"] if prefix == "image" else None,
                            annotation_contributor_ids=[str(ids["annotator"])]
                            if prefix == "image"
                            else [],
                            review_contributor_ids=None,
                            review_submitter_id=None,
                            total_predictions=0,
                        )
                        db.add(task)
                        await db.flush()
                        await ownership.reserve(
                            db,
                            run_id,
                            "task",
                            task.id,
                            logical_key=key + "/" + task_spec.key,
                            published=True,
                        )
                        originals[prefix] = (metadata, task.file_path)
                        await db.commit()
                    for reason in (REASON_TEXT, "unrelated recording feedback"):
                        db.add(
                            AnnotationFeedback(
                                kind="reject",
                                anchor_type="task",
                                project_id=ids["image_project"],
                                task_id=ids["image_task"],
                                body=reason,
                                author_id=ids["recorder"],
                            )
                        )
                        db.add(
                            Notification(
                                user_id=ids["annotator"],
                                type="task.rejected",
                                target_type="task",
                                target_id=ids["image_task"],
                                payload={"reject_reason": reason},
                            )
                        )
                    await ownership.settle(db, run_id, admission)
                    await db.commit()
                except BaseException as build_failure:
                    await db.rollback()
                    try:
                        await ownership.settle(db, run_id, admission)
                        await db.commit()
                    except BaseException as settlement_failure:
                        raise BaseExceptionGroup(
                            "Recording graph creation and settlement failed",
                            [build_failure, settlement_failure],
                        ) from None
                    raise
            os.environ["SCREENSHOT_FIXTURE_RUN_ID"] = str(run_id)
            yield ids
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if previous_run is None:
                os.environ.pop("SCREENSHOT_FIXTURE_RUN_ID", None)
            else:
                os.environ["SCREENSHOT_FIXTURE_RUN_ID"] = previous_run
            try:
                async with owner_engine.begin() as db:
                    for prefix, (metadata, path) in originals.items():
                        await db.execute(
                            text(
                                "UPDATE datasets SET metadata=CAST(:metadata AS jsonb) WHERE id=:id"
                            ),
                            {
                                "metadata": json.dumps(metadata),
                                "id": ids[prefix + "_dataset"],
                            },
                        )
                        await db.execute(
                            text("UPDATE tasks SET file_path=:path WHERE id=:id"),
                            {"path": path, "id": ids[prefix + "_task"]},
                        )
                response = await owner.post(
                    "/api/v1/__test/seed/screenshot-cleanup",
                    json={"run_id": str(run_id)},
                )
                assert response.status_code == 200, response.text
                await owner.shutdown_authority(run_id)
            except BaseException as cleanup_failure:
                if failure is not None:
                    raise BaseExceptionGroup(
                        "Recording graph and exact cleanup failed",
                        [failure, cleanup_failure],
                    ) from None
                raise
            finally:
                await owner_engine.dispose()


def canonical(value, aliases):
    if isinstance(value, datetime):
        return "<timestamp>"
    if isinstance(value, uuid.UUID):
        return aliases.get(str(value), str(value))
    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, (list, tuple)):
        return [canonical(item, aliases) for item in value]
    if isinstance(value, dict):
        return {k: canonical(v, aliases) for k, v in value.items()}
    return value


async def recording_snapshot(engine, ids, *, with_aliases=False):
    aliases = {str(ids[name]): name for name in ID_NAMES}
    aliases.update(
        {
            ids["owner_email"]: "owner@fixture.test",
            ids["recorder_email"]: RECORDER_EMAIL,
            ids["annotator_email"]: "annotator@fixture.test",
        }
    )
    scope = {
        "projects": [ids["image_project"], ids["ocr_project"]],
        "tasks": [ids["image_task"], ids["ocr_task"]],
        "users": [ids["owner"], ids["recorder"], ids["annotator"]],
    }
    async with engine.connect() as db:
        jobs = (
            (
                await db.execute(
                    text(
                        "SELECT id,kind,user_id,status,payload,result FROM async_jobs "
                        "WHERE project_id=ANY(:projects) OR user_id=ANY(:users) "
                        "ORDER BY created_at,id"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        failed = (
            (
                await db.execute(
                    text(
                        "SELECT id,task_id,project_id,ml_backend_id,error_type,message,extra "
                        "FROM failed_predictions WHERE project_id=ANY(:projects) "
                        "OR task_id=ANY(:tasks) ORDER BY created_at,id"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        for index, row in enumerate(jobs):
            aliases[str(row["id"])] = "job-" + str(index)
        for index, row in enumerate(failed):
            aliases[str(row["id"])] = "failed-" + str(index)
        tasks = (
            (
                await db.execute(
                    text(
                        "SELECT id,status,reviewer_id,assignee_id,total_predictions,reviewed_at,"
                        "reject_reason_type,reject_reason,reviewer_claimed_at,review_round_id,"
                        "annotation_contributor_ids,review_contributor_ids,review_submitter_id "
                        "FROM tasks WHERE project_id=ANY(:projects) OR id=ANY(:tasks) ORDER BY id"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        for row in tasks:
            if row["review_round_id"]:
                aliases[str(row["review_round_id"])] = "review-round"
        feedback = (
            (
                await db.execute(
                    text(
                        "SELECT kind,body,author_id,task_id FROM annotation_feedbacks "
                        "WHERE project_id=ANY(:projects) OR task_id=ANY(:tasks) "
                        "OR author_id=ANY(:users) ORDER BY body"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        notifications = (
            (
                await db.execute(
                    text(
                        "SELECT type,target_type,target_id,user_id,payload FROM notifications "
                        "WHERE user_id=ANY(:users) OR (target_type='task' AND target_id=ANY(:tasks)) "
                        "ORDER BY payload::text"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        users = (
            (
                await db.execute(
                    text(
                        "SELECT users.id,email,role,is_active,preferences FROM users "
                        "JOIN identity_account_preferences AS prefs ON prefs.user_id=users.id "
                        "WHERE users.id=ANY(:users) ORDER BY users.id"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        batches = (
            (
                await db.execute(
                    text(
                        "SELECT id,total_tasks,completed_tasks,review_tasks FROM task_batches "
                        "WHERE project_id=ANY(:projects) ORDER BY id"
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
    result = {
        "jobs": [dict(row) for row in jobs],
        "failed": [dict(row) for row in failed],
        "tasks": [dict(row) for row in tasks],
        "feedback": [dict(row) for row in feedback],
        "notifications": [dict(row) for row in notifications],
        "users": [dict(row) for row in users],
        "batches": [dict(row) for row in batches],
    }
    value = canonical(result, aliases)
    return (value, aliases) if with_aliases else value
