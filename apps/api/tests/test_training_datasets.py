"""Project authority, immutable snapshot and materialization regressions."""

import hashlib
import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import delete, event, select, update

from app.deps import get_training_dataset_dispatcher
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import training_datasets as service
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.dataset_task_facts import ProjectTaskSourceRead
from app.services.labeling_assets import SqlAlchemyProjectTaskSourceReader
from app.services.training_datasets import bind_training_authority
from tests.factory import create_batch, create_membership, create_project, create_task

dispatch_dataset = service.dispatch_dataset


def source_reader(db):
    return SqlAlchemyProjectTaskSourceReader(db)


async def freeze_selection_with_db_reader(db, *args, **kwargs):
    return await service.freeze_selection(
        db, *args, source_reader=source_reader(db), **kwargs
    )


class _NoopDatasetDispatcher:
    async def dispatch_dataset(self, *_args, **_kwargs):
        pass


@pytest.fixture
def source_headers(monkeypatch, app_module):
    monkeypatch.setattr(
        service,
        "capture_source_media",
        lambda sample: {"etag": '"original"', "size_bytes": 128, "version_id": None},
    )
    monkeypatch.setitem(
        app_module.dependency_overrides,
        get_training_dataset_dispatcher,
        _NoopDatasetDispatcher,
    )


async def reviewed_project(db, owner):
    project = await create_project(db, owner_id=owner.id)
    tasks = []
    for _ in range(3):
        task = await create_task(db, project_id=project.id, status="completed")
        task.reviewed_at = datetime.now(timezone.utc)
        task.is_labeled = True
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=owner.id,
                class_name="car",
                tool_unit_id="bbox",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            )
        )
        tasks.append(task)
    await db.flush()
    return project, tasks


async def test_selection_refreshes_live_source_without_mutating_prior_cut(
    db_session, super_admin, source_headers
):
    owner, _ = super_admin
    project, tasks = await reviewed_project(db_session, owner)
    task = tasks[0]
    annotation = await db_session.scalar(
        select(Annotation).where(Annotation.task_id == task.id)
    )
    annotation.geometry = {
        **annotation.geometry,
        "source": {"points": [[1, 2]]},
    }
    await db_session.flush()
    prior = await source_reader(db_session).read_project_task_source_facts(
        ProjectTaskSourceRead(
            project_id=project.id,
            tool_unit_id="bbox",
            task_ids=(task.id,),
            limit=2,
        )
    )
    prior_annotation = prior.tasks[0].annotations[0]
    exported = prior_annotation.as_json()
    exported["geometry"]["source"]["points"][0][0] = 99
    assert prior_annotation.as_json()["geometry"]["source"]["points"] == [[1, 2]]
    with pytest.raises(TypeError):
        prior.tool_bindings["bbox"]["classes"][0]["name"] = "changed"

    previous_task_version = task.version
    previous_annotation_version = annotation.version
    await db_session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(
            version=Task.version + 1,
            status="in_progress",
            reviewed_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await db_session.execute(
        update(Annotation)
        .where(Annotation.id == annotation.id)
        .values(
            version=Annotation.version + 1,
            class_name="truck",
            geometry={**annotation.geometry, "x": 0.4},
        )
        .execution_options(synchronize_session=False)
    )
    await db_session.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(
            tool_bindings={"bbox": {"enabled": True, "classes": [{"name": "truck"}]}}
        )
        .execution_options(synchronize_session=False)
    )
    excluded_snapshot, report = await freeze_selection_with_db_reader(
        db_session,
        project,
        TrainingDatasetSelection(task_ids=[task.id]),
        actor_id=owner.id,
    )
    assert excluded_snapshot["samples"] == []
    assert report["excluded"] == [{"task_id": str(task.id), "code": "not_reviewed"}]
    snapshot, _ = await freeze_selection_with_db_reader(
        db_session,
        project,
        TrainingDatasetSelection(mode="manager_confirmed", task_ids=[task.id]),
        actor_id=owner.id,
    )
    sample = snapshot["samples"][0]
    assert snapshot["classes"] == ["truck"]
    assert sample["task_version"] == previous_task_version + 1
    assert sample["eligibility"]["reviewed"] is False
    assert sample["annotations"] == [
        {
            **prior_annotation.as_json(),
            "version": previous_annotation_version + 1,
            "class_name": "truck",
            "geometry": {**annotation.geometry, "x": 0.4},
        }
    ]
    assert prior.tasks[0].task_version == previous_task_version
    assert prior.tasks[0].reviewed is True
    assert prior_annotation.class_name == "car"
    assert prior_annotation.geometry["x"] == 0.1


