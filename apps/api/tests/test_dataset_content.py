from datetime import datetime, timezone, timedelta
import hashlib
import io
import os
import uuid

from PIL import Image
from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_source import DatasetScanEntry
from app.db.models.dataset_asset import StorageLocationProbe
from app.schemas.dataset_source import DatasetSourceCreate
from app.schemas.storage_location import FilesystemLocationCreate
from app.services import (
    dataset_scans as scans,
    dataset_content as svc,
    filesystem_storage as fs,
    storage_location as locations,
)


def headers(actor):
    return {"Authorization": f"Bearer {actor[1]}"}


@pytest.fixture
async def content_world(db_session, super_admin, tmp_path, monkeypatch):
    return await create_content_world(db_session, super_admin[0], tmp_path, monkeypatch)


async def create_content_world(db_session, actor, tmp_path, monkeypatch):
    """Also usable with independently committed sessions in resource tests."""
    output = io.BytesIO()
    Image.new("RGB", (48, 32), "red").save(output, format="PNG")
    data = output.getvalue()
    root = tmp_path / "source"
    root.mkdir()
    (root / "图片 空格.png").write_bytes(data)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    os.link(root / "图片 空格.png", replacement / "图片 空格.png")
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(tmp_path)})
    dataset = Dataset(
        display_id=f"ds-{uuid.uuid4().hex[:10]}", name="Content", created_by=actor.id
    )
    db_session.add(dataset)
    await db_session.flush()
    location = await locations.create_filesystem(
        db_session,
        FilesystemLocationCreate(
            name=f"content-{uuid.uuid4().hex}", root_handle="test"
        ),
    )
    probe = fs.probe_directory(location.config, "source/图片 空格.png")
    for node in ["worker:content@test", locations.api_node_id()]:
        await locations.store_probe(
            db_session, location, node, probe, datetime.now(timezone.utc)
        )
    source = await scans.create_source(
        db_session,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name="Content",
            node_id="content@test",
            relative_path="source",
            cache_policy="none",
        ),
        actor,
    )
    run, _ = await scans.start_scan(db_session, source.id, actor)
    from app.db.models.async_job import AsyncJob

    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db_session, run.id, job.celery_task_id, "content@test")
    for _ in range(20):
        work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
        if work is None:
            break
        if work.get("transition"):
            continue
        manifest = fs.scan_directory(
            work["config"],
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest
        )
    await db_session.flush()
    assert run.complete
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    return root, data, actor, dataset, location, source, run, entry


async def test_verify_and_read_exact_content_without_creating_business_samples(
    httpx_client, db_session, super_admin, content_world
):
    root, data, _, _, _, _, run, entry = content_world
    base = f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}"
    digest = hashlib.sha256(data).hexdigest()
    assert (
        await httpx_client.get(
            base + "/content", params={"sha256": digest}, headers=headers(super_admin)
        )
    ).status_code == 409
    result = await httpx_client.post(base + "/verify", headers=headers(super_admin))
    assert result.status_code == 200, result.text
    assert result.json()["sha256"] == digest
    response = await httpx_client.get(
        base + "/content", params={"sha256": digest}, headers=headers(super_admin)
    )
    assert response.status_code == 200 and response.content == data
    assert response.headers["x-content-sha256"] == digest
    assert response.headers["cache-control"] == "no-store"
    assert (
        await httpx_client.get(
            base + "/content", params={"sha256": "0" * 64}, headers=headers(super_admin)
        )
    ).status_code == 409
    assert (
        await httpx_client.get(
            base + "/content",
            params={"sha256": digest},
            headers={**headers(super_admin), "Range": "bytes=0-1"},
        )
    ).status_code == 416
    entries = (
        await httpx_client.get(
            f"/api/v1/dataset-scans/{run.id}/entries", headers=headers(super_admin)
        )
    ).json()["items"]
    assert entries[0]["content_verified"] is True
    assert "node_id" not in str(entries) and str(root) not in str(entries)
    assert await db_session.scalar(select(func.count()).select_from(DatasetItem)) == 0
    assert (root / entry.relative_path).read_bytes() == data
    with pytest.raises(IntegrityError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(DatasetScanEntry)
                .where(
                    DatasetScanEntry.run_id == run.id,
                    DatasetScanEntry.path_digest == entry.path_digest,
                )
                .values(verified_content=None)
            )


