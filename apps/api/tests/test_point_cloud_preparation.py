"""Durable preparation scope, access, cancellation and source identity."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.schemas.point_cloud_densification import (
    GENERATION_PROFILE,
    PointCloudDensificationRequest,
)
from app.services.point_cloud_auxiliary import resolve_task_inputs
from app.services.point_cloud_densification.service import (
    create_preparation,
    logical_input_digest,
)
from app.workers import point_cloud_densification as worker
from tests.test_point_cloud_auxiliary import (
    seed,
    memory_storage as auxiliary_memory_storage,
)
from tests.test_point_cloud_densification import capability


@pytest.fixture
def memory_storage(monkeypatch):
    return auxiliary_memory_storage.__wrapped__(monkeypatch)


def test_backend_auth_failure_is_distinct_from_project_authority():
    assert worker._error_code(HTTPException(401, "ML backend: token rejected")) == (
        "producer_authentication_failed"
    )
    assert worker._error_code(HTTPException(401, "Account inactive")) == (
        "preparation_authority_revoked"
    )
    assert worker._error_code(
        HTTPException(502, {"error_code": "artifact_transfer_failed"})
    ) == ("producer_request_failed")


async def prepare_fixture(db, actor_id, storage):
    project, dataset, task, primary, camera = await seed(
        db, actor_id, storage, camera_count=6, profile=GENERATION_PROFILE
    )
    backend = MLBackendRegistry(
        name="omega-test", url=f"http://omega-{uuid.uuid4()}:8006", state="connected"
    )
    db.add(backend)
    await db.flush()
    pool = MLBackendServicePool(
        name="omega-producer",
        enabled=True,
        legacy_instance_id=backend.id,
        capability_snapshot={"point_cloud_densification": capability()},
    )
    db.add(pool)
    await db.flush()
    db.add_all(
        [
            MLBackendPoolMember(pool_id=pool.id, registry_id=backend.id),
            ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True),
        ]
    )
    project.point_cloud_enhancement_config = {
        **project.point_cloud_enhancement_config,
        "ml_backend_pool_id": str(pool.id),
    }
    await db.flush()
    return project, task, primary, camera, pool


async def test_preparation_freezes_logical_identity_and_reuses_active_job(
    db_session, super_admin, memory_storage
):
    project, task, _, camera, _ = await prepare_fixture(
        db_session, super_admin[0].id, memory_storage
    )
    request = PointCloudDensificationRequest(scope="task_ids", task_ids=[task.id])
    job, created = await create_preparation(
        db_session, project, super_admin[0].id, request
    )
    assert created and job.celery_task_id and job.status == "pending"
    again, created = await create_preparation(
        db_session, project, super_admin[0].id, request
    )
    assert not created and again.id == job.id
    full = await resolve_task_inputs(db_session, task, GENERATION_PROFILE)
    assert logical_input_digest(full) == job.payload["task_inputs"][str(task.id)]
    camera.metadata_ = {
        "calibration": {
            **camera.metadata_["calibration"],
            "intrinsic": [100, 0, 51, 0, 100, 50, 0, 0, 1],
        }
    }
    await db_session.flush()
    changed, created = await create_preparation(
        db_session, project, super_admin[0].id, request
    )
    assert created and changed.id != job.id


async def test_prepare_endpoint_durable_dispatch_dedupe_cancel_and_retry(
    db_session, super_admin, httpx_client, memory_storage, monkeypatch
):
    project, task, _, _, _ = await prepare_fixture(
        db_session, super_admin[0].id, memory_storage
    )
    calls = []
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.send_task",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            if args and args[0] == "app.workers.point_cloud_densification.run"
            else None
        ),
    )
    base = f"/api/v1/projects/{project.id}/point-cloud/augmentation"
    headers = {"Authorization": f"Bearer {super_admin[1]}"}
    body = {"scope": "task_ids", "task_ids": [str(task.id)]}
    response = await httpx_client.post(f"{base}/jobs", headers=headers, json=body)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert len(calls) == 1
    assert await db_session.get(AsyncJob, uuid.UUID(job_id)) is not None
    again = await httpx_client.post(f"{base}/jobs", headers=headers, json=body)
    assert again.status_code == 202 and again.json()["reused"] and len(calls) == 1
    cancelled = await httpx_client.post(
        f"/api/v1/async-jobs/{job_id}/cancel", headers=headers
    )
    assert (
        cancelled.status_code == 200
        and cancelled.json()["status"] == "cancel_requested"
    )
    job = await db_session.get(AsyncJob, uuid.UUID(job_id), populate_existing=True)
    assert job.status == "pending" and job.payload["cancel_requested"]
    with pytest.raises(worker.PreparationCancelled):
        await worker.check_authority(db_session, job.id)
    job.status = "failed"
    job.result = {"failed_task_ids": [str(task.id)]}
    await db_session.flush()
    retried = await httpx_client.post(
        f"{base}/jobs", headers=headers, json={**body, "retry_job_id": job_id}
    )
    assert retried.status_code == 202 and retried.json()["job_id"] != job_id


async def test_read_only_key_and_missing_calibration_fail_closed(
    db_session, super_admin, httpx_client, memory_storage, monkeypatch
):
    project, task, _, camera, _ = await prepare_fixture(
        db_session, super_admin[0].id, memory_storage
    )
    headers = {"Authorization": f"Bearer {super_admin[1]}"}
    base = f"/api/v1/projects/{project.id}/point-cloud/augmentation"
    key = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers,
        json={"name": "readonly", "scopes": ["annotations:read"]},
    )
    assert key.status_code == 201
    readonly = {"Authorization": f"Bearer {key.json()['plaintext']}"}
    denied = await httpx_client.post(
        f"{base}/jobs",
        headers=readonly,
        json={"scope": "task_ids", "task_ids": [str(task.id)]},
    )
    assert denied.status_code == 403
    camera.metadata_ = {}
    await db_session.flush()
    missing = await httpx_client.post(
        f"{base}/jobs",
        headers=headers,
        json={"scope": "task_ids", "task_ids": [str(task.id)]},
    )
    assert missing.status_code == 409 and missing.json()["detail"]["task_id"] == str(
        task.id
    )


async def test_dispatch_failure_remains_visible_and_terminal(
    db_session, super_admin, httpx_client, memory_storage, monkeypatch
):
    project, task, _, _, _ = await prepare_fixture(
        db_session, super_admin[0].id, memory_storage
    )

    def fail(*args, **kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr("app.workers.celery_app.celery_app.send_task", fail)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud/augmentation/jobs",
        headers={"Authorization": f"Bearer {super_admin[1]}"},
        json={"scope": "task_ids", "task_ids": [str(task.id)]},
    )
    assert response.status_code == 503
    job = (
        await db_session.execute(
            select(AsyncJob).where(AsyncJob.project_id == project.id)
        )
    ).scalar_one()
    assert job.status == "failed" and job.error_message == "preparation_dispatch_failed"


async def test_final_authority_uses_locked_current_account(
    db_session, super_admin, memory_storage
):

    project, task, _, _, _ = await prepare_fixture(
        db_session, super_admin[0].id, memory_storage
    )
    job, _ = await create_preparation(
        db_session,
        project,
        super_admin[0].id,
        PointCloudDensificationRequest(scope="task_ids", task_ids=[task.id]),
    )
    # Exercise the locking path in this transaction; concurrent revocation
    # locking is covered by the shared project write guard's own tests.
    await worker.check_authority(db_session, job.id, lock=True, task_id=task.id)
    assert db_session.in_transaction()
    assert (
        await db_session.execute(select(AsyncJob).where(AsyncJob.id == job.id))
    ).scalar_one().status == "pending"
    # A current identity-map object must never authorize an inactive account.
    super_admin[0].is_active = False
    await db_session.flush()
    with pytest.raises(Exception, match="preparation_authority_revoked"):
        await worker.check_authority(db_session, job.id)
