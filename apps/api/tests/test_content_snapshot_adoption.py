"""In-place image adoption preserves binary ownership and legacy admission."""

import asyncio
import contextvars
import hashlib
import io
import threading

from PIL import Image
import pytest
from starlette.requests import ClientDisconnect

from app.services import content_snapshot as service
from app.services.managed_original_reader import BinaryStage


def picture(format="PNG", *, animated=False):
    with io.BytesIO() as output, Image.new("RGB", (48, 32), "red") as image:
        if animated:
            with Image.new("RGB", (48, 32), "blue") as second:
                image.save(
                    output,
                    format=format,
                    save_all=True,
                    append_images=[second],
                    duration=100,
                )
        else:
            image.save(output, format=format)
        return output.getvalue()


def stage(raw, *, file=None, **changes):
    releases = []
    value = BinaryStage(
        **(
            {
                "file": file if file is not None else io.BytesIO(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "observation": {"size": len(raw), "bucket": "saved-originals"},
                "_release": lambda: releases.append("binary-slot"),
            }
            | changes
        )
    )
    return value, releases


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
async def test_adoption_uses_same_file_no_second_copy_and_no_legacy_image_slot(
    monkeypatch, format
):
    raw = picture(format)

    class ReadOnly(io.BytesIO):
        def write(self, data):
            raise AssertionError("Adoption must not copy or rewrite bytes")

    value, releases = stage(raw, file=ReadOnly(raw))
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))

    def no_temp(**kwargs):
        raise AssertionError("Adoption must not allocate a second temporary file")

    monkeypatch.setattr(service.tempfile, "TemporaryFile", no_temp)
    result = await service.adopt_stage(value)
    try:
        assert result.file is value.file and result.file.read() == raw
        assert result.sha256 == value.sha256 and result.size == value.size
        assert result.media_type == Image.MIME[format]
        assert result.public()["width"] == 48 and result.public()["height"] == 32
        assert releases == [] and service._slots._value == 1
    finally:
        result.close()
        result.close()
        value.close()
    assert releases == ["binary-slot"] and value.file.closed
    assert service._slots._value == 1


@pytest.mark.parametrize(
    "raw,code,status",
    [
        (b"", "invalid_image", 415),
        (b"opaque binary", "invalid_image", 415),
        (picture("GIF"), "image_format_unsupported", 415),
        (picture("PNG", animated=True), "image_format_unsupported", 415),
    ],
)
async def test_strict_image_rules_close_rejected_stage(raw, code, status):
    value, releases = stage(raw)
    with pytest.raises(service.ContentError, match=code) as caught:
        await service.adopt_stage(value)
    assert caught.value.status == status and value.file.closed
    assert releases == ["binary-slot"]


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"", "invalid_image"),
        (b"opaque binary", "invalid_image"),
        (picture("GIF"), "image_format_unsupported"),
    ],
)
async def test_quality_adoption_keeps_exact_verified_bytes_and_sha(raw, code):
    value, releases = stage(raw)
    result = await service.adopt_stage(value, inspect_quality=True)
    try:
        assert result.inspection_error == code and result.sha256 == value.sha256
        assert result.file is value.file and result.file.read() == raw
        assert result.size == len(raw) and releases == []
    finally:
        result.close()
    assert releases == ["binary-slot"]


@pytest.mark.parametrize("quality", [False, True])
async def test_image_size_limit_does_not_silently_expand_for_binary_stage(
    monkeypatch, quality
):
    raw = picture()
    monkeypatch.setattr(service, "MAX_BYTES", len(raw) - 1)
    value, releases = stage(raw)
    with pytest.raises(service.ContentError, match="content_too_large") as caught:
        await service.adopt_stage(value, inspect_quality=quality)
    assert (
        caught.value.status == 413 and value.file.closed and releases == ["binary-slot"]
    )


@pytest.mark.parametrize("quality", [False, True])
async def test_pixel_limit_and_quality_error_match_legacy_rules(monkeypatch, quality):
    monkeypatch.setattr(service, "MAX_PIXELS", 1)
    value, releases = stage(picture())
    if quality:
        result = await service.adopt_stage(value, inspect_quality=True)
        try:
            assert result.inspection_error == "image_pixel_budget_exceeded"
            assert result.sha256 == value.sha256
        finally:
            result.close()
    else:
        with pytest.raises(service.ContentError, match="image_pixel_budget_exceeded"):
            await service.adopt_stage(value)
    assert value.file.closed and releases == ["binary-slot"]