async def test_labeling_source_reader_scopes_orders_limits_and_uses_one_query(
    db_session, super_admin
):
    owner, _token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    foreign_project = await create_project(db_session, owner_id=owner.id)
    batch = await create_batch(db_session, project_id=project.id)
    other_batch = await create_batch(db_session, project_id=project.id)
    scoped = [await create_task(db_session, project_id=project.id) for _ in range(3)]
    other_batch_task = await create_task(db_session, project_id=project.id)
    foreign_task = await create_task(db_session, project_id=foreign_project.id)
    for task in scoped:
        task.batch_id = batch.id
    other_batch_task.batch_id = other_batch.id
    await db_session.flush()

    reader = source_reader(db_session)
    engine = db_session.bind.sync_engine
    statements = []

    def count_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        limited = await reader.read_project_task_source_facts(
            ProjectTaskSourceRead(
                project_id=project.id,
                tool_unit_id="bbox",
                batch_id=batch.id,
                limit=2,
            )
        )
        assert len(statements) == 1
        expected = sorted(scoped, key=lambda task: task.id)[:2]
        assert [fact.task_id for fact in limited.tasks] == [
            task.id for task in expected
        ]
        assert limited.tool_bindings is not None

        statements.clear()
        unrestricted = await reader.read_project_task_source_facts(
            ProjectTaskSourceRead(
                project_id=project.id,
                tool_unit_id="bbox",
                limit=4,
            )
        )
        assert len(statements) == 1
        assert [fact.task_id for fact in unrestricted.tasks] == sorted(
            [task.id for task in scoped] + [other_batch_task.id]
        )

        statements.clear()
        requested = await reader.read_project_task_source_facts(
            ProjectTaskSourceRead(
                project_id=project.id,
                tool_unit_id="bbox",
                batch_id=batch.id,
                task_ids=(
                    foreign_task.id,
                    other_batch_task.id,
                    scoped[2].id,
                    scoped[0].id,
                ),
                limit=4,
            )
        )
        assert len(statements) == 1
        assert [fact.task_id for fact in requested.tasks] == sorted(
            (scoped[0].id, scoped[2].id)
        )

        statements.clear()
        empty = await reader.read_project_task_source_facts(
            ProjectTaskSourceRead(
                project_id=project.id,
                tool_unit_id="bbox",
                task_ids=(foreign_task.id, uuid.uuid4()),
                limit=2,
            )
        )
        assert len(statements) == 1
        assert empty.tasks == () and empty.tool_bindings is None
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    with pytest.raises(ValueError, match="limit"):
        ProjectTaskSourceRead(project_id=project.id, tool_unit_id="bbox", limit=0)
    with pytest.raises(ValueError, match="limit"):
        ProjectTaskSourceRead(project_id=project.id, tool_unit_id="bbox", limit=20_002)


async def test_segmentation_freeze_keeps_region_instances_negatives_and_task_in_list(
    db_session, super_admin, httpx_client, source_headers
):
    from training_contract.dataset import SEGMENTATION

    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {"enabled": True, "classes": ["shape"]},
    }
    annotations = []
    for i in range(3):
        task = await create_task(db_session, project_id=project.id, status="completed")
        task.reviewed_at = datetime.now(timezone.utc)
        annotation = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=owner.id,
            class_name="shape",
            tool_unit_id="region",
            geometry={
                "type": "polygon",
                "points": [[0.1, 0.1], [0.8, 0.1], [0.7, 0.7], [0.1, 0.8]],
            },
        )
        db_session.add(annotation)
        annotations.append(annotation)
    negative = await create_task(db_session, project_id=project.id, status="completed")
    negative.reviewed_at = datetime.now(timezone.utc)
    negative.submitted_at = negative.skipped_at = datetime.now(timezone.utc)
    negative.skip_reason = "no_target"
    await db_session.flush()
    path = f"/api/v1/projects/{project.id}/training-datasets"
    headers = {"Authorization": f"Bearer {token}"}
    preflight = await httpx_client.post(
        f"{path}:preflight", headers=headers, json={"task": SEGMENTATION}
    )
    assert preflight.status_code == 200 and preflight.json()["ready"], preflight.text
    assert preflight.json()["classes"] == ["shape"]
    assert preflight.json()["summary"]["negatives"] == 1
    payload = {
        "task": SEGMENTATION,
        "name": "Polygons",
        "request_id": str(uuid.uuid4()),
        "preflight_digest": preflight.json()["digest"],
    }
    response = await httpx_client.post(path, headers=headers, json=payload)
    assert response.status_code == 202 and response.json()["task"] == SEGMENTATION, (
        response.text
    )
    version = await db_session.get(
        TrainingDatasetVersion, uuid.UUID(response.json()["id"])
    )
    assert version.snapshot["task"] == SEGMENTATION and version.snapshot["schema"] == 3
    assert {
        a["id"] for sample in version.snapshot["samples"] for a in sample["annotations"]
    } == {str(a.id) for a in annotations}
    assert all(
        a["version"] >= 1
        for sample in version.snapshot["samples"]
        for a in sample["annotations"]
    )
    listed = await httpx_client.get(path, headers=headers)
    assert (
        listed.status_code == 200 and listed.json()["items"][0]["task"] == SEGMENTATION
    )
    detail = await httpx_client.get(
        f"{path}/{version.id}?include_manifest=false", headers=headers
    )
    assert detail.status_code == 200 and detail.json()["task"] == SEGMENTATION
    assert detail.json()["class_count"] == 1
    assert detail.json()["source_media_type"] == "image"
    assert detail.json()["summary"]["objects"] == 3
    # Unsupported active geometry is an error, never a silently emitted negative.
    annotations[0].geometry = {
        **annotations[0].geometry,
        "holes": [[[0.2, 0.2], [0.3, 0.2], [0.3, 0.3]]],
    }
    await db_session.flush()
    rejected = await httpx_client.post(
        f"{path}:preflight", headers=headers, json={"task": SEGMENTATION}
    )
    assert not rejected.json()["ready"]
    assert rejected.json()["issues"][0]["code"] == "unsupported_annotation"
    assert rejected.json()["summary"]["negatives"] == 1


