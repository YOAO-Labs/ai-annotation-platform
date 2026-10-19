"""Small HTTP echo backend program for the retained-video container proof.

The acceptance test runs this program with ``python -c`` inside an owned test
container. It fetches only presigned frame URLs from the verifier's MinIO
endpoint and records hashes without retaining URLs or credentials.
"""

PROGRAM = r"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import signal
import sys
from threading import Event, Lock
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from PIL import Image

MINIO_HOST = os.environ["AAP_VIDEO_PEER_MINIO_HOST"]
MINIO_PORT = int(os.environ["AAP_VIDEO_PEER_MINIO_PORT"])
CALLS = []
CALLS_LOCK = Lock()
GATE_LOCK = Lock()
GATE_RELEASE = Event()
GATE_AFTER_CALLS = int(os.environ.get("AAP_VIDEO_PEER_GATE_AFTER_CALLS", "0"))
GATE_TIMEOUT_SECONDS = float(
    os.environ.get("AAP_VIDEO_PEER_GATE_TIMEOUT_SECONDS", "20")
)
EXPECTED_WIDTH = int(os.environ.get("AAP_VIDEO_PEER_EXPECTED_WIDTH", "160"))
EXPECTED_HEIGHT = int(os.environ.get("AAP_VIDEO_PEER_EXPECTED_HEIGHT", "90"))
GATE = {
    "enabled": GATE_AFTER_CALLS > 0,
    "entered": False,
    "task": None,
    "frame_index": None,
    "release_reason": None,
}
if GATE_AFTER_CALLS < 0 or not (0 < GATE_TIMEOUT_SECONDS <= 20):
    raise SystemExit("invalid acceptance gate configuration")
MAX_BODY = 2 * 1024 * 1024
MAX_FRAME = 4 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if self.path == "/__acceptance/gate-state":
            if not GATE["enabled"]:
                self._send(404, {"error": "not_found"})
                return
            with CALLS_LOCK, GATE_LOCK:
                self._send(
                    200,
                    {
                        "enabled": True,
                        "entered": GATE["entered"],
                        "task": GATE["task"],
                        "frame_index": GATE["frame_index"],
                        "release_reason": GATE["release_reason"],
                        "after_calls": len(CALLS),
                    },
                )
            return
        if self.path == "/__acceptance/calls":
            with CALLS_LOCK:
                self._send(200, {"calls": list(CALLS)})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path == "/__acceptance/release":
            if not GATE["enabled"]:
                self._send(404, {"error": "not_found"})
                return
            with GATE_LOCK:
                if GATE["entered"] and GATE["release_reason"] is None:
                    GATE["release_reason"] = "released"
                    GATE_RELEASE.set()
            self._send(200, {"released": GATE_RELEASE.is_set()})
            return
        if self.path != "/predict":
            self._send(404, {"error": "not_found"})
            return
        try:
            request_started = time.monotonic()
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > MAX_BODY:
                raise ValueError("request_size")
            request = json.loads(self.rfile.read(size))
            tasks = request.get("tasks") if isinstance(request, dict) else None
            if not isinstance(tasks, list) or not tasks or len(tasks) > 32:
                raise ValueError("request_shape")
            results = []
            observations = []
            for task in tasks:
                fetch_decode_started = time.monotonic()
                if not isinstance(task, dict) or not isinstance(task.get("file_path"), str):
                    raise ValueError("task_shape")
                url = urlsplit(task["file_path"])
                query = parse_qs(url.query)
                if not (
                    url.scheme == "http"
                    and url.hostname == MINIO_HOST
                    and url.port == MINIO_PORT
                    and not url.username
                    and not url.password
                    and ("X-Amz-Signature" in query or "AWSAccessKeyId" in query)
                ):
                    raise ValueError("frame_reference_scope")
                with urlopen(task["file_path"], timeout=10) as response:
                    content = response.read(MAX_FRAME + 1)
                if not content or len(content) > MAX_FRAME:
                    raise ValueError("frame_size")
                with Image.open(BytesIO(content)) as image:
                    image.verify()
                with Image.open(BytesIO(content)) as image:
                    image.load()
                    if image.format != "JPEG" or image.size != (
                        EXPECTED_WIDTH,
                        EXPECTED_HEIGHT,
                    ):
                        raise ValueError("frame_format")
                    pixel = image.convert("RGB").getpixel(
                        (EXPECTED_WIDTH // 2, EXPECTED_HEIGHT // 2)
                    )
                if pixel[0] < 200 or pixel[1] > 40 or pixel[2] > 40:
                    raise ValueError("frame_pixels")
                task_id = task.get("id")
                if not isinstance(task_id, (str, int)):
                    raise ValueError("task_id")
                observations.append(
                    {
                        "task": str(task_id),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "byte_size": len(content),
                        "width": EXPECTED_WIDTH,
                        "height": EXPECTED_HEIGHT,
                        "pixel": list(pixel),
                        "fetch_decode_ms": round(
                            (time.monotonic() - fetch_decode_started) * 1000, 3
                        ),
                    }
                )
                results.append(
                    {
                        "task": task_id,
                        "result": [
                            {
                                "type": "rectanglelabels",
                                "value": {
                                    "x": 25,
                                    "y": 25,
                                    "width": 50,
                                    "height": 50,
                                    "rectanglelabels": ["car"],
                                },
                            }
                        ],
                        "score": 0.9,
                        "model_version": "video-container-echo-v1",
                        "inference_time_ms": 1,
                    }
                )
            gate_task = observations[0]["task"] if observations else None
            should_gate = False
            with CALLS_LOCK:
                if GATE["enabled"] and len(CALLS) >= GATE_AFTER_CALLS:
                    with GATE_LOCK:
                        if not GATE["entered"]:
                            GATE["entered"] = True
                            GATE["task"] = gate_task
                            try:
                                GATE["frame_index"] = int(gate_task.rsplit(":", 1)[1])
                            except (AttributeError, TypeError, ValueError):
                                GATE["frame_index"] = None
                            should_gate = True
            service_ms = round((time.monotonic() - request_started) * 1000, 3)
            gate_wait_ms = 0.0
            if should_gate:
                gate_started = time.monotonic()
                if not GATE_RELEASE.wait(GATE_TIMEOUT_SECONDS):
                    with GATE_LOCK:
                        GATE["release_reason"] = "timeout"
                        GATE_RELEASE.set()
                gate_wait_ms = round((time.monotonic() - gate_started) * 1000, 3)
            for observation in observations:
                observation["peer_service_ms"] = service_ms
                observation["gate_wait_ms"] = gate_wait_ms
            with CALLS_LOCK:
                CALLS.extend(observations)
            self._send(200, {"results": results})
        except Exception as error:
            self._send(502, {"error": type(error).__name__})


signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
ThreadingHTTPServer(
    ("0.0.0.0", int(os.environ.get("AAP_VIDEO_PEER_PORT", "9100"))), Handler
).serve_forever()
"""
