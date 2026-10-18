from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, or_, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.bug_report import BugReport
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.export_artifact import ExportArtifact
from app.db.models.image_pyramid import ImagePyramidAsset
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.project_task_view import ProjectTaskView
from app.db.models.storage_connection import StorageConnection
from app.db.models.task import Task
from app.db.models.user import User

pytestmark = pytest.mark.asyncio

_TASK_REFERENCES = text(
    "SELECT task_id FROM aap_labeling_v1.project_task_references(:project_id)"
)


async def _seed(test_engine) -> dict[str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:12].upper()
    fixture = {
        "user_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "dataset_id": uuid.uuid4(),
        "item_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "pool_id": uuid.uuid4(),
        "association_id": uuid.uuid4(),
        "project_storage_id": uuid.uuid4(),
        "global_storage_id": uuid.uuid4(),
    }
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(
            User(
                id=fixture["user_id"],
                email=f"project-cleanup-{suffix}@example.test",
                name="Project cleanup fixture",
                password_hash="test-only",
                role="super_admin",
                is_active=True,
            )
        )
        await session.flush()
        session.add_all(
            [
                Project(
                    id=fixture["project_id"],
                    display_id=f"PC-{suffix}",
                    name="Project cleanup fixture",
                    type_label="Image",
                    type_key="image",
                    data_type="image",
                    owner_id=fixture["user_id"],
                ),
                Dataset(
                    id=fixture["dataset_id"],
                    display_id=f"DC-{suffix}",
                    name=f"project-cleanup-{suffix}",
                    data_type="image",
                    created_by=fixture["user_id"],
                    file_count=1,
                ),
                MLBackendServicePool(
                    id=fixture["pool_id"],
                    name=f"cleanup-pool-{suffix}",
                    enabled=False,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DatasetItem(
                    id=fixture["item_id"],
                    dataset_id=fixture["dataset_id"],
                    file_name="source.jpg",
                    file_path=f"{suffix}/source.jpg",
                    file_type="image",
                    file_size=10,
                ),
                ProjectDataset(
                    id=fixture["association_id"],
                    project_id=fixture["project_id"],
                    dataset_id=fixture["dataset_id"],
                ),
                Task(
                    id=fixture["task_id"],
                    project_id=fixture["project_id"],
                    dataset_item_id=fixture["item_id"],
                    display_id=f"TC-{suffix}",
                    file_name="source.jpg",
                    file_path=f"{suffix}/source.jpg",
                    file_type="image",
                ),
                StorageConnection(
                    id=fixture["project_storage_id"],
                    name="Project connection",
                    kind="s3",
                    config={},
                    scope="owner",
                    project_id=fixture["project_id"],
                    created_by=fixture["user_id"],
                ),
                StorageConnection(
                    id=fixture["global_storage_id"],
                    name="Global connection",
                    kind="s3",
                    config={},
                    scope="global",
                    project_id=None,
                    created_by=fixture["user_id"],
                ),
                ImagePyramidAsset(
                    id=uuid.uuid4(),
                    task_id=fixture["task_id"],
                    profile_version="task-v1",
                ),
                ImagePyramidAsset(
                    id=uuid.uuid4(),
                    dataset_item_id=fixture["item_id"],
                    profile_version="source-v1",
                ),
                ProjectMLBackendPool(
                    id=uuid.uuid4(),
                    project_id=fixture["project_id"],
                    pool_id=fixture["pool_id"],
                    enabled=False,
                ),
                ProjectPipeline(
                    id=uuid.uuid4(),
                    project_id=fixture["project_id"],
                    name="Project pipeline",
                    created_by=fixture["user_id"],
                ),
                ProjectTaskView(
                    id=uuid.uuid4(),
                    project_id=fixture["project_id"],
                    owner_id=fixture["user_id"],
                    name="Project view",
                ),
            ]
        )
        await session.flush()

        report_id = uuid.uuid4()
        session.add_all(
            [
                BugReport(
                    id=report_id,
                    display_id=f"BR-{suffix}",
                    reporter_id=fixture["user_id"],
                    route="/project",
                    user_role="super_admin",
                    project_id=fixture["project_id"],
                    task_id=fixture["task_id"],
                    title="Project task report",
                    description="Retain report while detaching deleted scope.",
                ),
                BugReport(
                    id=uuid.uuid4(),
                    display_id=f"BR2-{suffix}",
                    reporter_id=fixture["user_id"],
                    route="/task",
                    user_role="super_admin",
                    task_id=fixture["task_id"],
                    title="Task-only report",
                    description="Retain and detach task reference.",
                ),
                ExportArtifact(
                    id=uuid.uuid4(),
                    cache_key=uuid.uuid4().hex,
                    project_id=fixture["project_id"],
                    format="json",
                    object_key=f"exports/{suffix}.json",
                    file_count=1,
                    size_bytes=1,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                ),
                AsyncJob(
                    id=uuid.uuid4(),
                    kind="create_tasks",
                    project_id=fixture["project_id"],
                    user_id=fixture["user_id"],
                    status="pending",
                    payload={"schema": "aap-create-tasks/v1"},
                    result={"retained": "pending result"},
                ),
                AsyncJob(
                    id=uuid.uuid4(),
                    kind="create_tasks",
                    project_id=fixture["project_id"],
                    user_id=fixture["user_id"],
                    status="running",
                    payload={"schema": "aap-create-tasks/v1"},
                    result={"retained": "running result"},
                ),
                AsyncJob(
                    id=uuid.uuid4(),
                    kind="create_tasks",
                    project_id=fixture["project_id"],
                    user_id=fixture["user_id"],
                    status="completed",
                    completed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    payload={"schema": "aap-create-tasks/v1"},
                    result={"retained": "completed result"},
                    error_message="historical terminal message",
                ),
                AsyncJob(
                    id=uuid.uuid4(),
                    kind="dataset_import",
                    project_id=fixture["project_id"],
                    user_id=fixture["user_id"],
                    status="running",
                    payload={"retained": "other kind"},
                    result={"retained": "other result"},
                ),
            ]
        )
        await session.commit()
    return fixture


async def _cleanup(test_engine, fixture: dict[str, uuid.UUID]) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            delete(BugReport).where(
                or_(
                    BugReport.task_id == fixture["task_id"],
                    BugReport.project_id == fixture["project_id"],
                )
            )
        )
        await session.execute(
            delete(AsyncJob).where(AsyncJob.project_id == fixture["project_id"])
        )
        await session.execute(
            delete(ExportArtifact).where(
                ExportArtifact.project_id == fixture["project_id"]
            )
        )
        await session.execute(
            delete(Task).where(Task.project_id == fixture["project_id"])
        )
        await session.execute(
            delete(ProjectDataset).where(
                ProjectDataset.project_id == fixture["project_id"]
            )
        )
        await session.execute(
            delete(StorageConnection).where(
                StorageConnection.id.in_(
                    [fixture["project_storage_id"], fixture["global_storage_id"]]
                )
            )
        )
        await session.execute(
            delete(ImagePyramidAsset).where(
                ImagePyramidAsset.dataset_item_id == fixture["item_id"]
            )
        )
        await session.execute(
            delete(ProjectTaskView).where(
                ProjectTaskView.project_id == fixture["project_id"]
            )
        )
        await session.execute(
            delete(ProjectPipeline).where(
                ProjectPipeline.project_id == fixture["project_id"]
            )
        )
        await session.execute(
            delete(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == fixture["project_id"]
            )
        )
        await session.execute(
            delete(Project).where(Project.id == fixture["project_id"])
        )
        await session.execute(
            delete(DatasetItem).where(DatasetItem.dataset_id == fixture["dataset_id"])
        )
        await session.execute(
            delete(Dataset).where(Dataset.id == fixture["dataset_id"])
        )
        await session.execute(
            delete(MLBackendServicePool).where(
                MLBackendServicePool.id == fixture["pool_id"]
            )
        )
        await session.execute(delete(User).where(User.id == fixture["user_id"]))


