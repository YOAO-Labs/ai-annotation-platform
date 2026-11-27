"""Owner-backed training finalization retains its snapshot row fences."""

import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.project_task_dataset import ProjectTaskDataset
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.schemas.training_dataset import TrainingDatasetSelection
from app.deps import get_db, get_training_dataset_dispatcher
from app.services import training_datasets as service
from app.services import training_dataset_data as data
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.storage import storage_service
from tests.factory import create_user
from tests.sample_evaluation_original_fixtures import Objects
from app.workers import training_dataset as worker
from tests.test_training_datasets import reviewed_project
from tests.test_workspace_project_authority_locking import _api_url
from tests.training_original_fixtures import image_bytes, register_original


@asynccontextmanager
async def restricted_sessions(test_engine):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the disposable module verifier's real API LOGIN")
    url = _api_url(test_engine)
    engine = create_async_engine(url, hide_parameters=True)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            assert await db.scalar(text("SELECT current_user")) == url.username
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
async def runtime_factory(test_engine):
    async with restricted_sessions(test_engine) as maker:
        yield maker


@pytest.fixture(params=["owner", "restricted"])
async def finalization_caller(test_engine, request):
    if request.param == "owner":
        yield async_sessionmaker(test_engine, expire_on_commit=False)
    else:
        async with restricted_sessions(test_engine) as maker:
            yield maker


