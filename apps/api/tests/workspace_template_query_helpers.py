"""Owned process and API selectors for template-query acceptance."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
import tempfile

from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url

ROOT = Path(__file__).resolve().parents[3]


def template_query_dsn(test_engine, database=None):
    parsed = make_url(
        _role_url(
            "AAP_MODULE_DATA_WORKSPACE_TEMPLATE_QUERY_URL",
            "workspace_template_query_client",
        )
    )
    assert parsed.username == "workspace_template_query_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert parsed.database and parsed.database.endswith("_test")
    if database is not None:
        assert database.endswith("_test")
        parsed = parsed.set(database=database)
    _verify_postgres(parsed)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def api_template_query_binding(*, url=None, dsn=None, token, timeout=20):
    if bool(url) == bool(dsn):
        raise ValueError("Select exactly one Workspace template-query reader")
    changes = {
        "workspace_template_query_url": url or "",
        "workspace_template_query_database_url": dsn or "",
        "workspace_template_query_database_url_file": "",
        "workspace_template_query_timeout_seconds": timeout,
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
async def template_query_provider(dsn, token, *, configuration_dsn=None):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    parsed = make_url(dsn)
    assert parsed.username == "workspace_template_query_client"
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)
    with tempfile.TemporaryDirectory(
        prefix="workspace-template-query-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        dsn_file = directory / "query-database"
        token_file = directory / "membership-control"
        for path, value in ((dsn_file, dsn), (token_file, token)):
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
        port = _port()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "WORKSPACE_HOST": "127.0.0.1",
            "WORKSPACE_PORT": str(port),
            "WORKSPACE_TEMPLATE_QUERY_DATABASE_URL_FILE": str(dsn_file),
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": str(token_file),
            "WORKSPACE_TIMEOUT_SECONDS": "15",
        }
        if configuration_dsn is not None:
            parsed_configuration = make_url(configuration_dsn)
            assert parsed_configuration.username == "workspace_configuration_client"
            assert (
                parsed_configuration.host,
                parsed_configuration.port,
                parsed_configuration.database,
            ) == (parsed.host, parsed.port, parsed.database)
            configuration_file = directory / "configuration-database"
            configuration_file.write_text(configuration_dsn, encoding="utf-8")
            configuration_file.chmod(0o600)
            environment["WORKSPACE_CONFIGURATION_DATABASE_URL_FILE"] = str(
                configuration_file
            )
        owner = None
        try:
            owner = await asyncio.to_thread(
                _start,
                [sys.executable, "-m", "aap_workspace"],
                environment,
                directory,
                directory / "workspace-query.log",
                port,
                "/ready",
            )
            yield owner.origin
        finally:
            if owner is not None:
                owner.stop()
