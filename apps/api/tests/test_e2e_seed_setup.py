"""Fixture-owner credentials cannot reach ordinary routes or another database."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "web/e2e/fixtures/seed-request.py"


@pytest.mark.parametrize(
    "path,owner_database,runtime_database,host,mode",
    [
        ("/api/v1/users", "fixture_e2e", "fixture_e2e", "127.0.0.1", "e2e"),
        (
            "/api/v1/__test/seed/../../users",
            "fixture_e2e",
            "fixture_e2e",
            "127.0.0.1",
            "e2e",
        ),
        ("/api/v1/__test/seed/owned", "development", "development", "127.0.0.1", "e2e"),
        ("/api/v1/__test/seed/owned", "first_e2e", "second_e2e", "127.0.0.1", "e2e"),
        (
            "/api/v1/__test/seed/owned",
            "fixture_e2e",
            "fixture_e2e",
            "remote.invalid",
            "e2e",
        ),
        ("/api/v1/__test/seed/owned", "fixture_e2e", "fixture_e2e", "127.0.0.1", "dev"),
    ],
)
def test_owner_seed_refuses_cross_boundary_setup_without_leaking_credentials(
    path, owner_database, runtime_database, host, mode
):
    secret = "fixture-secret-not-for-output"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({
            "action": "execute",
            "operation_id": str(uuid.uuid4()),
            "payload": {"path": path, "data": {"namespace": "guarded"}},
        }),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ENVIRONMENT": "development",
            "AAP_WORKTREE_MODE": mode,
            "PLAYWRIGHT_E2E_DATABASE_URL": f"postgresql+asyncpg://owner:{secret}@{host}/{owner_database}",
            "DATABASE_URL": f"postgresql+asyncpg://runtime:{secret}@127.0.0.1/{runtime_database}",
        },
        timeout=10,
    )
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "runtime_database,enabled,environment",
    [
        ("foreign_e2e", "true", "development"),
        ("fixture_e2e", "false", "development"),
        ("fixture_e2e", "true", "production"),
    ],
)
def test_mask_lifecycle_owner_refuses_foreign_database_or_disabled_e2e(
    runtime_database, enabled, environment
):
    secret = "fixture-secret-not-for-output"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({
            "action": "execute",
            "operation_id": str(uuid.uuid4()),
            "payload": {"operation": "mask_slice_lifecycle", "data": {}},
        }),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ENVIRONMENT": environment,
            "E2E_SEED_ENABLED": enabled,
            "AAP_WORKTREE_MODE": "e2e",
            "PLAYWRIGHT_E2E_DATABASE_URL": f"postgresql+asyncpg://owner:{secret}@127.0.0.1/fixture_e2e",
            "DATABASE_URL": f"postgresql+asyncpg://runtime:{secret}@127.0.0.1/{runtime_database}",
        },
        timeout=10,
    )
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr
