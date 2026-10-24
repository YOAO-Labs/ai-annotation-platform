"""Pure download ownership tests; no application, database or network fixtures."""

import asyncio
import hashlib
import io
import threading

import pytest
from starlette.requests import ClientDisconnect

from app.services import managed_original_reader as reader
from app.services import original_binary_response as service
from app.services.content_snapshot import ContentError
from app.services.storage_access import mark_unsettled

from tests.test_managed_original_reader import Provider, RAW, spec


@pytest.fixture
def provider(monkeypatch):
    value = Provider()
    monkeypatch.setattr(reader, "create_client", lambda **_: value)
    monkeypatch.setattr(reader, "_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(2))
    return value


def scope(version="2.4"):
    return {"type": "http", "method": "GET", "asgi": {"spec_version": version}}


async def receive():
    await asyncio.Event().wait()


async def collect(response, *, version="2.4", receive_=receive):
    messages = []

    async def send(message):
        messages.append(message)

    await response(scope(version), receive_, send)
    return messages


def owned(body, *, size=None):
    assert service._slots.acquire(blocking=False)
    return reader.BinaryStage(
        body,
        hashlib.sha256(RAW).hexdigest(),
        len(RAW) if size is None else size,
        {},
        _release=service._slots.release,
        _retain_on_close_error=True,
    )


@pytest.mark.parametrize("outcome", ["normal", "cancel", "unsettled"])
async def test_retired_stage_close_is_off_loop_and_owned_until_real_completion(
    provider, outcome
):
    entered, release = threading.Event(), threading.Event()

    class SlowClose(io.BytesIO):
        def close(self):
            entered.set()
            assert release.wait(2)
            if outcome == "unsettled":
                raise mark_unsettled(OSError("unknown local close"))
            super().close()

    file = SlowClose(RAW)
    stage = owned(file)
    operation = asyncio.create_task(service.close_stage(stage))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0)
        assert not operation.done() and not file.closed
        assert service._slots._value == 1
        if outcome == "cancel":
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done() and service._slots._value == 1
    finally:
        release.set()
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    elif outcome == "unsettled":
        with pytest.raises(OSError) as caught:
            await operation
        assert caught.value.storage_operation_unsettled
        assert service._slots._value == 1
        io.BytesIO.close(file)
        return
    else:
        await operation
    assert file.closed and service._slots._value == 2


async def test_download_limits_are_explicit_and_socket_budget_is_separate(
    provider, monkeypatch
):
    calls = []

    def create_client(**kwargs):
        calls.append(kwargs)
        return provider

    monkeypatch.setattr(reader, "create_client", create_client)
    stage = await service.capture_download(spec())
    try:
        assert service.DOWNLOAD_MAX_BYTES == 512 * 1024**2
        assert service.COPY_SECONDS == service.RESPONSE_SECONDS == 120
        assert calls == [{"timeout_seconds": 30}]
        assert service._slots._value == 1 and reader._slots._value == 4
    finally:
        stage.close()
    assert service._slots._value == 2


@pytest.mark.parametrize("length", [0, len(RAW) - 1, len(RAW)])
async def test_budget_boundary_accepts_empty_and_exact_binary(
    provider, monkeypatch, length
):
    monkeypatch.setattr(service, "DOWNLOAD_MAX_BYTES", len(RAW))
    provider.raw = provider.actual = RAW[:length]
    stage = await service.capture_download(
        spec(size_bytes=length, sha256=hashlib.sha256(RAW[:length]).hexdigest())
    )
    messages = await collect(service.BinaryResponse(stage))
    assert b"".join(m.get("body", b"") for m in messages) == RAW[:length]
    assert stage.file.closed and service._slots._value == 2
    assert reader._slots._value == 4


async def test_over_budget_is_rejected_before_admission_or_provider_io(provider):
    with pytest.raises(ContentError, match="content_too_large") as caught:
        await service.capture_download(spec(size_bytes=service.DOWNLOAD_MAX_BYTES + 1))
    assert caught.value.status == 413
    assert provider.calls == []
    assert service._slots._value == 2 and reader._slots._value == 4


