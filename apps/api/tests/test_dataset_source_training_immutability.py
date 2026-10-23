"""Live object-store evidence that source synchronization cannot rewrite a freeze."""

import copy
import hashlib
import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.dataset_source import SourceRevisionAccept
from app.services import dataset_scans as scans
from app.services import dataset_source_changes as changes
from app.services import dataset_source_revisions as revisions
from app.services import filesystem_storage as fs
from app.services import training_datasets
from app.services.dataset import DatasetService
from app.services.dataset_source_access import capture_dataset_write
from app.services.dataset_storage_keys import managed_dataset_prefix
from app.services.storage import storage_service
from app.workers import training_dataset as worker
from tests.factory import create_project
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import (
    accept_all_files,
    picture,
    preflight,
    rescan,
)


def object_bytes(bucket, key):
    response = storage_service.client.get_object(Bucket=bucket, Key=key)
    try:
        return response["Body"].read()
    finally:
        response["Body"].close()


def frozen_objects(prefix):
    return {
        obj["key"]: object_bytes(storage_service.training_bucket, obj["key"])
        for obj in storage_service.list_objects(
            prefix, bucket=storage_service.training_bucket
        )
    }


@pytest.mark.parametrize("publication_fault", ["none", "ack", "ack_and_lookup"])
async def test_managed_source_changes_preserve_ready_training_version(
    db_session, content_world, super_admin, httpx_client, monkeypatch, publication_fault
):
    db, world = db_session, content_world
    lose_commit_ack = publication_fault != "none"
    dataset_id = world[3].id
    version_prefix = None
    try:
        world[5].ownership_mode = "managed_copy"
        await db.commit()
        (world[0] / "other.png").write_bytes(picture("blue"))
        world = await rescan(db, world)
        original = await accept_all_files(db, world)
        project = await create_project(db, owner_id=world[2].id)
        authority = await capture_dataset_write(db, dataset_id, world[2], None)
        await DatasetService(db).link_project(
            dataset_id, project.id, directory_authority=authority
        )
        tasks = list(
            await db.scalars(select(Task).where(Task.project_id == project.id))
        )
        assert len(tasks) == 2
        annotations = []
        for task in tasks:
            task.status = "completed"
            task.reviewed_at = datetime.now(timezone.utc)
            task.is_labeled = True
            annotation = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=world[2].id,
                class_name="car",
                tool_unit_id="bbox",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            )
            db.add(annotation)
            annotations.append(annotation)
        await db.commit()
        old_tasks = {
            t.id: (
                t.dataset_item_id,
                t.file_path,
                t.status,
                t.is_labeled,
                t.reviewed_at,
            )
            for t in tasks
        }
        old_labels = {a.id: copy.deepcopy(a.geometry) for a in annotations}
        old_items = {
            row["item_id"]: copy.deepcopy(
                (await db.get(DatasetItem, row["item_id"])).metadata_
            )
            for row in original.values()
        }

        async def no_dispatch(*args):
            pass

        monkeypatch.setattr(training_datasets, "dispatch_dataset", no_dispatch)
        url = f"/api/v1/projects/{project.id}/training-datasets"
        report = await httpx_client.post(
            url + ":preflight", headers=headers(super_admin), json={}
        )
        assert report.status_code == 200 and report.json()["ready"], report.text
        response = await httpx_client.post(
            url,
            headers=headers(super_admin),
            json={
                "name": "Frozen before source sync",
                "request_id": str(uuid.uuid4()),
                "preflight_digest": report.json()["digest"],
            },
        )
        assert response.status_code == 202, response.text
        version = await db.get(TrainingDatasetVersion, uuid.UUID(response.json()["id"]))
        version_id, job_id, attempt_id = (
            version.id,
            version.async_job_id,
            version.attempt_id,
        )
        version_prefix = f"projects/{project.id}/datasets/{version.id}/"
        raised = False
        real_commit = db.commit
        real_scalar = db.scalar
        lookup_failed = False

        async def uncertain_lookup(statement, *args, **kwargs):
            nonlocal lookup_failed
            if (
                publication_fault == "ack_and_lookup"
                and raised
                and "training_dataset_versions.package_key" in str(statement)
            ):
                lookup_failed = True
                raise RuntimeError("publication verification temporarily unavailable")
            return await real_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(db, "scalar", uncertain_lookup)

        async def commit_then_lose_ack():
            nonlocal raised
            publication = (
                version.status == "ready" and bool(version.package_key) and not raised
            )
            await real_commit()
            if publication and lose_commit_ack:
                raised = True
                raise RuntimeError("publication commit acknowledgement lost")

        monkeypatch.setattr(db, "commit", commit_then_lose_ack)

        @asynccontextmanager
        async def sessions():
            yield db

        await worker.build_dataset(version_id, job_id, attempt_id, sessions)
        monkeypatch.setattr(db, "commit", real_commit)
        monkeypatch.setattr(db, "scalar", real_scalar)
        assert lookup_failed == (publication_fault == "ack_and_lookup")
        await db.refresh(version)
        assert version.status == "ready", version.error_message
        assert raised == lose_commit_ack
        assert (await db.get(AsyncJob, job_id)).status == "completed"
        columns = (
            "snapshot",
            "request_digest",
            "snapshot_digest",
            "preparation_report",
            "manifest",
            "status",
            "async_job_id",
            "attempt_id",
            "package_key",
            "package_sha256",
            "size_bytes",
            "completed_at",
        )
        frozen = {key: copy.deepcopy(getattr(version, key)) for key in columns}
        package = object_bytes(storage_service.training_bucket, version.package_key)
        assert hashlib.sha256(package).hexdigest() == version.package_sha256
        objects = frozen_objects(version_prefix)
        assert len(objects) == 3  # Two frozen media objects and the published ZIP.
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest == version.manifest and len(manifest["samples"]) == 2
            for sample in manifest["samples"]:
                assert (
                    hashlib.sha256(archive.read(sample["image_path"])).hexdigest()
                    == sample["sha256"]
                )
                assert (
                    hashlib.sha256(archive.read(sample["label_path"])).hexdigest()
                    == sample["label_sha256"]
                )

        for record in world[2:]:
            await db.refresh(record)
        await db.refresh(project)
        (world[0] / "图片 空格.png").write_bytes(picture("green"))
        (world[0] / "other.png").unlink()
        world = await rescan(db, world)
        registration = await preflight(db, world)
        page = await changes.page(db, registration, None, 20, 0)
        assert page["counts"]["changed"] == page["counts"]["not_seen"] == 1
        change = next(c for c in page["items"] if c["kind"] == "changed")
        payload = SourceRevisionAccept(
            expected_generation=registration.generation,
            before_revision_id=change["before_revision_id"],
            candidate_sha256=change["candidate_sha256"],
        )
        result = await revisions.accept(
            db, registration.id, change["path_digest"], payload, world[2]
        )
        assert result["created"] and result["item_id"] not in old_items
        assert not (
            await revisions.accept(
                db, registration.id, change["path_digest"], payload, world[2]
            )
        )["created"]
        new_task = await db.scalar(
            select(Task).where(
                Task.project_id == project.id, Task.dataset_item_id == result["item_id"]
            )
        )
        assert new_task is not None and not new_task.is_labeled
        assert not list(
            await db.scalars(
                select(Annotation).where(Annotation.task_id == new_task.id)
            )
        )
        for task in tasks:
            await db.refresh(task)
            assert old_tasks[task.id] == (
                task.dataset_item_id,
                task.file_path,
                task.status,
                task.is_labeled,
                task.reviewed_at,
            )
        for annotation in annotations:
            await db.refresh(annotation)
            assert annotation.geometry == old_labels[annotation.id]
        for item_id, metadata in old_items.items():
            assert (await db.get(DatasetItem, item_id)).metadata_ == metadata

        scan, _ = await scans.start_scan(db, world[5].id, world[2])
        job = await db.get(AsyncJob, scan.async_job_id)
        scan_id, scan_task_id = scan.id, job.celery_task_id
        fence = await scans.claim(db, scan_id, scan_task_id, "content@test")
        work = await scans.next_directory(db, scan_id, scan_task_id, fence)
        while work and work.get("transition"):
            work = await scans.next_directory(db, scan_id, scan_task_id, fence)
        assert work is not None
        manifest = fs.scan_directory(
            work["config"],
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
        await scans.cancel_job(db, job.id, world[2])
        await db.commit()
        with pytest.raises(scans.LostScanLease):
            await scans.apply_directory(
                db, scan_id, scan_task_id, fence, work, manifest
            )
        await db.rollback()

        def no_materialization(*args):
            raise AssertionError("Ready replay must not materialize media again")

        monkeypatch.setattr(worker, "materialize_image", no_materialization)
        await worker.build_dataset(version_id, job_id, attempt_id, sessions)
        version = await db.get(
            TrainingDatasetVersion, version_id, populate_existing=True
        )
        assert {key: getattr(version, key) for key in columns} == frozen
        assert frozen_objects(version_prefix) == objects
    finally:
        await db.rollback()
        storage_service.delete_prefix(
            managed_dataset_prefix(dataset_id), bucket=storage_service.datasets_bucket
        )
        if version_prefix:
            storage_service.delete_prefix(
                version_prefix, bucket=storage_service.training_bucket
            )
