"""Physical Export work must drain without erasing errors or settlement evidence."""

from builtins import BaseExceptionGroup
import asyncio
import threading

import pytest

from app.services import export_execution_context, storage_access

pytestmark = pytest.mark.asyncio


class ExecutionOwner:
    def __init__(self, finished, *, settle_error=None):
        self.finished = finished
        self.settle_error = settle_error
        self.entered = 0
        self.settled = 0

    async def enter_io(self):
        self.entered += 1

    async def settle_io(self):
        assert self.finished.is_set()
        if self.settle_error is not None:
            raise self.settle_error
        self.settled += 1


@pytest.mark.parametrize("unknown", [False, True])
async def test_cancellation_retains_later_real_read_failure(tmp_path, unknown):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    owner = ExecutionOwner(finished)
    token = export_execution_context.bind(owner)

    def read_after_release():
        started.set()
        try:
            if not release.wait(3):
                raise TimeoutError("test did not release the physical read")
            try:
                return source.read_bytes()
            except FileNotFoundError as exc:
                if unknown:
                    storage_access.mark_unsettled(exc)
                raise
        finally:
            finished.set()

    try:
        task = asyncio.create_task(export_execution_context.run_sync(read_after_release))
        assert await asyncio.to_thread(started.wait, 3)
        source.unlink()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as captured:
            await task
    finally:
        release.set()
        export_execution_context.reset(token)

    cancellation, physical = captured.value.exceptions
    assert isinstance(cancellation, asyncio.CancelledError)
    assert isinstance(physical, FileNotFoundError)
    assert storage_access.failure_unsettled(captured.value) is unknown
    assert owner.entered == 1
    assert owner.settled == (0 if unknown else 1)


async def test_ordinary_physical_error_is_not_duplicated():
    finished = threading.Event()
    owner = ExecutionOwner(finished)
    token = export_execution_context.bind(owner)
    original = OSError("physical read failed")

    def fail():
        finished.set()
        raise original

    try:
        with pytest.raises(OSError) as captured:
            await export_execution_context.run_sync(fail)
    finally:
        export_execution_context.reset(token)
    assert captured.value is original
    assert owner.settled == 1


async def test_settlement_failure_preserves_primary_and_unsettled_state():
    finished = threading.Event()
    closing = OSError("owner settlement failed")
    owner = ExecutionOwner(finished, settle_error=closing)
    token = export_execution_context.bind(owner)
    original = FileNotFoundError("physical read failed")

    def fail():
        finished.set()
        raise original

    try:
        with pytest.raises(BaseExceptionGroup) as captured:
            await export_execution_context.run_sync(fail)
    finally:
        export_execution_context.reset(token)
    assert captured.value.exceptions == (original, closing)
    assert storage_access.failure_unsettled(captured.value)
    assert owner.settled == 0
