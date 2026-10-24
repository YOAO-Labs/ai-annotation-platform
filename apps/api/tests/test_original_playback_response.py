"""Pure playback range and physical read ownership tests; no DB or network."""

import asyncio
import hashlib
import os
from types import SimpleNamespace
import tempfile
import threading

from fastapi import HTTPException
import pytest
from starlette.requests import ClientDisconnect

from app.services import original_playback_response as service
from app.services.content_snapshot import ContentError
from app.services.storage_access import mark_unsettled

RAW = bytes(range(256)) * 4
SHA = hashlib.sha256(RAW).hexdigest()
ETAG = f'"sha256:{SHA}"'


def scope(version="2.4", method="GET"):
    return {"type": "http", "method": method, "asgi": {"spec_version": version}}


async def receive():
    await asyncio.Event().wait()


async def collect(response, *, version="2.4", receive_=receive, method="GET"):
    messages = []

    async def send(message):
        messages.append(message)

    await response(scope(version, method), receive_, send)
    return messages


def body(messages):
    return b"".join(message.get("body", b"") for message in messages)


@pytest.fixture
def pinned():
    with tempfile.TemporaryFile(mode="w+b") as file:
        file.write(RAW)
        file.flush()
        file.seek(13)
        value = SimpleNamespace(file=file, sha256=SHA, size=len(RAW), releases=0)

        async def release():
            assert not file.closed
            value.releases += 1

        value.release = release
        yield value


def response(stage, **options):
    return service.PlaybackResponse(
        stage, media_type="video/mp4", release=stage.release, **options
    )


@pytest.mark.parametrize(
    ("header", "start", "length", "content_range"),
    [
        ("bytes=0-0", 0, 1, "bytes 0-0/10"),
        ("bytes=2-6", 2, 5, "bytes 2-6/10"),
        ("bytes=8-100", 8, 2, "bytes 8-9/10"),
        ("bytes=3-", 3, 7, "bytes 3-9/10"),
        ("bytes=-3", 7, 3, "bytes 7-9/10"),
        ("bytes=-20", 0, 10, "bytes 0-9/10"),
        ("BYTES=0001-0002", 1, 2, "bytes 1-2/10"),
        (" bytes=\t1-2\t", 1, 2, "bytes 1-2/10"),
        ("bytes=-" + "9" * 500, 0, 10, "bytes 0-9/10"),
        ("bytes=8-" + "9" * 500, 8, 2, "bytes 8-9/10"),
    ],
)
def test_single_ranges_are_bounded_by_actual_representation(
    header, start, length, content_range
):
    assert service.select_range(10, ETAG, range_header=header) == service.PlaybackRange(
        206, start, length, content_range
    )


@pytest.mark.parametrize(
    "header",
    [None, "items=0-2", "unknown", "bytes=0-2, 4-5", "bytes=100-200,-0"],
)
def test_absent_unknown_unit_and_valid_multiple_ranges_use_full_response(header):
    assert service.select_range(10, ETAG, range_header=header) == service.PlaybackRange(
        200, 0, 10
    )


@pytest.mark.parametrize(
    "header",
    [
        "bytes",
        "bytes=",
        "bytes=-",
        "bytes=1",
        "bytes=+1-2",
        "bytes=1-+2",
        "bytes=1--2",
        "bytes=2-1",
        "bytes=1 -2",
        "bytes=٠-٢",
        "bytes=0-1,,3-4",
        "bytes=0-1,broken",
        "bytes=0-1,3-2",
        "bytes=0-1\r\nprivate",
        "bytes=0-1\x7f",
        "bytes=" + "9" * 512 + "-",
    ],
)
def test_malformed_bytes_ranges_fail_with_bounded_public_error(header):
    with pytest.raises(HTTPException) as caught:
        service.select_range(10, ETAG, range_header=header)
    assert caught.value.status_code == 400
    assert caught.value.detail == {"code": "playback_range_invalid"}


