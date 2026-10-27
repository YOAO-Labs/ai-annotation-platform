"""Video profiles use the existing complete-reader, close and durable-permit path."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import managed_original_reader as reader
from tests.test_export_media_storage import RAW
from tests.test_export_media_transfer import runtime as runtime, until
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory


def video(runtime, monkeypatch):
    # Exercise the actual boundary selection with small real bytes, without
    # allocating a multi-GiB fixture or weakening any full-reader algorithm.
    runtime.snapshot.input_schema = 4
    runtime.snapshot._input_snapshot = {"schema": 4}
    monkeypatch.setattr(physical, "MAX_OBJECT_BYTES", len(RAW) - 1)
    monkeypatch.setattr(physical, "MAX_VIDEO_OBJECT_BYTES", len(RAW))


async def test_video_copy_passes_snapshot_profile_into_real_capture_and_saved_write(
    runtime, monkeypatch
):
    video(runtime, monkeypatch)
    captured, original = [], reader._capture

    def capture(spec, maximum, *args, **kwargs):
        captured.append((spec, maximum))
        return original(spec, maximum, *args, **kwargs)

    monkeypatch.setattr(reader, "_capture", capture)
    result = await runtime.copy()
    assert result.original_sha == runtime.source.sha256 and result.size == len(RAW)
    assert captured[0][1] == len(RAW) > physical.MAX_OBJECT_BYTES
    assert captured[0][0].bucket == "saved-originals"
    key = physical.prefix_for(runtime.snapshot.id) + runtime.snapshot.relative
    assert runtime.client.current(key)["data"] == RAW
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 2


async def test_video_saved_copy_uses_same_profile_and_never_resolves_original_again(
    runtime, monkeypatch
):
    video(runtime, monkeypatch)
    await runtime.copy()
    resolved = runtime.resolve_calls
    runtime.provider.calls.clear()
    captures, original = [], reader._capture

    def capture(spec, maximum, *args, **kwargs):
        captures.append((spec, maximum))
        return original(spec, maximum, *args, **kwargs)

    monkeypatch.setattr(reader, "_capture", capture)
    result = await transfer.read_copy(
        runtime.base,
        runtime.snapshot,
        relative=runtime.snapshot.relative,
        guard=runtime.guard,
        read=lambda file: file.read(),
    )
    assert result == RAW and runtime.resolve_calls == resolved
    spec, maximum = captures[0]
    assert spec.bucket == "saved-export-media" and maximum == len(RAW)
    assert (
        spec.object_key
        == physical.prefix_for(runtime.snapshot.id) + runtime.snapshot.relative
    )
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)


def test_claimed_source_media_type_cannot_select_video_budget():
    source = SimpleNamespace(
        endpoint_url="http://saved-originals.test:9000",
        bucket="saved-originals",
        object_key="same/video.mp4",
        sha256="a" * 64,
        size_bytes=5 * 1024**3,
        location_id=physical.LEGACY_LOCATION_ID,
        maintenance_revision=1,
        media_type="video",
        input_schema=4,
    )
    with pytest.raises(transfer.ContentError):
        transfer._spec(source)
    assert (
        transfer._spec(
            source, maximum=physical.object_limit(SimpleNamespace(input_schema=4))
        ).size_bytes
        == 5 * 1024**3
    )


async def test_scalar_conflict_refuses_before_any_original_or_output_io(
    runtime, monkeypatch
):
    video(runtime, monkeypatch)
    runtime.snapshot._input_snapshot = {"schema": 3}
    with pytest.raises(physical.ExportMediaStorageError):
        await runtime.copy()
    assert not runtime.provider.calls and not runtime.client.calls
    assert not runtime.permits and runtime.guard_calls == 0


async def test_legacy_snapshot_cannot_copy_a_source_claiming_large_video_bytes(
    runtime, monkeypatch
):
    runtime.source = replace(runtime.source, size_bytes=physical.MAX_OBJECT_BYTES + 1)
    key = next(iter(runtime.snapshot._inventory["objects"]))
    runtime.snapshot._inventory["objects"][key]["size_bytes"] = (
        runtime.source.size_bytes
    )
    with pytest.raises(physical.ExportMediaStorageError):
        await runtime.copy()
    assert (
        not runtime.provider.calls and not runtime.client.calls and not runtime.permits
    )


@pytest.mark.parametrize("phase", ["capture", "put"])
async def test_video_repeated_cancellation_waits_for_actual_io_and_stage_close(
    runtime, monkeypatch, phase
):
    video(runtime, monkeypatch)
    runtime.block_phase = phase
    task = asyncio.create_task(runtime.copy())
    await until(runtime.blocked.is_set)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not all(permit.settled for permit in runtime.permits)
    finally:
        runtime.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(stage.file.closed for stage in runtime.stages)
    assert all(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 2


async def test_video_unknown_stage_close_retains_original_output_permits_and_capacity(
    runtime, monkeypatch
):
    video(runtime, monkeypatch)
    runtime.stage_close_failure = RuntimeError("unknown local close")
    with pytest.raises(RuntimeError) as failure:
        await runtime.copy()
    assert getattr(failure.value, "storage_operation_unsettled", False)
    assert runtime.stages[-1].file.closed
    assert not any(permit.settled for permit in runtime.permits)
    assert transfer._slots._value == 1
