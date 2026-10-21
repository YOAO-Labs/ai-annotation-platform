"""Owned Project-query credentials, selectors and installed runtime processes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile

from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url

ROOT = Path(__file__).resolve().parents[3]


def _dsn(test_engine, variable, role, database=None):
    parsed = make_url(_role_url(variable, role))
    assert parsed.username == role
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)
    if database is not None:
        assert database.endswith("_test")
        parsed = parsed.set(database=database)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def project_query_dsn(test_engine, database=None):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL",
        "workspace_project_query_client",
        database,
    )


def project_query_briefs_dsn(test_engine, database=None):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_IDENTITY_BRIEFS_URL",
        "identity_briefs_client",
        database,
    )


@asynccontextmanager
async def api_project_query_binding(*, url=None, dsn=None, token, timeout=20):
    if url and dsn:
        raise ValueError("Select at most one independent Project-query provider")
    changes = {
        "workspace_project_query_url": url or "",
        "workspace_project_query_database_url": dsn or "",
        "workspace_project_query_database_url_file": "",
        "workspace_project_query_timeout_seconds": timeout,
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


@asynccontextmanager
async def project_query_provider(dsn, token, *, briefs_dsn=None):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    parsed = make_url(dsn)
    assert parsed.username == "workspace_project_query_client"
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)
    interpreter = Path(os.environ["AAP_WORKSPACE_RUNTIME_PYTHON"])
    assert interpreter.is_file()
    with tempfile.TemporaryDirectory(
        prefix="workspace-project-query-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        port = _port()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "WORKSPACE_HOST": "127.0.0.1",
            "WORKSPACE_PORT": str(port),
            "WORKSPACE_TIMEOUT_SECONDS": "15",
        }
        secrets = [
            ("WORKSPACE_PROJECT_QUERY_DATABASE_URL_FILE", "query-database", dsn),
            ("WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE", "control-token", token),
        ]
        if briefs_dsn is not None:
            briefs = make_url(briefs_dsn)
            assert briefs.username == "identity_briefs_client"
            assert (briefs.host, briefs.port, briefs.database) == (
                parsed.host,
                parsed.port,
                parsed.database,
            )
            secrets.append(
                (
                    "WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL_FILE",
                    "briefs-database",
                    briefs_dsn,
                )
            )
        for variable, filename, value in secrets:
            path = directory / filename
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
            environment[variable] = str(path)
        owner = await asyncio.to_thread(
            _start,
            [str(interpreter), "-I", "-m", "aap_workspace"],
            environment,
            directory,
            directory / "owner.log",
            port,
            "/ready",
        )
        try:
            yield owner.origin
        finally:
            owner.stop()