@pytest.mark.parametrize(
    ("size", "header"),
    [
        (10, "bytes=10-"),
        (10, "bytes=99-100"),
        (10, "bytes=-0"),
        (0, "bytes=0-"),
        (0, "bytes=-1"),
        (10, "bytes=" + "9" * 500 + "-"),
    ],
)
def test_unsatisfiable_ranges_include_exact_total_size(size, header):
    with pytest.raises(HTTPException) as caught:
        service.select_range(size, ETAG, range_header=header)
    assert caught.value.status_code == 416
    assert caught.value.headers == {"Content-Range": f"bytes */{size}"}


@pytest.mark.parametrize(
    "if_range", ["W/" + ETAG, '"different"', "Wed, 21 Oct 2015 07:28:00 GMT", ""]
)
def test_if_range_requires_exact_strong_etag(if_range):
    assert service.select_range(
        10, ETAG, range_header="bytes=2-3", if_range=if_range
    ) == service.PlaybackRange(200, 0, 10)


def test_exact_strong_if_range_delivers_partial_representation():
    assert service.select_range(
        10, ETAG, range_header="bytes=2-3", if_range=ETAG
    ) == service.PlaybackRange(206, 2, 2, "bytes 2-3/10")


def test_head_ignores_even_malformed_or_overlong_range_and_if_range():
    assert service.select_range(
        10, ETAG, request_method="HEAD", range_header="x" * 1000, if_range="wrong"
    ) == service.PlaybackRange(200, 0, 10)


@pytest.mark.parametrize(
    ("method", "header", "status", "expected"),
    [
        ("GET", None, 200, RAW),
        ("GET", "bytes=3-9", 206, RAW[3:10]),
        ("HEAD", "bytes=3-9", 200, b""),
    ],
)
async def test_response_headers_body_and_pin_release(
    pinned, method, header, status, expected
):
    messages = await collect(
        response(pinned, request_method=method, range_header=header), method=method
    )
    assert messages[0]["status"] == status
    headers = dict(messages[0]["headers"])
    assert headers[b"content-type"] == b"video/mp4"
    assert (
        headers[b"content-length"]
        == str(len(RAW) if method == "HEAD" else len(expected)).encode()
    )
    assert headers[b"etag"] == ETAG.encode()
    assert headers[b"x-content-sha256"] == SHA.encode()
    assert headers[b"accept-ranges"] == b"bytes"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"cross-origin-resource-policy"] == b"same-origin"
    assert headers[b"referrer-policy"] == b"no-referrer"
    if status == 206:
        assert headers[b"content-range"] == f"bytes 3-9/{len(RAW)}".encode()
    else:
        assert b"content-range" not in headers
    assert body(messages) == expected
    assert messages[-1]["more_body"] is False
    assert pinned.releases == 1 and not pinned.file.closed
    assert pinned.file.tell() == 13


async def test_empty_verified_file_needs_no_descriptor_read(pinned, monkeypatch):
    pinned.size = 0
    pinned.sha256 = hashlib.sha256(b"").hexdigest()

    def unexpected(*args):
        raise AssertionError("No byte can be read from an empty representation")

    monkeypatch.setattr(service.os, "pread", unexpected)
    messages = await collect(response(pinned))
    assert messages[0]["status"] == 200 and body(messages) == b""
    assert dict(messages[0]["headers"])[b"content-length"] == b"0"
    assert pinned.releases == 1 and not pinned.file.closed


async def test_head_needs_no_descriptor_read(pinned, monkeypatch):
    def unexpected(*args):
        raise AssertionError("HEAD must not read the file")

    monkeypatch.setattr(service.os, "pread", unexpected)
    messages = await collect(response(pinned, request_method="HEAD"), method="HEAD")
    assert body(messages) == b"" and pinned.releases == 1


def test_constructor_failure_does_not_take_the_callers_pin(pinned):
    with pytest.raises(HTTPException) as caught:
        response(pinned, range_header="bytes=9999-")
    assert caught.value.status_code == 416
    assert pinned.releases == 0 and not pinned.file.closed


