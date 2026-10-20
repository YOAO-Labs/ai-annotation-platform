from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
PEER = ROOT / "apps/api/tests/fixtures/workspace_membership_peer.mjs"
LEGACY = ROOT / "apps/api/tests/fixtures/workspace_membership_legacy.json"


def test_workspace_membership_node_peer_self_check():
    result = subprocess.run(
        ["node", str(PEER), "--self-check"],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, "Workspace membership peer self-check failed"
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["checks"] >= 23
    assert report["digest_vectors"] == (8 if LEGACY.is_file() else 0)