@pytest.mark.parametrize(
    "changes",
    [
        {"size": True},
        {"size": -1},
        {"size": 1},
        {"sha256": "not-sha"},
        {"observation": None},
    ],
)
async def test_bad_stage_receipt_or_length_is_rejected_and_closed(changes):
    value, releases = stage(picture(), **changes)
    with pytest.raises(service.ContentError):
        await service.adopt_stage(value)
    assert value.file.closed and releases == ["binary-slot"]


async def test_exif_dimensions_preserve_original_file_and_bytes():
    with io.BytesIO() as output, Image.new("RGB", (12, 16), "red") as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(output, format="JPEG", exif=exif)
        raw = output.getvalue()
    value, releases = stage(raw)
    result = await service.adopt_stage(value)
    try:
        assert (result.width, result.height) == (16, 12)
        assert result.file.read() == raw
    finally:
        result.close()
    assert releases == ["binary-slot"]


@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_decode_is_offloop_and_cannot_close_file_before_thread_ends(
    monkeypatch, finish
):
    value, releases = stage(picture())
    entered, release = threading.Event(), threading.Event()
    owner_thread = threading.get_ident()
    inspect = service._inspect_image

    def blocked(*args):
        assert threading.get_ident() != owner_thread
        entered.set()
        assert release.wait(3)
        return inspect(*args)

    monkeypatch.setattr(service, "_inspect_image", blocked)
    if finish == "timeout":
        monkeypatch.setattr(service, "WAIT_SECONDS", 0.02)
    operation = asyncio.create_task(service.adopt_stage(value))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if finish == "cancel":
            operation.cancel()
        await asyncio.sleep(0.04)
        assert not operation.done() and not value.file.closed and releases == []
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    expected_error = (
        asyncio.CancelledError if finish == "cancel" else service.ContentError
    )
    with pytest.raises(expected_error) as error:
        await operation
    if finish == "timeout":
        assert error.value.code == "content_read_timeout"
    assert value.file.closed and releases == ["binary-slot"]


async def test_late_success_after_repeated_cancellation_is_disposed_once(monkeypatch):
    marker = contextvars.ContextVar("adopt-stage-context", default=None)
    marker.set("request")
    value, releases = stage(picture())
    entered, release = threading.Event(), threading.Event()

    def late(stage, stop, quality):
        assert marker.get() == "request"
        entered.set()
        assert release.wait(3)
        return service.Snapshot(
            stage.file,
            stage.sha256,
            stage.size,
            stage.observation,
            "image/png",
            48,
            32,
            _cleanup=stage.close,
        )

    monkeypatch.setattr(service, "_adopt_stage", late)
    operation = asyncio.create_task(service.adopt_stage(value))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert value.file.closed and releases == ["binary-slot"]


async def test_adoption_timeout_drains_and_preserves_timeout_status(monkeypatch):
    value, releases = stage(picture())
    entered, release = threading.Event(), threading.Event()
    inspect = service._inspect_image

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return inspect(*args)

    monkeypatch.setattr(service, "_inspect_image", blocked)
    monkeypatch.setattr(service, "WAIT_SECONDS", 0.01)
    operation = asyncio.create_task(service.adopt_stage(value))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.03)
        assert not operation.done() and not value.file.closed
    finally:
        release.set()
    with pytest.raises(service.ContentError, match="content_read_timeout") as caught:
        await operation
    assert caught.value.status == 504 and releases == ["binary-slot"]


async def test_late_adoption_close_uncertainty_survives_cancellation(monkeypatch):
    class UnknownClose(io.BytesIO):
        def close(self):
            first = not self.closed
            super().close()
            if first:
                raise OSError("private file close")

    raw = picture()
    value, releases = stage(raw, file=UnknownClose(raw))
    entered, release = threading.Event(), threading.Event()
    inspect = service._inspect_image

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return inspect(*args)

    monkeypatch.setattr(service, "_inspect_image", blocked)
    operation = asyncio.create_task(service.adopt_stage(value))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await operation
    assert caught.value.storage_operation_unsettled and releases == ["binary-slot"]


async def test_adopted_response_failure_closes_binary_owner_without_legacy_release(
    monkeypatch,
):
    value, releases = stage(picture())
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))
    result = await service.adopt_stage(value)
    response = service.SnapshotResponse(result)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("disconnected")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert value.file.closed and releases == ["binary-slot"]
    assert service._slots._value == 1


async def test_legacy_capture_still_owns_and_releases_legacy_slot(monkeypatch):
    from contextlib import contextmanager

    raw = picture()
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(1))

    @contextmanager
    def source():
        with io.BytesIO(raw) as body:
            yield body, {"size": len(raw)}

    result = await service.capture(source, hashlib.sha256(raw).hexdigest())
    assert service._slots._value == 0
    result.close()
    result.close()
    assert service._slots._value == 1
