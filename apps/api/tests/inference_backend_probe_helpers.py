"""Owned credential-probe process helpers; never use primary database credentials."""

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


def probe_dsn(database_url):
    target = make_url(database_url)
    value = make_url(
        _role_url(
            "AAP_MODULE_DATA_INFERENCE_BACKEND_PROBE_URL",
            "inference_backend_probe_client",
        )
    )
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    assert (value.host, value.port) == (target.host, target.port)
    assert value.database and value.database.endswith("_test")
    assert target.database == value.database or (
        target.database.startswith("aap_workspace_configuration_restore_")
        and target.database.endswith("_test")
    )
    value = value.set(database=target.database, drivername="postgresql")
    _verify_postgres(value)
    return value.render_as_string(hide_password=False)


@asynccontextmanager
async def probe_provider(database_url):
    dsn = probe_dsn(database_url)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(
        prefix="inference-backend-probe-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        dsn_file, token_file = directory / "probe-database", directory / "probe-token"
        for path, value in ((dsn_file, dsn), (token_file, token)):
            path.touch(mode=0o600)
            path.write_text(value, encoding="utf-8")
        port = _port()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "INFERENCE_HOST": "127.0.0.1",
            "INFERENCE_PORT": str(port),
            "INFERENCE_BACKEND_PROBE_DATABASE_URL_FILE": str(dsn_file),
            "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN_FILE": str(token_file),
        }
        owner = None
        try:
            owner = await asyncio.to_thread(
                _start,
                [sys.executable, "-m", "aap_inference"],
                environment,
                directory,
                directory / "inference-probe.log",
                port,
                "/health",
            )
            yield owner.origin, token
        finally:
            if owner is not None:
                owner.stop()
