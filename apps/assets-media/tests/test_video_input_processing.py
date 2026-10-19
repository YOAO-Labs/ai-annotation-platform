import asyncio
import hashlib
from pathlib import Path
import signal
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from aap_assets_video_inputs_runtime import processing as p
from video_input_fixtures import record, write_video


def sampling(**changes):
    return v.FrameSampling(**{"max_frames": 3, "step": 2, "width": 32, **changes})


def test_decoded_rows_with_unselected_missing_pts_keep_the_original_indices():
    rows = [
        b"key_frame=1|best_effort_timestamp_time=-0.015\n",
        b"key_frame=0|best_effort_timestamp_time=N/A\n",
        b"key_frame=0|best_effort_timestamp_time=0.022\n",
        b"key_frame=0|best_effort_timestamp_time=0.021\n",
        b"key_frame=0|best_effort_timestamp_time=0.022\n",
    ]
    selected = [
        entry
        for index, row in enumerate(rows)
        if (entry := p.decoded_frame_entry(row, index, sampling())) is not None
    ]
    assert [(entry.ordinal, entry.frame_index, entry.pts_ms) for entry in selected] == [
        (1, 0, -15),
        (2, 2, 22),
        (3, 4, 22),
    ]
    assert p.decoded_frame_entry(rows[0], 6, sampling()) is None


@pytest.mark.parametrize(
    "row",
    [
        b"key_frame=0|best_effort_timestamp_time=N/A\n",
        b"key_frame=0|pts_time=NaN\n",
        b"key_frame=0|pts_time=Infinity\n",
        b"key_frame=0|pts_time=9007199254740992\n",
        b"key_frame=0|best_effort_timestamp_time=N/A|pts_time=N/A|pkt_dts_time=0.200\n",
    ],
)
def test_selected_decoded_frame_without_representable_pts_fails_instead_of_renumbering(
    row,
):
    with pytest.raises(c.VideoInputFailure, match="timebase_incomplete"):
        p.decoded_frame_entry(row, 2, sampling())


@pytest.mark.parametrize(
    "raw,expected",
    [("-0.0015", -2), ("-0.0005", 0), ("0.0005", 0), ("0.0015", 2), ("0.022", 22)],
)
def test_pts_conversion_is_exact_decimal_milliseconds_with_half_even_rounding(
    raw, expected
):
    assert (
        p.decoded_frame_entry(
            f"key_frame=1|pts_time={raw}".encode(), 0, sampling()
        ).pts_ms
        == expected
    )


async def test_metadata_reader_handles_fragmented_output_and_bounds_the_document():
    reader = AsyncMock()
    reader.read.side_effect = [b'{"streams":', b"[]}", b""]
    assert await p._read_document(reader) == b'{"streams":[]}'
    reader.read.side_effect = [b"x" * 4096] * 17
    with pytest.raises(c.VideoInputFailure, match="processing_failed"):
        await p._read_document(reader)


@pytest.mark.parametrize("format_", ["jpeg", "webp"])
async def test_real_retained_video_produces_decoded_timebase_and_valid_frame_bytes(
    tmp_path, format_
):
    path = write_video(tmp_path / "source.mp4")
    selected = sampling(format=format_)
    probed = await p.probe_retained_video(path, selected, tmp_path / "entries.jsonl")
    assert probed.video.frame_count == 8
    assert (probed.video.width, probed.video.height) == (64, 48)
    assert [(entry.frame_index, entry.pts_ms) for entry in probed.entries()] == [
        (0, 0),
        (2, 200),
        (4, 400),
    ]
    assert [len(page) for page in probed.pages()] == [3]
    accepted = record("ready").plan.input.model_dump(mode="json")
    accepted["source"]["object_identity"]["byte_size"] = path.stat().st_size
    accepted["blob"]["byte_size"] = path.stat().st_size
    accepted["blob"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    accepted["video"] = probed.video.model_dump()
    plan = v.FrozenFramePlan.from_entries(
        v.FrozenVideoInput.model_validate(accepted), selected, probed.entries()
    )
    entry = list(probed.entries())[1]
    output = await p.extract_retained_frame(
        path, tmp_path / f"frame.{format_}", plan, entry
    )
    assert output.byte_size == output.path.stat().st_size > 0
    assert hashlib.sha256(output.path.read_bytes()).hexdigest() == output.sha256
    with Image.open(output.path) as image:
        assert image.format == ("JPEG" if format_ == "jpeg" else "WEBP")
        assert image.size == (32, 24)
    with pytest.raises(c.VideoInputFailure, match="invalid_request"):
        await p.extract_retained_frame(path, output.path, plan, entry)


async def test_real_probe_spool_budget_fails_without_a_partial_timebase(tmp_path):
    path = write_video(tmp_path / "source.mp4")
    destination = tmp_path / "entries.jsonl"
    with pytest.raises(c.VideoInputFailure, match="resource_limit"):
        await p.probe_retained_video(
            path,
            sampling(),
            destination,
            policy=p.VideoExecutionPolicy(max_timebase_bytes=10),
        )
    assert not destination.exists()


def fake_probe(tmp_path: Path, monkeypatch, *, frame_script: str):
    executable = tmp_path / "ffprobe"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,sys,time,os\n"
        "if '-show_frames' not in sys.argv:\n"
        " print(json.dumps({'streams':[{'width':64,'height':48,'duration':'1'}]}))\n"
        "else:\n" + "\n".join(" " + line for line in frame_script.splitlines()) + "\n"
    )
    executable.chmod(0o700)
    import os

    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    path = tmp_path / "source.mp4"
    path.write_bytes(b"verified-original")
    return path


