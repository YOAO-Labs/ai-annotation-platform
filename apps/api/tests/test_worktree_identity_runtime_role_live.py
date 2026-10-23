"""Installed API uses the generated worktree LOGIN with owner selectors absent."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import os
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_identity_embedded_acl import (
    exercise_embedded_http,
    seeded_embedded_accounts,
)
from tests.test_identity_embedded_acl_live import _PROBE_HEAD, _PROBE_TAIL
from tests.test_identity_e2e_replay import ROOT
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or not os.getenv("AAP_WORKSPACE_MODULE_API_IMAGE"),
        reason="requires the owned verifier and qualified installed API image",
    ),
]


@asynccontextmanager
async def generated_parent_runtime(test_engine, resources, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from worktree_env import application_environment, state_path
    from worktree_resources import LocalResources

    owner_url = test_engine.url.render_as_string(hide_password=False)
    backend = LocalResources(
        resources,
        settings.model_copy(
            update={"database_url": owner_url, "migration_database_url": owner_url}
        ),
        code_root=ROOT,
    )
    credentials = state_path(
        backend.root, resources["mode"], "runtime-credentials.json"
    )
    assert not credentials.exists()
    try:
        role = await backend.owned_runtime_role("create")
        assert role["owner"] == resources["owner"] and role["safe"]
        await backend.grant_runtime()
        assert await backend.verify_runtime_login()
        child = application_environment(backend._environment(6391))
        assert (
            make_url(child["DATABASE_URL"]).username == resources["database"] + "_app"
        )
        assert not any(
            child[name]
            for name in (
                "MIGRATION_DATABASE_URL",
                "TEST_DATABASE_URL",
                "PLAYWRIGHT_E2E_DATABASE_URL",
            )
        )
        yield backend
    finally:
        role = await backend.owned_runtime_role()
        if role is not None:
            await backend.owned_runtime_role("check_drop")
            async with test_engine.begin() as db:
                # This fixture attaches only new grants to an existing disposable
                # verifier DB; prove it owns no data before removing those ACLs.
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=:role))"
                    ),
                    {"role": role["name"]},
                )
                quoted = test_engine.dialect.identifier_preparer.quote_identifier(
                    role["name"]
                )
                await db.execute(text(f"DROP OWNED BY {quoted}"))
            assert await backend.owned_runtime_role("drop") is None
        credentials.unlink(missing_ok=True)


async def test_installed_api_uses_generated_runtime_without_owner_fallback(
    test_engine, tmp_path, monkeypatch, record_property
):
    resources, _bucket, _s3 = await assert_workspace_identity(test_engine)
    endpoint = urlsplit(endpoint_url())
    assert endpoint.hostname and endpoint.port
    source = tmp_path / "generated-runtime-probe"
    source.mkdir(mode=0o700)
    probe = source / "probe.py"
    probe.write_text(
        _PROBE_HEAD.replace('== "legacy_runtime"', "== sys.argv[4]")
        + inspect.getsource(exercise_embedded_http)
        + _PROBE_TAIL
    )
    probe.chmod(0o400)
    async with generated_parent_runtime(test_engine, resources, monkeypatch) as backend:
        cli = WorkspaceImportCli(
            test_engine,
            tmp_path,
            source_root=source,
            buckets=resources["buckets"],
            minio_authority=endpoint.netloc,
            minio_use_ssl=endpoint.scheme == "https",
            s3_access_key=os.environ["AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY"],
            s3_secret_key=os.environ["AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY"],
            runtime_database_url=backend.runtime_url(),
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
                        backend.runtime_url().username,
                    ),
                )
                record_property("generated_runtime_identity_cold", run.evidence)
                assert not run.timed_out and run.exit_code == 0, run.evidence
                assert "embedded Identity HTTP passed" in run.stdout, run.evidence
        finally:
            await asyncio.to_thread(cli.close)
