from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
import sys
import time
from uuid import uuid4

from PIL import Image
import pytest

from aap_assets_images.contract import (
    Artifact,
    ClaimedImageWork,
    DimensionsWorkSpec,
    ImageBackfillProgress,
    ImageBackfillSelection,
    ImageBackfillSpec,
    ImageClaim,
    ImageFailure,
    ImageLeaseState,
    ImageOrphanCandidate,
    ImageOwner,
    ImageReconcileResult,
    ImageSource,
    ImageWorkOutcome,
    ObjectIdentity,
    PyramidWorkSpec,
    ThumbnailWorkSpec,
)
from aap_assets_images.policy import ImagePolicy
from aap_assets_images_runtime.config import ConfigurationError, load_config
from aap_assets_images_runtime.storage import DownloadedSource, SourceHeader
from aap_assets_images_runtime.worker import ImageExecutor, _run_child


def _png_bytes(size=(4, 2)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (50, 100, 150)).save(output, format="PNG")
    return output.getvalue()


def _jpeg_with_large_icc(size=(4, 2), *, icc_bytes=300 * 1024) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (50, 100, 150)).save(
        output, format="JPEG", quality=85, icc_profile=b"i" * icc_bytes
    )
    return output.getvalue()


def _policy(**patch) -> ImagePolicy:
    return ImagePolicy(job_timeout_seconds=30, lease_seconds=91, **patch)