async def test_probe_uses_real_decoded_count_including_unselected_missing_pts(
    tmp_path, monkeypatch
):
    path = fake_probe(
        tmp_path,
        monkeypatch,
        frame_script="""print('key_frame=1|pts_time=0')
print('key_frame=0|pts_time=N/A')
print('key_frame=0|pts_time=0.200')
print('key_frame=0|pts_time=0.300')
print('key_frame=0|pts_time=0.400')""",
    )
    probed = await p.probe_retained_video(path, sampling(), tmp_path / "entries.jsonl")
    assert probed.video.frame_count == 5
    assert [entry.frame_index for entry in probed.entries()] == [0, 2, 4]


async def test_probe_never_skips_a_selected_pts_gap_and_cleans_partial_spool(
    tmp_path, monkeypatch
):
    path = fake_probe(
        tmp_path,
        monkeypatch,
        frame_script="""print('key_frame=1|pts_time=0')
print('key_frame=0|pts_time=0.100')
print('key_frame=0|pts_time=N/A')
print('key_frame=0|pts_time=0.300')""",
    )
    destination = tmp_path / "entries.jsonl"
    with pytest.raises(c.VideoInputFailure, match="timebase_incomplete"):
        await p.probe_retained_video(path, sampling(), destination)
    assert not destination.exists()


async def test_probe_cancellation_kills_and_reaps_child_before_removing_workspace(
    tmp_path, monkeypatch
):
    pid_path = tmp_path / "child.pid"
    path = fake_probe(
        tmp_path,
        monkeypatch,
        frame_script=f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "print('key_frame=1|pts_time=0', flush=True)\ntime.sleep(60)",
    )
    destination = tmp_path / "entries.jsonl"
    task = asyncio.create_task(p.probe_retained_video(path, sampling(), destination))
    async with asyncio.timeout(5):
        while not pid_path.exists():
            await asyncio.sleep(0.01)
    pid = int(pid_path.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    import os

    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)
    assert not destination.exists()


async def test_probe_does_not_accept_silent_partial_decode_after_ffprobe_reports_errors(
    tmp_path, monkeypatch
):
    path = fake_probe(
        tmp_path,
        monkeypatch,
        frame_script="print('key_frame=1|pts_time=0')\nprint('corrupt decoded frame', file=sys.stderr)",
    )
    destination = tmp_path / "entries.jsonl"
    with pytest.raises(c.VideoInputFailure, match="processing_failed"):
        await p.probe_retained_video(path, sampling(), destination)
    assert not destination.exists()


def test_execution_budgets_reject_invalid_limits_before_starting_workers():
    for values in (
        {"lease_seconds": 60},
        {"job_timeout_seconds": True},
        {"max_temp_bytes": 1},
        {"max_timebase_bytes": 0},
    ):
        with pytest.raises(ValueError):
            p.VideoExecutionPolicy(**values)


async def test_heartbeat_loss_interrupts_and_reaps_active_probe(tmp_path, monkeypatch):
    from contextlib import suppress
    from types import SimpleNamespace
    import os

    from aap_assets_video_inputs_runtime.worker import _Lease
    from video_input_fixtures import claim

    pid_path = tmp_path / "revoked-child.pid"
    path = fake_probe(
        tmp_path,
        monkeypatch,
        frame_script=f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "print('key_frame=1|pts_time=0', flush=True)\ntime.sleep(60)",
    )
    started = asyncio.Event()
    beats = 0

    async def heartbeat(_operation, request):
        nonlocal beats
        beats += 1
        if beats > 1:
            await started.wait()
            raise c.VideoInputFailure("lease_lost")
        return c.VideoInputLease(
            **request.model_dump(exclude={"lease_seconds"}),
            lease_expires_at="2026-10-19T01:00:00Z",
        )

    lease = _Lease(SimpleNamespace(call=heartbeat), claim(), p.VideoExecutionPolicy())
    lease.interval = 0.01
    await lease.start()
    destination = tmp_path / "revoked-entries.jsonl"
    task = asyncio.create_task(
        lease.call(p.probe_retained_video, path, sampling(), destination)
    )
    try:
        async with asyncio.timeout(5):
            while not pid_path.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_path.read_text())
        started.set()
        with pytest.raises(c.VideoInputFailure, match="lease_lost"):
            await asyncio.wait_for(task, timeout=2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)
        assert not destination.exists()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, c.VideoInputFailure):
            await task
        await lease.close()
