"""Processes and credentials confined to the owned health-observation test DB."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading

from sqlalchemy.engine import make_url

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url

ROOT = Path(__file__).resolve().parents[3]

_SELECTION_SETTINGS = (
    "inference_health_observation_url",
    "inference_health_observation_token",
    "inference_health_observation_token_file",
)


def observation_dsn(database_url):
    target = make_url(database_url)
    value = make_url(
        _role_url(
            "AAP_MODULE_DATA_INFERENCE_HEALTH_OBSERVATION_URL",
            "inference_health_observation_client",
        )
    )
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    assert (value.host, value.port) == (target.host, target.port)
    assert value.database and value.database.endswith("_test")
    assert target.database == value.database or (
        target.database.startswith("aap_health_observation_restore_")
        and target.database.endswith("_test")
    )
    value = value.set(database=target.database, drivername="postgresql")
    _verify_postgres(value)
    return value.render_as_string(hide_password=False)


@asynccontextmanager
async def observation_provider(database_url):
    dsn = observation_dsn(database_url)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(
        prefix="inference-health-observation-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        dsn_file, token_file = directory / "database", directory / "token"
        for path, value in ((dsn_file, dsn), (token_file, token)):
            path.touch(mode=0o600)
            path.write_text(value, encoding="utf-8")
        port = _port()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "INFERENCE_HOST": "127.0.0.1",
            "INFERENCE_PORT": str(port),
            "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL_FILE": str(dsn_file),
            "INFERENCE_HEALTH_OBSERVATION_TOKEN_FILE": str(token_file),
        }
        owner = None
        try:
            owner = await asyncio.to_thread(
                _start,
                [sys.executable, "-m", "aap_inference"],
                environment,
                directory,
                directory / "inference-health-observation.log",
                port,
                "/health",
            )
            yield owner.origin, token
        finally:
            if owner is not None:
                owner.stop()


def clear_observation_selection(monkeypatch):
    """Remove every legacy selector so a test proves the HTTP-only boundary."""
    from app.config import settings

    for name in _SELECTION_SETTINGS:
        monkeypatch.setattr(settings, name, "")


async def select_binding(stack, monkeypatch, test_engine, binding="http"):
    """Bind the only remaining observation selector: the HTTP owner process."""
    if binding != "http":
        raise ValueError("health observation is HTTP-only")
    from app.config import settings

    clear_observation_selection(monkeypatch)
    origin, token = await stack.enter_async_context(
        observation_provider(test_engine.url)
    )
    monkeypatch.setattr(settings, "inference_health_observation_url", origin)
    monkeypatch.setattr(settings, "inference_health_observation_token", token)


@contextmanager
def backend_server(
    *,
    setup_failed=False,
    plain=False,
    nonfinite=False,
    null_health=False,
    on_health=None,
):
    """Local Inference backend used only through the sealed observation owner."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            setup = self.path.endswith("/setup")
            if not setup and on_health is not None:
                on_health()
            body = (
                b"null"
                if null_health and not setup
                else b"ready"
                if plain and not setup
                else json.dumps(
                    {"supported_prompts": ["bbox"]}
                    if setup
                    else {
                        "compute": {"configured_device": "cpu"},
                        **({"host": {"value": float("nan")}} if nonfinite else {}),
                    }
                ).encode()
            )
            self.send_response(503 if setup and setup_failed else 200)
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
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
