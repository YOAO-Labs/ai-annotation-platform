"""Opt-in real S3 acceptance for staged source publication."""

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_upload_intent import DatasetUploadIntent
from app.services.storage import storage_service

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


def _image(colour):
    result = BytesIO()
    Image.new("RGB", (23, 31), colour).save(result, format="PNG")
    return result.getvalue()


def _verify_bucket_scope():
    tmpdir = Path(os.environ["TMPDIR"]).resolve()
    assert (
        tmpdir.name == "tmp"
        and tmpdir.parent.name == "test"
        and tmpdir.parent.parent.name == ".worktree"
    )
    manifest_path = tmpdir.parent / "resources.json"
    assert not manifest_path.is_symlink()
    manifest = json.loads(manifest_path.read_text())
    resources = manifest["resources"]
    assert (
        resources["mode"] == "test"
        and manifest["minio"]["endpoint"] == settings.minio_endpoint
    )
    assert resources["buckets"]["MINIO_IMPORT_BUCKET"] == storage_service.import_bucket
    assert (
        resources["buckets"]["MINIO_DATASETS_BUCKET"] == storage_service.datasets_bucket
    )
    owner = resources["owner"]
    for bucket in (storage_service.import_bucket, storage_service.datasets_bucket):
        tags = storage_service.client.get_bucket_tagging(Bucket=bucket)["TagSet"]
        assert any(
            tag["Key"] == "aap-worktree-owner" and tag["Value"] == owner for tag in tags
        ), "S3 bucket ownership changed"


async def _put(url, content):
    # The browser may use a frontend proxy path; this test uses its original MinIO origin.
    public = (
        settings.minio_public_url.rstrip("/") if settings.minio_public_url else None
    )
    if public and url.startswith(public + "/"):
        scheme = "https" if settings.minio_use_ssl else "http"
        url = f"{scheme}://{settings.minio_endpoint}" + url[len(public) :]
    parsed = urlsplit(url)
    assert parsed.netloc == settings.minio_endpoint
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.put(
                url, content=content, headers={"Content-Type": "image/png"}
            )
    except httpx.HTTPError:
        pytest.fail("The owned staging PUT failed")
    assert response.status_code == 200


async def test_real_staging_put_cannot_change_completed_source_and_duplicate_is_retained(
    httpx_client, db_session, super_admin
):
    _verify_bucket_scope()
    user, token = super_admin
    dataset_id = uuid4()
    db_session.add(
        Dataset(
            id=dataset_id,
            display_id=f"DS-S3-{uuid4().hex[:12]}",
            name=f"source-s3-{uuid4().hex}",
            data_type="image",
            created_by=user.id,
        )
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    exact_keys = []
    original, changed = _image("red"), _image("blue")
    try:
        initialized = await httpx_client.post(
            f"/api/v1/datasets/{dataset_id}/items/upload-init",
            headers=headers,
            json={"file_name": "sub/source.png", "content_type": "image/png"},
        )
        assert initialized.status_code == 200, initialized.text
        item_id = UUID(initialized.json()["item_id"])
        stage = f"dataset-uploads/{item_id}/source"
        exact_keys.append((storage_service.import_bucket, stage))
        await _put(initialized.json()["upload_url"], original)
        path = f"/api/v1/datasets/{dataset_id}/items/upload-complete/{item_id}"
        completed = await httpx_client.post(path, headers=headers)
        assert completed.status_code == 200, completed.text
        item = await db_session.get(DatasetItem, item_id, populate_existing=True)
        final = item.file_path
        exact_keys.append((storage_service.datasets_bucket, final))
        assert (item.width, item.height) == (23, 31)
        await _put(initialized.json()["upload_url"], changed)
        replay = await httpx_client.post(path, headers=headers)
        assert replay.json() == completed.json()
        response = storage_service.client.get_object(
            Bucket=storage_service.datasets_bucket, Key=final
        )
        try:
            assert response["Body"].read() == original
        finally:
            response["Body"].close()
        duplicate = await httpx_client.post(
            f"/api/v1/datasets/{dataset_id}/items/upload-init",
            headers=headers,
            json={"file_name": "same.png", "content_type": "image/png"},
        )
        duplicate_id = UUID(duplicate.json()["item_id"])
        exact_keys.append(
            (storage_service.import_bucket, f"dataset-uploads/{duplicate_id}/source")
        )
        await _put(duplicate.json()["upload_url"], original)
        rejected = await httpx_client.post(
            f"/api/v1/datasets/{dataset_id}/items/upload-complete/{duplicate_id}",
            headers=headers,
        )
        assert rejected.status_code == 409 and rejected.json()["detail"][
            "duplicate_of"
        ] == str(item_id)
        deleted = await httpx_client.delete(
            f"/api/v1/datasets/{dataset_id}/items/{item_id}", headers=headers
        )
        assert deleted.status_code == 204, deleted.text
        assert storage_service.client.head_object(
            Bucket=storage_service.datasets_bucket, Key=final
        )["ContentLength"] == len(original)
    finally:
        for bucket, key in exact_keys:
            await asyncio.to_thread(
                storage_service.client.delete_object, Bucket=bucket, Key=key
            )


async def test_real_copy_etag_race_preserves_pending_intent_for_a_new_attempt(
    httpx_client, db_session, super_admin, monkeypatch
):
    _verify_bucket_scope()
    user, token = super_admin
    dataset_id = uuid4()
    db_session.add(
        Dataset(
            id=dataset_id,
            display_id=f"DS-RACE-{uuid4().hex[:10]}",
            name=f"source-race-{uuid4().hex}",
            data_type="image",
            created_by=user.id,
        )
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    initialized = await httpx_client.post(
        f"/api/v1/datasets/{dataset_id}/items/upload-init",
        headers=headers,
        json={"file_name": "source.png", "content_type": "image/png"},
    )
    assert initialized.status_code == 200, initialized.text
    item_id = UUID(initialized.json()["item_id"])
    stage, final_keys = f"dataset-uploads/{item_id}/source", []
    original_copy = storage_service.client.copy_object
    original, changed = _image("red"), _image("blue")

    def change_before_copy(**kwargs):
        final_keys.append(kwargs["Key"])
        storage_service.client.put_object(
            Bucket=storage_service.import_bucket,
            Key=stage,
            Body=changed,
            ContentType="image/png",
        )
        return original_copy(**kwargs)

    try:
        await _put(initialized.json()["upload_url"], original)
        monkeypatch.setattr(storage_service.client, "copy_object", change_before_copy)
        path = f"/api/v1/datasets/{dataset_id}/items/upload-complete/{item_id}"
        rejected = await httpx_client.post(path, headers=headers)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"] == {"reason": "source_changed"}
        await db_session.rollback()
        assert await db_session.get(DatasetItem, item_id) is None
        intent = await db_session.get(
            DatasetUploadIntent, item_id, populate_existing=True
        )
        assert intent.state == "pending" and intent.publication_id is None
        monkeypatch.setattr(storage_service.client, "copy_object", original_copy)
        completed = await httpx_client.post(path, headers=headers)
        assert completed.status_code == 200, completed.text
        final = await db_session.scalar(
            select(DatasetItem.file_path).where(DatasetItem.id == item_id)
        )
        assert final != final_keys[0]
        final_keys.append(final)
    finally:
        for key in final_keys:
            await asyncio.to_thread(
                storage_service.client.delete_object,
                Bucket=storage_service.datasets_bucket,
                Key=key,
            )
        await asyncio.to_thread(
            storage_service.client.delete_object,
            Bucket=storage_service.import_bucket,
            Key=stage,
        )
