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
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from PIL import Image

MINIO_HOST = os.environ["AAP_VIDEO_PEER_MINIO_HOST"]
MINIO_PORT = int(os.environ["AAP_VIDEO_PEER_MINIO_PORT"])
CALLS = []
CALLS_LOCK = Lock()
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
        if self.path == "/__acceptance/calls":
            with CALLS_LOCK:
                self._send(200, {"calls": list(CALLS)})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/predict":
            self._send(404, {"error": "not_found"})
            return
        try:
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
                    if image.format != "JPEG" or image.size != (160, 90):
                        raise ValueError("frame_format")
                    pixel = image.convert("RGB").getpixel((80, 45))
                if pixel[0] < 200 or pixel[1] > 40 or pixel[2] > 40:
                    raise ValueError("frame_pixels")
                task_id = task.get("id")
                if not isinstance(task_id, (str, int)):
                    raise ValueError("task_id")
                observations.append({
                    "task": str(task_id),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "byte_size": len(content),
                    "width": 160,
                    "height": 90,
                    "pixel": list(pixel),
                })
                results.append({
                    "task": task_id,
                    "result": [{
                        "type": "rectanglelabels",
                        "value": {
                            "x": 25,
                            "y": 25,
                            "width": 50,
                            "height": 50,
                            "rectanglelabels": ["car"],
                        },
                    }],
                    "score": 0.9,
                    "model_version": "video-container-echo-v1",
                    "inference_time_ms": 1,
                })
            with CALLS_LOCK:
                CALLS.extend(observations)
            self._send(200, {"results": results})
        except Exception as error:
            self._send(502, {"error": type(error).__name__})


ThreadingHTTPServer(
    ("0.0.0.0", int(os.environ.get("AAP_VIDEO_PEER_PORT", "9100"))), Handler
).serve_forever()
"""