def _source(data: bytes, *, width=None, height=None) -> ImageSource:
    item_id = uuid4()
    return ImageSource(
        owner=ImageOwner(kind="dataset_item", id=item_id),
        task_id=None,
        project_id=None,
        dataset_id=uuid4(),
        item_id=item_id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity=ObjectIdentity(
            etag="source-etag", byte_size=len(data), version_id="v1"
        ),
        source_drifted=False,
        upload_ready=True,
        file_path="dataset/image.png",
        file_name="image.png",
        file_type="image",
        bucket_kind="datasets",
        file_size=len(data),
        content_hash=None,
        width=width,
        height=height,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


def _claim(source: ImageSource | None, spec, *, job_id=None) -> ImageClaim:
    job_id = job_id or uuid4()
    lease = uuid4()
    kind = spec.kind
    owner = None if kind == "image_backfill" else source.owner
    output_prefix = None
    asset_id = None
    pyramid_generation = None
    if kind == "thumbnail":
        output_prefix = f"image-assets/{job_id}/g1/a{lease}/"
    elif kind == "pyramid":
        asset_id = uuid4()
        pyramid_generation = 1
        output_prefix = f"image-pyramids/{asset_id}/g1/a{lease}/"
    return ImageClaim(
        job_id=job_id,
        generation=1,
        lease_token=lease,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        owner=owner,
        source=source,
        spec=spec,
        output_prefix=output_prefix,
        pyramid_asset_id=asset_id,
        pyramid_generation=pyramid_generation,
    )


class _FakeDatabase:
    def __init__(self, claim, *, advance_results=None, finish_failures=None):
        self.claim = claim
        self.advance_results = list(advance_results or [])
        self.finish_failures = list(finish_failures or [])
        self.calls = []

    async def call(self, name, request):
        self.calls.append((name, request))
        if name == "claim_image_media":
            return ClaimedImageWork(claim=self.claim)
        if name == "heartbeat_image_media":
            return ImageLeaseState(
                job_id=request.job_id,
                generation=request.generation,
                lease_token=request.lease_token,
                active=True,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        if name == "advance_image_backfill":
            return self.advance_results.pop(0)
        if name == "reconcile_image_media":
            return ImageReconcileResult(
                expired_jobs=0,
                requeued_jobs=0,
                failed_jobs=0,
                canceled_jobs=0,
                retired_generations=0,
                queued_orphans=len(request.candidates),
                ignored_candidates=0,
                more=False,
            )
        if name == "fail_image_media":
            return ImageWorkOutcome(
                job_id=request.job_id,
                generation=request.generation,
                outcome="failed",
            )
        if name == "finish_image_media":
            if self.finish_failures:
                raise ImageFailure(self.finish_failures.pop(0))
            return ImageWorkOutcome(
                job_id=request.job_id,
                generation=request.generation,
                outcome="published",
            )
        raise AssertionError(f"unexpected database operation: {name}")


class _FakeStorage:
    def __init__(self, source_bytes=b"", *, header=None, heads=None):
        self.source_bytes = source_bytes
        self.header = header if header is not None else source_bytes
        self.heads = list(heads or [])
        self.headers = []
        self.downloads = []
        self.uploads = []
        self.sources = []
        self.orphans = []

    async def head_source(self, source):
        self.sources.append(source)
        return self.heads.pop(0) if self.heads else source.object_identity

    async def read_source_header(self, source):
        self.headers.append(source)
        return SourceHeader(
            identity=source.object_identity,
            data=self.header[: 256 * 1024],
        )

    async def download_source(self, source, destination):
        self.downloads.append(source)
        destination.write_bytes(self.source_bytes)
        return DownloadedSource(
            identity=source.object_identity,
            byte_size=len(self.source_bytes),
            sha256=hashlib.sha256(self.source_bytes).hexdigest(),
        )

    async def upload_cache(self, path, key):
        data = Path(path).read_bytes()
        artifact = Artifact(
            key=key,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self.uploads.append((key, data, artifact))
        return artifact

    async def iter_orphan_generations(self):
        for candidate in self.orphans:
            yield candidate


def _image_backfill_claim(source: ImageSource, policy: ImagePolicy):
    selection = ImageBackfillSelection(
        owner_kind="dataset_item",
        dataset_id=source.dataset_id,
        purpose="thumbnail",
    )
    return _claim(None, ImageBackfillSpec(selection=selection, policy=policy))


def test_config_requires_explicit_legacy_bucket_and_hides_all_secrets():
    values = {
        "ASSETS_IMAGES_DATABASE_URL": "postgresql://image:db-secret@db/image",
        "ASSETS_MEDIA_S3_ENDPOINT": "http://s3.internal",
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": "http://s3.public",
        "ASSETS_MEDIA_DATASETS_BUCKET": "datasets",
        "ASSETS_MEDIA_CACHE_BUCKET": "derived-cache",
        "ASSETS_MEDIA_S3_ACCESS_KEY": "access-secret",
        "ASSETS_MEDIA_S3_SECRET_KEY": "secret-secret",
        "ASSETS_IMAGES_LEGACY_BUCKET": "annotations",
    }

    config = load_config(values)

    assert config.storage.legacy_bucket == "annotations"
    assert config.srgb_profile == Path("/usr/share/color/icc/sRGB.icc")
    rendered = repr(config)
    assert "db-secret" not in rendered
    assert "access-secret" not in rendered
    assert "secret-secret" not in rendered
    with pytest.raises(ConfigurationError, match="ASSETS_IMAGES_LEGACY_BUCKET"):
        load_config(
            {
                key: value
                for key, value in values.items()
                if key != "ASSETS_IMAGES_LEGACY_BUCKET"
            }
        )


def test_config_accepts_only_existing_explicit_minio_bucket_mapping():
    values = {
        "ASSETS_IMAGES_DATABASE_URL": "postgresql://image@db/image",
        "ASSETS_MEDIA_S3_ENDPOINT": "http://s3.internal",
        "ASSETS_MEDIA_DATASETS_BUCKET": "datasets",
        "ASSETS_MEDIA_CACHE_BUCKET": "derived-cache",
        "ASSETS_MEDIA_S3_ACCESS_KEY": "access",
        "ASSETS_MEDIA_S3_SECRET_KEY": "secret",
        "MINIO_BUCKET": "aap-wt-annotations",
    }

    assert load_config(values).storage.legacy_bucket == "aap-wt-annotations"
    with pytest.raises(ConfigurationError):
        load_config(
            {key: value for key, value in values.items() if key != "MINIO_BUCKET"}
        )


@pytest.mark.asyncio
async def test_dimensions_worker_uses_bounded_header_and_finishes_only_after_source_check():
    data = _png_bytes((4, 2))
    source = _source(data)
    claim = _claim(
        source,
        DimensionsWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "published"
    names = [name for name, _request in database.calls]
    assert names == [
        "claim_image_media",
        "heartbeat_image_media",
        "finish_image_media",
    ]
    finish = database.calls[-1][1]
    assert finish.result.width == 4
    assert finish.result.height == 2
    assert storage.sources == [source]


@pytest.mark.asyncio
async def test_dimensions_worker_fails_instead_of_publishing_after_source_changes():
    data = _png_bytes()
    source = _source(data)
    changed_identity = source.object_identity.model_copy(update={"etag": "changed"})
    claim = _claim(
        source,
        DimensionsWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data, heads=[changed_identity])

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert [name for name, _request in database.calls] == [
        "claim_image_media",
        "heartbeat_image_media",
        "fail_image_media",
    ]
    assert database.calls[-1][1].error_code == "source_changed"


@pytest.mark.asyncio
async def test_dimensions_worker_rejects_header_that_disagrees_with_known_source_facts():
    data = _png_bytes((4, 2))
    source = _source(data, width=6, height=2)
    claim = _claim(
        source,
        DimensionsWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert database.calls[-1][0] == "fail_image_media"
    assert database.calls[-1][1].error_code == "dimension_mismatch"


@pytest.mark.asyncio
async def test_dimensions_worker_rejects_unknown_header_over_source_cap_without_download():
    data = b"not-an-image"
    source = _source(data)
    oversized = source.model_copy(
        update={
            "object_identity": source.object_identity.model_copy(
                update={"byte_size": 536_870_913}
            ),
            "file_size": 536_870_913,
        }
    )
    claim = _claim(
        oversized,
        DimensionsWorkSpec(
            expected_source_snapshot=oversized.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert storage.headers == [oversized]
    assert storage.downloads == []
    assert database.calls[-1][1].error_code == "resource_limit"


@pytest.mark.asyncio
async def test_dimensions_worker_probes_small_image_with_large_icc_header():
    data = _jpeg_with_large_icc()
    source = _source(data)
    claim = _claim(
        source,
        DimensionsWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            generate_preview=True,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "published"
    assert len(storage.headers) == 1
    assert len(storage.downloads) == 1
    finish = database.calls[-1][1]
    assert (finish.result.width, finish.result.height) == (4, 2)
    assert finish.result.source_sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_thumbnail_worker_probes_small_image_with_large_icc_header():
    data = _jpeg_with_large_icc()
    source = _source(data)
    claim = _claim(
        source,
        ThumbnailWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "published"
    assert len(storage.headers) == 1
    assert len(storage.downloads) == 1
    assert [name for name, _request in database.calls][-1] == "finish_image_media"
    assert len(storage.uploads) == 1
    assert storage.uploads[0][0].endswith("/thumbnail.webp")


@pytest.mark.asyncio
async def test_finish_retries_only_explicit_lock_contention_with_same_result_and_lease(
    monkeypatch,
):
    import aap_assets_images_runtime.worker as worker_module

    data = _png_bytes()
    source = _source(data)
    claim = _claim(
        source,
        DimensionsWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(
        claim,
        finish_failures=["source_busy", "operation_busy"],
    )
    monkeypatch.setattr(worker_module.random, "uniform", lambda _low, _high: 0.0)

    result = await ImageExecutor(database, _FakeStorage(data)).execute_once()

    finishes = [
        request for name, request in database.calls if name == "finish_image_media"
    ]
    assert result.outcome == "published"
    assert len(finishes) == 3
    assert finishes[0] is finishes[1] is finishes[2]
    assert finishes[0].lease_token == claim.lease_token


@pytest.mark.asyncio
async def test_thumbnail_worker_runs_killable_child_and_publishes_attempt_scoped_bytes():
    data = _png_bytes((4, 2))
    source = _source(data)
    claim = _claim(
        source,
        ThumbnailWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "published"
    assert len(storage.uploads) == 1
    key, content, artifact = storage.uploads[0]
    assert key == claim.output_prefix + "thumbnail.webp"
    assert content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()
    finish = database.calls[-1][1]
    assert finish.result.blurhash
    assert finish.result.width == 4 and finish.result.height == 2


@pytest.mark.asyncio
async def test_thumbnail_pipeline_rejects_pyramid_eligible_image_before_full_download():
    data = _png_bytes((4096, 4096))
    source = _source(data)
    claim = _claim(
        source,
        ThumbnailWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert storage.headers == [source]
    assert storage.downloads == []
    assert storage.uploads == []
    assert database.calls[-1][1].error_code == "not_eligible"


@pytest.mark.asyncio
async def test_thumbnail_pipeline_rejects_registered_dimension_mismatch_before_download():
    data = _png_bytes((4, 2))
    source = _source(data, width=6, height=2)
    claim = _claim(
        source,
        ThumbnailWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert storage.headers == [source]
    assert storage.downloads == []
    assert database.calls[-1][1].error_code == "dimension_mismatch"


@pytest.mark.asyncio
async def test_thumbnail_pipeline_rejects_oversized_source_before_any_object_read():
    data = _png_bytes()
    source = _source(data)
    oversized = source.model_copy(
        update={
            "object_identity": source.object_identity.model_copy(
                update={"byte_size": 536_870_913}
            ),
            "file_size": 536_870_913,
        }
    )
    claim = _claim(
        oversized,
        ThumbnailWorkSpec(
            expected_source_snapshot=oversized.source_snapshot,
            policy=_policy(),
        ),
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert storage.headers == []
    assert storage.downloads == []
    assert database.calls[-1][1].error_code == "resource_limit"


@pytest.mark.asyncio
async def test_pyramid_worker_executes_real_libvips_child_and_publishes_every_artifact(
    tmp_path,
):
    try:
        import pyvips

        pyvips.version(0)
    except Exception as exc:
        pytest.skip(f"libvips loader is unavailable: {type(exc).__name__}")

    from PIL import ImageCms

    data = _png_bytes((2, 2))
    source = _source(data)
    claim = _claim(
        source,
        PyramidWorkSpec(
            expected_source_snapshot=source.source_snapshot,
            policy=_policy(),
        ),
    )
    profile = tmp_path / "srgb.icc"
    profile.write_bytes(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    )
    database = _FakeDatabase(claim)
    storage = _FakeStorage(data)

    result = await ImageExecutor(database, storage, srgb_profile=profile).execute_once()

    assert result.outcome == "published"
    finish = database.calls[-1][1]
    pyramid = finish.result
    assert pyramid.manifest.schema_ == "aap-image-pyramid/v1"
    assert pyramid.source_sha256 == hashlib.sha256(data).hexdigest()
    assert pyramid.tile_count == sum(
        level.columns * level.rows for level in pyramid.manifest.levels
    )
    assert {key for key, _data, _artifact in storage.uploads} == {
        pyramid.manifest_artifact.key,
        pyramid.overview_artifact.key,
        *(tile.artifact.key for tile in pyramid.tile_artifacts),
    }


@pytest.mark.asyncio
async def test_backfill_observes_every_candidate_before_cursor_commit():
    source_bytes = b"source"
    source = _source(source_bytes)
    policy = _policy()
    claim = _image_backfill_claim(source, policy)
    batch_token = uuid4()
    prepare = ImageBackfillProgress(
        job_id=claim.job_id,
        generation=claim.generation,
        lease_token=claim.lease_token,
        scheduled=0,
        skipped=0,
        complete=False,
        next_owner_kind=source.owner.kind,
        next_owner_id=source.owner.id,
        batch_token=batch_token,
        candidates=[source],
        needs_observation=True,
    )
    committed = ImageBackfillProgress(
        job_id=claim.job_id,
        generation=claim.generation,
        lease_token=claim.lease_token,
        scheduled=1,
        skipped=0,
        complete=True,
        next_owner_kind=None,
        next_owner_id=None,
        batch_token=batch_token,
    )
    database = _FakeDatabase(claim, advance_results=[prepare, committed])
    storage = _FakeStorage(source_bytes)

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "published"
    advances = [
        request for name, request in database.calls if name == "advance_image_backfill"
    ]
    assert len(advances) == 2
    assert advances[0].observations is None
    assert advances[1].observations.batch_token == batch_token
    observation = advances[1].observations.items[0]
    assert observation.owner == source.owner
    assert observation.source_snapshot == source.source_snapshot
    assert observation.object_identity == source.object_identity
    assert storage.sources == [source]


@pytest.mark.asyncio
async def test_backfill_storage_failure_does_not_advance_or_skip_the_candidate():
    source = _source(b"source")
    claim = _image_backfill_claim(source, _policy())
    prepare = ImageBackfillProgress(
        job_id=claim.job_id,
        generation=claim.generation,
        lease_token=claim.lease_token,
        scheduled=0,
        skipped=0,
        complete=False,
        next_owner_kind=source.owner.kind,
        next_owner_id=source.owner.id,
        batch_token=uuid4(),
        candidates=[source],
        needs_observation=True,
    )
    database = _FakeDatabase(claim, advance_results=[prepare])
    storage = _FakeStorage(source_bytes=b"source")

    async def unavailable(_source):
        raise ImageFailure("storage_unavailable")

    storage.head_source = unavailable
    executor = ImageExecutor(database, storage)
    with pytest.raises(ImageFailure, match="storage_unavailable"):
        await executor.execute_once()

    assert [name for name, _request in database.calls].count(
        "advance_image_backfill"
    ) == 1
    assert "fail_image_media" not in [name for name, _request in database.calls]


@pytest.mark.asyncio
async def test_backfill_rejects_candidates_outside_the_accepted_selector():
    source = _source(b"source")
    claim = _image_backfill_claim(source, _policy())
    foreign = source.model_copy(update={"dataset_id": uuid4()})
    prepare = ImageBackfillProgress(
        job_id=claim.job_id,
        generation=claim.generation,
        lease_token=claim.lease_token,
        scheduled=0,
        skipped=0,
        complete=False,
        next_owner_kind=source.owner.kind,
        next_owner_id=source.owner.id,
        batch_token=uuid4(),
        candidates=[foreign],
        needs_observation=True,
    )
    database = _FakeDatabase(claim, advance_results=[prepare])
    storage = _FakeStorage(source_bytes=b"source")

    result = await ImageExecutor(database, storage).execute_once()

    assert result.outcome == "failed"
    assert storage.sources == []
    assert [name for name, _request in database.calls] == [
        "claim_image_media",
        "heartbeat_image_media",
        "advance_image_backfill",
        "fail_image_media",
    ]
    assert database.calls[-1][1].error_code == "invalid_response"


@pytest.mark.asyncio
async def test_reconcile_sends_all_candidates_in_bounded_batches():
    claim = None
    database = _FakeDatabase(claim)
    storage = _FakeStorage()
    now = datetime.now(timezone.utc)
    storage.orphans = [
        ImageOrphanCandidate(asset_id=uuid4(), generation=1, last_modified=now)
        for _ in range(205)
    ]

    result = await ImageExecutor(database, storage).reconcile_once()

    assert result.queued_orphans == 5
    requests = [
        request for name, request in database.calls if name == "reconcile_image_media"
    ]
    assert [len(request.candidates) for request in requests] == [100, 100, 5]
    assert all(request.batch_size == 100 for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["timeout", "lease_lost"])
async def test_child_process_group_is_terminated_and_waited_on_timeout_or_lease_loss(
    monkeypatch, tmp_path, code
):
    import aap_assets_images_runtime.worker as worker_module

    actual_create = asyncio.create_subprocess_exec
    children = []

    async def sleeping_child(*_args, **kwargs):
        process = await actual_create(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            **kwargs,
        )
        children.append(process)
        return process

    class FailedHeartbeat:
        deadline = time.monotonic() + 30

        def check(self):
            raise ImageFailure(code)

    monkeypatch.setattr(worker_module.asyncio, "create_subprocess_exec", sleeping_child)
    workspace = tmp_path / "work"
    workspace.mkdir()
    output_dir = workspace / "output"
    output_dir.mkdir()
    policy = _policy()
    with pytest.raises(ImageFailure, match=code):
        await _run_child(
            {"schema": "test"},
            workspace=workspace,
            output_dir=output_dir,
            policy=policy,
            heartbeat=FailedHeartbeat(),
        )
    assert len(children) == 1
    assert children[0].returncode is not None


def test_image_child_environment_does_not_receive_storage_or_database_secrets(
    monkeypatch,
):
    from aap_assets_images_runtime.worker import _child_environment

    monkeypatch.setenv("ASSETS_IMAGES_DATABASE_URL", "postgresql://private")
    monkeypatch.setenv("ASSETS_MEDIA_S3_SECRET_KEY", "private-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-aws-secret")

    environment = _child_environment()

    assert "ASSETS_IMAGES_DATABASE_URL" not in environment
    assert "ASSETS_MEDIA_S3_SECRET_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
