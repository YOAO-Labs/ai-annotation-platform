"""Cold installed API keeps unset Identity selectors working under closed ACLs."""

from __future__ import annotations

import asyncio
import inspect
import os
from urllib.parse import urlsplit

import pytest

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_identity_embedded_acl import (
    embedded as _embedded,
    exercise_embedded_http,
    seeded_embedded_accounts,
)
from tests.workspace_import_cli_helpers import WorkspaceImportCli

embedded = _embedded
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or not os.getenv("AAP_WORKSPACE_MODULE_API_IMAGE"),
        reason="requires the owned verifier and qualified installed API image",
    ),
]


async def test_installed_api_keeps_embedded_bindings_after_private_acl_closure(
    test_engine, embedded, tmp_path, record_property
):
    _engine, _apply = embedded
    resources, _bucket, _s3 = await assert_workspace_identity(test_engine)
    endpoint = urlsplit(endpoint_url())
    assert endpoint.hostname and endpoint.port
    source = tmp_path / "embedded-identity-probe"
    source.mkdir(mode=0o700)
    probe = source / "probe.py"
    probe.write_text(
        _PROBE_HEAD + inspect.getsource(exercise_embedded_http) + _PROBE_TAIL
    )
    probe.chmod(0o400)
    cli = WorkspaceImportCli(
        test_engine,
        tmp_path,
        source_root=source,
        buckets=resources["buckets"],
        minio_authority=endpoint.netloc,
        minio_use_ssl=endpoint.scheme == "https",
        s3_access_key=os.environ["AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY"],
        s3_secret_key=os.environ["AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY"],
    )
    try:
        await asyncio.to_thread(cli.start)
        async with seeded_embedded_accounts(test_engine) as (actor, target):
            run = await asyncio.to_thread(
                cli.run_command,
                (
                    "python",
                    "/fixture/probe.py",
                    actor.email,
                    str(target.id),
                    target.email,
                ),
            )
            record_property("identity_embedded_acl_cold", run.evidence)
            assert not run.timed_out and run.exit_code == 0, run.evidence
            assert "embedded Identity HTTP passed" in run.stdout, run.evidence
    finally:
        await asyncio.to_thread(cli.close)


_PROBE_HEAD = """import asyncio, os, socket, sys, threading, time
from types import SimpleNamespace
import httpx, uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import settings
from app.schemas.user import UserOut
from app.main import app

DEFAULT_PASSWORD = "Test1234"
assert not settings.identity_url
assert not settings.e2e_seed_enabled
for name in type(settings).model_fields:
    if name.startswith("identity_") and name.endswith(("database_url", "database_url_file")):
        assert not getattr(settings, name), name

async def assert_storage_denied():
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert not await db.scalar(text("SELECT has_table_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,DELETE')"))
            assert not await db.scalar(text("SELECT has_any_column_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,REFERENCES')"))
    finally:
        await engine.dispose()

asyncio.run(assert_storage_denied())
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
deadline = time.monotonic() + 30
while not server.started and thread.is_alive() and time.monotonic() < deadline:
    time.sleep(0.03)
assert server.started, "installed API failed full lifespan startup"

"""

_PROBE_TAIL = """
async def main():
    actor = SimpleNamespace(email=sys.argv[1])
    target = SimpleNamespace(id=sys.argv[2], email=sys.argv[3])
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=90) as client:
        await exercise_embedded_http(client, actor, target)
try:
    asyncio.run(main())
    print("embedded Identity HTTP passed", flush=True)
finally:
    server.should_exit = True
    thread.join(30)
    assert not thread.is_alive(), "installed API failed lifespan shutdown"
"""
