"""Project authority, immutable snapshot and materialization regressions."""

import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import training_datasets as service
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.factory import (
    create_api_key,
    create_membership,
    create_project,
    create_task,
)
from tests.sample_evaluation_original_fixtures import Objects
from tests.training_original_fixtures import (
    image_bytes,
    original_receipt,
    register_original,
    staged_image,
)

dispatch_dataset = service.dispatch_dataset


@pytest.fixture
def source_headers(monkeypatch):
    provider = Objects().install(monkeypatch)
    monkeypatch.setattr(service, "dispatch_dataset", _no_dispatch)
    yield provider
    assert all(body.closed for body in provider.bodies)


async def _no_dispatch(*args, **kwargs):
    pass


async def reviewed_project(db, owner, *, original_objects=None):
    project = await create_project(db, owner_id=owner.id)
    tasks = []
    for _ in range(3):
        task = await create_task(db, project_id=project.id, status="completed")
        if original_objects is not None:
            await register_original(db, task, owner, provider=original_objects)
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
        await register_original(db_session, task, owner, provider=source_headers)
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
    await register_original(db_session, negative, owner, provider=source_headers)
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
    project, tasks = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
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
    fresh = await httpx_client.post(
        path, headers=headers, json={**payload, "request_id": str(uuid.uuid4())}
    )
    assert fresh.status_code == 409


async def test_only_current_review_round_can_imply_negative(
    db_session, super_admin, source_headers
):
    owner, _ = super_admin
    project, _ = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    task = await create_task(db_session, project_id=project.id, status="completed")
    await register_original(db_session, task, owner, provider=source_headers)
    task.reviewed_at = datetime.now(timezone.utc)
    task.skip_reason = "no_target"
    task.skipped_at = datetime.now(timezone.utc) - timedelta(days=1)
    task.submitted_at = datetime.now(timezone.utc)
    await db_session.flush()
    snapshot, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(),
        actor_id=owner.id,
        trusted_actor=True,
    )
    assert {"task_id": str(task.id), "code": "unconfirmed_negative"} in report[
        "excluded"
    ]
    task.skipped_at = task.submitted_at
    await db_session.flush()
    snapshot, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(),
        actor_id=owner.id,
        trusted_actor=True,
    )
    assert report["summary"]["negatives"] == 1
    assert any(s["task_id"] == str(task.id) for s in snapshot["samples"])


async def test_manager_confirmation_does_not_turn_unselected_empty_tasks_into_negatives(
    db_session, super_admin, source_headers
):
    owner, _ = super_admin
    project, tasks = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    blank = await create_task(db_session, project_id=project.id)
    snapshot, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(
            mode="manager_confirmed", task_ids=[t.id for t in tasks] + [blank.id]
        ),
        actor_id=owner.id,
        trusted_actor=True,
    )
    assert report["summary"]["negatives"] == 0
    assert report["excluded"] == [
        {"task_id": str(blank.id), "code": "unconfirmed_negative"}
    ]


async def test_member_cannot_access_versions_and_project_deletion_is_atomic(
    db_session, super_admin, annotator, httpx_client, source_headers
):
    owner, token = super_admin
    project, tasks = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
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
    project, _ = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    with pytest.raises(TrainingDatasetError) as error:
        await service.freeze_selection(
            db_session,
            project,
            TrainingDatasetSelection(task_ids=[uuid.uuid4()]),
            actor_id=owner.id,
            trusted_actor=True,
        )
    assert error.value.status_code == 404


async def test_revoked_creator_cannot_publish(db_session, super_admin, source_headers):
    owner, _ = super_admin
    project, _ = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    snapshot, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(),
        actor_id=owner.id,
        trusted_actor=True,
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
        await service.dataset_authority(
            db_session,
            version,
            training_authority=service.bind_training_authority(db_session),
        )
    assert error.value.code == "authority_revoked"


