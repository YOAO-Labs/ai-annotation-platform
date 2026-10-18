"""Small loopback-only health endpoint using the Python standard library."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


class HealthState:
    def __init__(self, *, poll_seconds: float):
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._last_heartbeat = time.monotonic()
        self._ready = False
        self._stopping = False
        self._completed = 0
        self._failed = 0

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def set_ready(self, ready: bool) -> None:
        with self._lock:
            self._ready = ready
            self._last_heartbeat = time.monotonic()

    def add_result(self, *, completed: int, failed: int) -> None:
        with self._lock:
            self._completed += completed
            self._failed += failed
            self._last_heartbeat = time.monotonic()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True

    def status(self) -> dict[str, int | bool]:
        with self._lock:
            live_window = max(60.0, self._poll_seconds * 3)
            live = not self._stopping and (
                time.monotonic() - self._last_heartbeat <= live_window
            )
            return {
                "live": live,
                "ready": live and self._ready,
                "completed": self._completed,
                "failed": self._failed,
            }


class _HealthServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_health_server(state: HealthState, host: str, port: int) -> _HealthServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = state.status()
            if self.path == "/health/live":
                code, body = (200, {"live": True}) if status["live"] else (503, status)
            elif self.path == "/health/ready":
                code, body = (200, status) if status["ready"] else (503, status)
            else:
                code, body = 404, {"error": "not_found"}
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _HealthServer((host, port), Handler)
    threading.Thread(
        target=server.serve_forever,
        name="assets-cleanup-health",
        daemon=True,
    ).start()
    return server