async def test_concurrent_real_fd_ranges_have_independent_offsets(pinned, monkeypatch):
    barrier = threading.Barrier(2)
    pread = os.pread

    def concurrent_read(fd, count, offset):
        barrier.wait(timeout=3)
        return pread(fd, count, offset)

    monkeypatch.setattr(service.os, "pread", concurrent_read)
    first, second = await asyncio.gather(
        collect(response(pinned, range_header="bytes=11-210")),
        collect(response(pinned, range_header="bytes=611-810")),
    )
    assert body(first) == RAW[11:211]
    assert body(second) == RAW[611:811]
    assert pinned.file.tell() == 13
    assert pinned.releases == 2 and not pinned.file.closed


async def test_offset_reads_are_bounded_and_end_at_exact_tail(pinned, monkeypatch):
    raw = b"x" * (service.CHUNK_BYTES * 2 + 7)
    pinned.file.seek(0)
    pinned.file.write(raw)
    pinned.file.flush()
    pinned.size = len(raw)
    pinned.sha256 = hashlib.sha256(raw).hexdigest()
    pread = os.pread
    calls = []

    def record(fd, count, offset):
        calls.append((count, offset))
        return pread(fd, count, offset)

    monkeypatch.setattr(service.os, "pread", record)
    messages = await collect(response(pinned))
    assert body(messages) == raw
    assert calls == [
        (service.CHUNK_BYTES, 0),
        (service.CHUNK_BYTES, service.CHUNK_BYTES),
        (7, 2 * service.CHUNK_BYTES),
    ]
    assert pinned.releases == 1 and not pinned.file.closed


@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("phase", ["http.response.start", "http.response.body"])
async def test_send_failure_releases_pin_without_closing_stage(pinned, version, phase):
    async def send(message):
        if message["type"] == phase:
            raise OSError("private disconnect detail")

    with pytest.raises((OSError, ClientDisconnect)):
        await response(pinned)(scope(version), receive, send)
    assert pinned.releases == 1 and not pinned.file.closed


