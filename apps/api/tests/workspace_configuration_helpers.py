"""Owned provider and API bindings for Workspace configuration acceptance."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading

from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url
from tests.inference_backend_probe_helpers import probe_provider

ROOT = Path(__file__).resolve().parents[3]


def configuration_dsn(test_engine, database: str | None = None) -> str:
    value = make_url(
        _role_url(
            "AAP_MODULE_DATA_WORKSPACE_CONFIGURATION_URL",
            "workspace_configuration_client",
        )
    )
    assert value.username == "workspace_configuration_client"
    assert (value.host, value.port, value.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert value.database and value.database.endswith("_test")
    _verify_postgres(value)
    if database is not None:
        assert database.startswith(
            "aap_workspace_configuration_restore_"
        ) and database.endswith("_test")
        value = value.set(database=database)
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def api_configuration_binding(*, url=None, dsn=None, token, timeout=15):
    if bool(url) == bool(dsn):
        raise ValueError("Select exactly one Workspace configuration owner")
    changes = {
        "workspace_configuration_url": url or "",
        "workspace_configuration_database_url": dsn or "",
        "workspace_configuration_database_url_file": "",
        "workspace_configuration_timeout_seconds": timeout,
        "workspace_membership_control_token": token,
        "workspace_membership_control_token_file": "",
    }
    original = {key: getattr(settings, key) for key in changes}
    try:
        for key, value in changes.items():
            setattr(settings, key, value)
        yield
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


class _DropOnceRelay:
    """Test-only HTTP relay that loses one committed command response."""

    def __init__(self, origin: str, operation_id: str):
        self.origin = origin.rstrip("/")
        self.operation_id = operation_id
        self._lock = threading.Lock()
        self.dropped_count = 0
        self.server = None
        self.thread = None
        self.url = None

    def start(self) -> str:
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                headers = {
                    key: self.headers[key]
                    for key in ("authorization", "content-type")
                    if self.headers.get(key) is not None
                }
                try:
                    with httpx.Client(trust_env=False, timeout=20) as client:
                        response = client.post(
                            relay.origin + self.path,
                            content=body,
                            headers=headers,
                        )
                except httpx.HTTPError:
                    self.send_error(502)
                    return

                drop = False
                try:
                    reply = json.loads(response.content)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    reply = None
                if (
                    response.status_code == 200
                    and isinstance(reply, dict)
                    and reply.get("ok") is True
                    and reply.get("operation_id") == relay.operation_id
                ):
                    with relay._lock:
                        if relay.dropped_count == 0:
                            relay.dropped_count = 1
                            drop = True
                if drop:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return

                self.send_response(response.status_code)
                self.send_header(
                    "content-type",
                    response.headers.get("content-type", "application/json"),
                )
                self.send_header("content-length", str(len(response.content)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(response.content)
                self.close_connection = True

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self.url

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        assert self.dropped_count == 1, (
            "The owned relay did not drop the committed reply"
        )


@asynccontextmanager
async def provider_url(
    dsn,
    token,
    *,
    video_segment_size_frames=18_000,
    setup_timeout=10,
    drop_operation_id=None,
):
    parsed = make_url(dsn)
    assert parsed.username == "workspace_configuration_client"
    assert parsed.database and parsed.database.endswith("_test")
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    _verify_postgres(parsed)
    async with probe_provider(dsn) as (probe_url, probe_token):
        with tempfile.TemporaryDirectory(
            prefix="workspace-configuration-", dir=ROOT / ".worktree"
        ) as temporary:
            directory = Path(temporary)
            dsn_file = directory / "configuration-database"
            token_file = directory / "membership-control"
            for path, value in ((dsn_file, dsn), (token_file, token)):
                path.write_text(value, encoding="utf-8")
                path.chmod(0o600)
            probe_token_file = directory / "inference-probe-control"
            probe_token_file.touch(mode=0o600)
            probe_token_file.write_text(probe_token, encoding="utf-8")
            port = _port()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "WORKSPACE_HOST": "127.0.0.1",
                "WORKSPACE_PORT": str(port),
                "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE": str(dsn_file),
                "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": str(token_file),
                "WORKSPACE_CONFIGURATION_VIDEO_SEGMENT_SIZE_FRAMES": str(
                    video_segment_size_frames
                ),
                "WORKSPACE_CONFIGURATION_VIDEO_TRACKER_WINDOW_SIZE_FRAMES": "300",
                "WORKSPACE_CONFIGURATION_VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES": "16",
                "WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS": str(setup_timeout),
                "WORKSPACE_TIMEOUT_SECONDS": "15",
                "WORKSPACE_INFERENCE_PROBE_URL": probe_url,
                "WORKSPACE_INFERENCE_PROBE_TOKEN_FILE": str(probe_token_file),
            }
            assert not {
                "WORKSPACE_MEMBERSHIP_DATABASE_URL",
                "WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE",
                "WORKSPACE_OWNER_TRANSFER_DATABASE_URL",
                "WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE",
            }.intersection(environment)
            owner = None
            relay = None
            try:
                owner = await asyncio.to_thread(
                    _start,
                    [sys.executable, "-m", "aap_workspace"],
                    environment,
                    directory,
                    directory / "workspace-owner.log",
                    port,
                    "/ready",
                )
                origin = owner.origin
                if drop_operation_id is not None:
                    relay = _DropOnceRelay(origin, str(drop_operation_id))
                    origin = relay.start()
                yield origin
            finally:
                if relay is not None:
                    try:
                        relay.stop()
                    finally:
                        if owner is not None:
                            owner.stop()
                elif owner is not None:
                    owner.stop()
