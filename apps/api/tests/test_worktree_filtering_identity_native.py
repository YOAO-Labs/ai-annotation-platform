"""Actual managed filtering CLI under its generated ordinary E2E LOGIN."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.factory import create_user
from tests.test_workspace_consumer_profile_native import _PRIVATE
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_worktree_identity_runtime_role_native import (
    ROOT,
    _assert_private_identity_denied,
    _backend,
    _credentials_path,
)
from worktree_env import application_environment, state_path


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable PostgreSQL verifier",
    ),
]


def _run_cli(backend, source):
    scripts = backend.root / "scripts"
    scripts.mkdir(exist_ok=True)
    apps = backend.root / "apps"
    apps.mkdir(exist_ok=True)
    api = apps / "api"
    if not api.exists():
        api.symlink_to(ROOT / "apps/api", target_is_directory=True)
    assert api.resolve() == (ROOT / "apps/api").resolve()
    for original, target in (
        (source, scripts / "worktree_filtering.py"),
        (ROOT / "scripts/worktree_env.py", scripts / "worktree_env.py"),
    ):
        shutil.copyfile(original, target)
        assert target.read_bytes() == original.read_bytes()
    redis = backend.redis_info()
    assert redis
    environment = application_environment(backend._environment(redis["port"]))
    environment["PYTHONPATH"] = str(ROOT / "scripts")
    assert not any(
        environment[k] for k in ("MIGRATION_DATABASE_URL", "TEST_DATABASE_URL")
    )
    return subprocess.run(
        [sys.executable, str(scripts / "worktree_filtering.py")],
        cwd=ROOT / "apps/api",
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


async def test_filtering_cli_preserves_guards_seed_and_resume_with_private_users_denied(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    # An isolated source fixture permits running this same acceptance against the
    # exact old CLI without reverting the shared checkout.
    source = Path(
        os.getenv(
            "AAP_FILTERING_CLI_FIXTURE_SOURCE", ROOT / "scripts/worktree_filtering.py"
        )
    )
    assert source.is_file()
    backend = _backend(tmp_path, test_engine, mode="e2e")
    credential_path = _credentials_path(backend)
    manifest_path = state_path(backend.root, "e2e", "data", "filtering.json")
    owner_engine = runtime_engine = None
    runs = []
    media = ROOT / "docs-site/public/home/sam-tools/smart-point.webm"
    media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
    try:
        await asyncio.to_thread(backend.prepare)
        credentials = credential_path.read_bytes()
        runtime_engine = create_async_engine(
            backend.runtime_url(), hide_parameters=True
        )
        owner_engine = create_async_engine(
            backend.url.set(database=backend.resources["database"]),
            hide_parameters=True,
        )
        role = backend.runtime_url().username
        async with owner_engine.begin() as db:
            for relation in _PRIVATE:
                await db.execute(text(f'REVOKE ALL ON public.{relation} FROM "{role}"'))
                columns = await db.scalar(
                    text(
                        "SELECT string_agg(quote_ident(attname),',' ORDER BY attnum) FROM pg_attribute WHERE attrelid=CAST(:relation AS regclass) AND attnum>0 AND NOT attisdropped"
                    ),
                    {"relation": "public." + relation},
                )
                await db.execute(
                    text(f'REVOKE ALL ({columns}) ON public.{relation} FROM "{role}"')
                )
        async with runtime_engine.connect() as db:
            for relation in _PRIVATE:
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user,:table,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR has_any_column_privilege(current_user,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"table": "public." + relation},
                ), relation
        await _assert_private_identity_denied(
            runtime_engine, backend.runtime_url().username
        )
        async with async_sessionmaker(
            owner_engine, expire_on_commit=False
        ).begin() as db:
            account = await create_user(
                db,
                "employee",
                f"occupied-{uuid.uuid4().hex}@e2e.test",
                "Existing inactive",
            )
            account.is_active = False
            account_id = account.id
        async with runtime_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM aap_identity_v1.account_profiles")
                )
                == 1
            )
        refusal = await asyncio.to_thread(_run_cli, backend, source)
        runs.append(
            {
                "stage": "occupied",
                "exit_code": refusal.returncode,
                "stdout": refusal.stdout,
                "stderr": refusal.stderr,
            }
        )
        assert refusal.returncode == 1 and "已有数据" in refusal.stderr, runs[-1]
        assert not manifest_path.exists()
        async with owner_engine.begin() as db:
            assert await db.scalar(text("SELECT count(*) FROM users")) == 1
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": account_id})
        first = await asyncio.to_thread(_run_cli, backend, source)
        runs.append(
            {
                "stage": "fresh",
                "exit_code": first.returncode,
                "stdout": first.stdout,
                "stderr": first.stderr,
            }
        )
        assert first.returncode == 0 and "已创建筛选验收数据" in first.stdout, runs[-1]
        document = json.loads(manifest_path.read_text())
        assert document["owner"] == backend.resources["owner"]
        project_id = uuid.UUID(document["manifest"]["image"]["project_id"])
        async with owner_engine.begin() as db:
            await db.execute(
                text("UPDATE projects SET name='Kept on resume' WHERE id=:id"),
                {"id": project_id},
            )
        async with runtime_engine.connect() as db:
            counts = (
                await db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM aap_identity_v1.account_profiles), (SELECT count(*) FROM aap_workspace_v1.project_facts), (SELECT count(*) FROM tasks)"
                    )
                )
            ).one()
        resumed = await asyncio.to_thread(_run_cli, backend, source)
        runs.append(
            {
                "stage": "resume",
                "exit_code": resumed.returncode,
                "stdout": resumed.stdout,
                "stderr": resumed.stderr,
            }
        )
        assert resumed.returncode == 0 and "复用筛选验收数据" in resumed.stdout, runs[
            -1
        ]
        assert json.loads(manifest_path.read_text()) == document
        async with runtime_engine.connect() as db:
            assert (
                await db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM aap_identity_v1.account_profiles), (SELECT count(*) FROM aap_workspace_v1.project_facts), (SELECT count(*) FROM tasks)"
                    )
                )
            ).one() == counts
            assert (
                await db.scalar(
                    text(
                        "SELECT name FROM aap_workspace_v1.project_facts WHERE id=:id"
                    ),
                    {"id": project_id},
                )
                == "Kept on resume"
            )
        assert credential_path.read_bytes() == credentials
        assert hashlib.sha256(media.read_bytes()).hexdigest() == media_hash
        await _assert_private_identity_denied(
            runtime_engine, backend.runtime_url().username
        )
    finally:
        record_property(
            "filtering_cli_source_sha256",
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        record_property("filtering_cli_runs", json.dumps(runs))
        record_property(
            "filtering_cli_workspace_private_relations_denied", list(_PRIVATE)
        )
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if owner_engine is not None:
            await owner_engine.dispose()
        resources_path = state_path(backend.root, "e2e", "resources.json")
        if credential_path.exists() or resources_path.exists():
            await asyncio.to_thread(backend.destroy, backend.resources["confirmation"])
