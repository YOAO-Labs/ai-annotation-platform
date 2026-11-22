"""Supported-media dispatch and durable owned cache publication regressions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services import export_media_bundles, export_outputs, export_storage
from app.services.content_snapshot import ContentError
from app.services.exporting import cache
from app.workers import export as export_worker
from app.workers.celery_app import celery_app
from app.workers import lidar_export as owned_worker
from sqlalchemy import select
from tests.test_lidar_export_inputs import input_db as input_db, setup
from tests.test_lidar_export_worker import (
    Harness,
    request,
    state,
    worker_db as worker_db,
)


class _CacheDB:
    def __init__(self, value: object) -> None:
        self.value = value
        self.deleted: list[object] = []

    async def scalar(self, _statement: object):
        return self.value

    async def delete(self, value: object) -> None:
        self.deleted.append(value)


def test_cache_key_is_target_order_independent_and_contract_sensitive() -> None:
    scope_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    updated_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
    base = cache.compute_cache_key(
        scope_id,
        ["coco", "yolo"],
        True,
        "keyframes",
        updated_at,
        5,
    )
    assert base == "7b334c5eaaac40eed576b1afa0247db6306df8a96bb6e6a19c26a09d593149ef"
    assert base != "81b5ba8014714ec9ccdef7792801b8fb8624ec1f748174acbf2143af0198b9bf"
    assert base == cache.compute_cache_key(
        scope_id,
        ["yolo", "coco"],
        True,
        "keyframes",
        updated_at,
        5,
    )
    assert base != cache.compute_cache_key(
        scope_id,
        ["coco", "yolo"],
        True,
        "keyframes",
        updated_at,
        4,
    )
    assert base != cache.compute_cache_key(
        scope_id,
        ["coco", "yolo"],
        True,
        "keyframes",
        updated_at,
        5,
        axis_frame="source",
    )
    front = cache.compute_cache_key(
        scope_id,
        ["kitti"],
        True,
        "keyframes",
        updated_at,
        5,
        options_digest=export_worker.canonical_digest(
            {"lidar_camera_role": "camera_front"}
        ),
    )
    left = cache.compute_cache_key(
        scope_id,
        ["kitti"],
        True,
        "keyframes",
        updated_at,
        5,
        options_digest=export_worker.canonical_digest(
            {"lidar_camera_role": "camera_left"}
        ),
    )
    assert front != left


@pytest.mark.asyncio
async def test_cache_lookup_delegates_saved_output_verification(monkeypatch):
    artifact = SimpleNamespace(object_key="video/project/job.zip")
    db = _CacheDB(artifact)
    verify = AsyncMock(return_value=None)
    monkeypatch.setattr(cache.export_outputs, "verify_cache", verify)
    assert await cache.lookup(db, "cache-key") is None
    verify.assert_awaited_once_with(db, artifact)
    assert db.deleted == []


@pytest.mark.asyncio
async def test_cache_lookup_returns_explicit_verified_hit(monkeypatch):
    artifact, hit = object(), object()
    db = _CacheDB(artifact)
    verify = AsyncMock(return_value=hit)
    monkeypatch.setattr(cache.export_outputs, "verify_cache", verify)
    assert await cache.lookup(db, "cache-key") is hit
    verify.assert_awaited_once_with(db, artifact)


def test_export_worker_registration_and_route_are_stable() -> None:
    task_name = "app.workers.export.run_export"
    assert export_worker.run_export.name == task_name
    assert celery_app.conf.task_routes[task_name] == {"queue": "export"}


async def test_mapped_legacy_output_honors_prepared_target_and_current_authority(
    monkeypatch,
):
    candidate = SimpleNamespace(
        storage_ref={"schema": 1}, expires_at=datetime.max.replace(tzinfo=timezone.utc)
    )
    db, job, prepared = object(), object(), object()
    ready = AsyncMock(return_value="https://target.invalid/accurate-version")
    authority = AsyncMock()
    direct = Mock(side_effect=AssertionError("mapped legacy must not sign source"))
    monkeypatch.setattr(export_outputs, "prepared_download", ready)
    monkeypatch.setattr(export_worker.output_authority, "validate", authority)
    monkeypatch.setattr(export_storage, "sign", direct)
    assert (
        await export_worker._sign_export_output(
            db, job, candidate, "export.zip", prepared=prepared
        )
        == "https://target.invalid/accurate-version"
    )
    ready.assert_awaited_once_with(
        db, candidate, download_name="export.zip", prepared=prepared
    )
    authority.assert_awaited_once_with(db, job, lock=True)
    direct.assert_not_called()


def test_owned_video_cache_key_uses_full_frozen_input_digest():
    project_id = uuid.uuid4()

    def receipt(digest, **options):
        return SimpleNamespace(
            project_id=project_id,
            input_digest=digest,
            _input_snapshot={
                "schema": 4,
                "batch_id": None,
                "targets": ["davis", "mots"],
                "annotations": [],
                "options": options,
            },
        )

    first = receipt("1" * 64)
    assert owned_worker._cache_key(first) != owned_worker._cache_key(receipt("2" * 64))
    assert owned_worker._cache_key(first) == owned_worker._cache_key(receipt("1" * 64))
    assert owned_worker._cache_key(first) != owned_worker._cache_key(
        receipt("1" * 64, axis_frame="source")
    )


async def test_claimed_owner_duplicate_cannot_copy_or_replace_live_attempt(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    claimed = await export_media_bundles.claim(db, receipt.id)
    assert claimed is not None
    row = await db.get(AsyncJob, job_id)
    row.status, row.celery_task_id, row.progress_pct = (
        "running",
        args["celery_task_id"],
        41,
    )
    await db.commit()
    await owned_worker.run_owned(db, **args)
    current = await state(db, AsyncJob, job_id)
    assert current.status == "running" and current.progress_pct == 41
    current_owner = await state(db, ExportMediaBundle, receipt.id)
    assert current_owner.lease_token == claimed.lease_token
    assert current_owner.lease_expires_at == claimed.lease_expires_at
    assert harness.prepares == 0 and not harness.objects and not harness.copies
    assert await db.get(ExportOutput, receipt.zip_output_id) is None
    assert not list((await db.scalars(select(ExportArtifact))).all())


async def test_verified_cache_hit_rechecks_authority_before_returning_url(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    _, original, args = await request(db, project)
    await owned_worker.run_owned(db, **args)
    job_id, receipt, args = await request(db, project)
    lookup = owned_worker._lookup
    verified = []

    async def revoke_after_verification(*args, **kwargs):
        hit = await lookup(*args, **kwargs)
        assert hit is not None
        verified.append(hit)
        harness.revoked = True
        return hit

    monkeypatch.setattr(owned_worker, "_lookup", revoke_after_verification)
    with pytest.raises(ContentError, match="original_key_revoked"):
        await owned_worker.run_owned(db, **args)
    assert verified and harness.prepares == 1 and len(harness.objects) == 1
    assert (await state(db, AsyncJob, job_id)).status == "failed"
    assert (await state(db, ExportMediaBundle, original.id)).state == "ready"
    unused = await state(db, ExportMediaBundle, receipt.id)
    assert unused.state == "building" and unused.published_at is None
    assert await db.get(ExportOutput, receipt.zip_output_id) is None
    assert len(harness.notices) == 2


async def test_cache_record_failure_keeps_physical_candidate_and_pending_owner(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    delete = Mock()
    monkeypatch.setattr(export_storage, "delete", delete)
    record = AsyncMock(side_effect=RuntimeError("cache record failed"))
    monkeypatch.setattr(owned_worker.cache, "record", record)
    with pytest.raises(RuntimeError, match="cache record failed"):
        await owned_worker.run_owned(db, **args)
    record.assert_awaited_once()
    assert (await state(db, AsyncJob, job_id)).status == "failed"
    output = await state(db, ExportOutput, receipt.zip_output_id)
    assert output.state == "pending" and output.media_bundle_id == receipt.id
    saved_output = export_outputs.snapshot(output)
    owner = await state(db, ExportMediaBundle, receipt.id)
    assert owner.state == "building" and owner.published_at is None
    assert harness.objects[saved_output.id] and harness.prepares == 1
    assert all(not path.exists() for path in harness.built_paths)
    delete.assert_not_called()
    assert not list((await db.scalars(select(ExportArtifact))).all())
