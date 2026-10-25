"""Consumer-visible input and transport failure boundaries for the observer CLI."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy.engine import make_url

from scripts.video_worker_fence import (
    LocalFenceRefused,
    WorkerDatabaseCredential,
    prove_stale_worker_dsns_denied,
)


_SCRIPT = Path(__file__).with_name("video_worker_fence.py")


@pytest.mark.parametrize("kind", ["public", "symlink", "malformed", "oversized"])
def test_cli_refuses_unsafe_input_without_echoing_credentials(tmp_path, kind):
    path = tmp_path / "credentials.json"
    secret = "this-is-a-private-credential"
    path.write_text(json.dumps({"observer_url": secret}))
    path.chmod(0o600)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "linked.json"
        link.symlink_to(path)
        path = link
    elif kind == "oversized":
        path.write_text(secret * 4096)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--input-file", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"reason": "invalid_operator_input"}
    assert secret not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.asyncio
async def test_successful_stale_login_does_not_count_as_a_fence(monkeypatch):
    class Connection:
        async def exec_driver_sql(self, _statement):
            return 1

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

        async def dispose(self):
            pass

    monkeypatch.setattr(
        "scripts.video_worker_fence.create_async_engine", lambda *a, **kw: Engine()
    )
    url = make_url("postgresql://old_worker:private@worker-postgres:5432/owned_test")
    credential = WorkerDatabaseCredential(
        "DATABASE_URL", "old_worker", "owned_test", "worker-postgres", 5432, False, url
    )
    with pytest.raises(LocalFenceRefused, match="stale_worker_login_still_succeeds"):
        await prove_stale_worker_dsns_denied(
            (credential,), host="127.0.0.1", port=5432, database="owned_test"
        )
