"""Owned minimal screenshot graphs for actual Identity consumer acceptance."""

from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.dataset import Dataset, ProjectDataset
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.notification import Notification
from app.services.screenshot_seed_spec import PROJECT_SPECS, SEED_MANAGED_BY
from scripts.screenshot_review_reject_fixture import REASON_TEXT

ROOT = Path(__file__).resolve().parents[3]
IDS = {
    name: uuid.uuid5(uuid.NAMESPACE_URL, "aap-identity-recording:" + name)
    for name in (
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
        "batch",
    )
}
RECORDER_EMAIL = "recording-inactive-viewer@fixture.test"
INITIAL_PREFERENCES = {
    "workbench": {"layout": {"workspace": {"contexts": {"old": {"visible": True}}}}},
    "existing": {"kept": 1},
}


@asynccontextmanager
async def recording_graph(engine):
    ids = IDS
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            for name, role, active in [
                ("owner", "super_admin", True),
                ("recorder", "viewer", False),
                ("annotator", "employee", True),
            ]:
                db.add(
                    User(
                        id=ids[name],
                        email=RECORDER_EMAIL
                        if name == "recorder"
                        else f"{name}@fixture.test",
                        name=name,
                        password_hash="fixture-not-for-login",
                        role=role,
                        is_active=active,
                        preferences=INITIAL_PREFERENCES,
                    )
                )
            await db.flush()
            db.add(
                MLBackendRegistry(
                    id=ids["backend"],
                    name="RapidOCR isolated recording fixture",
                    url="http://never-called.invalid:8765",
                    state="connected",
                )
            )
            await db.flush()
            for prefix, key in [("image", "image_demo"), ("ocr", "ocr_demo")]:
                spec = PROJECT_SPECS[key]
                task_spec = (
                    next(task for task in spec.tasks if task.status == "review")
                    if prefix == "image"
                    else spec.tasks[0]
                )
                project = Project(
                    id=ids[prefix + "_project"],
                    display_id=spec.display_id,
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
                    display_id=spec.dataset_display_id,
                    name="Owned recording " + prefix,
                    created_by=ids["owner"],
                    metadata_={
                        "seed": {"managed_by": SEED_MANAGED_BY, "logical_key": key}
                    },
                )
                db.add_all([project, dataset])
                await db.flush()
                db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
                if prefix == "image":
                    db.add(
                        TaskBatch(
                            id=ids["batch"],
                            project_id=project.id,
                            display_id="B-RECORDING",
                            name="Recording review",
                            status="draft",
                        )
                    )
                    await db.flush()
                db.add(
                    Task(
                        id=ids[prefix + "_task"],
                        project_id=project.id,
                        batch_id=ids["batch"] if prefix == "image" else None,
                        display_id="T-RECORDING-" + prefix,
                        file_name=Path(task_spec.file_path).name,
                        file_path=task_spec.file_path,
                        file_type="image",
                        status="rejected" if prefix == "image" else task_spec.status,
                        assignee_id=ids["annotator"] if prefix == "image" else None,
                        reviewer_id=ids["recorder"] if prefix == "image" else None,
                        annotation_contributor_ids=[str(ids["annotator"])]
                        if prefix == "image"
                        else [],
                        review_contributor_ids=None,
                        review_submitter_id=None,
                        total_predictions=0,
                    )
                )
            await db.flush()
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
            await db.commit()
        yield ids
    finally:
        async with engine.begin() as db:
            project_ids = [ids["image_project"], ids["ocr_project"]]
            await db.execute(
                text("DELETE FROM notifications WHERE user_id=ANY(:ids)"),
                {"ids": [ids["owner"], ids["recorder"], ids["annotator"]]},
            )
            for table in (
                "annotation_feedbacks",
                "async_jobs",
                "failed_predictions",
                "tasks",
                "task_batches",
                "project_datasets",
            ):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE project_id=ANY(:ids)"),
                    {"ids": project_ids},
                )
            await db.execute(
                text("DELETE FROM projects WHERE id=ANY(:ids)"), {"ids": project_ids}
            )
            await db.execute(
                text("DELETE FROM datasets WHERE id=ANY(:ids)"),
                {"ids": [ids["image_dataset"], ids["ocr_dataset"]]},
            )
            await db.execute(
                text("DELETE FROM ml_backend_registry WHERE id=:id"),
                {"id": ids["backend"]},
            )
            await db.execute(
                text("DELETE FROM users WHERE id=ANY(:ids)"),
                {"ids": [ids["owner"], ids["recorder"], ids["annotator"]]},
            )


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


async def recording_snapshot(engine, *, with_aliases=False):
    aliases = {str(value): key for key, value in IDS.items()}
    async with engine.connect() as db:
        jobs = (
            (
                await db.execute(
                    text(
                        "SELECT id,kind,user_id,status,payload,result FROM async_jobs ORDER BY created_at,id"
                    )
                )
            )
            .mappings()
            .all()
        )
        failed = (
            (
                await db.execute(
                    text(
                        "SELECT id,task_id,project_id,ml_backend_id,error_type,message,extra FROM failed_predictions ORDER BY created_at,id"
                    )
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
                        "SELECT id,status,reviewer_id,assignee_id,total_predictions,reviewed_at,reject_reason_type,reject_reason,reviewer_claimed_at,review_round_id,annotation_contributor_ids,review_contributor_ids,review_submitter_id FROM tasks ORDER BY id"
                    )
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
                        "SELECT kind,body,author_id,task_id FROM annotation_feedbacks ORDER BY body"
                    )
                )
            )
            .mappings()
            .all()
        )
        notifications = (
            (
                await db.execute(
                    text(
                        "SELECT type,target_type,target_id,user_id,payload FROM notifications ORDER BY payload::text"
                    )
                )
            )
            .mappings()
            .all()
        )
        users = (
            (
                await db.execute(
                    text(
                        "SELECT id,email,role,is_active,preferences FROM users ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
        batches = (
            (
                await db.execute(
                    text(
                        "SELECT id,total_tasks,completed_tasks,review_tasks FROM task_batches ORDER BY id"
                    )
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
