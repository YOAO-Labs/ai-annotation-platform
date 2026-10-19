"""Graceful SIGTERM drain tests for the standalone Media worker process."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from aap_assets_media_runtime import worker


_CHILD = r"""
import asyncio
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from aap_assets_media_runtime import worker

mode = sys.argv[1]
events_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])

def record(value):
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(value + "\n")
        stream.flush()

class Media:
    async def maintenance_once(self):
        record("media-maintenance")

class Runtime:
    media_executor = Media()
    video_executor = object()

    async def maintenance_once(self):
        record("video-maintenance")

    async def close(self):
        record("closed")

class Poller:
    def __init__(self, _media, _video):
        self.claims = 0

    async def execute_once(self, **_selectors):
        self.claims += 1
        record(f"claim:{self.claims}")
        if mode in {"inflight", "once"} and self.claims == 1:
            record("entered")
            while not release_path.exists():
                await asyncio.sleep(0.01)
            record("finished")
            return object()
        return None

async def create_runtime(_config):
    return Runtime()

def previous_handler(_signum, _frame):
    return None

previous_sigint = signal.getsignal(signal.SIGINT)
worker.load_config = lambda: SimpleNamespace(poll_seconds=30)
worker.create_worker_runtime = create_runtime
worker.FairWorkerPoller = Poller
signal.signal(signal.SIGTERM, previous_handler)
sys.argv = ["assets-media-worker"]
if mode == "once":
    sys.argv.append("--once")
worker.main()
if (
    signal.getsignal(signal.SIGTERM) is not previous_handler
    or signal.getsignal(signal.SIGINT) is not previous_sigint
):
    raise SystemExit(91)
record("signal-restored")
"""


def _child_environment() -> dict[str, str]:
    repo = Path(__file__).resolve().parents[3]
    source_paths = (
        repo / "apps/assets-media/src",
        repo / "apps/media-probe/src",
    )
    inherited = os.environ.get("PYTHONPATH", "")
    path_values = [str(path) for path in source_paths]
    if inherited:
        path_values.append(inherited)
    pythonpath = os.pathsep.join(path_values)
    return {**os.environ, "PYTHONPATH": pythonpath}


def _wait_for_event(
    process: subprocess.Popen[str], events_path: Path, expected: str, timeout: float = 5
) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = (
            events_path.read_text(encoding="utf-8").splitlines()
            if events_path.exists()
            else []
        )
        if expected in events:
            return events
        if process.poll() is not None:
            pytest.fail(
                "worker child exited before reaching the expected state", pytrace=False
            )
        time.sleep(0.01)
    pytest.fail("worker child did not reach the expected state", pytrace=False)


@pytest.mark.parametrize("mode", ["idle", "inflight", "once"])
def test_cli_sigterm_closes_idle_or_drains_current_unit(tmp_path: Path, mode: str):
    events_path = tmp_path / "worker.events"
    release_path = tmp_path / "release"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            mode,
            str(events_path),
            str(release_path),
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        expected = "entered" if mode in {"inflight", "once"} else "claim:1"
        _wait_for_event(process, events_path, expected)
        process.send_signal(signal.SIGTERM)
        if mode in {"inflight", "once"}:
            time.sleep(0.05)
            assert process.poll() is None
            assert "closed" not in events_path.read_text(encoding="utf-8").splitlines()
            release_path.touch()
        process.communicate(timeout=5)
        assert process.returncode == 0
        events = events_path.read_text(encoding="utf-8").splitlines()
        assert events.count("claim:1") == 1
        assert sum(event.startswith("claim:") for event in events) == 1
        if mode in {"inflight", "once"}:
            assert (
                events.index("entered")
                < events.index("finished")
                < events.index("closed")
            )
        else:
            assert "finished" not in events
        assert events.index("closed") < events.index("signal-restored")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.asyncio
async def test_embedded_worker_loop_does_not_change_process_signal_handlers(
    monkeypatch,
):
    signal_before = signal.getsignal(signal.SIGTERM)
    sigint_before = signal.getsignal(signal.SIGINT)
    stopped = asyncio.Event()
    events: list[str] = []

    async def _nothing():
        return None

    class Runtime:
        media_executor = SimpleNamespace(maintenance_once=_nothing)
        video_executor = object()

        async def maintenance_once(self):
            return None

        async def close(self):
            events.append("closed")

    class Poller:
        def __init__(self, _media, _video):
            pass

        async def execute_once(self, **_selectors):
            events.append("unit-finished")
            stopped.set()
            return object()

    async def create_runtime(_config):
        return Runtime()

    monkeypatch.setattr(
        worker, "load_config", lambda: SimpleNamespace(poll_seconds=0.01)
    )
    monkeypatch.setattr(worker, "create_worker_runtime", create_runtime)
    monkeypatch.setattr(worker, "FairWorkerPoller", Poller)

    await worker._run_worker(once=False, shutdown_event=stopped)

    assert events == ["unit-finished", "closed"]
    assert signal.getsignal(signal.SIGTERM) is signal_before
    assert signal.getsignal(signal.SIGINT) is sigint_before
