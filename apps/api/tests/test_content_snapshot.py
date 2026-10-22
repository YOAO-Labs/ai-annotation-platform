import asyncio
from contextlib import contextmanager
import hashlib
import io
import threading

from PIL import Image
import pytest
from starlette.requests import ClientDisconnect

from app.services import content_snapshot as svc
from app.services import filesystem_storage as fs
from app.config import settings


def image_bytes(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(output, format="PNG")
    return output.getvalue()


def reader(data, *, size=None):
    @contextmanager
    def factory():
        with io.BytesIO(data) as body:
            yield body, {"size": len(data) if size is None else size}

    return factory


async def test_snapshot_is_exact_and_owns_capacity_until_closed(monkeypatch):
    monkeypatch.setattr(svc, "_slots", threading.BoundedSemaphore(1))
    data = image_bytes()
    snap = await svc.capture(reader(data), hashlib.sha256(data).hexdigest())
    assert snap.file.read() == data
    assert snap.public()["width"] == 48
    with pytest.raises(svc.ContentError, match="capacity"):
        await svc.capture(reader(data))
    snap.close()
    snap.close()
    assert snap.file.closed
    other = await svc.capture(reader(data))
    other.close()


@pytest.mark.parametrize(
    "case,code",
    [
        ("advertised", "content_too_large"),
        ("actual", "content_too_large"),
        ("truncated", "source_changed"),
        ("digest", "content_mismatch"),
        ("invalid", "invalid_image"),
        ("pixels", "image_pixel_budget_exceeded"),
        ("deadline", "content_read_timeout"),
        ("temp_error", "disk full"),
    ],
)
async def test_rejected_captures_release_all_resources(monkeypatch, case, code):
    monkeypatch.setattr(svc, "_slots", threading.BoundedSemaphore(1))
    data = image_bytes()
    size, expected = len(data), None
    if case == "advertised":
        size = svc.MAX_BYTES + 1
    if case == "actual":
        monkeypatch.setattr(svc, "MAX_BYTES", len(data) - 1)
        size = 1
    if case == "truncated":
        size += 1
    if case == "digest":
        expected = "0" * 64
    if case == "invalid":
        data = b"not an image"
        size = len(data)
    if case == "pixels":
        monkeypatch.setattr(svc, "MAX_PIXELS", 1)
    if case == "deadline":
        monkeypatch.setattr(svc, "COPY_SECONDS", -1)
    if case == "temp_error":
        monkeypatch.setattr(
            svc.tempfile,
            "TemporaryFile",
            lambda **kw: (_ for _ in ()).throw(OSError("disk full")),
        )
    with pytest.raises((svc.ContentError, OSError), match=code):
        await svc.capture(reader(data, size=size), expected)
    assert svc._slots.acquire(blocking=False)
    svc._slots.release()


@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_cancelled_and_timed_out_readers_keep_slot_until_thread_stops(
    monkeypatch,
    finish,
):
    monkeypatch.setattr(svc, "_slots", threading.BoundedSemaphore(1))
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Blocked(io.BytesIO):
        def read(self, count):
            entered.set()
            assert release.wait(3)
            return super().read(count)

    @contextmanager
    def blocked():
        try:
            with Blocked(image_bytes()) as body:
                yield body, {"size": len(image_bytes())}
        finally:
            closed.set()

    if finish == "timeout":
        monkeypatch.setattr(svc, "WAIT_SECONDS", 0.05)
    task = asyncio.create_task(svc.capture(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if finish == "cancel":
            task.cancel()
        with pytest.raises(
            asyncio.CancelledError if finish == "cancel" else svc.ContentError
        ):
            await task
        with pytest.raises(svc.ContentError, match="capacity"):
            await svc.capture(reader(image_bytes()))
    finally:
        release.set()
    assert await asyncio.to_thread(closed.wait, 1)
    async with asyncio.timeout(2):
        while svc._slots._value != 1:
            await asyncio.sleep(0.01)


async def test_response_send_failure_closes_snapshot(monkeypatch):
    monkeypatch.setattr(svc, "_slots", threading.BoundedSemaphore(1))
    snap = await svc.capture(reader(image_bytes()))
    response = svc.SnapshotResponse(snap)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("disconnected")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert snap.file.closed and svc._slots._value == 1


async def test_exif_orientation_uses_browser_dimensions_without_changing_bytes():
    image = Image.new("RGB", (12, 16), "red")
    exif = image.getexif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    data = output.getvalue()
    snapshot = await svc.capture(reader(data))
    try:
        assert (snapshot.width, snapshot.height) == (16, 12)
        assert snapshot.file.read() == data
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "directory", "root_replaced", "file_replaced"]
)
async def test_filesystem_content_rejects_unsafe_and_replaced_sources(
    tmp_path, monkeypatch, kind
):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(root)})
    config = {"root_handle": "test"}
    anchor = fs.capture_anchor(config)
    path = root / "image.png"
    if kind == "symlink":
        path.symlink_to("/etc/passwd")
    elif kind == "fifo":
        import os

        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(image_bytes())
    if kind == "root_replaced":
        root.rename(tmp_path / "old")
        root.mkdir()
        path.write_bytes(image_bytes())

    @contextmanager
    def factory():
        with fs.content_file(config, "", "image.png", expected_anchor=anchor) as opened:
            if kind == "file_replaced":
                path.unlink()
                path.write_bytes(image_bytes("blue"))
            yield opened

    with pytest.raises(fs.FilesystemStorageError):
        await svc.capture(factory)
