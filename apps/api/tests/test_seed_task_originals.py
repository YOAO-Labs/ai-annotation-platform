"""Business fixture cleanup cannot erase independent Task original responsibility."""

import uuid

from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creations
from app.services.storage import storage_service
from tests.test_seed_owned import _build_owned, _cleanup_owned


async def test_namespace_cleanup_preserves_known_and_unfinished_task_originals(
    httpx_client, db_session
):
    namespace = "to" + uuid.uuid4().hex[:8]
    world = await _build_owned(httpx_client, namespace)
    records = []
    try:
        response = await httpx_client.post(
            "/api/v1/__test/seed/login", json={"email": world["admin_email"]}
        )
        assert response.status_code == 200, response.text
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        for complete in (True, False):
            response = await httpx_client.post(
                "/api/v1/files/upload-init",
                headers=headers,
                json={
                    "project_id": world["project_id"],
                    "file_name": "retained.bin",
                    "content_type": "application/octet-stream",
                },
            )
            assert response.status_code == 200, response.text
            task_id = uuid.UUID(response.json()["task_id"])
            task = await db_session.get(Task, task_id)
            allocation = await creations.for_task(
                db_session, task_id=task_id, object_key=task.file_path
            )
            payload = b"retained candidate"
            storage_service.client.put_object(
                Bucket=allocation["staging_bucket"],
                Key=allocation["staging_key"],
                Body=payload,
                ContentType="application/octet-stream",
            )
            if complete:
                response = await httpx_client.post(
                    f"/api/v1/files/upload-complete/{task_id}", headers=headers
                )
                assert response.status_code == 200, response.text
            records.append((task_id, allocation, complete))
        response = await _cleanup_owned(httpx_client, namespace)
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert await db_session.get(Project, uuid.UUID(world["project_id"])) is None
        for task_id, allocation, complete in records:
            assert await db_session.get(Task, task_id) is None
            ledger = await db_session.get(ManagedOriginalCreation, allocation["id"])
            assert ledger.status == ("verified" if complete else "allocated")
            owner = await db_session.get(TaskOriginal, allocation["id"])
            assert bool(owner) == complete
            bucket, key = (
                (owner.bucket, owner.object_key)
                if complete
                else (allocation["staging_bucket"], allocation["staging_key"])
            )
            body = storage_service.client.get_object(Bucket=bucket, Key=key)["Body"]
            try:
                assert body.read() == b"retained candidate"
            finally:
                body.close()
        assert (await _cleanup_owned(httpx_client, namespace)).status_code == 200
    finally:
        # Immutable physical responsibility is removed only by destroying the
        # launcher's whole disposable test mode, never by bypassing its guards.
        assert (await _cleanup_owned(httpx_client, namespace)).status_code == 200
