"""Pure image and physical-owner checks for current evaluation originals."""

import asyncio
from contextvars import ContextVar
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError
import pytest

from app.services import sample_evaluation_originals as service
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.managed_original_reader import BinaryStage
from app.services.sample_evaluations import engine
from tests.test_managed_original_reader import Provider


def image_bytes(format="PNG", size=(4, 3), *, orientation=None, frames=1):
    output = io.BytesIO()
    image = Image.new("RGB", size, "navy")
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    if frames > 1:
        options.update(
            save_all=True,
            append_images=[Image.new("RGB", size, "red") for _ in range(frames - 1)],
        )
    image.save(output, format=format, **options)
    return output.getvalue()


def stage(raw):
    return BinaryStage(io.BytesIO(raw), hashlib.sha256(raw).hexdigest(), len(raw), {})


def snapshot(**changes):
    return {
        "file_type": "image",
        "expected_width": 4,
        "expected_height": 3,
        **changes,
    }


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"])
def test_inspection_keeps_the_evaluation_format_set_and_raw_digest(format):
    captured = stage(image_bytes(format))
    try:
        assert service.inspect_image(captured, snapshot()) == {
            "sha256": captured.sha256,
            "width": 4,
            "height": 3,
        }
    finally:
        captured.close()


@pytest.mark.parametrize("orientation", range(1, 9))
def test_real_exif_orientation_uses_logical_dimensions(orientation):
    raw = image_bytes("JPEG", orientation=orientation)
    captured = stage(raw)
    expected = (3, 4) if orientation in {5, 6, 7, 8} else (4, 3)
    try:
        result = service.inspect_image(
            captured,
            snapshot(expected_width=expected[0], expected_height=expected[1]),
        )
        assert (result["width"], result["height"]) == expected
        assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    finally:
        captured.close()


@pytest.mark.parametrize("orientation", [0, 9, 65535])
def test_invalid_exif_orientation_is_not_a_current_image_receipt(orientation):
    captured = stage(image_bytes("JPEG", orientation=orientation))
    try:
        with pytest.raises(ValueError, match="orientation is invalid"):
            service.inspect_image(captured, snapshot())
    finally:
        captured.close()


def test_expected_dimensions_are_logical_and_cannot_be_replaced_by_encoded_size():
    captured = stage(image_bytes("JPEG", orientation=6))
    try:
        with pytest.raises(ValueError, match="logical dimensions differ"):
            service.inspect_image(captured, snapshot())
    finally:
        captured.close()


@pytest.mark.parametrize("format", ["GIF", "TIFF"])
def test_real_multiframe_files_are_rejected(format):
    captured = stage(image_bytes(format, frames=2))
    try:
        with pytest.raises(ValueError, match="pixel/frame budget"):
            service.inspect_image(captured, snapshot())
    finally:
        captured.close()


@pytest.mark.parametrize("size,accepted", [((4000, 4000), True), ((4001, 4000), False)])
def test_real_image_respects_the_existing_sixteen_megapixel_boundary(size, accepted):
    assert engine.MAX_PIXELS == 16_000_000
    captured = stage(image_bytes(size=size))
    expected = snapshot(expected_width=size[0], expected_height=size[1])
    try:
        if accepted:
            result = service.inspect_image(captured, expected)
            assert (result["width"], result["height"]) == size
        else:
            with pytest.raises(ValueError, match="pixel/frame budget"):
                service.inspect_image(captured, expected)
    finally:
        captured.close()


def test_invalid_bytes_do_not_become_a_current_image_receipt():
    captured = stage(b"not an image")
    try:
        with pytest.raises(UnidentifiedImageError):
            service.inspect_image(captured, snapshot())
    finally:
        captured.close()


class ObservedPermit:
    """Observe the exception and physical state at the durable owner's exit seam."""

    def __init__(self, world):
        self.world = world
        self.entered = False
        self.exits = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, _type, error, _traceback):
        self.exits.append(
            {
                "error": error,
                "stages_closed": all(item._closed for item in self.world.stages),
                "files_closed": all(item.file.closed for item in self.world.stages),
                "slots": service._slots._value,
            }
        )


@pytest.fixture
def world(monkeypatch):
    raw = image_bytes()
    provider = Provider()
    provider.raw = provider.actual = raw
    source = SimpleNamespace(
        endpoint_url=provider.meta.endpoint_url,
        bucket="saved-evaluation-originals",
        object_key="current/image.png",
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        location_id=uuid.uuid4(),
        maintenance_revision=9,
    )
    value = SimpleNamespace(
        db=SimpleNamespace(commit=AsyncMock()),
        authority=object(),
        snapshot=snapshot(),
        source=source,
        provider=provider,
        stages=[],
    )
    value.permit = ObservedPermit(value)
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(service, "freeze_source", AsyncMock(return_value=source))
    monkeypatch.setattr(service, "recheck_source", AsyncMock())
    monkeypatch.setattr(service, "recheck_authority", AsyncMock())
    monkeypatch.setattr(
        service.storage_access, "acquire", AsyncMock(return_value=value.permit)
    )
    monkeypatch.setattr(service.reader, "create_client", lambda **_: provider)
    capture = service.reader.capture

    async def captured(*args, **kwargs):
        result = await capture(*args, **kwargs)
        value.stages.append(result)
        return result

    monkeypatch.setattr(service.reader, "capture", captured)
    return value