async def _cleanup_result(
    session: AsyncSession, schema: str, function: str, project_id
):
    return await session.scalar(
        text(f"SELECT {schema}.{function}(:project_id)"),
        {"project_id": project_id},
    )


async def test_frozen_schema_matches_module_owned_function_signatures():
    root = Path(__file__).parents[1] / "alembic/sql"
    schema = json.loads(
        (root / "0198_project_owned_cleanup_contract.schema.json").read_text(
            encoding="utf-8"
        )
    )
    artifact = (root / "0198_project_owned_cleanup_contract.sql").read_text(
        encoding="utf-8"
    )
    functions = {
        "aap_assets_v1.delete_project_owned_records(uuid)",
        "aap_inference_v1.clear_project_ml_backend_pool(uuid)",
        "aap_inference_v1.clear_project_pipelines(uuid)",
        "aap_insights_v1.clear_project_task_views(uuid)",
        "aap_diagnostics_v1.detach_project_references(uuid)",
        "aap_export_v1.clear_project_export_artifacts(uuid)",
        "aap_jobs_v1.detach_project_references(uuid)",
    }
    assert set(schema["x-postgresql-functions"]) == functions
    assert schema["x-postgresql-dependencies"] == [
        "aap_labeling_v1.project_task_references(uuid)"
    ]
    assert all(name.rsplit("(", 1)[0] in artifact for name in functions)
    assert artifact.count("CREATE FUNCTION") == len(functions)
    assert artifact.count("REVOKE ALL ON FUNCTION") == len(functions)
    assert artifact.count("SECURITY DEFINER") == len(functions)
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == len(functions)
    assert artifact.count("CREATE SCHEMA") == 4
    assert "CREATE FUNCTION aap_labeling_v1.project_task_references" not in artifact
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False