@asynccontextmanager
async def committed_project(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    objects = Objects().install(monkeypatch)
    async with maker.begin() as db:
        owner = await create_user(
            db,
            "project_admin",
            f"training-fence-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project, tasks = await reviewed_project(db, owner)
        for index, task in enumerate(tasks):
            await register_original(
                db,
                task,
                owner,
                raw=image_bytes(color=(index * 40, 20, 100)),
                provider=objects,
            )
        project_id, owner_id = project.id, owner.id
        item_ids = [task.dataset_item_id for task in tasks]
        dataset_ids = list(
            await db.scalars(
                select(DatasetItem.dataset_id).where(DatasetItem.id.in_(item_ids))
            )
        )
    try:
        yield maker, owner, project, objects
    finally:
        async with maker.begin() as db:
            for model, predicate in (
                (
                    TrainingStorageDeletion,
                    TrainingStorageDeletion.prefix.like(
                        f"projects/{project_id}/datasets/%"
                    ),
                ),
                (
                    TrainingDatasetVersion,
                    TrainingDatasetVersion.project_id == project_id,
                ),
                (ProjectTaskDataset, ProjectTaskDataset.project_id == project_id),
                (AsyncJob, AsyncJob.project_id == project_id),
                (Annotation, Annotation.project_id == project_id),
                (Task, Task.project_id == project_id),
                (Project, Project.id == project_id),
                (Dataset, Dataset.id.in_(dataset_ids)),
                (User, User.id == owner_id),
            ):
                await db.execute(delete(model).where(predicate))
        assert all(body.closed for body in objects.bodies)


@pytest.mark.parametrize("locked_row", ["task", "annotation"])
async def test_finalization_fails_closed_on_locked_snapshot_rows(
    test_engine, monkeypatch, locked_row, finalization_caller
):
    async with committed_project(test_engine, monkeypatch) as (
        maker,
        owner,
        project,
        objects,
    ):
        owner_id = owner.id
        async with finalization_caller() as db:
            prepared = await service.prepare_selection(
                db,
                project,
                TrainingDatasetSelection(),
                actor_id=owner_id,
                trusted_actor=True,
            )
            assert prepared.report["ready"]
            original_reads = list(objects.calls)
            task_id = uuid.UUID(prepared.snapshot["samples"][0]["task_id"])
            async with maker() as editor:
                if locked_row == "task":
                    statement = select(Task.id).where(Task.id == task_id)
                else:
                    statement = select(Annotation.id).where(
                        Annotation.task_id == task_id
                    )
                assert (await editor.execute(statement.with_for_update())).first()
                with pytest.raises(ContentError) as failure:
                    await service.finalize_selection(db, prepared)
                assert failure.value.code == "storage_original_busy"
                await editor.rollback()
            snapshot, report = await service.finalize_selection(db, prepared)
            assert snapshot == prepared.snapshot
            assert report["digest"] == prepared.report["digest"]
            await db.rollback()
            async with maker.begin() as editor:
                annotation = await editor.scalar(
                    select(Annotation).where(Annotation.task_id == task_id)
                )
                annotation.geometry = {
                    "type": "bbox",
                    "x": 0.2,
                    "y": 0.2,
                    "w": 0.1,
                    "h": 0.1,
                }
                annotation.version += 1
            with pytest.raises(TrainingDatasetError) as changed:
                await service.finalize_selection(db, prepared)
            assert changed.value.code == "preflight_changed"
            assert objects.calls == original_reads


async def test_api_login_publishes_and_replays_immutable_training_package(
    test_engine, runtime_factory, monkeypatch, app_module
):
    from app.core.security import create_access_token
    from app.services import jobs_lifecycle_data as jobs

    async with committed_project(test_engine, monkeypatch) as (
        maker,
        owner,
        project,
        objects,
    ):
        dispatches = []

        async def dispatch(version_id, job_id, attempt_id, *, execution_id):
            dispatches.append((version_id, job_id, attempt_id, execution_id))

        async def request_session():
            async with runtime_factory() as db:
                yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, request_session)
        monkeypatch.setitem(
            app_module.dependency_overrides,
            get_training_dataset_dispatcher,
            lambda: SimpleNamespace(dispatch_dataset=dispatch),
        )
        token = create_access_token(subject=str(owner.id), role=owner.role)
        headers = {"Authorization": f"Bearer {token}"}
        path = f"/api/v1/projects/{project.id}/training-datasets"
        prefix = f"projects/{project.id}/datasets/"
        bucket = storage_service.training_bucket
        try:
            async with runtime_factory() as db:
                for name in (
                    "training_dataset_versions",
                    "project_task_datasets",
                    "task_dataset_version_memberships",
                    "training_dataset_analyses",
                    "training_dataset_analyzed_samples",
                    "training_storage_deletions",
                ):
                    async with db.begin():
                        assert not await db.scalar(
                            text(
                                "SELECT has_any_column_privilege(current_user, :relation, 'SELECT,INSERT,UPDATE,REFERENCES')"
                            ),
                            {"relation": f"public.{name}"},
                        )
                        with pytest.raises(DBAPIError) as denied:
                            async with db.begin_nested():
                                await db.execute(
                                    text(f"SELECT * FROM public.{name} LIMIT 1")
                                )
                        assert denied.value.orig.sqlstate == "42501"
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'yoao_learning_v1.training_original_history()', 'EXECUTE')"
                    )
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                preflight = await client.post(
                    path + ":preflight", headers=headers, json={}
                )
                assert preflight.status_code == 200, preflight.text
                assert preflight.json()["ready"]
                body = {
                    "name": "Retained package",
                    "request_id": str(uuid.uuid4()),
                    "preflight_digest": preflight.json()["digest"],
                }
                created = await client.post(path, headers=headers, json=body)
                assert created.status_code == 202, created.text
                replay = await client.post(path, headers=headers, json=body)
                assert replay.status_code == 202, replay.text
                assert replay.json() == created.json()
                assert replay.json()["version_ordinal"] == 1
                assert len(dispatches) == 1
                version_id, job_id, attempt_id, execution_id = dispatches[0]
                assert str(version_id) == created.json()["id"]
                conflict = await client.post(
                    path, headers=headers, json={**body, "name": "Other"}
                )
                assert conflict.status_code == 409, conflict.text
                await worker.build_dataset(
                    version_id,
                    job_id,
                    attempt_id,
                    runtime_factory,
                    celery_task_id=execution_id,
                )
                async with runtime_factory() as db:
                    published = await data.read_version(
                        db, version_id, project_id=project.id
                    )
                    assert published.status == "ready", published.error_message
                    job = await jobs.get_job(db, job_id)
                    assert job.status == "completed"
                    facts = await data.read_version_facts(
                        db, version_id, project_id=project.id
                    )
                    assert (
                        "samples" not in facts.snapshot
                        and "authorization" not in facts.snapshot
                    )
                    assert not await data.publish_version(
                        db,
                        published,
                        manifest={**published.manifest, "summary": {}},
                        package_key=published.package_key,
                        package_sha256="0" * 64,
                        size_bytes=published.size_bytes,
                    )
                    await db.commit()
                prefix, bucket = (
                    published.storage_ref["prefix"],
                    published.storage_ref["bucket"],
                )
                read = storage_service.client.get_object
                with read(Bucket=bucket, Key=published.package_key)["Body"] as stream:
                    package = stream.read()
                assert hashlib.sha256(package).hexdigest() == published.package_sha256
                objects.objects.clear()
                await worker.build_dataset(
                    version_id,
                    job_id,
                    attempt_id,
                    runtime_factory,
                    celery_task_id=execution_id,
                )
                with read(Bucket=bucket, Key=published.package_key)["Body"] as stream:
                    assert stream.read() == package
                insights = f"{path}/{version_id}/insights"
                page = await client.get(
                    f"{insights}/samples?limit=1&offset=1", headers=headers
                )
                assert page.status_code == 200, page.text
                ordered = sorted(
                    sample["task_id"] for sample in published.manifest["samples"]
                )
                assert page.json()["total"] == len(ordered)
                assert [
                    sample["task_id"] for sample in page.json()["items"]
                ] == ordered[1:2]
                sample = await client.get(
                    f"{insights}/samples/{ordered[1]}", headers=headers
                )
                assert sample.status_code == 200, sample.text
                assert sample.json()["source_lineage"]["kind"] == "managed_original"
                async with maker() as db:
                    persisted = await db.get(TrainingDatasetVersion, version_id)
                    assert persisted.snapshot == published.snapshot
                    assert persisted.manifest == published.manifest
                    assert persisted.storage_ref == published.storage_ref
                removed = await client.delete(f"{path}/{version_id}", headers=headers)
                assert removed.status_code == 204, removed.text
                assert storage_service.list_objects(prefix, bucket=bucket) == []
        finally:
            storage_service.delete_prefix(prefix, bucket=bucket)