async def test_verified_stage_owns_quota_until_it_closes_before_the_permit_exit(world):
    async with service.current_media(
        world.db, world.authority, world.snapshot
    ) as result:
        assert result == (
            world.source,
            {"sha256": world.source.sha256, "width": 4, "height": 3},
        )
        assert service._slots._value == 0
        assert world.permit.entered and world.permit.exits == []
        assert not world.stages[0]._closed
    assert world.stages[0]._closed and world.stages[0].file.closed
    assert service._slots._value == 1
    assert world.permit.exits == [
        {"error": None, "stages_closed": True, "files_closed": True, "slots": 1}
    ]
    service.storage_access.acquire.assert_awaited_once_with(
        world.db,
        world.source.location_id,
        "evaluation_original_read",
        expected_revision=9,
    )


async def test_the_exact_sixteen_mebibyte_limit_is_accepted(world):
    assert engine.MAX_RAW_BYTES == 16 * 1024**2
    raw = image_bytes()
    raw += b"\0" * (engine.MAX_RAW_BYTES - len(raw))
    world.provider.raw = world.provider.actual = raw
    world.source.size_bytes = len(raw)
    world.source.sha256 = hashlib.sha256(raw).hexdigest()
    async with service.current_media(world.db, world.authority, world.snapshot) as (
        _,
        media,
    ):
        assert media == {"sha256": world.source.sha256, "width": 4, "height": 3}
    assert service._slots._value == 1 and world.stages[0].file.closed


async def test_oversize_source_is_rejected_before_physical_admission(world):
    world.source.size_bytes = engine.MAX_RAW_BYTES + 1
    async with service.current_media(
        world.db, world.authority, world.snapshot
    ) as result:
        assert result == (None, None)
    assert world.provider.calls == [] and not world.permit.entered
    assert service._slots._value == 1
    service.storage_access.acquire.assert_not_awaited()
    service.recheck_authority.assert_awaited_once_with(world.db, world.authority)


async def test_non_image_source_has_no_lookup_or_object_io(world):
    world.snapshot["file_type"] = "video"
    async with service.current_media(
        world.db, world.authority, world.snapshot
    ) as result:
        assert result == (None, None)
    service.freeze_source.assert_not_awaited()
    assert world.provider.calls == [] and not world.permit.entered


async def test_full_admission_does_not_start_object_io_or_release_another_owner(world):
    assert service._slots.acquire(blocking=False)
    try:
        async with service.current_media(
            world.db, world.authority, world.snapshot
        ) as result:
            assert result == (world.source, None)
        assert world.provider.calls == [] and service._slots._value == 0
        assert len(world.permit.exits) == 1
    finally:
        service._slots.release()


async def test_decode_failure_still_closes_the_verified_stage(world):
    raw = b"verified but not an image"
    world.provider.raw = world.provider.actual = raw
    world.source.size_bytes = len(raw)
    world.source.sha256 = hashlib.sha256(raw).hexdigest()
    async with service.current_media(
        world.db, world.authority, world.snapshot
    ) as result:
        assert result == (world.source, None)
    assert world.stages[0].file.closed and service._slots._value == 1
    assert world.permit.exits[0]["files_closed"]


@pytest.mark.parametrize("when", ["before", "after"])
async def test_authority_recheck_failures_are_not_swallowed_as_bad_media(world, when):
    error = HTTPException(403, {"code": "content_authority_revoked"})
    service.recheck_source.side_effect = [error] if when == "before" else [None, error]
    with pytest.raises(HTTPException) as caught:
        async with service.current_media(world.db, world.authority, world.snapshot):
            pass
    assert caught.value is error
    if when == "before":
        assert world.provider.calls == []
    else:
        assert world.stages[0].file.closed and service._slots._value == 1


async def test_bounded_work_keeps_the_request_context():
    owner = ContextVar("evaluation-owner")
    reset = owner.set("current evaluation")
    try:
        assert await service.bounded_thread(owner.get) == "current evaluation"
    finally:
        owner.reset(reset)


