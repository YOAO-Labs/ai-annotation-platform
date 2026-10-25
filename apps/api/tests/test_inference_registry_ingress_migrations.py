"""Exercise every migration phase using only a verified disposable owner target."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_asset_storage_cleanup_runtime import _verify_postgres

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_registry_ingress_full_migration_roundtrip(test_engine):
    await assert_owned_test_database(test_engine)
    _verify_postgres(test_engine.url)
    root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    environment["MIGRATION_DATABASE_URL"] = test_engine.url.render_as_string(
        hide_password=False
    )
    environment.pop("AAP_WORKTREE_MODE", None)
    identity = str(uuid.uuid4())
    environment["AAP_MIGRATION_VALIDATION_OWNER"] = (
        "registry-ingress-validator:" + identity
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "rtk",
            "proxy",
            sys.executable,
            str(root / "scripts/validate_migrations.py"),
            "--json",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    report = json.loads(result.stdout)
    output = (
        root
        / ".worktree"
        / f"inference-registry-ingress-full-migrations-{identity}.json"
    )
    assert not output.exists()
    output.write_text(json.dumps(report, indent=2) + "\n")
    assert result.returncode == 0, report
    assert report["result"] == "passed"
    assert report["head"] == "0278"
    assert report["cleanup_errors"] == []
    assert report["leftover_owned_databases"] == []
