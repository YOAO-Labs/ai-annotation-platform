"""HTTP range delivery after verified image capture; no database or live source."""

import asyncio
from contextlib import contextmanager
import hashlib
import io
import threading
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException, Request
import httpx
from PIL import Image
import pytest
from starlette.requests import ClientDisconnect

from app.services import content_snapshot as snapshots


def image_bytes(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(output, format="PNG")
    return output.getvalue()


def reader(data):
    @contextmanager
    def opened():
        with io.BytesIO(data) as body:
            yield body, {"size": len(data)}

    return opened


def app_for(kind, monkeypatch, *, agent=True, data=None, failure=None):
    from app.api.v1 import dataset_sources, files
    from app.services import dataset_content, dataset_references

    data = image_bytes() if data is None else data
    created, calls = [], []

    async def capture(*args, **kwargs):
        calls.append((args, kwargs))
        if failure is not None:
            raise failure
        if kwargs["require_range_support"] and not agent:
            raise HTTPException(416, "Range is not supported for verified snapshots")
        expected = args[3] if kind == "task" else kwargs["expected_sha256"]
        result = await snapshots.capture(reader(data), expected)
        if agent:
            result._agent_receipt = object()
        created.append(result)
        return result

    monkeypatch.setattr(dataset_content, "scan_snapshot", capture)
    monkeypatch.setattr(dataset_content, "task_snapshot", capture)
    monkeypatch.setattr(dataset_references, "reference_snapshot", capture)
    app = FastAPI()

    @app.get("/content")
    async def content(request: Request, sha256: str):
        if kind == "scan":
            return await dataset_sources.read_scan_content(
                uuid.uuid4(), "p" * 64, request, sha256, db=None, actor=object()
            )
        if kind == "item":
            return await dataset_sources.reference_item_content(
                uuid.uuid4(), request, sha256, db=None, actor=object()
            )
        return await files.get_verified_task_content(
            uuid.uuid4(), request, sha256, db=None, current_user=object()
        )

    return app, created, calls


@pytest.mark.parametrize("kind", ["scan", "item", "task"])
@pytest.mark.parametrize("range_header,interval", [(None, None), ("bytes=2-8", (2, 8))])
async def test_agent_routes_deliver_range_only_after_complete_capture(
    kind, range_header, interval, monkeypatch
):
    monkeypatch.setattr(snapshots, "_slots", threading.BoundedSemaphore(1))
    data = image_bytes()
    digest = hashlib.sha256(data).hexdigest()
    app, created, calls = app_for(kind, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/content",
            params={"sha256": digest},
            headers={"Range": range_header} if range_header else {},
        )
    assert len(calls) == len(created) == 1
    assert calls[0][1]["require_range_support"] is (range_header is not None)
    assert response.status_code == (206 if interval else 200)
    assert response.content == (data[2:9] if interval else data)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == f'"sha256:{digest}"'
    assert response.headers["x-content-sha256"] == digest
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == str(len(response.content))
    if interval:
        assert response.headers["content-range"] == f"bytes 2-8/{len(data)}"
    else:
        assert "content-range" not in response.headers
    assert created[0].file.closed and snapshots._slots._value == 1


@pytest.mark.parametrize("kind", ["scan", "item", "task"])
async def test_range_cannot_bypass_full_sha_verification(kind, monkeypatch):
    data = image_bytes()
    changed = data[:-1] + bytes([data[-1] ^ 1])
    assert changed[:2] == data[:2]
    app, created, _ = app_for(kind, monkeypatch, data=changed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/content",
            params={"sha256": hashlib.sha256(data).hexdigest()},
            headers={"Range": "bytes=0-1"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "content_mismatch"
    assert not created
    assert "content-range" not in response.headers


@pytest.mark.parametrize("kind", ["scan", "item", "task"])
async def test_provider_authority_failure_precedes_range_selection(kind, monkeypatch):
    app, created, calls = app_for(
        kind, monkeypatch, failure=HTTPException(403, "Current authority revoked")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/content", params={"sha256": "a" * 64}, headers={"Range": "bytes=0-1"}
        )
    assert response.status_code == 403
    assert len(calls) == 1 and not created
    assert "content-range" not in response.headers


@pytest.mark.parametrize(
    "range_header,interval",
    [
        ("bytes=3-", (3, None)),
        ("bytes=-5", (-5, None)),
        ("bytes=1-999999", (1, None)),
        ("bytes=-999999", (0, None)),
    ],
)
async def test_single_range_open_suffix_and_clamped_end(range_header, interval):
    data = image_bytes()
    snapshot = await snapshots.capture(reader(data))
    snapshot._range_supported = True
    response = snapshots.SnapshotResponse(snapshot, range_header=range_header)
    try:
        body = b"".join([part async for part in response.body_iterator])
        assert response.status_code == 206 and body == data[slice(*interval)]
        start = interval[0] if interval[0] >= 0 else len(data) + interval[0]
        assert (
            response.headers["content-range"]
            == f"bytes {start}-{len(data) - 1}/{len(data)}"
        )
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "if_range", [None, "correct", "different", "weak", "Wed, 21 Oct 2015 07:28:00 GMT"]
)
async def test_if_range_uses_exact_strong_verified_identity(if_range):
    data = image_bytes()
    snapshot = await snapshots.capture(reader(data))
    snapshot._range_supported = True
    etag = f'"sha256:{snapshot.sha256}"'
    supplied = {
        "correct": etag,
        "different": '"sha256:' + "0" * 64 + '"',
        "weak": "W/" + etag,
    }.get(if_range, if_range)
    response = snapshots.SnapshotResponse(
        snapshot, range_header="bytes=0-1", if_range=supplied
    )
    try:
        body = b"".join([part async for part in response.body_iterator])
        matched = if_range in (None, "correct")
        assert response.status_code == (206 if matched else 200)
        assert body == (data[:2] if matched else data)
        assert ("content-range" in response.headers) == matched
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "range_header,status",
    [
        ("bytes=999999-", 416),
        ("bytes=-0", 416),
        ("bytes=0-1,3-4", 416),
        ("bytes=2-1", 400),
        ("bytes=", 400),
        ("bytes=" + "1" * 513 + "-", 400),
    ],
)
async def test_rejected_ranges_close_the_verified_snapshot(
    range_header, status, monkeypatch
):
    monkeypatch.setattr(snapshots, "_slots", threading.BoundedSemaphore(1))
    snapshot = await snapshots.capture(reader(image_bytes()))
    snapshot._range_supported = True
    with pytest.raises(HTTPException) as caught:
        snapshots.SnapshotResponse(snapshot, range_header=range_header)
    assert caught.value.status_code == status
    if status == 416:
        assert caught.value.headers["Content-Range"] == f"bytes */{snapshot.size}"
    assert snapshot.file.closed and snapshots._slots._value == 1


async def test_non_agent_full_reads_and_range_rejection_keep_existing_contract(
    monkeypatch,
):
    data = image_bytes()
    app, created, _ = app_for("item", monkeypatch, agent=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        full = await client.get(
            "/content", params={"sha256": hashlib.sha256(data).hexdigest()}
        )
        ranged = await client.get(
            "/content",
            params={"sha256": hashlib.sha256(data).hexdigest()},
            headers={"Range": "bytes=0-1"},
        )
    assert full.status_code == 200 and full.content == data
    assert full.headers["accept-ranges"] == "none"
    assert ranged.status_code == 416
    assert len(created) == 1
    assert all(snapshot.file.closed for snapshot in created)


@pytest.mark.parametrize(
    "kind", ["scan", "reference", "managed_task", "reference_task"]
)
async def test_non_agent_provider_rejects_range_before_source_or_cache_io(
    kind, monkeypatch
):
    from app.services import (
        dataset_content as content,
        dataset_references as references,
    )
    from app.services import (
        original_content_access as access,
        original_cache,
        storage_access,
    )

    actor = SimpleNamespace(id=uuid.uuid4())

    async def unexpected(*args, **kwargs):
        pytest.fail(
            "Unsupported Range must not capture source/cache bytes or acquire I/O"
        )

    async def context(*args, **kwargs):
        return {"location_kind": "filesystem"}

    async def authority(*args, **kwargs):
        return None

    monkeypatch.setattr(snapshots, "capture", unexpected)
    monkeypatch.setattr(original_cache, "read", unexpected)
    monkeypatch.setattr(storage_access, "acquire", unexpected)
    monkeypatch.setattr(content, "key_authority", authority)
    monkeypatch.setattr(references, "reference_context", context)
    if kind == "scan":

        async def scan_context(*args, **kwargs):
            return SimpleNamespace(kind="filesystem"), None, None, None, None, None

        monkeypatch.setattr(content, "scan_context", scan_context)
        operation = content.scan_snapshot(
            None, uuid.uuid4(), "p" * 64, actor, require_range_support=True
        )
    elif kind == "reference":
        operation = references.reference_snapshot(
            None, uuid.uuid4(), actor, require_range_support=True
        )
    else:

        async def actor_and_key(*args):
            return actor, None

        async def source(*args, **kwargs):
            return SimpleNamespace(
                bucket="originals" if kind == "managed_task" else None,
                item_id=uuid.uuid4(),
            )

        monkeypatch.setattr(access, "capture_credentials", authority)
        monkeypatch.setattr(access, "actor_and_key", actor_and_key)
        monkeypatch.setattr(content, "task_source", source)
        monkeypatch.setattr(content, "_managed_snapshot", unexpected)
        operation = content.task_snapshot(
            None, uuid.uuid4(), actor, "a" * 64, require_range_support=True
        )
    with pytest.raises(HTTPException) as caught:
        await operation
    assert caught.value.status_code == 416


async def test_preview_range_is_still_rejected_before_preview_capture(monkeypatch):
    from app.api.v1.dataset_sources import read_directory_preview
    from app.services import directory_previews

    async def unexpected(*args):
        pytest.fail("preview Range must not read the preview or source")

    monkeypatch.setattr(directory_previews, "read_preview", unexpected)
    request = Request({"type": "http", "headers": [(b"range", b"bytes=0-1")]})
    with pytest.raises(HTTPException) as caught:
        await read_directory_preview(
            uuid.uuid4(), "p" * 64, request, db=None, actor=object()
        )
    assert caught.value.status_code == 416


async def test_send_failure_releases_range_snapshot_and_capacity(monkeypatch):
    monkeypatch.setattr(snapshots, "_slots", threading.BoundedSemaphore(1))
    snapshot = await snapshots.capture(reader(image_bytes()))
    snapshot._range_supported = True
    response = snapshots.SnapshotResponse(snapshot, range_header="bytes=0-5")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Client left")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert snapshot.file.closed and snapshots._slots._value == 1


@pytest.mark.parametrize("finish", ["cancel", "timeout", "timeout_then_cancel"])
async def test_interrupted_range_waits_for_physical_read_before_closing(
    monkeypatch, finish
):
    monkeypatch.setattr(snapshots, "_slots", threading.BoundedSemaphore(1))
    snapshot = await snapshots.capture(reader(image_bytes()))
    snapshot._range_supported = True
    underlying = snapshot.file
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        @property
        def closed(self):
            return underlying.closed

        def seek(self, offset):
            return underlying.seek(offset)

        def read(self, size):
            entered.set()
            assert release.wait(3)
            assert not underlying.closed
            return underlying.read(size)

        def close(self):
            underlying.close()

    snapshot.file = Blocked()
    response = snapshots.SnapshotResponse(snapshot, range_header="bytes=0-5")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    if finish.startswith("timeout"):
        monkeypatch.setattr(snapshots, "RESPONSE_SECONDS", 0.02)
    operation = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if finish == "cancel":
            operation.cancel()
        await asyncio.sleep(0.04)
        assert not operation.done() and not underlying.closed
        assert snapshots._slots._value == 0
        if finish != "timeout":
            operation.cancel()
    finally:
        release.set()
    with pytest.raises(TimeoutError if finish == "timeout" else asyncio.CancelledError):
        await operation
    assert underlying.closed and snapshots._slots._value == 1


@pytest.mark.parametrize("kind", ["agent", "filesystem"])
async def test_verified_cache_uses_current_source_kind_for_range_capability(
    kind, monkeypatch
):
    from app.services import dataset_references as references, original_cache

    snapshot = await snapshots.capture(reader(image_bytes()))
    context = {
        "sha256": snapshot.sha256,
        "source_policy": "on_demand",
        "location_policy": "on_demand",
        "source_id": uuid.uuid4(),
        "asset_id": uuid.uuid4(),
        "location_kind": kind,
    }

    async def current(*args, **kwargs):
        return dict(context)

    async def authority(*args):
        return None

    async def cached(*args, **kwargs):
        return snapshot

    monkeypatch.setattr(references, "reference_context", current)
    monkeypatch.setattr(references, "_cache_factory", lambda db: object())
    monkeypatch.setattr(original_cache, "read", cached)
    monkeypatch.setattr("app.services.dataset_content.key_authority", authority)
    actual = await references.reference_snapshot(
        None,
        uuid.uuid4(),
        SimpleNamespace(id=uuid.uuid4()),
        expected_sha256=snapshot.sha256,
    )
    try:
        assert actual.supports_ranges is (kind == "agent")
    finally:
        actual.close()
