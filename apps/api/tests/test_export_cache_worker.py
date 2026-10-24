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
from app.services.exporting.reference_media import ExportCancelled
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


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


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


@pytest.fixture
def owned_dispatch(monkeypatch):
    """Exercise outer routing; the shared publisher owns all terminal writes."""
    project = SimpleNamespace(id=uuid.uuid4(), data_type="video")
    job = SimpleNamespace(
        id=uuid.uuid4(), status="pending", celery_task_id=None, payload={}
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=project),
        execute=AsyncMock(return_value=_ScalarResult(job)),
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    class Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(export_worker, "async_sessionmaker", Mock(return_value=Session))
    delegate = AsyncMock()
    monkeypatch.setattr(owned_worker, "run_owned", delegate)
    generic = {}
    for module, names in (
        (export_worker, ["build_export_zip", "_sign_export_output"]),
        (export_worker.export_cache, ["lookup", "record"]),
        (export_worker.async_job_svc, ["mark_running", "mark_complete", "mark_failed"]),
    ):
        for name in names:
            generic[name] = AsyncMock()
            monkeypatch.setattr(module, name, generic[name])
    redis = Mock()
    monkeypatch.setattr(export_worker.aioredis, "from_url", redis)
    args = dict(
        project_id=str(project.id),
        batch_id=str(uuid.uuid4()),
        task_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
        targets=["aap_json"],
        opts={"include_attributes": False},
        async_job_id=str(job.id),
        celery_task_id="owned-delivery",
        worker_node_id="authorized-worker-node",
    )
    return SimpleNamespace(
        project=project,
        job=job,
        db=db,
        engine=engine,
        delegate=delegate,
        generic=generic,
        redis=redis,
        args=args,
    )


def assert_generic_untouched(wiring):
    for mock in wiring.generic.values():
        mock.assert_not_awaited()
    wiring.redis.assert_not_called()
    wiring.engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media,targets",
    [
        ("video", ["davis", "mots"]),
        ("video", ["aap_json"]),
        ("image", ["coco"]),
        ("lidar", ["pointmask", "aap_json"]),
    ],
)
async def test_supported_media_delegates_exact_selection_before_generic_cache(
    owned_dispatch, media, targets
):
    wiring = owned_dispatch
    wiring.project.data_type = media
    args = {**wiring.args, "targets": targets}
    await export_worker._run_export(**args)
    expected = {**args}
    if media == "lidar":
        expected.pop("worker_node_id")
    else:
        expected["media_kind"] = media
    wiring.delegate.assert_awaited_once_with(wiring.db, **expected)
    assert wiring.db.rollback.await_count == 1
    wiring.db.commit.assert_not_awaited()
    assert_generic_untouched(wiring)


@pytest.mark.asyncio
@pytest.mark.parametrize("media", ["video", "image"])
async def test_durable_media_proof_still_routes_when_project_is_unavailable(
    owned_dispatch, media
):
    wiring = owned_dispatch
    wiring.db.get.return_value = None
    wiring.job.payload = {media + "_media": {"bundle_id": str(uuid.uuid4())}}
    await export_worker._run_export(**wiring.args)
    wiring.delegate.assert_awaited_once_with(wiring.db, **wiring.args, media_kind=media)
    assert_generic_untouched(wiring)


@pytest.mark.asyncio
@pytest.mark.parametrize("media", ["video", "image", "lidar"])
async def test_owned_authority_failure_cannot_fall_back_to_generic_publication(
    owned_dispatch, media
):
    wiring = owned_dispatch
    wiring.project.data_type = media
    args = {
        **wiring.args,
        "targets": ["pointmask"] if media == "lidar" else ["aap_json"],
    }
    failure = ContentError("original_key_revoked")
    wiring.delegate.side_effect = failure
    with pytest.raises(ContentError) as caught:
        await export_worker._run_export(**args)
    assert caught.value is failure and wiring.delegate.await_count == 1
    assert wiring.db.rollback.await_count >= 2
    wiring.db.commit.assert_not_awaited()
    assert_generic_untouched(wiring)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "completed"])
@pytest.mark.parametrize("media", ["video", "image"])
async def test_late_owned_error_preserves_committed_terminal_job(
    owned_dispatch, terminal, media
):
    wiring = owned_dispatch
    wiring.project.data_type = media
    wiring.delegate.side_effect = OSError("commit acknowledgement unknown")
    wiring.db.scalar.return_value = terminal
    await export_worker._run_export(**wiring.args)
    wiring.delegate.assert_awaited_once()
    wiring.db.scalar.assert_awaited_once()
    wiring.db.commit.assert_not_awaited()
    assert_generic_untouched(wiring)


@pytest.mark.asyncio
async def test_owned_cancel_is_not_rewritten_as_failure(owned_dispatch):
    wiring = owned_dispatch
    wiring.delegate.side_effect = ExportCancelled()
    await export_worker._run_export(**wiring.args)
    wiring.delegate.assert_awaited_once()
    wiring.db.commit.assert_not_awaited()
    assert_generic_untouched(wiring)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "running"])
async def test_terminal_and_stale_deliveries_never_reenter_owned_publisher(
    owned_dispatch, status
):
    wiring = owned_dispatch
    wiring.job.status, wiring.job.celery_task_id = status, "other-active-delivery"
    await export_worker._run_export(**wiring.args)
    wiring.delegate.assert_not_awaited()
    assert_generic_untouched(wiring)


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
    # Equal project, targets and annotation counts cannot alias changed originals,
    # business facts, or probe receipts represented in the frozen input digest.
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
