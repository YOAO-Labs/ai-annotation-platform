"""Application-level creation isolation and durable business publication contracts."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.services.dataset import DatasetService
from app.services.storage import storage_service
from tests.fakes.managed_creation_storage import CreationStorage


@pytest.fixture
def creation_storage(monkeypatch):
    from app.services import managed_original_creation as creations

    fake = CreationStorage()
    monkeypatch.setattr(storage_service, "client", fake)
    monkeypatch.setattr(creations, "create_client", lambda **_: fake)
    monkeypatch.setattr(
        "app.workers.media.generate_thumbnail.delay", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.workers.media.generate_video_metadata.delay", lambda *a, **k: None
    )
    return fake


async def initialize(httpx_client, db, user, token):
    dataset = await DatasetService(db).create(
        f"creation-{uuid.uuid4()}", "", "image", user.id
    )
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/items/upload-init",
        headers={"Authorization": f"Bearer {token}"},
        json={"file_name": "folder/a.bin", "content_type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    item = await db.get(DatasetItem, uuid.UUID(response.json()["item_id"]))
    return dataset, item


@pytest.mark.asyncio
async def test_direct_replayed_staging_cannot_change_accepted_original_and_retirement_keeps_receipt(
    httpx_client, db_session, super_admin, creation_storage
):
    from app.db.models.managed_original_creation import ManagedOriginalCreation

    user, token = super_admin
    dataset, item = await initialize(httpx_client, db_session, user, token)
    fake = creation_storage
    operation, signed = fake.presigned[-1]
    assert operation == "put_object"
    assert signed["Bucket"] == storage_service.import_bucket
    assert signed["Key"].startswith("__aap_upload_staging__/")
    assert signed["Key"] != item.file_path and item.file_path not in fake.objects
    fake.objects[signed["Key"]] = b"original bytes"
    headers = {"Authorization": f"Bearer {token}"}
    complete_url = f"/api/v1/datasets/{dataset.id}/items/upload-complete/{item.id}"
    response = await httpx_client.post(complete_url, headers=headers)
    assert response.status_code == 200, response.text
    assert fake.objects[item.file_path] == b"original bytes"
    location = await db_session.scalar(
        select(DatasetAssetLocation)
        .join(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == item.id)
    )
    creation_id = location.creation_id
    assert creation_id
    receipt = await db_session.get(ManagedOriginalCreation, creation_id)
    assert receipt.status == "verified"
    assert receipt.object_key == item.file_path
    before = len([call for call in fake.calls if call[0] == "complete"])
    fake.objects[signed["Key"]] = b"replayed bytes"
    response = await httpx_client.post(complete_url, headers=headers)
    assert response.status_code == 200, response.text
    assert fake.objects[item.file_path] == b"original bytes"
    assert len([call for call in fake.calls if call[0] == "complete"]) == before
    assert item.file_size == len(b"original bytes")
    assert location.creation_id == creation_id
    item_id = item.id
    assert await DatasetService(db_session).delete_item(item_id)
    await db_session.commit()
    retirement = await db_session.scalar(
        select(ManagedOriginalRetirement).where(
            ManagedOriginalRetirement.dataset_item_id == item_id
        )
    )
    assert retirement.creation_id == creation_id
    assert retirement.sha256 == hashlib.sha256(b"original bytes").hexdigest()
    assert await db_session.get(ManagedOriginalCreation, creation_id)
    assert not [call for call in fake.calls if call[0] == "delete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [b"client bytes", b"other owner bytes"])
async def test_direct_final_collision_never_adopts_or_deletes_same_digest(
    httpx_client, db_session, super_admin, creation_storage, existing
):
    from app.db.models.managed_original_creation import ManagedOriginalCreation

    user, token = super_admin
    dataset, item = await initialize(httpx_client, db_session, user, token)
    fake = creation_storage
    fake.objects[fake.presigned[-1][1]["Key"]] = b"client bytes"
    fake.objects[item.file_path] = existing
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/items/upload-complete/{item.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409, response.text
    assert fake.objects[item.file_path] == existing
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    assert asset.verification_state == "unverified"
    creation = await db_session.scalar(
        select(ManagedOriginalCreation).where(
            ManagedOriginalCreation.object_key == item.file_path
        )
    )
    assert creation.status == "conflict"
    assert not [call for call in fake.calls if call[0] == "delete"]


def test_final_managed_namespace_never_gets_client_put_url(creation_storage):
    from app.services.dataset_storage_keys import managed_dataset_key

    key = managed_dataset_key(uuid.uuid4(), "a.bin")
    with pytest.raises(ValueError, match="controlled server creation"):
        storage_service.generate_upload_url(key, bucket=storage_service.datasets_bucket)
    assert creation_storage.presigned == []
