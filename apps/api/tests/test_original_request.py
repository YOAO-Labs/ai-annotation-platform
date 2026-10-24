"""Disconnects before streaming must settle the actual snapshot owner."""

import asyncio
import io
from types import SimpleNamespace

import pytest

from app.services.managed_original_reader import BinaryStage
from app.services.original_request import connected_snapshot


def staged():
    return BinaryStage(io.BytesIO(b"verified"), "a" * 64, 8, {})


def receiver():
    disconnect = asyncio.Event()
    finished = asyncio.Event()

    async def receive():
        try:
            await disconnect.wait()
            return {"type": "http.disconnect"}
        finally:
            finished.set()

    return SimpleNamespace(receive=receive), disconnect, finished


@pytest.mark.parametrize("fails", [False, True])
async def test_immediate_capture_does_not_require_the_watcher_to_start(fails):
    request, _, _ = receiver()
    stage = staged()

    async def capture():
        if fails:
            raise ValueError("source unavailable")
        return stage

    try:
        if fails:
            with pytest.raises(ValueError, match="source unavailable"):
                await connected_snapshot(request, capture())
        else:
            assert await connected_snapshot(request, capture()) is stage
        assert not stage.file.closed
    finally:
        stage.close()


async def test_ready_snapshot_is_transferred_and_receive_watcher_has_stopped():
    request, _, finished = receiver()
    stage = staged()

    async def capture():
        await asyncio.sleep(0)
        return stage

    assert await connected_snapshot(request, capture()) is stage
    assert finished.is_set() and not stage.file.closed
    stage.close()


@pytest.mark.parametrize("cancel_again", [False, True])
async def test_disconnect_waits_for_capture_drain_and_closes_late_stage(cancel_again):
    request, disconnected, finished = receiver()
    entered, cancelled, physical_end = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stage = staged()

    async def capture():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            while not physical_end.is_set():
                try:
                    await physical_end.wait()
                except asyncio.CancelledError:
                    pass
            return stage  # The physical reader won the late completion race.

    pending = asyncio.create_task(connected_snapshot(request, capture()))
    await entered.wait()
    disconnected.set()
    await cancelled.wait()
    if cancel_again:
        pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not stage.file.closed
    physical_end.set()
    assert await pending is None
    assert stage.file.closed and finished.is_set()


async def test_external_cancellation_is_propagated_after_late_capture_is_closed():
    request, _, finished = receiver()
    entered = asyncio.Event()
    stage = staged()

    async def capture():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return stage

    pending = asyncio.create_task(connected_snapshot(request, capture()))
    await entered.wait()
    pending.cancel()
    # A capture that suppresses cancellation cannot transfer its late stage.
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stage.file.closed and finished.is_set()


async def test_materialized_result_is_transferred_without_a_physical_close():
    request, _, finished = receiver()
    result = {"source": "found", "media": "same"}

    async def operation():
        await asyncio.sleep(0)
        return result

    assert await connected_snapshot(request, operation(), close_result=False) is result
    assert finished.is_set()


@pytest.mark.parametrize("interruption", ["disconnect", "external"])
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_materialized_result_drains_late_work_without_trying_to_close_the_dict(
    interruption, cancel_again
):
    request, disconnected, watcher_finished = receiver()
    entered, cancelled, actual_end = asyncio.Event(), asyncio.Event(), asyncio.Event()
    result = {"source": "found", "media": "same"}

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            while not actual_end.is_set():
                try:
                    await actual_end.wait()
                except asyncio.CancelledError:
                    pass
            return result

    pending = asyncio.create_task(
        connected_snapshot(request, operation(), close_result=False)
    )
    await entered.wait()
    if interruption == "disconnect":
        disconnected.set()
    else:
        pending.cancel()
    await cancelled.wait()
    try:
        if cancel_again:
            pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        actual_end.set()
    if interruption == "disconnect":
        assert await pending is None
    else:
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert watcher_finished.is_set()


async def test_materialized_operation_keeps_unknown_drain_failure_on_external_cancel():
    from app.services.storage_access import mark_unsettled

    request, _, watcher_finished = receiver()
    entered, cancelled, actual_end = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as failure:
            cancelled.set()
            while not actual_end.is_set():
                try:
                    await actual_end.wait()
                except asyncio.CancelledError:
                    pass
            raise mark_unsettled(failure)

    pending = asyncio.create_task(
        connected_snapshot(request, operation(), close_result=False)
    )
    await entered.wait()
    pending.cancel()
    await cancelled.wait()
    try:
        pending.cancel()
        assert not pending.done()
    finally:
        actual_end.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await pending
    assert caught.value.storage_operation_unsettled and watcher_finished.is_set()
