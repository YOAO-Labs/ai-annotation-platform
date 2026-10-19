"""Bounded HTTP model peer that consumes the retained original video bytes."""

PROGRAM = r"""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import sys
from threading import Event, Lock
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

ORIGIN = urlsplit(os.environ["AAP_TRACKER_PEER_STORAGE_ORIGIN"])
EXPECTED_SHA = os.environ["AAP_TRACKER_PEER_SOURCE_SHA256"]
CALLS = []
LOCK = Lock()
GATE_AFTER_CALLS = int(os.environ.get("AAP_TRACKER_PEER_GATE_AFTER_CALLS", "0"))
GATE_RELEASE = Event()
GATE = {"entered": False, "released": False}
MAX_REQUEST = 2 * 1024 * 1024
MAX_VIDEO = 16 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/health", "/health/ready"}:
            self.send_json(200, {"status": "ok"})
        elif self.path == "/__acceptance/calls":
            with LOCK:
                self.send_json(200, {"calls": list(CALLS), "gate": dict(GATE)})
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path == "/__acceptance/release":
            with LOCK:
                GATE["released"] = True
                GATE_RELEASE.set()
            self.send_json(200, {"released": True})
            return
        if self.path != "/predict":
            self.send_json(404, {"error": "not_found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST:
                raise ValueError("request_size")
            request = json.loads(self.rfile.read(size))
            task, context = request["task"], request["context"]
            source = task["file_path"]
            url = urlsplit(source)
            query = parse_qs(url.query)
            if not (
                url.scheme == ORIGIN.scheme
                and url.netloc == ORIGIN.netloc
                and not url.username
                and not url.password
                and "/accepted-video-inputs/" in url.path
                and url.path.endswith("/source")
                and len(query.get("versionId", [])) == 1
                and query["versionId"][0] not in {"", "null"}
                and ("X-Amz-Signature" in query or "AWSAccessKeyId" in query)
            ):
                raise ValueError("source_reference_scope")
            with urlopen(source, timeout=10) as response:
                body = response.read(MAX_VIDEO + 1)
            digest = sha256(body).hexdigest()
            if not 0 < len(body) <= MAX_VIDEO or digest != EXPECTED_SHA:
                raise ValueError("source_identity")
            first, last = context["from_frame"], context["to_frame"]
            if (
                context["type"] != "video_tracker"
                or type(first) is not int
                or type(last) is not int
                or not 0 <= first <= last < 100
            ):
                raise ValueError("tracker_range")
            with LOCK:
                should_gate = (
                    GATE_AFTER_CALLS > 0
                    and len(CALLS) >= GATE_AFTER_CALLS
                    and not GATE["entered"]
                )
                if should_gate:
                    GATE["entered"] = True
                CALLS.append({
                    "job_id": context["job_id"],
                    "task_id": str(task["id"]),
                    "from_frame": first,
                    "to_frame": last,
                    "source_sha256": digest,
                    "byte_size": len(body),
                    "version_id": query["versionId"][0],
                })
            if should_gate and not GATE_RELEASE.wait(20):
                raise TimeoutError("acceptance_gate")
            frames = range(first, last + 1)
            if context["direction"] == "backward":
                frames = range(last, first - 1, -1)
            geometry = {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
            if context.get("output_geometry") == "mask":
                geometry = {
                    "type": "mask",
                    "rle": {"encoding": "coco_rle", "size": [48, 80], "counts": [0, 3840]},
                    "bbox": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                }
            self.send_json(200, {
                "result": [{
                    "frame_index": frame,
                    "geometry": geometry,
                    "confidence": 0.9,
                } for frame in frames],
                "model_version": "retained-tracker-acceptance",
            })
        except Exception as exc:
            self.send_json(502, {"error": type(exc).__name__})


signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
ThreadingHTTPServer(("127.0.0.1", int(os.environ["AAP_TRACKER_PEER_PORT"])), Handler).serve_forever()
"""
