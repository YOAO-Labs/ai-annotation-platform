from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
PROVIDER = ROOT / "apps/workspace/conformance/node/provider.mjs"
PEER = ROOT / "apps/api/tests/fixtures/workspace_owner_transfer_peer.mjs"


def _run(path: Path, *args: str):
    return subprocess.run(
        ["node", str(path), *args],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_workspace_owner_transfer_node_provider_self_check():
    result = _run(PROVIDER, "--self-check")
    assert result.returncode == 0, "Node provider self-check failed"
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["checks"] >= 12


def test_workspace_owner_transfer_node_consumer_self_check():
    result = _run(PEER, "--self-check")
    assert result.returncode == 0, "Node consumer self-check failed"
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["checks"] >= 12