@pytest.mark.parametrize("phase", ["decode", "classify"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_interruption_waits_for_real_decode_or_classification_before_retiring(
    world, monkeypatch, phase, finish
):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    inspect = service.inspect_image

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5), "test did not release its physical thread"
            return inspect(*args) if phase == "decode" else "labels"
        finally:
            ended.set()

    if phase == "decode":
        monkeypatch.setattr(service, "inspect_image", blocked)
    if finish == "timeout":
        monkeypatch.setattr(service, "THREAD_SECONDS", 0.03)

    async def operation():
        async with service.current_media(world.db, world.authority, world.snapshot) as (
            _,
            media,
        ):
            if phase == "classify":
                await service.bounded_thread(blocked)
            return media

    pending = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.08)
        assert not pending.done() and not ended.is_set()
        assert world.permit.entered and world.permit.exits == []
        assert not world.stages[0]._closed and service._slots._value == 0
    finally:
        release.set()
    if finish == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await pending
    elif phase == "classify":
        with pytest.raises(ContentError, match="content_read_timeout") as caught:
            await pending
        assert caught.value.status == 504
    else:
        assert await pending is None
    assert ended.is_set() and world.stages[0].file.closed
    assert world.permit.exits[0]["stages_closed"]
    assert world.permit.exits[0]["files_closed"]
    assert service._slots._value == 1


@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_late_unsettled_work_remains_marked_after_interruption(
    monkeypatch, finish
):
    entered, release = threading.Event(), threading.Event()

    def unknown_close():
        entered.set()
        assert release.wait(5), "test did not release its physical thread"
        raise storage_access.mark_unsettled(OSError("unknown physical close"))

    if finish == "timeout":
        monkeypatch.setattr(service, "THREAD_SECONDS", 0.03)
    pending = asyncio.create_task(service.bounded_thread(unknown_close))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.08)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(
        asyncio.CancelledError if finish == "cancel" else ContentError
    ) as caught:
        await pending
    assert caught.value.storage_operation_unsettled


@pytest.mark.parametrize(
    "finish", ["cancel", "unknown", "unknown_cancel", "unsettled_then_cancel"]
)
async def test_real_temporary_close_owns_quota_and_permit_until_it_settles(
    world, monkeypatch, finish
):
    entered, release = threading.Event(), threading.Event()
    captured = []
    # This test targets final file ownership; image decoding has its own real-Pillow cases.
    monkeypatch.setattr(service, "inspect_image", lambda *_: {"width": 4, "height": 3})

    class ClosingFile:
        def __init__(self, file):
            self.file = file

        @property
        def closed(self):
            return self.file.closed

        def close(self):
            entered.set()
            assert release.wait(5), "test did not release its physical close"
            if finish.startswith("unknown"):
                raise OSError("temporary file close is unknown")
            self.file.close()

    async def operation():
        async with service.current_media(world.db, world.authority, world.snapshot):
            original = world.stages[0].file
            captured.append(original)
            world.stages[0].file = ClosingFile(original)
            if finish == "unsettled_then_cancel":
                raise storage_access.mark_unsettled(OSError("prior unresolved work"))

    pending = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if finish in {"cancel", "unknown_cancel", "unsettled_then_cancel"}:
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not captured[0].closed
        assert world.permit.exits == [] and service._slots._value == 0
    finally:
        release.set()
    try:
        with pytest.raises(
            OSError if finish == "unknown" else asyncio.CancelledError
        ) as caught:
            await pending
        if finish.startswith("unknown"):
            assert caught.value.storage_operation_unsettled
            assert not captured[0].closed and service._slots._value == 0
            assert world.permit.exits[0]["error"].storage_operation_unsettled
        else:
            assert captured[0].closed and service._slots._value == 1
            assert world.permit.exits[0]["files_closed"]
            if finish == "unsettled_then_cancel":
                assert caught.value.storage_operation_unsettled
                assert world.permit.exits[0]["error"].storage_operation_unsettled
    finally:
        # A deliberately unknown close cannot be declared settled by production code.
        # The test still owns the exact real descriptor and removes its own artifact.
        captured[0].close()


def test_current_source_label_work_preserves_unknown_rle_close(monkeypatch):
    from app.services import raster_mask_storage
    from app.services.sample_evaluation_sources import classify_labels

    failure = storage_access.mark_unsettled(ContentError("storage_operation_unsettled"))

    def unresolved(_reference):
        raise failure

    monkeypatch.setattr(raster_mask_storage, "load_coco_rle_sync", unresolved)
    current = {
        **snapshot(),
        "annotations": [
            {
                "tool_unit_id": "region",
                "class_name": "car",
                "geometry": {"type": "raster_mask", "mask": {}},
            }
        ],
    }
    with pytest.raises(ContentError) as caught:
        classify_labels(current, ["car"], task="segment", preserve_unsettled=True)
    assert caught.value is failure
    assert classify_labels(current, ["car"], task="segment") is None