def test_default_detection_selection_keeps_legacy_canonical_bytes():
    expected = {
        "batch_id": None,
        "task_ids": [],
        "mode": "reviewed",
        "confirmed_negative_ids": [],
        "seed": 42,
        "validation_fraction": 0.2,
        "parent_id": None,
    }
    assert TrainingDatasetSelection().canonical_payload() == expected
    assert (
        TrainingDatasetSelection(task="image-detection").canonical_payload() == expected
    )


async def test_snapshot_is_fixed_after_task_edits_and_creation_is_idempotent(
    db_session, super_admin, httpx_client, source_headers
):
    owner, token = super_admin
    project, tasks = await reviewed_project(db_session, owner)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/projects/{project.id}/training-datasets"
    preflight = await httpx_client.post(f"{path}:preflight", headers=headers, json={})
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["ready"]
    payload = {
        "name": "First round",
        "request_id": str(uuid.uuid4()),
        "preflight_digest": preflight.json()["digest"],
    }
    response = await httpx_client.post(path, headers=headers, json=payload)
    assert response.status_code == 202, response.text
    version_id = uuid.UUID(response.json()["id"])
    version = await db_session.get(TrainingDatasetVersion, version_id)
    assert version.preparation_report["snapshot_digest"] == version.snapshot_digest
    assert version.preparation_report["summary"] == preflight.json()["summary"]
    assert version.preparation_report["excluded"] == preflight.json()["excluded"]
    saved = json.dumps(version.snapshot, sort_keys=True)
    task = tasks[0]
    task.status = "in_progress"
    task.version += 1
    await db_session.flush()
    repeated = await httpx_client.post(path, headers=headers, json=payload)
    assert repeated.status_code == 202
    assert repeated.json()["id"] == str(version_id)
    await db_session.refresh(version)
    assert json.dumps(version.snapshot, sort_keys=True) == saved
    changed = await httpx_client.post(
        path, headers=headers, json={**payload, "name": "Different name"}
    )
    assert changed.status_code == 409
    await db_session.refresh(version)
    assert json.dumps(version.snapshot, sort_keys=True) == saved
    fresh = await httpx_client.post(
        path,
        headers=headers,
        json={**payload, "request_id": str(uuid.uuid4())},
    )
    assert fresh.status_code == 409


async def test_stale_preflight_and_invalid_selection_do_not_create_dataset_rows(
    db_session, super_admin, httpx_client, source_headers
):
    from app.db.models.project_task_dataset import (
        ProjectTaskDataset,
        TaskDatasetVersionMembership,
    )

    owner, token = super_admin
    project, tasks = await reviewed_project(db_session, owner)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/projects/{project.id}/training-datasets"
    preflight = await httpx_client.post(f"{path}:preflight", headers=headers, json={})
    assert preflight.status_code == 200 and preflight.json()["ready"]
    stale_request_id = uuid.uuid4()
    stale_payload = {
        "name": "Stale preflight",
        "request_id": str(stale_request_id),
        "preflight_digest": preflight.json()["digest"],
    }
    tasks[0].status = "in_progress"
    await db_session.flush()
    stale = await httpx_client.post(path, headers=headers, json=stale_payload)
    assert stale.status_code == 409
    invalid_request_id = uuid.uuid4()
    invalid = await httpx_client.post(
        path,
        headers=headers,
        json={
            "name": "Unknown task",
            "task_ids": [str(uuid.uuid4())],
            "request_id": str(invalid_request_id),
            "preflight_digest": "0" * 64,
        },
    )
    assert invalid.status_code == 404
    assert (
        await db_session.scalar(
            select(TrainingDatasetVersion.id).where(
                TrainingDatasetVersion.project_id == project.id
            )
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(AsyncJob.id).where(
                AsyncJob.project_id == project.id,
                AsyncJob.kind == "training_dataset",
            )
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(ProjectTaskDataset.id).where(
                ProjectTaskDataset.project_id == project.id
            )
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(TaskDatasetVersionMembership.version_id)
            .join(
                ProjectTaskDataset,
                TaskDatasetVersionMembership.task_dataset_id == ProjectTaskDataset.id,
            )
            .where(ProjectTaskDataset.project_id == project.id)
        )
        is None
    )


async def test_only_current_review_round_can_imply_negative(
    db_session, super_admin, source_headers
):
    owner, _ = super_admin
    project, _ = await reviewed_project(db_session, owner)
    task = await create_task(db_session, project_id=project.id, status="completed")
    task.reviewed_at = datetime.now(timezone.utc)
    task.skip_reason = "no_target"
    task.skipped_at = datetime.now(timezone.utc) - timedelta(days=1)
    task.submitted_at = datetime.now(timezone.utc)
    await db_session.flush()
    snapshot, report = await freeze_selection_with_db_reader(
        db_session, project, TrainingDatasetSelection(), actor_id=owner.id
    )
    assert {"task_id": str(task.id), "code": "unconfirmed_negative"} in report[
        "excluded"
    ]
    task.skipped_at = task.submitted_at
    await db_session.flush()
    snapshot, report = await freeze_selection_with_db_reader(
        db_session, project, TrainingDatasetSelection(), actor_id=owner.id
    )
    assert report["summary"]["negatives"] == 1
    assert any(s["task_id"] == str(task.id) for s in snapshot["samples"])