async def test_read_requires_current_api_capability_and_source_authority(
    httpx_client, db_session, super_admin, project_admin, content_world
):
    _, _, _, _, location, _, run, entry = content_world
    base = f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}/verify"
    assert (
        await httpx_client.post(base, headers=headers(project_admin))
    ).status_code == 404
    probe = await db_session.get(
        StorageLocationProbe, (location.id, locations.api_node_id())
    )
    probe.observed_at -= timedelta(minutes=6)
    await db_session.flush()
    response = await httpx_client.post(base, headers=headers(super_admin))
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "api_read_not_verified"
    )
    assert entry.verified_content is None


async def test_verified_content_cannot_silently_change(
    httpx_client, super_admin, content_world
):
    root, _, _, _, _, _, run, entry = content_world
    base = f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}"
    response = await httpx_client.post(base + "/verify", headers=headers(super_admin))
    sha = response.json()["sha256"]
    path = root / entry.relative_path
    path.unlink()
    path.write_bytes(b"new content")
    response = await httpx_client.get(
        base + "/content", params={"sha256": sha}, headers=headers(super_admin)
    )
    assert response.status_code == 409
    assert entry.verified_content["sha256"] == sha


@pytest.mark.parametrize("mutation", ["actor", "location", "source"])
async def test_changes_during_read_cannot_commit_receipt(
    db_session, content_world, monkeypatch, mutation
):
    _, _, actor, _, location, source, run, entry = content_world
    original = svc.snapshots.capture
    observed = []

    async def altered(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        observed.append(snapshot)
        if mutation == "actor":
            actor.role = "employee"
        elif mutation == "location":
            location.config_revision += 1
        else:
            source.enabled = False
        await db_session.flush()
        return snapshot

    monkeypatch.setattr(svc.snapshots, "capture", altered)
    with pytest.raises(
        (fs.FilesystemStorageError, svc.snapshots.ContentError, HTTPException)
    ):
        await svc.scan_snapshot(
            db_session, run.id, entry.path_digest, actor, verify=True
        )
    await db_session.refresh(entry)
    assert entry.verified_content is None
    assert observed[0].file.closed


async def test_preexisting_hardlink_cannot_hide_source_directory_replacement(
    db_session, content_world
):
    root, _, actor, _, _, _, run, entry = content_world
    root.rename(root.parent / "old-source")
    (root.parent / "replacement").rename(root)
    with pytest.raises(fs.FilesystemStorageError, match="directory_identity_changed"):
        await svc.scan_snapshot(
            db_session, run.id, entry.path_digest, actor, verify=True
        )
    assert entry.verified_content is None


async def test_initial_content_verification_requires_comparable_scan_host(
    db_session, content_world
):
    _, _, actor, _, _, _, run, entry = content_world
    run.snapshot = {**run.snapshot, "scan_host_id": "other-host"}
    await db_session.flush()
    with pytest.raises(
        svc.snapshots.ContentError, match="initial_verification_node_mismatch"
    ):
        await svc.scan_snapshot(
            db_session, run.id, entry.path_digest, actor, verify=True
        )


@pytest.mark.parametrize("change", ["revoke", "rotate", "scope", "expire"])
async def test_api_key_changes_during_copy_prevent_receipt(
    httpx_client, db_session, super_admin, content_world, monkeypatch, change
):
    from app.db.models.api_key import ApiKey

    _, _, _, _, _, _, run, entry = content_world
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "content", "scopes": ["datasets:write"]},
    )
    assert created.status_code == 201
    key = await db_session.get(ApiKey, uuid.UUID(created.json()["id"]))
    original = svc.snapshots.capture
    captured = []

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        captured.append(result)
        if change == "revoke":
            key.revoked_at = datetime.now(timezone.utc)
        elif change == "rotate":
            key.key_hash = "rotated-key"
        elif change == "scope":
            key.scopes = []
        else:
            key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.flush()
        return result

    monkeypatch.setattr(svc.snapshots, "capture", changed)
    response = await httpx_client.post(
        f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}/verify",
        headers={"Authorization": "Bearer " + created.json()["plaintext"]},
    )
    assert response.status_code == 403
    assert captured[0].file.closed
    await db_session.refresh(entry)
    assert entry.verified_content is None
