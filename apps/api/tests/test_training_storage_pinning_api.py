"""Explicit legacy verification, authorization and pinned download publication."""

from copy import deepcopy
import threading
import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset_asset import StorageBinding
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import storage_access
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_project
from tests.test_training_storage_adoption import retained as retained
from tests.test_training_instance_rle import frozen as frozen


@pytest.fixture
async def legacy_version(db_session, super_admin, retained, monkeypatch):
    from app.api.v1 import training_datasets as api

    store, old_owner, values = retained
    actor, token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    identifier = uuid.uuid4()
    prefix = f"projects/{project.id}/datasets/{identifier}/"
    store.objects = {
        key.replace(old_owner.prefix, prefix, 1): data
        for key, data in store.objects.items()
    }
    store.metadata = {
        key.replace(old_owner.prefix, prefix, 1): metadata
        for key, metadata in store.metadata.items()
    }
    values = deepcopy(values)
    values["package_key"] = values["package_key"].replace(old_owner.prefix, prefix, 1)
    version = TrainingDatasetVersion(
        id=identifier,
        project_id=project.id,
        creator_id=actor.id,
        request_id=uuid.uuid4(),
        request_digest=content_digest(values),
        name="Legacy retained copy",
        **values,
    )
    db_session.add(version)
    await ensure_legacy_bindings(db_session)
    await db_session.flush()
    store.bucket = storage_service.training_bucket
    monkeypatch.setattr(api, "_adoption_capacity", threading.BoundedSemaphore(1))

    def verify(owner, state):
        from app.services.training_storage_adoption import verify

        return verify(store, owner, state)

    monkeypatch.setattr(api, "_verify_retained_operation", verify)
    return version, store, {"Authorization": f"Bearer {token}"}


async def test_get_does_not_adopt_explicit_pin_is_idempotent_and_download_uses_saved_bucket(
    httpx_client, db_session, legacy_version, monkeypatch
):
    version, store, auth = legacy_version
    path = f"/api/v1/projects/{version.project_id}/training-datasets/{version.id}"
    response = await httpx_client.get(path, headers=auth)
    assert response.status_code == 200, response.text
    assert version.storage_ref is None and store.reads == []
    snapshot, manifest = deepcopy(version.snapshot), deepcopy(version.manifest)
    original_bucket = store.bucket
    # An explicit candidate comes from the registered binding, never this value.
    monkeypatch.setattr(
        storage_service, "training_bucket", "different-current-training"
    )
    response = await httpx_client.post(path + "/storage-pin", headers=auth)
    assert response.status_code == 200, response.text
    await db_session.refresh(version)
    assert version.storage_ref["bucket"] == original_bucket
    assert version.storage_ref["origin"] == "verified-retained"
    assert version.storage_ref["evidence"]["object_count"] == len(store.objects)
    assert version.snapshot == snapshot and version.manifest == manifest
    reads = len(store.reads)
    again = await httpx_client.post(path + "/storage-pin", headers=auth)
    assert again.status_code == 200 and len(store.reads) == reads
    signed = []
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key, *, bucket: bucket == original_bucket and key in store.objects,
    )

    def sign(key, *, bucket, **kwargs):
        signed.append((key, bucket))
        return "http://owned-storage/saved-copy"

    monkeypatch.setattr(storage_service, "generate_download_url", sign)
    downloaded = await httpx_client.get(path + "/download", headers=auth)
    assert downloaded.status_code == 200, downloaded.text
    assert signed == [(version.package_key, original_bucket)]


@pytest.mark.parametrize("change", ["missing", "changed", "active"])
async def test_unavailable_changed_or_active_legacy_copy_never_pins(
    httpx_client, db_session, legacy_version, change
):
    version, store, auth = legacy_version
    if change == "missing":
        store.objects.pop(version.package_key)
    elif change == "changed":
        store.objects[version.package_key] = b"different package"
    else:
        version.status = "running"
        await db_session.flush()
    before = deepcopy(store.objects)
    response = await httpx_client.post(
        f"/api/v1/projects/{version.project_id}/training-datasets/{version.id}/storage-pin",
        headers=auth,
    )
    assert response.status_code == 409, response.text
    await db_session.refresh(version)
    assert version.storage_ref is None and store.objects == before


async def test_candidate_change_during_io_rejects_its_previous_result(
    httpx_client, db_session, legacy_version, monkeypatch
):
    version, store, auth = legacy_version
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        binding = await db_session.scalar(
            select(StorageBinding).where(StorageBinding.purpose == "training")
        )
        binding.bucket = "changed-persisted-binding"
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        f"/api/v1/projects/{version.project_id}/training-datasets/{version.id}/storage-pin",
        headers=auth,
    )
    assert response.status_code == 409, response.text
    await db_session.refresh(version)
    assert version.storage_ref is None


async def test_employee_cannot_adopt_a_project_owners_copy(
    httpx_client, legacy_version, annotator
):
    version, store, _ = legacy_version
    _, token = annotator
    response = await httpx_client.post(
        f"/api/v1/projects/{version.project_id}/training-datasets/{version.id}/storage-pin",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code in {403, 404}
    assert not store.reads and version.storage_ref is None


@pytest.mark.parametrize("change", ["revoke", "rotate", "scope", "expire"])
async def test_api_key_change_during_verification_never_publishes_pin(
    httpx_client, db_session, legacy_version, monkeypatch, change
):
    from datetime import datetime, timedelta, timezone
    from app.db.models.api_key import ApiKey

    version, store, auth = legacy_version
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=auth,
        json={
            "name": "legacy pin",
            "scopes": ["annotations:read", "datasets:read", "datasets:write"],
        },
    )
    assert created.status_code == 201, created.text
    key = await db_session.get(ApiKey, uuid.UUID(created.json()["id"]))
    original = storage_access.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "revoke":
            key.revoked_at = datetime.now(timezone.utc)
        elif change == "rotate":
            key.key_hash = "rotated-key"
        elif change == "scope":
            key.scopes = ["datasets:read"]
        else:
            key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.flush()
        return result

    monkeypatch.setattr(storage_access, "run", changed)
    response = await httpx_client.post(
        f"/api/v1/projects/{version.project_id}/training-datasets/{version.id}/storage-pin",
        headers={"Authorization": "Bearer " + created.json()["plaintext"]},
    )
    assert response.status_code == 403, response.text
    await db_session.refresh(version)
    assert version.storage_ref is None and store.reads