async def test_download_pool_and_existing_four_slots_are_independent(provider):
    downloads, images = [], []
    try:
        downloads = [await service.capture_download(spec()) for _ in range(2)]
        calls = len(provider.calls)
        with pytest.raises(ContentError, match="snapshot_capacity_exceeded") as caught:
            await service.capture_download(spec())
        assert caught.value.status == 429 and len(provider.calls) == calls
        images = [
            await reader.capture(spec(), max_bytes=len(RAW), timeout_seconds=3)
            for _ in range(4)
        ]
        with pytest.raises(ContentError, match="snapshot_capacity_exceeded"):
            await reader.capture(spec(), max_bytes=len(RAW), timeout_seconds=3)
        assert service._slots._value == reader._slots._value == 0
        downloads[0].close()
        assert service._slots._value == 1 and reader._slots._value == 0
    finally:
        for stage in downloads + images:
            stage.close()
    assert service._slots._value == 2 and reader._slots._value == 4


@pytest.mark.parametrize("admission", [False, object()])
async def test_invalid_admission_is_rejected_before_provider_io(provider, admission):
    with pytest.raises(ContentError, match="managed_original_read_invalid"):
        await reader.capture(
            spec(), max_bytes=len(RAW), timeout_seconds=3, admission=admission
        )
    assert provider.calls == [] and reader._slots._value == 4


@pytest.mark.parametrize("seconds", [True, 0, -1, float("nan"), float("inf"), 4])
async def test_invalid_transport_budget_is_rejected_before_io(provider, seconds):
    with pytest.raises(ContentError, match="managed_original_read_invalid"):
        await reader.capture(
            spec(),
            max_bytes=len(RAW),
            timeout_seconds=3,
            transport_timeout_seconds=seconds,
        )
    assert provider.calls == [] and reader._slots._value == 4


async def test_default_reader_transport_budget_is_unchanged(provider, monkeypatch):
    calls = []

    def create_client(**kwargs):
        calls.append(kwargs)
        return provider

    monkeypatch.setattr(reader, "create_client", create_client)
    stage = await reader.capture(spec(), max_bytes=len(RAW), timeout_seconds=41)
    stage.close()
    assert calls == [{"timeout_seconds": 41}]
    assert reader._slots._value == 4 and service._slots._value == 2


async def test_response_headers_are_exact_and_do_not_expose_source(provider):
    stage = await service.capture_download(spec())
    messages = await collect(service.BinaryResponse(stage))
    assert messages[0]["status"] == 200
    headers = dict(messages[0]["headers"])
    assert headers == {
        b"content-type": b"application/octet-stream",
        b"content-disposition": b'attachment; filename="original.bin"',
        b"cache-control": b"no-store",
        b"x-content-type-options": b"nosniff",
        b"content-length": str(len(RAW)).encode(),
        b"etag": f'"sha256:{spec().sha256}"'.encode(),
        b"x-content-sha256": spec().sha256.encode(),
        b"accept-ranges": b"none",
    }
    assert b"".join(m.get("body", b"") for m in messages) == RAW
    assert messages[-1]["more_body"] is False
    assert stage.file.closed and service._slots._value == 2