async def make_version(db, owner, project):
    snapshot, report = await service.freeze_selection(
        db, project, TrainingDatasetSelection(), actor_id=owner.id, trusted_actor=True
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
    from app.services.training_dataset_storage import allocate

    await allocate(db, version)
    await service.enqueue_dataset(db, version)
    await db.commit()
    return version


async def test_cancel_reconciles_domain_and_execution_fences_deletion(
    db_session, super_admin, httpx_client, source_headers, monkeypatch
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncConnection
    from app.workers.celery_app import celery_app

    monkeypatch.setattr(celery_app.control, "revoke", lambda *args, **kwargs: None)
    owner, token = super_admin
    project, _ = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
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


@pytest.mark.parametrize("accepted_status", ["pending", "running", "ready"])
async def test_dispatch_exception_never_overwrites_an_accepted_attempt(
    db_session, super_admin, source_headers, monkeypatch, accepted_status
):
    owner, _ = super_admin
    project, _ = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    version = await make_version(db_session, owner, project)

    async def broker_response_lost(*args, **kwargs):
        version.status = accepted_status
        await db_session.commit()
        raise OSError("broker response lost")

    monkeypatch.setattr(service.asyncio, "to_thread", broker_response_lost)
    await dispatch_dataset(db_session, version)
    await db_session.refresh(version)
    assert version.status == (
        "failed" if accepted_status == "pending" else accepted_status
    )


async def test_api_key_scopes_and_post_queue_revocation(
    db_session, super_admin, httpx_client, source_headers
):
    owner, _ = super_admin
    project, _ = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
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
    snapshot, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(),
        actor_id=owner.id,
        api_key_id=str(key.id),
        trusted_actor=True,
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
        await service.dataset_authority(
            db_session,
            version,
            training_authority=service.bind_training_authority(db_session),
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
            await register_original(
                db_session,
                task,
                owner,
                raw=buffer.getvalue(),
                bucket=storage_service.bucket,
            )
        await db_session.flush()
        snapshot, report = await service.freeze_selection(
            db_session,
            project,
            TrainingDatasetSelection(),
            actor_id=owner.id,
            trusted_actor=True,
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
        from app.services.training_dataset_storage import allocate

        await allocate(db_session, version)
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
        storage_service.delete_prefix(source_prefix)
        await service.enqueue_dataset(db_session, version)
        await db_session.commit()
        monkeypatch.setattr(worker, "build_training_package", original_bridge)
        await worker.build_dataset(
            version.id, version.async_job_id, version.attempt_id, sessions
        )
        await db_session.refresh(version)
        assert version.status == "ready", version.error_message
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
        assert version.snapshot == snapshot
        original_checksum = version.package_sha256
        await worker.build_dataset(
            version.id, version.async_job_id, version.attempt_id, sessions
        )
        await db_session.refresh(version)
        assert version.package_sha256 == original_checksum
        # A late cancellation after upload must discard this attempt's ZIP.
        version.status = "failed"
        await service.enqueue_dataset(db_session, version)
        await db_session.commit()
        cancelled_attempt = version.attempt_id
        authority_calls = 0
        original_authority = worker.require_authority

        async def cancel_before_publication(db, current):
            nonlocal authority_calls
            authority_calls += 1
            await original_authority(db, current)
            if authority_calls == 2:
                current.status = "cancelled"
                await service.jobs.mark_cancelled(db, current.async_job_id)
                await db.commit()

        monkeypatch.setattr(worker, "require_authority", cancel_before_publication)
        await worker.build_dataset(
            version.id, version.async_job_id, cancelled_attempt, sessions
        )
        await db_session.refresh(version)
        assert version.status == "cancelled"
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


def test_changed_verified_original_is_rejected_before_preserving_media(
    tmp_path, monkeypatch
):
    from botocore.exceptions import ClientError
    from app.services.storage import storage_service
    from app.workers.training_dataset import materialize_image

    def missing_retained(**kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")

    def unexpected_io(*args, **kwargs):
        pytest.fail("Changed staged bytes must not trigger raw GETs or retained PUTs")

    monkeypatch.setattr(storage_service.client, "head_object", missing_retained)
    monkeypatch.setattr(storage_service.client, "get_object", unexpected_io)
    monkeypatch.setattr(storage_service.client, "upload_file", unexpected_io)
    original, changed = image_bytes(color="red"), image_bytes(color="blue")
    receipt = original_receipt(original)
    sample = {
        "task_id": str(uuid.uuid4()),
        "source_key": "test-training-source/image.png",
        "original": receipt,
        "media": {"sha256": receipt["sha256"], "size_bytes": len(original)},
    }
    stage = staged_image(changed)
    try:
        with pytest.raises(TrainingDatasetError) as error:
            materialize_image(
                sample,
                "test-training-source",
                "frozen",
                tmp_path,
                bucket="retained-training",
                verified_original=stage,
            )
        assert error.value.code == "source_media_changed"
        assert not stage.file.closed
    finally:
        stage.close()


async def test_mixed_project_extracts_only_selected_binding_and_never_assumes_other_task_negatives(
    db_session,
    super_admin,
    source_headers,
):
    owner, _ = super_admin
    project, boxes = await reviewed_project(
        db_session, owner, original_objects=source_headers
    )
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {"enabled": True, "classes": ["shape"]},
    }
    region_only = await create_task(
        db_session, project_id=project.id, status="completed"
    )
    await register_original(db_session, region_only, owner, provider=source_headers)
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
    detect, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(task="detect"),
        actor_id=owner.id,
        trusted_actor=True,
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
    segment, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(task="segmentation"),
        actor_id=owner.id,
        trusted_actor=True,
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
    _, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(task="segment"),
        actor_id=owner.id,
        trusted_actor=True,
    )
    assert (
        not report["ready"] and report["issues"][0]["code"] == "unsupported_annotation"
    )
    # The manager's negative confirmation is explicit and scoped to detect.
    snapshot, report = await service.freeze_selection(
        db_session,
        project,
        TrainingDatasetSelection(
            task="detect",
            mode="manager_confirmed",
            task_ids=[task.id for task in [*boxes, region_only]],
            confirmed_negative_ids=[region_only.id],
        ),
        actor_id=owner.id,
        trusted_actor=True,
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
