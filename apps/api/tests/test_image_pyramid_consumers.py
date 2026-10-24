"""Consumer routing never reconstructs a pyramid's bucket from its key."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys
import uuid

import pytest

from app.api.v1.tasks import _shared as shared
from app.services.dataset import DatasetDeleteCleanup, DatasetService
from app.services.storage import storage_service
from app.workers import media


def _task():
    return SimpleNamespace(
        id=uuid.uuid4(),
        dataset_item_id=None,
        file_type="image",
        file_path="original.jpg",
    )


@pytest.mark.parametrize(
    "checked", [None, ("image-pyramids/other/overview.webp", "stale")]
)
def test_sync_task_serializer_never_signs_an_unchecked_pyramid(monkeypatch, checked):
    task = _task()
    task._image_pyramid_thumbnail = checked
    monkeypatch.setattr(
        shared.TaskOut, "model_validate", lambda *_args, **_kw: SimpleNamespace()
    )
    sign = MagicMock(side_effect=AssertionError("current bucket signing is forbidden"))
    monkeypatch.setattr(storage_service, "generate_download_url", sign)

    result = shared._task_with_url(
        task, thumbnail_path="image-pyramids/legacy/g1/overview.webp"
    )

    assert result.thumbnail_url is None
    sign.assert_not_called()


def test_sync_task_serializer_reuses_only_the_exact_verified_path(monkeypatch):
    task = _task()
    path = "image-pyramids/objects/owner/overview.webp"
    task._image_pyramid_thumbnail = (path, "verified-owner-url")
    monkeypatch.setattr(
        shared.TaskOut, "model_validate", lambda *_args, **_kw: SimpleNamespace()
    )
    sign = MagicMock(side_effect=AssertionError("unexpected generic signing"))
    monkeypatch.setattr(storage_service, "generate_download_url", sign)

    assert (
        shared._task_with_url(task, thumbnail_path=path).thumbnail_url
        == "verified-owner-url"
    )
    sign.assert_not_called()


def test_sync_task_serializer_preserves_regular_thumbnail_signing(monkeypatch):
    task = _task()
    monkeypatch.setattr(
        shared.TaskOut, "model_validate", lambda *_args, **_kw: SimpleNamespace()
    )
    sign = MagicMock(return_value="regular-url")
    monkeypatch.setattr(storage_service, "generate_download_url", sign)

    assert (
        shared._task_with_url(
            task, thumbnail_path="thumbnails/normal.webp"
        ).thumbnail_url
        == "regular-url"
    )
    sign.assert_called_once()


async def test_task_batch_rechecks_and_clears_previous_pyramid_thumbnail(monkeypatch):
    from app.services import image_pyramid as pyramids, image_pyramid_objects as objects

    task = _task()
    path = "image-pyramids/objects/owner/overview.webp"
    dimensions = {task.id: (100, 200, path, None, None)}
    checked = AsyncMock(side_effect=["pinned-url", None])
    monkeypatch.setattr(objects, "thumbnail_url", checked)
    monkeypatch.setattr(pyramids, "task_pyramid_assets", AsyncMock(return_value={}))
    db = object()

    assert await shared._attach_image_pyramids_batch(db, [task], dimensions) == {}
    assert task._image_pyramid_thumbnail == (path, "pinned-url")
    assert await shared._attach_image_pyramids_batch(db, [task], dimensions) == {}
    assert task._image_pyramid_thumbnail == (path, None)
    checked.assert_awaited_with(db, "task", task.id, path)
    dimensions[task.id] = (100, 200, None, None, None)
    assert await shared._attach_image_pyramids_batch(db, [task], dimensions) == {}
    assert task._image_pyramid_thumbnail is None


def test_dataset_delete_cleanup_does_not_guess_any_pyramid_bucket(monkeypatch):
    delete_prefix = MagicMock(
        side_effect=AssertionError("pyramid cleanup belongs to durable GC")
    )
    delete_object = MagicMock()
    monkeypatch.setattr(storage_service, "delete_prefix", delete_prefix)
    monkeypatch.setattr(storage_service, "delete_object", delete_object)

    DatasetService.cleanup_deleted_storage(
        DatasetDeleteCleanup(
            directory_objects=(("saved-preview-bucket", "preview.webp"),)
        )
    )

    delete_prefix.assert_not_called()
    delete_object.assert_called_once_with("preview.webp", bucket="saved-preview-bucket")


def test_http_backfill_dispatch_cannot_enable_system_pyramids(monkeypatch):
    captured = []

    async def run(project_id, *, allow_pyramid=False):
        captured.append((project_id, allow_pyramid))

    monkeypatch.setattr(media, "_backfill_tasks", run)
    media.backfill_tasks.run("project")
    assert captured == [("project", False)]


async def test_dimension_probe_uses_verified_media_and_drains_native_work(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services import image_pyramid_originals as originals

    source, authority, stage, db = object(), object(), object(), object()
    identifier = uuid.uuid4()
    observed = []
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    monkeypatch.setattr(
        originals, "system_authorization", MagicMock(return_value={"mode": "system"})
    )
    monkeypatch.setattr(originals, "delegated", AsyncMock(return_value=authority))

    @asynccontextmanager
    async def media_context(actual_db, actual_authority, actual_source):
        assert (actual_db, actual_authority, actual_source) == (db, authority, source)
        observed.append("open")
        try:
            yield stage
        finally:
            observed.append("close")

    async def physical(call, actual_stage):
        assert call is media._pyramid_dimensions_from_stage
        assert actual_stage is stage
        observed.append("drained")
        return 1000, 2000

    monkeypatch.setattr(originals, "media", media_context)
    monkeypatch.setattr(originals, "physical", physical)

    assert await media._pyramid_dimensions(db, "task", identifier) == (1000, 2000)
    assert observed == ["open", "drained", "close"]
    originals.pin.assert_awaited_once_with(db, "task", identifier)
    originals.system_authorization.assert_called_once_with("task", identifier, source)


@pytest.mark.parametrize(
    "orientation,expected", [(1, (20000, 15000)), (6, (15000, 20000))]
)
def test_vips_header_probe_preserves_300mp_budget_and_orientation(
    monkeypatch, orientation, expected
):
    image = SimpleNamespace(
        width=20000,
        height=15000,
        get_typeof=lambda _name: True,
        get=lambda _name: orientation,
    )
    image_factory = MagicMock(return_value=image)
    fake_vips = SimpleNamespace(
        Image=SimpleNamespace(new_from_file=image_factory),
        cache_set_max=MagicMock(),
        cache_set_max_mem=MagicMock(),
        cache_set_max_files=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "pyvips", fake_vips)
    stage = SimpleNamespace(file=SimpleNamespace(fileno=lambda: 123))

    assert media._pyramid_dimensions_from_stage(stage) == expected
    assert image_factory.call_args.args == ("/proc/self/fd/123",)
    fake_vips.cache_set_max.assert_called_once_with(0)


def test_vips_header_cleanup_failure_keeps_physical_uncertainty(monkeypatch):
    image = SimpleNamespace(width=10, height=10, get_typeof=lambda _name: False)
    fake_vips = SimpleNamespace(
        Image=SimpleNamespace(new_from_file=lambda *_args, **_kwargs: image),
        cache_set_max=MagicMock(side_effect=RuntimeError("close failed")),
    )
    monkeypatch.setitem(sys.modules, "pyvips", fake_vips)
    stage = SimpleNamespace(file=SimpleNamespace(fileno=lambda: 123))

    with pytest.raises(Exception) as failure:
        media._pyramid_dimensions_from_stage(stage)

    assert failure.value.storage_operation_unsettled is True


def test_vips_header_decode_wrapper_preserves_physical_uncertainty(monkeypatch):
    from app.services.image_pyramid import ImagePyramidError
    from app.services.storage_access import mark_unsettled

    uncertain = mark_unsettled(RuntimeError("native constructor interrupted"))
    fake_vips = SimpleNamespace(
        Image=SimpleNamespace(new_from_file=MagicMock(side_effect=uncertain)),
        cache_set_max=MagicMock(),
        cache_set_max_mem=MagicMock(),
        cache_set_max_files=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "pyvips", fake_vips)
    stage = SimpleNamespace(file=SimpleNamespace(fileno=lambda: 123))

    with pytest.raises(ImagePyramidError) as failure:
        media._pyramid_dimensions_from_stage(stage)

    assert failure.value.code == "decode_failed"
    assert failure.value.storage_operation_unsettled is True


def test_vips_header_errors_do_not_keep_native_image_in_traceback(monkeypatch):
    import weakref
    from app.services.image_pyramid import ImagePyramidError

    image_refs = []

    class NativeImage:
        width = 100
        height = 100

        def get_typeof(self, _name):
            raise RuntimeError("header unavailable")

    def new_image(*_args, **_kwargs):
        image = NativeImage()
        image_refs.append(weakref.ref(image))
        return image

    fake_vips = SimpleNamespace(
        Image=SimpleNamespace(new_from_file=new_image),
        cache_set_max=MagicMock(),
        cache_set_max_mem=MagicMock(),
        cache_set_max_files=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "pyvips", fake_vips)
    stage = SimpleNamespace(file=SimpleNamespace(fileno=lambda: 123))

    with pytest.raises(ImagePyramidError) as failure:
        media._pyramid_dimensions_from_stage(stage)

    assert failure.value.code == "decode_failed"
    assert image_refs[0]() is None
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


@pytest.mark.parametrize("capture_failure", [False, True])
async def test_large_image_seed_registers_only_after_verified_read_and_close(
    monkeypatch, tmp_path, capture_failure
):
    from contextlib import asynccontextmanager
    from hashlib import sha256
    from app.services import dataset_assets, image_pyramid_originals as originals
    from app.services import managed_original_reader as reader, storage_access
    from app.services.content_snapshot import ContentError
    from scripts import seed_large_images as seed

    content = b"fixture bytes checked against the source body"
    fixture = seed.LargeImageFixture(
        id="pinned-test",
        label="Pinned",
        role="test",
        filename="pinned.png",
        format="png",
        width=10,
        height=10,
        pixel_count=100,
        byte_size=len(content),
        sha256=sha256(content).hexdigest(),
        source_page="https://example.test/source",
        credit="test",
        usage_policy="https://example.test/license",
        usage_note="test",
    )
    path = tmp_path / fixture.filename
    path.write_bytes(content)
    binding = SimpleNamespace(location_id=uuid.uuid4(), bucket="saved-fixture-bucket")
    events = []
    db = MagicMock()
    initial = MagicMock()
    initial.scalars.return_value = []
    count = MagicMock()
    count.scalar_one.return_value = 1
    db.execute = AsyncMock(side_effect=[initial, count])
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(file_count=0))

    @asynccontextmanager
    async def session():
        yield db

    @asynccontextmanager
    async def permit():
        events.append("admit")
        try:
            yield SimpleNamespace(revision=23)
        finally:
            events.append("settle")

    async def acquire(*_args, **_kwargs):
        return permit()

    async def physical(call, *args):
        return call(*args)

    async def capture(spec, **kwargs):
        assert spec.sha256 == fixture.sha256
        assert spec.size_bytes == len(content)
        assert spec.bucket == binding.bucket
        assert spec.object_key == fixture.storage_key
        assert kwargs["max_bytes"] == seed.settings.image_pyramid_max_source_bytes
        events.append("verified-read")
        if capture_failure:
            raise ContentError("content_mismatch")
        return SimpleNamespace(observation={"etag": '"verified-etag"'})

    async def close(_stage):
        events.append("closed")

    async def register(*args, **kwargs):
        assert events == ["admit", "verified-read", "closed", "settle", "gate"]
        assert kwargs["sha256"] == fixture.sha256
        assert kwargs["size_bytes"] == len(content)
        assert kwargs["etag"] == '"verified-etag"'
        events.append("registered")

    async def check(*_args, **kwargs):
        assert kwargs == {"expected_revision": 23, "lock": True}
        events.append("gate")

    monkeypatch.setattr(seed, "async_session", session)
    monkeypatch.setattr(seed, "_ensure_source_object", lambda *_args: False)
    monkeypatch.setattr(
        dataset_assets, "ensure_legacy_bindings", AsyncMock(return_value=binding)
    )
    register_mock = AsyncMock(side_effect=register)
    monkeypatch.setattr(dataset_assets, "register_item_asset", register_mock)
    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(storage_access, "check", check)
    monkeypatch.setattr(reader, "capture", capture)
    monkeypatch.setattr(originals, "physical", physical)
    monkeypatch.setattr("app.services.original_binary_response.close_stage", close)

    if capture_failure:
        with pytest.raises(ContentError) as failure:
            await seed._ensure_fixture_item(uuid.uuid4(), fixture, path)
        assert failure.value.code == "content_mismatch"
        register_mock.assert_not_awaited()
        db.add.assert_not_called()
        assert events == ["admit", "verified-read", "settle"]
    else:
        item, uploaded = await seed._ensure_fixture_item(uuid.uuid4(), fixture, path)
        assert item.file_path == fixture.storage_key
        assert uploaded is False
        assert events[-1] == "registered"
        register_mock.assert_awaited_once()