async def test_response_reads_in_bounded_chunks_including_the_exact_tail(provider):
    raw = b"x" * (2 * service.CHUNK_BYTES + 3)
    provider.raw = provider.actual = raw
    stage = await service.capture_download(
        spec(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    )
    messages = await collect(service.BinaryResponse(stage))
    chunks = [message["body"] for message in messages if message.get("body")]
    assert list(map(len, chunks)) == [service.CHUNK_BYTES, service.CHUNK_BYTES, 3]
    assert b"".join(chunks) == raw
    assert stage.file.closed and service._slots._value == 2


@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("phase", ["http.response.start", "http.response.body"])
async def test_send_failure_closes_owned_stage(provider, version, phase):
    stage = await service.capture_download(spec())

    async def send(message):
        if message["type"] == phase:
            raise OSError("private disconnect detail")

    with pytest.raises((ClientDisconnect, OSError)):
        await service.BinaryResponse(stage)(scope(version), receive, send)
    assert stage.file.closed and service._slots._value == 2


@pytest.mark.parametrize("failure", ["truncated", "overread", "nonbytes", "read_error"])
async def test_response_never_sends_past_declared_size_or_private_read_errors(
    provider, failure
):
    class Bad(io.BytesIO):
        def read(self, count):
            if failure == "overread":
                return b"x" * (count + 1)
            if failure == "nonbytes":
                return "private text"
            if failure == "read_error":
                raise OSError("private filesystem detail")
            return b""

    stage = owned(Bad(RAW))
    sent = []

    async def send(message):
        sent.append(message)

    with pytest.raises(ContentError) as caught:
        await service.BinaryResponse(stage)(scope(), receive, send)
    assert "private" not in str(caught.value)
    assert not any(m.get("body") for m in sent)
    assert stage.file.closed and service._slots._value == 2


@pytest.mark.parametrize("finish", ["cancel", "timeout", "disconnect"])
async def test_response_drains_blocked_local_read_before_closing_and_releasing(
    provider, monkeypatch, finish
):
    entered, release = threading.Event(), threading.Event()

    class Blocked(io.BytesIO):
        reading = False

        def read(self, count):
            self.reading = True
            entered.set()
            assert release.wait(3)
            try:
                return super().read(count)
            finally:
                self.reading = False

        def close(self):
            assert not self.reading, "File closed while its read was still active"
            super().close()

    stage = owned(Blocked(RAW))
    if finish == "timeout":
        monkeypatch.setattr(service, "RESPONSE_SECONDS", 0.02)

    async def disconnected():
        assert await asyncio.to_thread(entered.wait, 1)
        return {"type": "http.disconnect"}

    operation = asyncio.create_task(
        collect(
            service.BinaryResponse(stage),
            version="2.0" if finish == "disconnect" else "2.4",
            receive_=disconnected if finish == "disconnect" else receive,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if finish == "cancel":
            operation.cancel()
        await asyncio.sleep(0.04)
        assert not operation.done() and not stage.file.closed
        assert service._slots._value == 1
        if finish == "cancel":
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
    finally:
        release.set()
    if finish == "disconnect":
        await operation
    else:
        with pytest.raises(
            asyncio.CancelledError if finish == "cancel" else ContentError
        ):
            await operation
    assert stage.file.closed and service._slots._value == 2


@pytest.mark.parametrize("finish", ["normal", "send_error", "cancel"])
async def test_response_waits_for_real_close_despite_repeated_cancel(provider, finish):
    entered, release = threading.Event(), threading.Event()

    class Closing(io.BytesIO):
        def close(self):
            entered.set()
            assert release.wait(3)
            super().close()

    stage = owned(Closing(RAW))

    async def send(message):
        if finish == "send_error" and message["type"] == "http.response.body":
            raise OSError("disconnected")

    operation = asyncio.create_task(
        service.BinaryResponse(stage)(scope(), receive, send)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not operation.done() and not stage.file.closed
        assert service._slots._value == 1
        if finish == "cancel":
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
    finally:
        release.set()
    if finish == "normal":
        await operation
    else:
        with pytest.raises(
            asyncio.CancelledError if finish == "cancel" else ClientDisconnect
        ):
            await operation
    assert stage.file.closed and service._slots._value == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_unknown_file_close_keeps_download_admission_and_error_responsibility(
    provider, cancel
):
    entered, release = threading.Event(), threading.Event()

    class Unknown(io.BytesIO):
        def close(self):
            entered.set()
            assert release.wait(3)
            raise OSError("private unknown close")

    body = Unknown(RAW)
    stage = owned(body)
    operation = asyncio.create_task(collect(service.BinaryResponse(stage)))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if cancel:
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
        release.set()
        with pytest.raises(
            asyncio.CancelledError if cancel else ContentError
        ) as caught:
            await operation
        assert caught.value.storage_operation_unsettled
        assert "private" not in str(caught.value)
        assert not body.closed and service._slots._value == 1
        stage.close()
        assert service._slots._value == 1
    finally:
        release.set()
        # Test-owned uncertainty is resolved explicitly, never by production GC.
        io.BytesIO.close(body)
        service._slots.release()


@pytest.mark.parametrize("phase", ["read", "client_close"])
async def test_copy_cancellation_drains_its_independent_pool(provider, phase):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(3)

    setattr(provider, phase + "_hook", blocked)
    operation = asyncio.create_task(service.capture_download(spec()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert service._slots._value == 1 and reader._slots._value == 4
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert provider.closed == 1 and all(body.closed for body in provider.bodies)
    assert service._slots._value == 2 and reader._slots._value == 4


async def test_copy_deadline_does_not_release_while_provider_read_is_active(
    provider, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(service, "COPY_SECONDS", 0.02)

    def blocked(*args):
        entered.set()
        assert release.wait(3)

    provider.read_hook = blocked
    operation = asyncio.create_task(service.capture_download(spec()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.04)
        assert not operation.done() and service._slots._value == 1
    finally:
        release.set()
    with pytest.raises(ContentError, match="content_read_timeout") as caught:
        await operation
    assert caught.value.status == 504
    assert provider.closed == 1 and all(body.closed for body in provider.bodies)
    assert service._slots._value == 2 and reader._slots._value == 4


async def test_unknown_remote_close_preserves_error_but_releases_closed_local_file(
    provider,
):
    provider.body_close_error = OSError("private provider close")
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        await service.capture_download(spec())
    assert caught.value.storage_operation_unsettled
    assert service._slots._value == 2 and reader._slots._value == 4


async def test_failed_copy_with_unknown_temporary_close_retains_download_admission(
    provider, monkeypatch
):
    class Unknown(io.BytesIO):
        def close(self):
            raise OSError("private temporary close")

    temporary = Unknown()
    monkeypatch.setattr(reader.tempfile, "TemporaryFile", lambda **_: temporary)
    provider.actual = b"broken same size!"
    try:
        with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
            await service.capture_download(spec())
        assert caught.value.storage_operation_unsettled
        assert not temporary.closed and service._slots._value == 1
        assert reader._slots._value == 4
    finally:
        io.BytesIO.close(temporary)
        service._slots.release()


async def test_late_download_stage_close_is_off_loop_and_drained_after_cancellation(
    provider, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    closing, close_release = threading.Event(), threading.Event()
    returned = []

    class SlowClose(io.BytesIO):
        def close(self):
            closing.set()
            assert close_release.wait(3)
            super().close()

    def late(spec, maximum, seconds, deadline, stop, release_slot, **options):
        entered.set()
        assert release.wait(3)
        stage = reader.BinaryStage(
            SlowClose(RAW),
            spec.sha256,
            len(RAW),
            {},
            _release=release_slot,
            _retain_on_close_error=options["retain_on_close_error"],
        )
        returned.append(stage)
        return stage

    monkeypatch.setattr(reader, "_capture", late)
    operation = asyncio.create_task(service.capture_download(spec()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.to_thread(closing.wait, 1)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and not returned[0].file.closed
        assert service._slots._value == 1 and reader._slots._value == 4
    finally:
        release.set()
        close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert returned[0].file.closed
    assert service._slots._value == 2 and reader._slots._value == 4


@pytest.mark.parametrize("outcome", ["bytes", "unsettled"])
async def test_read_completion_cancel_race_retrieves_the_actual_future_outcome(
    provider, monkeypatch, outcome
):
    stage = owned(io.BytesIO(RAW))
    loop = asyncio.get_running_loop()
    execute = loop.run_in_executor
    reads = []

    def run_in_executor(executor, function, *args):
        if args and args[0] == stage.file.read:
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
        return execute(executor, function, *args)

    monkeypatch.setattr(loop, "run_in_executor", run_in_executor)
    operation = asyncio.create_task(collect(service.BinaryResponse(stage)))
    with pytest.raises(asyncio.CancelledError) as caught:
        await operation
    assert getattr(caught.value, "storage_operation_unsettled", False) == (
        outcome == "unsettled"
    )
    assert len(reads) == 1 and not reads[0]._log_traceback
    assert stage.file.closed and service._slots._value == 2
