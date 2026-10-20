"""Owned test-process helpers for the single owner-transfer capability."""

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


def transfer_dsn(test_engine, database=None):
    value = make_url(
        _role_url(
            "AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL",
            "workspace_owner_transfer_client",
        )
    )
    assert (value.host, value.port, value.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert value.database.endswith("_test")
    _verify_postgres(value)
    if database is not None:
        assert database.startswith(
            "aap_workspace_owner_transfer_restore_"
        ) and database.endswith("_test")
        value = value.set(database=database)
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def api_transfer_binding(*, url=None, dsn=None, token):
    assert not (url and dsn)
    changes = {
        "workspace_owner_transfer_url": url or "",
        "workspace_owner_transfer_database_url": dsn or "",
        "workspace_owner_transfer_database_url_file": "",
        "workspace_membership_control_token": token,
        "workspace_membership_control_token_file": "",
        "workspace_membership_timeout_seconds": 15,
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
async def _provider_url(
    dsn, token, *, node, drop_operation_id=None, membership_dsn=None
):
    parsed = make_url(dsn)
    assert parsed.username == "workspace_owner_transfer_client"
    assert parsed.database.endswith("_test")
    _verify_postgres(parsed)
    with tempfile.TemporaryDirectory(
        prefix="owner-transfer-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        dsn_file, token_file = directory / "database", directory / "control"
        for path, value in ((dsn_file, dsn), (token_file, token)):
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
        port = _port()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "WORKSPACE_HOST": "127.0.0.1",
            "WORKSPACE_PORT": str(port),
            "WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE": str(dsn_file),
            "WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE": str(token_file),
        }
        if membership_dsn is not None:
            assert node is False
            membership = make_url(membership_dsn)
            assert membership.username == "workspace_membership_client"
            assert (membership.host, membership.port, membership.database) == (
                parsed.host,
                parsed.port,
                parsed.database,
            )
            member_file = directory / "membership-database"
            member_file.write_text(membership_dsn, encoding="utf-8")
            member_file.chmod(0o600)
            environment["WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE"] = str(member_file)
        if node:
            command = [
                "node",
                str(ROOT / "apps/workspace/conformance/node/provider.mjs"),
            ]
            if drop_operation_id:
                environment["AAP_WORKSPACE_OWNER_TRANSFER_DROP_OPERATION_ID"] = (
                    drop_operation_id
                )
        else:
            assert drop_operation_id is None
            interpreter = Path(os.environ["AAP_WORKSPACE_RUNTIME_PYTHON"])
            assert interpreter.is_file()
            command = [str(interpreter), "-I", "-m", "aap_workspace"]
        owner = await asyncio.to_thread(
            _start,
            command,
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


def node_provider_url(dsn, token, *, drop_operation_id=None):
    return _provider_url(dsn, token, node=True, drop_operation_id=drop_operation_id)


def python_provider_url(dsn, token, *, membership_dsn=None):
    return _provider_url(dsn, token, node=False, membership_dsn=membership_dsn)
