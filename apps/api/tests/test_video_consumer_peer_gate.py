"""Local protocol tests for the optional container-acceptance peer gate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import socket
import subprocess
import sys
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from tests.fixtures.video_consumer_ml_peer import PROGRAM


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _jpeg() -> bytes:
    output = BytesIO()
    image = Image.new("RGB", (160, 90), "black")
    image.paste("red", (72, 37, 88, 53))
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


class _FrameHandler(BaseHTTPRequestHandler):
    content = _jpeg()

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        body = self.content
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _request(origin: str, method: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        f"{origin}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _wait_ready(origin: str, process: subprocess.Popen, timeout: float = 5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("peer process exited before becoming ready")
        try:
            status, _ = _request(origin, "GET", "/health")
            if status == 200:
                return
        except Exception:
            time.sleep(0.03)
    raise AssertionError("peer process did not become ready")


def _start_peer(
    *, gate_after_calls: int
) -> tuple[subprocess.Popen, str, int, ThreadingHTTPServer]:
    frame_server = ThreadingHTTPServer(("127.0.0.1", 0), _FrameHandler)
    frame_thread = Thread(target=frame_server.serve_forever, daemon=True)
    frame_thread.start()
    frame_port = int(frame_server.server_address[1])
    peer_port = _port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "AAP_VIDEO_PEER_MINIO_HOST": "127.0.0.1",
        "AAP_VIDEO_PEER_MINIO_PORT": str(frame_port),
        "AAP_VIDEO_PEER_PORT": str(peer_port),
        "AAP_VIDEO_PEER_EXPECTED_WIDTH": "160",
        "AAP_VIDEO_PEER_EXPECTED_HEIGHT": "90",
        "AAP_VIDEO_PEER_GATE_AFTER_CALLS": str(gate_after_calls),
        "AAP_VIDEO_PEER_GATE_TIMEOUT_SECONDS": "3",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", PROGRAM],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    origin = f"http://127.0.0.1:{peer_port}"
    try:
        _wait_ready(origin, process)
    except BaseException:
        process.terminate()
        process.wait(timeout=3)
        frame_server.shutdown()
        frame_server.server_close()
        raise
    return process, origin, frame_port, frame_server


def _predict(origin: str, frame_port: int, task: str):
    return _request(
        origin,
        "POST",
        "/predict",
        {
            "tasks": [
                {
                    "id": task,
                    "file_path": (
                        f"http://127.0.0.1:{frame_port}/frame"
                        "?X-Amz-Signature=local-test"
                    ),
                }
            ]
        },
    )


def _stop_peer(process: subprocess.Popen, frame_server: ThreadingHTTPServer):
    process.terminate()
    process.wait(timeout=3)
    frame_server.shutdown()
    frame_server.server_close()


def test_peer_gate_holds_one_response_until_explicit_release():
    process, origin, frame_port, frame_server = _start_peer(gate_after_calls=1)
    try:
        assert _predict(origin, frame_port, "prior:0")[0] == 200
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(_predict, origin, frame_port, "held:7")
            deadline = time.monotonic() + 2
            state = None
            while time.monotonic() < deadline:
                _, state = _request(origin, "GET", "/__acceptance/gate-state")
                if state["entered"]:
                    break
                time.sleep(0.02)
            assert state == {
                "enabled": True,
                "entered": True,
                "task": "held:7",
                "frame_index": 7,
                "release_reason": None,
                "after_calls": 1,
            }
            assert len(_request(origin, "GET", "/__acceptance/calls")[1]["calls"]) == 1
            assert _request(origin, "POST", "/__acceptance/release")[1] == {
                "released": True
            }
            assert pending.result(timeout=2)[0] == 200
        calls = _request(origin, "GET", "/__acceptance/calls")[1]["calls"]
        assert [call["task"] for call in calls] == ["prior:0", "held:7"]
        assert all(call["width"] == 160 and call["height"] == 90 for call in calls)
        assert all(call["fetch_decode_ms"] >= 0 for call in calls)
        assert calls[1]["gate_wait_ms"] > 0
        assert calls[1]["peer_service_ms"] < calls[1]["gate_wait_ms"]
    finally:
        _stop_peer(process, frame_server)


def test_peer_gate_is_disabled_for_default_smoke_profile():
    process, origin, frame_port, frame_server = _start_peer(gate_after_calls=0)
    try:
        assert _request(origin, "GET", "/__acceptance/gate-state")[0] == 404
        assert _predict(origin, frame_port, "smoke:0")[0] == 200
        state = _request(origin, "GET", "/__acceptance/calls")[1]["calls"]
        assert len(state) == 1 and state[0]["task"] == "smoke:0"
    finally:
        _stop_peer(process, frame_server)
