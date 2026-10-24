"""Pure FD/probe admission and actual native-process settlement regressions."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting import video_probe as probe


def payload(**stream):
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 4,
                "height": 3,
                "avg_frame_rate": "30000/1001",
                "nb_read_frames": "7",
                "nb_frames": "9999",
                **stream,
            }
        ],
        "format": {"duration": "1.234"},
    }


def test_actual_decoded_count_overrides_advertised_and_duration_estimates():
    receipt = probe.parse(payload())
    assert receipt == {
        "probe_contract": 1,
        "video": {
            "frame_count": 7,
            "fps": 29.97,
            "width": 4,
            "height": 3,
            "codec": "h264",
            "duration_ms": 1234,
        },
    }


@pytest.mark.parametrize("value", [None, "N/A", True, 7, "-1", "0", "1000001", "７"])
def test_missing_or_unbounded_real_count_cannot_be_replaced_by_metadata(value):
    with pytest.raises(ContentError, match="video_export_probe_"):
        probe.parse(payload(nb_read_frames=value))


@pytest.mark.parametrize(
    "change",
    [{"width": 0}, {"height": -1}, {"avg_frame_rate": "0/0"}, {"codec_name": ""}],
)
def test_invalid_dimensions_codec_or_fps_are_not_defaulted(change):
    with pytest.raises(ContentError):
        probe.parse(payload(**change))


def test_probe_borrows_same_regular_fd_and_restores_cursor():
    with tempfile.TemporaryFile() as file:
        file.write(b"verified original bytes")
        file.seek(3)

        def capture(command, descriptors):
            assert descriptors == (file.fileno(),)
            assert command[-1] == f"/proc/self/fd/{file.fileno()}"
            assert (
                "-count_frames" in command
                and "nb_read_frames" in command[command.index("-show_entries") + 1]
            )
            assert file.tell() == 0
            os.read(file.fileno(), 4)
            return json.dumps(payload()).encode()

        with patch.object(probe, "_capture", capture):
            assert probe.inspect(file)["video"]["frame_count"] == 7
        assert not file.closed and file.tell() == 3


def test_probe_rejects_empty_or_oversized_source_before_native_constructor():
    with (
        tempfile.TemporaryFile() as file,
        patch.object(
            probe,
            "_capture",
            side_effect=AssertionError("native work before admission"),
        ),
    ):
        with pytest.raises(ContentError, match="source_budget"):
            probe.inspect(file)
        file.truncate(probe.MAX_SOURCE_BYTES + 1)
        with pytest.raises(ContentError, match="source_budget"):
            probe.inspect(file)


def native_capture(program, *, interrupt=None):
    """Exercise real pipe/kill/wait/close while bypassing only decoder argv policy."""
    processes, original = [], subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        if interrupt is not None:
            original_wait = process.wait
            attempts = 0

            def wait(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise interrupt()
                return original_wait(*args, **kwargs)

            process.wait = wait
        return process

    return (
        processes,
        patch.object(probe.subprocess, "Popen", spawn),
        patch.object(
            probe.media_process,
            "_decoder_args",
            return_value=[sys.executable, "-c", program],
        ),
    )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_each_native_pipe_is_bounded_and_process_really_closes(stream):
    processes, spawn, policy = native_capture(
        f"import sys,time;sys.{stream}.buffer.write(b'x'*200000);sys.{stream}.flush();time.sleep(60)"
    )
    with spawn, policy, pytest.raises(ContentError, match="output_budget"):
        probe._capture([], ())
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, asyncio.CancelledError])
def test_repeated_baseexception_during_probe_and_wait_drains_native_child(interruption):
    processes, spawn, policy = native_capture(
        "import time;time.sleep(60)", interrupt=interruption
    )
    with (
        spawn,
        policy,
        patch.object(probe.selectors.BaseSelector, "select", side_effect=interruption),
    ):
        # DefaultSelector overrides select; interrupt its actual implementation.
        with patch.object(
            probe.selectors.DefaultSelector, "select", side_effect=interruption
        ):
            with pytest.raises(interruption):
                probe._capture([], ())
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_unknown_native_constructor_is_preserved_through_inspect():
    with tempfile.TemporaryFile() as file:
        file.write(b"verified original")
        file.seek(0)
        failure = OSError("constructor acknowledgment lost")
        with (
            patch.object(probe.media_process, "_decoder_args", return_value=[]),
            patch.object(probe.subprocess, "Popen", side_effect=failure),
        ):
            with pytest.raises(OSError) as caught:
                probe.inspect(file)
        assert caught.value is failure
        assert caught.value.storage_operation_unsettled
        assert not file.closed