async def test_manager_confirmation_does_not_turn_unselected_empty_tasks_into_negatives(
    db_session, super_admin, source_headers
):
    owner, _ = super_admin
    project, tasks = await reviewed_project(db_session, owner)
    blank = await create_task(db_session, project_id=project.id)
    snapshot, report = await freeze_selection_with_db_reader(
        db_session,
        project,
        TrainingDatasetSelection(
            mode="manager_confirmed", task_ids=[t.id for t in tasks] + [blank.id]
        ),
        actor_id=owner.id,
    )
    assert report["summary"]["negatives"] == 0
    assert report["excluded"] == [
        {"task_id": str(blank.id), "code": "unconfirmed_negative"}
    ]


async def test_member_cannot_access_versions_and_project_deletion_is_atomic(
    db_session, super_admin, annotator, httpx_client, source_headers
):
    owner, token = super_admin
    project, tasks = await reviewed_project(db_session, owner)
    employee, employee_token = annotator
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=employee.id,
        role="annotator",
        assigned_by=owner.id,
    )
    path = f"/api/v1/projects/{project.id}/training-datasets"
    response = await httpx_client.get(
        path, headers={"Authorization": f"Bearer {employee_token}"}
    )
    assert response.status_code == 403
    headers = {"Authorization": f"Bearer {token}"}
    report = (
        await httpx_client.post(f"{path}:preflight", headers=headers, json={})
    ).json()
    response = await httpx_client.post(
        path,
        headers=headers,
        json={
            "name": "Preserve",
            "request_id": str(uuid.uuid4()),
            "preflight_digest": report["digest"],
        },
    )
    assert response.status_code == 202, response.text
    deleted = await httpx_client.delete(
        f"/api/v1/projects/{project.id}", headers=headers
    )
    assert deleted.status_code == 409, deleted.text
    from app.db.models.task import Task

    assert await db_session.get(Task, tasks[0].id) is not None


async def test_foreign_parent_and_unknown_tasks_rejected(
    db_session, super_admin, source_headers
):
    owner, _ = super_admin
    project, _ = await reviewed_project(db_session, owner)
    with pytest.raises(TrainingDatasetError) as error:
        await freeze_selection_with_db_reader(
            db_session,
            project,
            TrainingDatasetSelection(task_ids=[uuid.uuid4()]),
            actor_id=owner.id,
        )
    assert error.value.status_code == 404


async def test_revoked_creator_cannot_publish(db_session, super_admin, source_headers):
    owner, _ = super_admin
    project, _ = await reviewed_project(db_session, owner)
    snapshot, report = await freeze_selection_with_db_reader(
        db_session, project, TrainingDatasetSelection(), actor_id=owner.id
    )
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest=report["digest"],
        snapshot_digest=report["digest"],
        snapshot=snapshot,
        name="Revoked",
    )
    db_session.add(version)
    await db_session.flush()
    owner.is_active = False
    await db_session.flush()
    with pytest.raises(TrainingDatasetError) as error:
        await bind_training_authority(db_session).assert_authority(
            project_id=version.project_id,
            actor_id=version.creator_id,
            api_key_id=version.snapshot.get("authorization", {}).get("api_key_id"),
        )
    assert error.value.code == "authority_revoked"


async def make_version(db, owner, project):
    snapshot, report = await freeze_selection_with_db_reader(
        db, project, TrainingDatasetSelection(), actor_id=owner.id
    )
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest=report["digest"],
        snapshot_digest=report["digest"],
        snapshot=snapshot,
        name="Lifecycle",
    )
    db.add(version)
    await db.flush()
    await service.enqueue_dataset(db, version)
    await db.commit()
    return version


async def make_minimal_cancel_target(db, owner):
    from app.services import async_job as async_job_service

    project = await create_project(db, owner_id=owner.id)
    job = await async_job_service.create_job(
        db,
        kind="training_dataset",
        user_id=owner.id,
        project_id=project.id,
        celery_task_id="stale-execution",
    )
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        async_job_id=job.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot_digest="b" * 64,
        snapshot={"task": "detect", "classes": [], "samples": []},
        name="Cancellation regression",
    )
    db.add(version)
    await db.flush()
    return project, job, version


