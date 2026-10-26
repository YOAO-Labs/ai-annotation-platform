"""Shared controlled ML backend server for Registry ingress acceptance tests."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


@contextmanager
def _server(case, trace):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            trace.append("http:" + path)
            status = 200
            if path == "/health":
                if case == "live_failed":
                    status = 503
                body = (
                    b"ready"
                    if case == "live_plain_health"
                    else json.dumps(
                        {
                            "compute": {"configured_device": "cpu"},
                            **(
                                {"host": {"n": float("nan")}}
                                if case == "live_nan"
                                else {}
                            ),
                        }
                    ).encode()
                )
            else:
                if case in {"new_failed", "existing_failed", "live_setup_failed"}:
                    status = 503
                setup = {
                    "name": "mock-v2-backend"
                    if case.startswith("stub_")
                    else "Controlled",
                    "supported_prompts": ["bbox"],
                    "model_version": "one",
                }
                if case in {"invalid_setup", "manual_invalid_setup"}:
                    setup = ["bad"]
                if case == "bad_name":
                    setup["name"] = {"bad": "name"}
                if case == "live_empty_setup":
                    setup = None
                body = json.dumps(setup).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
