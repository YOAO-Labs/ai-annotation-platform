from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from aap_media_probe import probe as python_probe

REPO_ROOT = Path(__file__).resolve().parents[4]
PEER = Path(__file__).with_name("node_peer.mjs")
TOKEN = "node-replacement-acceptance-token-20261018"
MAX_FILE_BYTES = 1 << 20


def _require_tool(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        pytest.skip(f"{name} is required for the replacement acceptance test")
    return value


def _ffmpeg_fixture(tmp_path: Path) -> Path:
    ffmpeg = _require_tool("ffmpeg")
    fixture = tmp_path / "cfr-bframes.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=25",
            "-frames:v",
            "24",
            "-c:v",
            "mpeg4",
            "-bf",
            "2",
            "-g",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(fixture),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return fixture


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_env(*, tmpdir: Path) -> dict[str, str]:
    tmpdir.mkdir()
    return {
        **({"PATH": os.environ["PATH"]} if "PATH" in os.environ else {}),
        "TMPDIR": str(tmpdir),
        "MEDIA_PROBE_TOKEN": TOKEN,
        "MEDIA_PROBE_MAX_FILE_BYTES": str(MAX_FILE_BYTES),
    }


def _stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return process.communicate(timeout=5)


def _start_node(tmpdir: Path, *, max_file_bytes: int = MAX_FILE_BYTES):
    node = _require_tool("node")
    env = _base_env(tmpdir=tmpdir)
    env["MEDIA_PROBE_MAX_FILE_BYTES"] = str(max_file_bytes)
    env["MEDIA_PROBE_PORT"] = "0"
    process = subprocess.Popen(
        [node, str(PEER)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        ready = selector.select(timeout=10)
        if not ready:
            stdout, stderr = _stop(process)
            raise AssertionError(f"Node peer did not become ready: {stdout}\n{stderr}")
        line = process.stdout.readline()
        state = json.loads(line)
        assert set(state) == {"port"}
        assert 0 < state["port"] <= 65535
        return process, f"http://127.0.0.1:{state['port']}"
    except BaseException:
        if process.poll() is None:
            _stop(process)
        raise
    finally:
        selector.close()


def _start_python_service(tmpdir: Path, *, max_file_bytes: int = MAX_FILE_BYTES):
    source = Path(
        os.environ.get("AAP_MEDIA_PROBE_SOURCE_DIR", REPO_ROOT / "apps/media-probe/src")
    ).resolve()
    assert source.is_dir(), (
        "apps/media-probe/src must be integrated, or set AAP_MEDIA_PROBE_SOURCE_DIR "
        "to the source directory for local pre-integration acceptance"
    )
    python = os.environ.get("AAP_MEDIA_PROBE_SERVICE_PYTHON", sys.executable)
    env = {
        **({"PATH": os.environ["PATH"]} if "PATH" in os.environ else {}),
        "TMPDIR": str(tmpdir),
        "PYTHONPATH": str(source),
        "MEDIA_PROBE_TOKEN": TOKEN,
        "MEDIA_PROBE_MAX_FILE_BYTES": str(max_file_bytes),
    }
    tmpdir.mkdir()
    port = _free_port()
    process = subprocess.Popen(
        [
            python,
            "-m",
            "uvicorn",
            "aap_media_probe.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = _stop(process)
                    raise AssertionError(
                        f"Python Media Probe exited early: {stdout}\n{stderr}"
                    )
                try:
                    response = client.get(f"{base_url}/health")
                    if response.status_code == 200:
                        assert response.json() == {"status": "ok"}
                        return process, base_url
                except httpx.HTTPError:
                    time.sleep(0.05)
        stdout, stderr = _stop(process)
        raise AssertionError(
            f"Python Media Probe did not become ready: {stdout}\n{stderr}"
        )
    except BaseException:
        if process.poll() is None:
            _stop(process)
        raise


def _post(base_url: str, path: str, body: bytes, *, token: str | None = TOKEN):
    headers = {"Content-Type": "application/octet-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(trust_env=False, timeout=60) as client:
        return client.post(
            f"{base_url}{path}",
            headers=headers,
            content=body,
        )


def _ndjson_data(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    lines = response.content.splitlines(keepends=True)
    assert lines and all(line.endswith(b"\n") for line in lines)
    records = [json.loads(line) for line in lines]
    completion = records[-1]
    assert completion["type"] == "complete"
    assert completion["count"] == len(records) - 1
    assert completion["sha256"] == hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    return records[:-1]


def test_node_peer_replaces_the_python_probe_over_the_wire_and_restarts(tmp_path):
    oracle = python_probe
    fixture = _ffmpeg_fixture(tmp_path)
    media = fixture.read_bytes()
    assert media and len(media) < MAX_FILE_BYTES

    metadata = oracle.probe_video_file(fixture)
    frames = oracle.probe_video_frame_timetable(fixture)
    start_frame = 37
    packets = oracle.probe_chunk_samples(fixture, start_frame)
    assert metadata["fps"] == 25.0 and metadata["frame_count"] == 24
    assert len(frames) == 24 and any(row["pict_type"] == "B" for row in frames)
    assert any(
        packets[index]["pts_ms"] > packets[index + 1]["pts_ms"]
        for index in range(len(packets) - 1)
    ), "fixture must contain B-frame packet presentation reordering"

    operations = [
        ("metadata", "/v1/metadata", metadata),
        ("frames", "/v1/frames", frames),
        ("packets", f"/v1/packets?start_frame={start_frame}", packets),
    ]
    python_process, python_url = _start_python_service(tmp_path / "python-tmp")
    node_process, node_url = _start_node(tmp_path / "node-tmp")
    node_responses: dict[str, bytes] = {}
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get(f"{node_url}/health").json() == {"status": "ok"}
            assert client.get(f"{python_url}/health").json() == {"status": "ok"}
        for name, route, expected in operations:
            node_response = _post(node_url, route, media)
            python_response = _post(python_url, route, media)
            assert node_response.status_code == python_response.status_code == 200
            node_responses[name] = node_response.content
            if name == "metadata":
                assert node_response.json() == expected
                assert python_response.json() == expected
            else:
                expected_type = "frame" if name == "frames" else "packet"
                expected_records = [
                    {"type": expected_type, "data": row} for row in expected
                ]
                assert _ndjson_data(node_response) == expected_records
                assert _ndjson_data(python_response) == expected_records

        with socket.socket() as sentinel:
            sentinel.bind(("127.0.0.1", 0))
            sentinel.listen()
            sentinel.settimeout(0.05)
            sentinel_port = sentinel.getsockname()[1]
            stopped = threading.Event()
            connections: list[bool] = []

            def reject_local_probe_connections():
                while not stopped.is_set():
                    try:
                        connection, _ = sentinel.accept()
                    except socket.timeout:
                        continue
                    connections.append(True)
                    connection.close()

            sentinel_thread = threading.Thread(
                target=reject_local_probe_connections, daemon=True
            )
            sentinel_thread.start()
            playlist = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-TARGETDURATION:1\n"
                "#EXTINF:1.0,\n"
                f"http://127.0.0.1:{sentinel_port}/segment.ts\n"
                "#EXT-X-ENDLIST\n"
            ).encode()
            try:
                for base_url in (node_url, python_url):
                    rejected = _post(base_url, "/v1/metadata", playlist)
                    assert rejected.status_code == 422
                    assert rejected.json()["code"] == "probe_failed"
            finally:
                stopped.set()
                sentinel_thread.join(timeout=1)
            assert not sentinel_thread.is_alive()
            assert not connections
        assert not any((tmp_path / "node-tmp").iterdir())
        assert not any((tmp_path / "python-tmp").iterdir())
    finally:
        _stop(node_process)
        _stop(python_process)

    restarted, restarted_url = _start_node(tmp_path / "node-restart-tmp")
    try:
        for name, route, _expected in operations:
            replay = _post(restarted_url, route, media)
            assert replay.status_code == 200
            assert replay.content == node_responses[name]
        assert not any((tmp_path / "node-restart-tmp").iterdir())
    finally:
        _stop(restarted)


def test_python_rounding_edges_match_the_original_builtin_semantics():
    node = _require_tool("node")
    module_url = PEER.as_uri()
    script = f"""
import assert from 'node:assert/strict';
import {{ pythonRoundInteger, pythonRoundDigits }} from {json.dumps(module_url)};
assert.deepEqual([0.5, 1.5, 2.5, -0.5, -1.5, -2.5].map(pythonRoundInteger), [0, 2, 2, 0, -2, -2]);
assert.equal(pythonRoundDigits(2.675, 2), 2.67);
assert.equal(pythonRoundDigits(-2.675, 2), -2.67);
assert.equal(pythonRoundDigits(29.97002997, 3), 29.97);
assert.equal(pythonRoundDigits(29.9685, 3), 29.968);
assert.equal(pythonRoundDigits(23.9765, 3), 23.977);
"""
    environment = {"PATH": os.environ.get("PATH", "")}
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_node_peer_rejects_bad_auth_invalid_media_and_oversized_uploads(tmp_path):
    fixture = _ffmpeg_fixture(tmp_path)
    process, base_url = _start_node(tmp_path / "node-errors-tmp", max_file_bytes=256)
    try:
        unauthenticated = _post(
            base_url, "/v1/metadata", b"valid-looking-body", token=None
        )
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "invalid_request"

        invalid_type = httpx.post(
            f"{base_url}/v1/metadata",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "text/plain"},
            content=b"not video",
            trust_env=False,
        )
        assert invalid_type.status_code == 400
        assert invalid_type.json()["code"] == "invalid_request"

        invalid_media = _post(base_url, "/v1/metadata", b"not a video")
        assert invalid_media.status_code == 422
        assert invalid_media.json()["code"] == "probe_failed"

        oversized = _post(base_url, "/v1/metadata", fixture.read_bytes())
        assert oversized.status_code == 413
        assert oversized.json()["code"] == "file_too_large"

        with httpx.Client(trust_env=False, timeout=10) as client:
            chunked = client.post(
                f"{base_url}/v1/metadata",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/octet-stream",
                },
                content=iter((b"a" * 200, b"b" * 200)),
            )
        assert chunked.status_code == 413
        assert chunked.json()["code"] == "file_too_large"
        deadline = time.monotonic() + 1
        while (
            any((tmp_path / "node-errors-tmp").iterdir())
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not any((tmp_path / "node-errors-tmp").iterdir())
    finally:
        _stop(process)