@pytest.mark.parametrize("finish", ["cancel", "timeout", "disconnect"])
async def test_blocked_pread_drains_before_unpin_despite_repeated_cancellation(
    pinned, monkeypatch, finish
):
    entered, finish_read = threading.Event(), threading.Event()
    pread = os.pread
    reading = False

    def blocked(fd, count, offset):
        nonlocal reading
        reading = True
        entered.set()
        try:
            assert finish_read.wait(3)
            return pread(fd, count, offset)
        finally:
            reading = False

    async def release():
        assert not reading, "Response pin released during a physical read"
        pinned.releases += 1

    async def disconnected():
        assert await asyncio.to_thread(entered.wait, 1)
        return {"type": "http.disconnect"}

    pinned.release = release
    monkeypatch.setattr(service.os, "pread", blocked)
    if finish == "timeout":
        monkeypatch.setattr(service, "RESPONSE_SECONDS", 0.02)
    operation = asyncio.create_task(
        collect(
            response(pinned),
            version="2.0" if finish == "disconnect" else "2.4",
            receive_=disconnected if finish == "disconnect" else receive,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if finish == "cancel":
            operation.cancel()
        await asyncio.sleep(0.04)
        assert not operation.done() and pinned.releases == 0
        assert not pinned.file.closed
        if finish == "cancel":
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done() and pinned.releases == 0
    finally:
        finish_read.set()
    if finish == "disconnect":
        await operation
    else:
        with pytest.raises(
            asyncio.CancelledError if finish == "cancel" else ContentError
        ):
            await operation
    assert pinned.releases == 1 and not pinned.file.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_release_callback_is_drained_and_called_once(pinned, cancel):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def release():
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        pinned.releases += 1

    pinned.release = release
    operation = asyncio.create_task(collect(response(pinned)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
        assert not operation.done() and pinned.releases == 0 and calls == 1
    finally:
        finish.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation
    assert pinned.releases == 1 and calls == 1 and not pinned.file.closed


@pytest.mark.parametrize("failure", ["empty", "overread", "nonbytes", "private_error"])
async def test_bad_local_read_never_sends_incorrect_bytes_and_releases_pin(
    pinned, monkeypatch, failure
):
    def bad(fd, count, offset):
        if failure == "overread":
            return b"x" * (count + 1)
        if failure == "nonbytes":
            return "private text"
        if failure == "private_error":
            raise OSError("private filesystem detail")
        return b""

    monkeypatch.setattr(service.os, "pread", bad)
    messages = []

    async def send(message):
        messages.append(message)

    with pytest.raises(ContentError) as caught:
        await response(pinned)(scope(), receive, send)
    assert "private" not in str(caught.value)
    assert body(messages) == b""
    assert pinned.releases == 1 and not pinned.file.closed


async def test_short_reads_continue_from_the_actual_independent_offset(
    pinned, monkeypatch
):
    pread = os.pread
    offsets = []

    def short(fd, count, offset):
        offsets.append(offset)
        return pread(fd, min(count, 3), offset)

    monkeypatch.setattr(service.os, "pread", short)
    messages = await collect(response(pinned, range_header="bytes=11-20"))
    assert body(messages) == RAW[11:21]
    assert offsets == [11, 14, 17, 20]
    assert pinned.releases == 1 and not pinned.file.closed


async def test_executor_submission_failure_releases_pin_without_private_error(
    pinned, monkeypatch
):
    loop = asyncio.get_running_loop()

    def unavailable(*args):
        raise RuntimeError("private executor detail")

    monkeypatch.setattr(loop, "run_in_executor", unavailable)
    with pytest.raises(ContentError) as caught:
        await collect(response(pinned))
    assert "private" not in str(caught.value)
    assert pinned.releases == 1 and not pinned.file.closed


async def test_release_invocation_failure_is_unsettled_and_never_closes_stage(pinned):
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise RuntimeError("private release invocation")

    pinned.release = unavailable
    with pytest.raises(ContentError) as caught:
        await collect(response(pinned))
    assert caught.value.storage_operation_unsettled
    assert "private" not in str(caught.value)
    assert pinned.releases == 0 and calls == 1 and not pinned.file.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_unknown_release_preserves_responsibility_and_sanitizes_error(
    pinned, cancel
):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def release():
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        raise OSError("private release detail")

    pinned.release = release
    operation = asyncio.create_task(collect(response(pinned)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
        assert not operation.done() and pinned.releases == 0
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError if cancel else ContentError) as caught:
        await operation
    assert caught.value.storage_operation_unsettled
    assert "private" not in str(caught.value)
    assert pinned.releases == 0 and calls == 1 and not pinned.file.closed


@pytest.mark.parametrize("outcome", ["bytes", "unsettled"])
async def test_read_completion_cancel_race_retrieves_actual_future(
    pinned, monkeypatch, outcome
):
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    reads = []

    def run_in_executor(executor, function, *args):
        if args and args[0] == os.pread:
            pending = loop.create_future()
            reads.append(pending)

            def complete_and_cancel():
                if outcome == "unsettled":
                    pending.set_exception(mark_unsettled(OSError("private late read")))
                else:
                    pending.set_result(RAW)
                operation.cancel()

            loop.call_soon(complete_and_cancel)
            return pending
        return original(executor, function, *args)

    monkeypatch.setattr(loop, "run_in_executor", run_in_executor)
    operation = asyncio.create_task(collect(response(pinned)))
    with pytest.raises(asyncio.CancelledError) as caught:
        await operation
    assert getattr(caught.value, "storage_operation_unsettled", False) == (
        outcome == "unsettled"
    )
    assert len(reads) == 1 and not reads[0]._log_traceback
    assert pinned.releases == 1 and not pinned.file.closed
