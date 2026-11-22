"""Business fixture cleanup cannot erase independent Task original responsibility."""

import uuid

from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creations
from app.services.storage import storage_service
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    fixture_and_fenced_http as fixture_and_fenced_http,
)
from tests.seed_owned_scenarios import _build_owned, _cleanup_owned


async def test_namespace_cleanup_preserves_known_and_unfinished_task_originals(
    fixture_and_fenced_http, committed_owner
):
    httpx_client = fixture_and_fenced_http
    db_session = committed_owner
    run = httpx_client.owner_suite.new_run("to" + uuid.uuid4().hex[:8])
    world = await _build_owned(httpx_client, run)
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
            await db_session.rollback()
            payload = b"retained candidate"
            staged = storage_service.client.put_object(
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
            records.append((task_id, allocation, complete, staged.get("VersionId")))
        response = await _cleanup_owned(httpx_client, run)
        assert response.status_code == 200, response.text
        db_session.expire_all()
        assert await db_session.get(Project, uuid.UUID(world["project_id"])) is None
        for task_id, allocation, complete, staged_version in records:
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
            request = {"Bucket": bucket, "Key": key}
            version = owner.version_id if complete else staged_version
            if version and version != "null":
                request["VersionId"] = version
            body = storage_service.client.get_object(**request)["Body"]
            try:
                assert body.read() == b"retained candidate"
            finally:
                body.close()
        assert (await _cleanup_owned(httpx_client, run)).status_code == 200
    finally:
        assert (await _cleanup_owned(httpx_client, run)).status_code == 200
