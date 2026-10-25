"""Independent Node consumer self-check for the account-lock wire shape."""

from __future__ import annotations

import json
import os
import subprocess

from tests.test_identity_admission_live import ROOT


def test_node_account_lock_peer_rejects_adversarial_shapes():
    peer = ROOT / "apps/api/tests/fixtures/identity_account_locks_peer.mjs"
    result = subprocess.run(
        ["node", str(peer), "--self-check"],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, "Independent account-lock peer self-check failed"
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report == {"ok": True, "checks": 10}
