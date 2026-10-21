"""Start only an owned Registry provider using a disposable function-only DSN."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import sys
import tempfile

from sqlalchemy.engine import make_url

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url

ROOT = Path(__file__).resolve().parents[3]


def ingress_dsn(database_url):
    target = make_url(database_url)
    value = make_url(
        _role_url(
            "AAP_MODULE_DATA_INFERENCE_REGISTRY_INGRESS_URL",
            "inference_registry_ingress_client",
        )
    )
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    assert (value.host, value.port, value.database) == (
        target.host,
        target.port,
        target.database,
    )
    assert target.database.endswith("_test")
    _verify_postgres(value)
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def ingress_provider(database_url):
    dsn = ingress_dsn(database_url)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(
        prefix="inference-registry-ingress-", dir=ROOT / ".worktree"
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
            "INFERENCE_REGISTRY_INGRESS_DATABASE_URL_FILE": str(dsn_file),
            "INFERENCE_REGISTRY_INGRESS_TOKEN_FILE": str(token_file),
        }
        owner = None
        try:
            owner = await asyncio.to_thread(
                _start,
                [sys.executable, "-m", "aap_inference"],
                environment,
                directory,
                directory / "inference-registry-ingress.log",
                port,
                "/health",
            )
            yield owner.origin, token
        finally:
            if owner is not None:
                owner.stop()


async def select_binding(stack, monkeypatch, test_engine, binding):
    from app.config import settings

    for name in ("url", "database_url", "database_url_file", "token", "token_file"):
        monkeypatch.setattr(settings, "inference_registry_ingress_" + name, "")
    if binding == "dsn":
        monkeypatch.setattr(
            settings,
            "inference_registry_ingress_database_url",
            ingress_dsn(test_engine.url),
        )
    elif binding == "http":
        origin, token = await stack.enter_async_context(
            ingress_provider(test_engine.url)
        )
        monkeypatch.setattr(settings, "inference_registry_ingress_url", origin)
        monkeypatch.setattr(settings, "inference_registry_ingress_token", token)
