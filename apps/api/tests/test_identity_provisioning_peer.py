from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]


def test_identity_provisioning_node_peer_self_check():
    result = subprocess.run(
        [
            "node",
            str(ROOT / "apps/api/tests/fixtures/identity_provisioning_peer.mjs"),
            "--self-check",
        ],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, "Identity provisioning peer self-check failed"
    assert json.loads(result.stdout) == {"ok": True, "checks": 24}