async def test_cancel_reconciles_domain_and_execution_fences_deletion(
    db_session, super_admin, httpx_client, source_headers, monkeypatch
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncConnection
    from app.workers.celery_app import celery_app

    monkeypatch.setattr(celery_app.control, "revoke", lambda *args, **kwargs: None)
    owner, token = super_admin
    project, _ = await reviewed_project(db_session, owner)
    version = await make_version(db_session, owner, project)
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{version.async_job_id}/cancel", headers=headers
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(version)
    assert version.status == "cancelled"
    job = await db_session.get(AsyncJob, version.async_job_id)
    assert job.status == "cancelled"
    bind = db_session.bind
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    async with engine.connect() as execution:
        key = func.hashtextextended(str(version.id), 1)
        await execution.execute(select(func.pg_advisory_lock(key)))
        try:
            for suffix, method in [
                ("", httpx_client.delete),
                ("/retry", httpx_client.post),
            ]:
                blocked = await method(
                    f"/api/v1/projects/{project.id}/training-datasets/{version.id}{suffix}",
                    headers=headers,
                )
                assert blocked.status_code == 409, blocked.text
        finally:
            await execution.execute(select(func.pg_advisory_unlock(key)))
            await execution.commit()


@pytest.mark.parametrize(
    ("fresh_job_status", "version_status", "missing_version"),
    [
        ("running", "running", False),
        ("completed", "running", False),
        ("running", "ready", False),
        ("running", None, True),
    ],
)
async def test_training_dataset_cancellation_uses_fresh_job_and_version_state(
    db_session,
    super_admin,
    monkeypatch,
    fresh_job_status,
    version_status,
    missing_version,
):
    from app.db.models.notification import Notification
    from app.services.training_dataset_cancellation_contract import (
        TrainingDatasetCancellationFailure,
    )
    from app.services.training_datasets import SqlTrainingDatasetCancellation

    owner, _ = super_admin
    project, job, version = await make_minimal_cancel_target(db_session, owner)
    assert job.status == "pending"
    assert job.celery_task_id == "stale-execution"
    assert version.status == "pending"

    await db_session.execute(
        update(AsyncJob)
        .where(AsyncJob.id == job.id)
        .values(status=fresh_job_status, celery_task_id="fresh-execution")
        .execution_options(synchronize_session=False)
    )
    if missing_version:
        await db_session.execute(
            delete(TrainingDatasetVersion)
            .where(TrainingDatasetVersion.id == version.id)
            .execution_options(synchronize_session=False)
        )
    else:
        await db_session.execute(
            update(TrainingDatasetVersion)
            .where(TrainingDatasetVersion.id == version.id)
            .values(status=version_status)
            .execution_options(synchronize_session=False)
        )

    # The identity-map rows stay stale until the cancellation owner re-reads them.
    assert job.status == "pending"
    assert job.celery_task_id == "stale-execution"
    assert version.status == "pending"
    execution_ids = []
    published = []

    class CaptureExecutionControl:
        def request_cancel(self, execution_id):
            execution_ids.append(execution_id)

    async def capture_redis_publish(*, user_id, message):
        published.append((user_id, message))

    monkeypatch.setattr("app.services.notification._publish", capture_redis_publish)
    cancellation = SqlTrainingDatasetCancellation(
        db_session, execution_control=CaptureExecutionControl()
    )

    if fresh_job_status == "running" and version_status == "running":
        await cancellation.request_cancel(
            job_id=job.id, project_id=project.id, actor_id=owner.id
        )
        await db_session.commit()
        await db_session.refresh(job)
        await db_session.refresh(version)
        assert execution_ids == ["fresh-execution"]
        assert job.status == "cancelled"
        assert version.status == "cancelled"
        assert version.error_code == "cancelled"
        assert version.error_message == "训练数据构建已取消"
        assert version.completed_at is not None
        notification = await db_session.scalar(
            select(Notification).where(
                Notification.user_id == owner.id,
                Notification.type == "job.cancelled",
                Notification.target_type == "async_job",
                Notification.target_id == job.id,
            )
        )
        assert notification is not None
        assert len(published) == 1
        assert published[0][0] == owner.id
        assert published[0][1]["type"] == "job.cancelled"
    else:
        with pytest.raises(TrainingDatasetCancellationFailure) as exc:
            await cancellation.request_cancel(
                job_id=job.id, project_id=project.id, actor_id=owner.id
            )
        assert exc.value.status_code == 409
        assert exc.value.detail == "训练数据构建已结束"
        assert execution_ids == []
        await db_session.refresh(job)
        assert job.status == fresh_job_status
        assert job.celery_task_id == "fresh-execution"
        if missing_version:
            assert (
                await db_session.scalar(
                    select(TrainingDatasetVersion.id).where(
                        TrainingDatasetVersion.id == version.id
                    )
                )
                is None
            )
        else:
            await db_session.refresh(version)
            assert version.status == version_status
        notification_id = await db_session.scalar(
            select(Notification.id).where(
                Notification.user_id == owner.id,
                Notification.target_id == job.id,
            )
        )
        assert notification_id is None
        assert published == []


async def test_training_dataset_cancellation_restores_current_account_rejection(
    db_session, super_admin
):
    from app.application.async_job_contracts import AsyncJobOperationFailure
    from app.application.job_cancellation import SqlJobCancellation
    from app.db.models.user import User

    owner, _ = super_admin
    _project, job, version = await make_minimal_cancel_target(db_session, owner)
    assert owner.is_active is True
    await db_session.execute(
        update(User)
        .where(User.id == owner.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert owner.is_active is True
    execution_ids = []

    class CaptureExecutionControl:
        def request_cancel(self, execution_id):
            execution_ids.append(execution_id)

    with pytest.raises(AsyncJobOperationFailure) as exc:
        await SqlJobCancellation(
            db_session, execution_control=CaptureExecutionControl()
        ).cancel(job_id=job.id, actor_id=owner.id)

    assert exc.value.status_code == 401
    assert exc.value.detail == "账号已停用或不存在"
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}
    assert execution_ids == []
    assert job.status == "pending"
    assert version.status == "pending"


async def test_training_dataset_cancellation_preserves_unexpected_owner_exception(
    db_session, super_admin, monkeypatch
):
    from app.application.job_cancellation import SqlJobCancellation

    owner, _ = super_admin
    _project, job, _version = await make_minimal_cancel_target(db_session, owner)
    owner_failure = RuntimeError("owner dependency failed")

    async def fail_project_manager(*_args, **_kwargs):
        raise owner_failure

    monkeypatch.setattr(
        "app.services.training_datasets.lock_project_manager", fail_project_manager
    )
    execution_ids = []

    class CaptureExecutionControl:
        def request_cancel(self, execution_id):
            execution_ids.append(execution_id)

    with pytest.raises(RuntimeError) as exc:
        await SqlJobCancellation(
            db_session, execution_control=CaptureExecutionControl()
        ).cancel(job_id=job.id, actor_id=owner.id)

    assert exc.value is owner_failure
    assert execution_ids == []


@pytest.mark.parametrize("accepted_status", ["pending", "running", "ready"])
async def test_dispatch_exception_never_overwrites_an_accepted_attempt(
    db_session, super_admin, source_headers, accepted_status
):
    owner, _ = super_admin
    project, _ = await reviewed_project(db_session, owner)
    version = await make_version(db_session, owner, project)

    class BrokerResponseLostDispatcher:
        async def dispatch_dataset(self, *_args, **_kwargs):
            version.status = accepted_status
            await db_session.commit()
            raise OSError("broker response lost")

    await dispatch_dataset(
        db_session, version, dispatcher=BrokerResponseLostDispatcher()
    )
    await db_session.refresh(version)
    assert version.status == (
        "failed" if accepted_status == "pending" else accepted_status
    )


async def test_api_key_scopes_and_post_queue_revocation(
    db_session, super_admin, httpx_client, source_headers
):
    from tests.factory import create_api_key

    owner, _ = super_admin
    project, _ = await reviewed_project(db_session, owner)
    key, token = await create_api_key(db_session, owner.id, "training", [])
    path = f"/api/v1/projects/{project.id}/training-datasets"
    denied = await httpx_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403
    key.scopes = ["*"]
    await db_session.flush()
    response = await httpx_client.get(
        path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    snapshot, report = await freeze_selection_with_db_reader(
        db_session,
        project,
        TrainingDatasetSelection(),
        actor_id=owner.id,
        api_key_id=str(key.id),
    )
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest=report["digest"],
        snapshot_digest=report["digest"],
        snapshot=snapshot,
        name="Key revoked",
    )
    db_session.add(version)
    await db_session.flush()
    key.revoked_at = datetime.now(timezone.utc)
    await db_session.flush()
    with pytest.raises(TrainingDatasetError) as error:
        await bind_training_authority(db_session).assert_authority(
            project_id=version.project_id,
            actor_id=version.creator_id,
            api_key_id=version.snapshot.get("authorization", {}).get("api_key_id"),
        )
    assert error.value.code == "api_key_revoked"


async def test_real_storage_materialization_retry_preserves_originals(
    db_session, super_admin, monkeypatch
):
    from app.services.storage import storage_service
    from app.workers import training_dataset as worker

    owner, _ = super_admin
    project, tasks = await reviewed_project(db_session, owner)
    source_prefix = f"test-training/{uuid.uuid4()}/"
    training_prefix = f"projects/{project.id}/"
    try:
        for index, task in enumerate(tasks):
            task.file_path = f"{source_prefix}{task.id}.png"
            buffer = io.BytesIO()
            Image.new("RGB", (32, 24), (index * 80, 20, 40)).save(buffer, format="PNG")
            storage_service.put_bytes(
                task.file_path, buffer.getvalue(), content_type="image/png"
            )
        await db_session.flush()
        snapshot, report = await freeze_selection_with_db_reader(
            db_session, project, TrainingDatasetSelection(), actor_id=owner.id
        )
        assert report["ready"]
        version = TrainingDatasetVersion(
            project_id=project.id,
            creator_id=owner.id,
            request_id=uuid.uuid4(),
            request_digest=report["digest"],
            snapshot_digest=report["digest"],
            snapshot=snapshot,
            name="Actual media",
        )
        db_session.add(version)
        await db_session.flush()
        await service.enqueue_dataset(db_session, version)
        await db_session.commit()

        @asynccontextmanager
        async def sessions():
            yield db_session

        original_bridge = worker.build_training_package

        def temporary_failure(*args, **kwargs):
            raise TrainingDatasetError(
                "temporary_failure", "Simulated packaging failure"
            )

        monkeypatch.setattr(worker, "build_training_package", temporary_failure)
        await worker.build_dataset(
            version.id, version.async_job_id, version.attempt_id, sessions
        )
        await db_session.refresh(version)
        assert version.status == "failed"
        assert version.error_code == "temporary_failure"
        assert version.completed_at is not None
        storage_service.delete_prefix(source_prefix)
        await service.enqueue_dataset(db_session, version)
        await db_session.commit()
        monkeypatch.setattr(worker, "build_training_package", original_bridge)
        await worker.build_dataset(
            version.id, version.async_job_id, version.attempt_id, sessions
        )
        await db_session.refresh(version)
        assert version.status == "ready", version.error_message
        assert version.progress_pct == 100
        assert version.completed_at is not None
        assert version.error_code is None
        assert version.error_message is None
        response = storage_service.client.get_object(
            Bucket=storage_service.training_bucket, Key=version.package_key
        )
        try:
            package_bytes = response["Body"].read()
        finally:
            response["Body"].close()
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert {sample["split"] for sample in manifest["samples"]} == {
                "train",
                "val",
            }
            for sample in manifest["samples"]:
                with Image.open(
                    io.BytesIO(archive.read(sample["image_path"]))
                ) as image:
                    assert image.size == (32, 24)
        assert version.manifest == manifest
        assert version.size_bytes == len(package_bytes)
        assert version.package_sha256 == hashlib.sha256(package_bytes).hexdigest()
        assert version.snapshot == snapshot
        original_checksum = version.package_sha256
        ready_package_key = version.package_key
        ready_attempt_id = version.attempt_id
        await worker.build_dataset(
            version.id, version.async_job_id, uuid.uuid4(), sessions
        )
        await db_session.refresh(version)
        assert version.status == "ready"
        assert version.attempt_id == ready_attempt_id
        assert version.package_key == ready_package_key
        assert version.package_sha256 == original_checksum
        package_head = storage_service.client.head_object(
            Bucket=storage_service.training_bucket,
            Key=ready_package_key,
        )
        assert package_head["ContentLength"] == version.size_bytes
        await worker.build_dataset(
            version.id, version.async_job_id, version.attempt_id, sessions
        )
        await db_session.refresh(version)
        assert version.package_sha256 == original_checksum
        # A failed retry must preserve the original published fields and object.
        version.status = "failed"
        await service.enqueue_dataset(db_session, version)
        await db_session.commit()
        failed_attempt = version.attempt_id
        monkeypatch.setattr(worker, "build_training_package", temporary_failure)
        await worker.build_dataset(
            version.id, version.async_job_id, failed_attempt, sessions
        )
        await db_session.refresh(version)
        assert version.status == "failed"
        assert version.error_code == "temporary_failure"
        assert version.package_key == ready_package_key
        assert version.package_sha256 == original_checksum
        assert version.size_bytes == len(package_bytes)
        assert version.manifest == manifest
        assert version.snapshot == snapshot
        failed_object = storage_service.client.get_object(
            Bucket=storage_service.training_bucket, Key=ready_package_key
        )
        try:
            preserved_bytes = failed_object["Body"].read()
        finally:
            failed_object["Body"].close()
        assert hashlib.sha256(preserved_bytes).hexdigest() == original_checksum
        # A late cancellation after upload discards only this attempt's ZIP.
        await service.enqueue_dataset(db_session, version)
        await db_session.commit()
        cancelled_attempt = version.attempt_id
        monkeypatch.setattr(worker, "build_training_package", original_bridge)
        authority_calls = 0
        original_authority = service.SqlTrainingAuthority.assert_authority

        async def cancel_before_publication(authority, **facts):
            nonlocal authority_calls
            authority_calls += 1
            await original_authority(authority, **facts)
            if authority_calls == 2:
                db = authority._db
                current = await db.get(TrainingDatasetVersion, version.id)
                current.status = "cancelled"
                await service.jobs.mark_cancelled(db, current.async_job_id)
                await db.commit()

        monkeypatch.setattr(
            service.SqlTrainingAuthority, "assert_authority", cancel_before_publication
        )
        await worker.build_dataset(
            version.id, version.async_job_id, cancelled_attempt, sessions
        )
        await db_session.refresh(version)
        assert version.status == "cancelled"
        assert version.package_key == ready_package_key
        assert version.package_sha256 == original_checksum
        assert version.size_bytes == len(package_bytes)
        assert version.manifest == manifest
        cancel_package_head = storage_service.client.head_object(
            Bucket=storage_service.training_bucket, Key=ready_package_key
        )
        assert cancel_package_head["ContentLength"] == len(package_bytes)
        assert (
            storage_service.list_objects(
                f"{training_prefix}datasets/{version.id}/attempts/{cancelled_attempt}/",
                bucket=storage_service.training_bucket,
            )
            == []
        )
    finally:
        storage_service.delete_prefix(source_prefix)
        storage_service.delete_prefix(
            training_prefix, bucket=storage_service.training_bucket
        )


def test_changed_source_is_rejected_before_preserving_media(tmp_path):
    from app.services.storage import storage_service
    from app.workers.training_dataset import materialize_image

    prefix = f"test-training-source/{uuid.uuid4()}/"
    key = prefix + "image.png"
    try:
        storage_service.put_bytes(key, b"original", content_type="image/png")
        sample = {
            "task_id": str(uuid.uuid4()),
            "source_bucket": storage_service.bucket,
            "source_key": key,
        }
        sample["media"] = service.capture_source_media(sample)
        storage_service.put_bytes(key, b"changed", content_type="image/png")
        if version_id := sample["media"]["version_id"]:
            # An overwrite alone keeps the frozen source readable in versioned buckets.
            storage_service.client.delete_object(
                Bucket=storage_service.bucket, Key=key, VersionId=version_id
            )
        with pytest.raises(TrainingDatasetError) as error:
            materialize_image(sample, prefix.rstrip("/"), "frozen", tmp_path)
        assert error.value.code == "source_media_changed"
        assert (
            storage_service.list_objects(prefix, bucket=storage_service.training_bucket)
            == []
        )
    finally:
        storage_service.delete_prefix(prefix)
        storage_service.delete_prefix(prefix, bucket=storage_service.training_bucket)


def test_versioned_source_overwrite_preserves_frozen_image(tmp_path):
    from app.services.storage import storage_service
    from app.workers.training_dataset import materialize_image

    prefix = f"test-training-source/{uuid.uuid4()}/"
    key = prefix + "image.png"
    image = io.BytesIO()
    Image.new("RGB", (7, 5), "red").save(image, format="PNG")
    original = image.getvalue()
    try:
        storage_service.put_bytes(key, original, content_type="image/png")
        sample = {
            "task_id": str(uuid.uuid4()),
            "source_bucket": storage_service.bucket,
            "source_key": key,
        }
        sample["media"] = service.capture_source_media(sample)
        if not sample["media"]["version_id"]:
            pytest.skip("requires object versioning for frozen source retention")
        storage_service.put_bytes(key, b"changed", content_type="image/png")
        media = materialize_image(sample, prefix.rstrip("/"), "frozen", tmp_path)
        assert media["sha256"] == hashlib.sha256(original).hexdigest()
        assert (media["width"], media["height"]) == (7, 5)
        assert (tmp_path / sample["task_id"]).read_bytes() == original
    finally:
        storage_service.delete_prefix(prefix)
        storage_service.delete_prefix(prefix, bucket=storage_service.training_bucket)


async def test_mixed_project_extracts_only_selected_binding_and_never_assumes_other_task_negatives(
    db_session,
    super_admin,
    source_headers,
):
    owner, _ = super_admin
    project, boxes = await reviewed_project(db_session, owner)
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {"enabled": True, "classes": ["shape"]},
    }
    region_only = await create_task(
        db_session, project_id=project.id, status="completed"
    )
    region_only.reviewed_at = datetime.now(timezone.utc)
    polygons = []
    for task in [*boxes, region_only]:
        ann = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=owner.id,
            tool_unit_id="region",
            class_name="shape",
            geometry={
                "type": "polygon",
                "points": [[0.1, 0.1], [0.8, 0.1], [0.7, 0.7], [0.1, 0.8]],
            },
        )
        polygons.append(ann)
        db_session.add(ann)
    await db_session.flush()
    detect, report = await freeze_selection_with_db_reader(
        db_session, project, TrainingDatasetSelection(task="detect"), actor_id=owner.id
    )
    assert report["ready"]
    assert report["summary"]["eligible"] == 3 and report["summary"]["objects"] == 3
    assert report["summary"]["excluded_annotations"] == 4
    assert report["excluded"] == [
        {"task_id": str(region_only.id), "code": "unconfirmed_negative"}
    ]
    assert detect["annotation_scope"]["tool_unit_ids"] == ["bbox"]
    assert all(
        a["tool_unit_id"] == "bbox"
        for sample in detect["samples"]
        for a in sample["annotations"]
    )
    segment, report = await freeze_selection_with_db_reader(
        db_session,
        project,
        TrainingDatasetSelection(task="segmentation"),
        actor_id=owner.id,
    )
    assert report["ready"] and report["summary"]["eligible"] == 4
    assert (
        report["classes"] == ["shape"]
        and report["summary"]["excluded_annotations"] == 3
    )
    assert segment["source"]["tool_bindings"] == {
        "region": project.tool_bindings["region"]
    }
    assert all(
        a["tool_unit_id"] == "region"
        for sample in segment["samples"]
        for a in sample["annotations"]
    )
    # Invalid geometry in the selected binding remains a hard validation issue.
    polygons[0].geometry = {
        **polygons[0].geometry,
        "holes": [[[0.2, 0.2], [0.3, 0.2], [0.3, 0.3]]],
    }
    await db_session.flush()
    _, report = await freeze_selection_with_db_reader(
        db_session, project, TrainingDatasetSelection(task="segment"), actor_id=owner.id
    )
    assert (
        not report["ready"] and report["issues"][0]["code"] == "unsupported_annotation"
    )
    # The manager's negative confirmation is explicit and scoped to detect.
    snapshot, report = await freeze_selection_with_db_reader(
        db_session,
        project,
        TrainingDatasetSelection(
            task="detect",
            mode="manager_confirmed",
            task_ids=[task.id for task in [*boxes, region_only]],
            confirmed_negative_ids=[region_only.id],
        ),
        actor_id=owner.id,
    )
    assert report["ready"] and report["summary"]["negatives"] == 1
    assert (
        next(
            sample
            for sample in snapshot["samples"]
            if sample["task_id"] == str(region_only.id)
        )["annotations"]
        == []
    )


def test_task_aliases_merge_semantically_and_conflicts_fail():
    from pydantic import ValidationError

    assert (
        TrainingDatasetSelection(task="detect", task_type="detection").task == "detect"
    )
    assert TrainingDatasetSelection(task_type="segmentation").task == "segment"
    with pytest.raises(ValidationError):
        TrainingDatasetSelection(task="detect", task_type="segmentation")
    with pytest.raises(ValidationError):
        TrainingDatasetSelection(task="aap")