async def test_assets_cleanup_uses_labeling_task_reference_capability(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            task_ids = list(
                (
                    await session.execute(
                        _TASK_REFERENCES, {"project_id": fixture["project_id"]}
                    )
                )
                .scalars()
                .all()
            )
            assert task_ids == [fixture["task_id"]]
            assert (
                await _cleanup_result(
                    session,
                    "aap_assets_v1",
                    "delete_project_owned_records",
                    fixture["project_id"],
                )
                == 3
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.project_datasets WHERE project_id=:id"
                    ),
                    {"id": fixture["project_id"]},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_connections WHERE project_id=:id"
                    ),
                    {"id": fixture["project_id"]},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets "
                        "WHERE task_id = :task_id"
                    ),
                    {"task_id": fixture["task_id"]},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets "
                        "WHERE dataset_item_id = :item_id"
                    ),
                    {"item_id": fixture["item_id"]},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_connections WHERE scope='global'"
                    )
                )
                >= 1
            )
    finally:
        await _cleanup(test_engine, fixture)


async def test_project_cleanup_helpers_preserve_retained_history_and_caller_rollback(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            await session.begin()
            assert (
                await _cleanup_result(
                    session,
                    "aap_inference_v1",
                    "clear_project_ml_backend_pool",
                    fixture["project_id"],
                )
                == 1
            )
            assert (
                await _cleanup_result(
                    session,
                    "aap_inference_v1",
                    "clear_project_pipelines",
                    fixture["project_id"],
                )
                == 1
            )
            assert (
                await _cleanup_result(
                    session,
                    "aap_insights_v1",
                    "clear_project_task_views",
                    fixture["project_id"],
                )
                == 1
            )
            assert (
                await _cleanup_result(
                    session,
                    "aap_diagnostics_v1",
                    "detach_project_references",
                    fixture["project_id"],
                )
                == 2
            )
            assert (
                await _cleanup_result(
                    session,
                    "aap_export_v1",
                    "clear_project_export_artifacts",
                    fixture["project_id"],
                )
                == 1
            )
            assert (
                await _cleanup_result(
                    session,
                    "aap_jobs_v1",
                    "detach_project_references",
                    fixture["project_id"],
                )
                == 4
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.bug_reports "
                        "WHERE task_id = :task_id OR project_id = :project_id"
                    ),
                    {
                        "task_id": fixture["task_id"],
                        "project_id": fixture["project_id"],
                    },
                )
                == 0
            )
            jobs = (
                (
                    await session.execute(
                        text(
                            "SELECT kind, status, project_id, completed_at, error_message, result "
                            "FROM public.async_jobs "
                            "WHERE user_id = :user_id ORDER BY kind, status"
                        ),
                        {"user_id": fixture["user_id"]},
                    )
                )
                .mappings()
                .all()
            )
            assert len(jobs) == 4
            assert all(job["project_id"] is None for job in jobs)
            cancelled_results = {
                job["result"]["retained"]
                for job in jobs
                if job["kind"] == "create_tasks" and job["status"] == "cancelled"
            }
            assert cancelled_results == {"pending result", "running result"}
            retained_results = {
                (job["kind"], job["status"], job["result"]["retained"])
                for job in jobs
                if job["status"] != "cancelled"
            }
            assert retained_results == {
                ("create_tasks", "completed", "completed result"),
                ("dataset_import", "running", "other result"),
            }
            completed = next(
                job
                for job in jobs
                if job["kind"] == "create_tasks" and job["status"] == "completed"
            )
            assert completed["completed_at"] == datetime(
                2026, 1, 2, tzinfo=timezone.utc
            )
            assert completed["error_message"] == "historical terminal message"
            await session.rollback()

        async with maker() as verify:
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.project_datasets WHERE project_id=:id"
                    ),
                    {"id": fixture["project_id"]},
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.project_ml_backend_pool WHERE project_id=:id"
                    ),
                    {"id": fixture["project_id"]},
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.export_artifacts WHERE project_id=:id"
                    ),
                    {"id": fixture["project_id"]},
                )
                == 1
            )
            statuses = set(
                (
                    await verify.execute(
                        text("SELECT status FROM public.async_jobs WHERE user_id=:id"),
                        {"id": fixture["user_id"]},
                    )
                )
                .scalars()
                .all()
            )
            assert statuses == {"pending", "running", "completed"}
    finally:
        await _cleanup(test_engine, fixture)