async def test_uncommitted_training_tombstone_cannot_purge_live_owner(
    test_engine, runtime_factory, monkeypatch
):
    from app.services import training_dataset_storage, training_storage_cleanup
    from app.services.training_dataset_bridge import content_digest

    async with committed_project(test_engine, monkeypatch) as (
        maker,
        owner,
        project,
        _,
    ):
        async with maker.begin() as db:
            snapshot = {"schema": 1, "classes": ["car"], "samples": []}
            digest = content_digest(snapshot)
            version = await data.insert_version(
                db,
                data.NewVersion(
                    project.id,
                    owner.id,
                    None,
                    uuid.uuid4(),
                    digest,
                    "Staged cleanup",
                    snapshot,
                    digest,
                    {},
                ),
            )
            await training_dataset_storage.allocate(db, version)
            await db.execute(
                update(TrainingDatasetVersion)
                .where(TrainingDatasetVersion.id == version.id)
                .values(status="failed")
            )
        async with runtime_factory() as db:
            version = await data.read_version(db, version.id, lock=True)
            prefix = training_dataset_storage.prefix_for(project.id, version.id)
            intent = await training_storage_cleanup.stage_deletion(db, version, prefix)
            async with runtime_factory() as cleanup_db:
                assert not await training_storage_cleanup.purge_requested(
                    cleanup_db, intent
                )
            assert not await training_storage_cleanup.purge_requested(db, intent)
            await db.rollback()
        async with maker() as db:
            assert await db.get(TrainingStorageDeletion, intent) is None
            assert await db.get(TrainingDatasetVersion, version.id) is not None
